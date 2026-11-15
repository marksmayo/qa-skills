#!/usr/bin/env python3
"""Read a Cargo project or workspace statically: crates, targets, features, tests, clap CLIs and risky sites
(stdlib only; no cargo needed).

    python cargo_project.py <repo> [--metadata cargo-metadata.json] [--out cargo.json]
    python cargo_project.py --self-test

Exit 0 with the model; exit 3 when <repo> has no Cargo.toml (nothing to read: say so, never report an empty model as
a Rust analysis). Manifests are read with tomllib (Python 3.11+; a small fallback reader otherwise). Rust sources are
scanned with comments and string literals masked, so braces and keywords inside them never count. The scan is a
static approximation of what cargo and rustc decide: `notes` lists what it could not resolve. With --metadata (the
JSON of `cargo metadata --format-version 1 --no-deps`, made by the caller), the crates' targets and features come
from cargo itself and the scan adds the rest.

Library use:
    import cargo_project
    m = cargo_project.read_cargo(repo)                # or read_cargo(repo, metadata=json.load(...))
    m['is_cargo'], m['workspace'], m['crates']        # see read_cargo() for the full shape
    cargo_project.test_ids(m)                         # ['<target>::<path::name>', ...], the ids run_tests.py records
    cargo_project.targets_reaching(m, 'crates/a/src/x.rs')   # test targets whose build includes that file
    cargo_project.target_files(m)                     # {'<crate>:<kind>:<name>': {files of that target}}
    cargo_project.cfg_features(['all(feature = "x", test)'])  # (['x'], []): the features a cfg list gates on
    cargo_project.test_lines(text)                    # {line numbers} of #[cfg(test)] items and #[test] functions

clap builder CLIs follow the builder's nesting: an argument or command belongs to the command whose call encloses it,
so `.arg(...)` chained on the root after a `.subcommand(...)` stays on the root. Library loads cover libloading
(Library::new, open, load_with_flags), dlopen and LoadLibrary/LoadLibraryEx (A/W). A relative path reaches
relative_paths when it is the call's literal argument, or (with `via` and `literals`) when it gets there through a
const, static or let binding, PathBuf::from / Path::new, a w! literal macro, or an array, vec! or if/match-chosen list
of candidates iterated into the call (`for p in list`, `list.iter().find_map(|p| ...)`). A literal joined onto
another base (base.join("x")) is not resolved against the working directory and is not counted.
"""
from __future__ import annotations
import argparse, glob, json, os, re, shutil, sys, tempfile
from pathlib import Path, PurePosixPath

TOOL = 'cargo_project.py'
SCHEMA_VERSION = 1
PRUNE = {'target', '.git', 'node_modules', 'vendor', '.cargo'}


# ---------------------------------------------------------------- manifests
def _toml_loads(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        return _mini_toml(text)


def _mini_toml(text: str) -> dict:
    """A small TOML reader for manifests when tomllib is missing: tables, arrays of tables, strings, booleans,
    numbers, (multi-line) arrays of scalars and one-line inline tables."""
    root: dict = {}
    cur = root
    buf = ''
    for raw in text.splitlines():
        line = re.sub(r'\s+#.*$', '', raw).strip() if not buf else raw.strip()
        if not line or line.startswith('#'):
            continue
        if buf:
            buf += ' ' + line
            if buf.count('[') > buf.count(']'):
                continue
            line, buf = buf, ''
        m = re.match(r'^\[\[([^\]]+)\]\]$', line)
        if m:
            parent = root
            keys = [k.strip().strip('"') for k in m.group(1).split('.')]
            for k in keys[:-1]:
                parent = parent.setdefault(k, {})
            cur = {}
            parent.setdefault(keys[-1], []).append(cur)
            continue
        m = re.match(r'^\[([^\]]+)\]$', line)
        if m:
            cur = root
            for k in [k.strip().strip('"') for k in m.group(1).split('.')]:
                cur = cur.setdefault(k, {})
            continue
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        val = val.strip()
        if val.startswith('[') and val.count('[') > val.count(']'):
            buf = line
            continue
        target = cur
        parts = [k.strip().strip('"') for k in key.strip().split('.')]
        for k in parts[:-1]:
            target = target.setdefault(k, {})
        target[parts[-1]] = _mini_value(val)
    return root


def _mini_value(v: str):
    v = v.strip().rstrip(',')
    if v.startswith('"') or v.startswith("'"):
        return v[1:v.rfind(v[0])] if v.rfind(v[0]) > 0 else v[1:]
    if v in ('true', 'false'):
        return v == 'true'
    if v.startswith('['):
        inner = v[1:v.rfind(']')]
        return [_mini_value(x) for x in re.findall(r'"[^"]*"|\'[^\']*\'|[^,\s]+', inner) if x.strip()]
    if v.startswith('{'):
        out = {}
        for k, x in re.findall(r'([\w-]+)\s*=\s*("[^"]*"|\[[^\]]*\]|[^,}]+)', v):
            out[k] = _mini_value(x)
        return out
    try:
        return int(v)
    except ValueError:
        return v


def _read_manifest(path: Path, notes: list) -> dict:
    try:
        return _toml_loads(path.read_text(encoding='utf-8', errors='replace'))
    except Exception as e:   # noqa: BLE001  (invalid TOML)
        notes.append(f'{path.name}: could not be parsed ({e})')
        return {}


# ---------------------------------------------------------------- source masking
def mask(text: str) -> str:
    """text with comments and the contents of string and char literals replaced by spaces (newlines kept), so the
    result has the same length and line numbers. Quotes stay, so `Command::new("x")` masks to `Command::new("  ")`."""
    out = list(text)
    i, n = 0, len(text)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
        c = text[i]
        if c == '/' and text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == '/' and text.startswith('/*', i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith('/*', j):
                    depth, j = depth + 1, j + 2
                elif text.startswith('*/', j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif c in 'rb' and re.match(r'(?:br|r)(#*)"', text[i:i + 40]) and (i == 0 or not (text[i - 1].isalnum()
                                                                                              or text[i - 1] == '_')):
            m = re.match(r'(?:br|r)(#*)"', text[i:])
            end = '"' + m.group(1)
            j = text.find(end, i + m.end())
            j = n if j < 0 else j
            blank(i + m.end(), j)
            i = j + len(end)
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            blank(i + 1, min(j, n))
            i = j + 1
        elif c == "'":
            if i + 1 < n and text[i + 1] == '\\':
                j = text.find("'", i + 2)
                if 0 < j < i + 12:
                    blank(i + 1, j)
                    i = j + 1
                    continue
            if i + 2 < n and text[i + 2] == "'":
                blank(i + 1, i + 2)
                i += 3
                continue
            i += 1
        else:
            i += 1
    return ''.join(out)


def _balanced(text: str, start: int, open_c: str = '(', close_c: str = ')') -> int:
    """Index just past the bracket that closes the one at text[start] (masked text), or len(text)."""
    depth = 0
    for k in range(start, len(text)):
        if text[k] == open_c:
            depth += 1
        elif text[k] == close_c:
            depth -= 1
            if depth == 0:
                return k + 1
    return len(text)


def _kebab(name: str) -> str:
    s = re.sub(r'(?<=[a-z0-9])([A-Z])', r'-\1', name).replace('_', '-')
    return s.lower()


# ---------------------------------------------------------------- per-file scan
ITEM = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:const|async|unsafe|extern(?:\s+"[^"]*")?)\s+)*'
                  r'(fn|mod|struct|enum|impl|trait)\b\s*(\w*)')
TEST_ATTR = re.compile(r'#\[\s*(?:[\w:]+::)?(test|rstest|test_case|quickcheck|proptest)\b')
CFG_ATTR = re.compile(r'#\[\s*cfg\s*\((.*)\)\s*\]\s*$', re.S)
FEATURE = re.compile(r'feature\s*=\s*"([^"]+)"')
ENV_READ = re.compile(r'\b(?:std::)?env::(var|var_os)\s*\(\s*"([^"]*)"|\b(option_env|env)!\s*\(\s*"([^"]*)"')
ENV_DYNAMIC = re.compile(r'\b(?:std::)?env::(?:var|var_os)\s*\(\s*([^")\s][^)]*)\)')
LIB_LOAD = re.compile(r'\b((?:libloading::)?(?:os::\w+::)?Library::(?:new|open|load_with_flags)|dlopen'
                      r'|LoadLibrary(?:Ex)?[AW]?)\s*\(')
PROCESS_CALL = re.compile(r'\b((?:std::|tokio::|async_std::)?process::)?Command::new\s*\(')
CURRENT_DIR = re.compile(r'\b(?:std::)?env::current_dir\s*\(\s*\)|\bcurrent_dir\s*\(\s*\)')


def _line_of(text: str, idx: int) -> int:
    return text.count('\n', 0, idx) + 1


def _arg_text(orig: str, masked: str, open_idx: int) -> str:
    end = _balanced(masked, open_idx)
    return re.sub(r'\s+', ' ', orig[open_idx + 1:end - 1]).strip()


def _relative_literal(expr: str) -> str | None:
    """The literal when expr is a string literal naming a relative path (./x, x/y, ..\\x); None otherwise."""
    m = re.match(r'^(?:r#*)?"([^"]*)"#*$', expr.strip())
    if not m:
        return None
    p = m.group(1)
    if not p or p.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:[\\/]', p):
        return None
    if '/' in p or '\\' in p or p.startswith('.'):
        return p
    return None


STR_LIT = re.compile(r'(?:\b[bc])?(?:\br(#*)"(.*?)"\1|"((?:[^"\\]|\\.)*)")', re.S)
PATH_CTOR = re.compile(r'\b(?:(?:std::)?(?:path|ffi)::)?(PathBuf::from|Path::new|String::from|OsStr::new|OsString::from|'
                       r'CString::new)\s*\(')
LIT_MACRO = re.compile(r'\b(w|s|h|vec)!\s*[(\[]')
BINDING = re.compile(r'\b(const|static|let)\s+(?:mut\s+)?([A-Za-z_]\w*)\s*(?::[^=;{}]{0,80}?(?:\[[^\]]*\][^=;{}]{0,20}?)?)?'
                     r'=(?![=>])\s*')
FOR_BIND = re.compile(r'\bfor\s+(?:\(\s*(?:\w+\s*,\s*)?)?&?(?:mut\s+)?([A-Za-z_]\w*)\s*\)?\s+in\s+&?(?:mut\s+)?'
                      r'(?:[\w]+::)*([A-Za-z_]\w*)\b')
CLOSURE_BIND = re.compile(r'\b([A-Za-z_]\w*)\s*(?:\.\s*(?:iter|into_iter)\s*\(\s*\)\s*)?(?:\.\s*\w+\s*\([^()|]*\)\s*)*'
                          r'\.\s*(?:map|find|find_map|filter|filter_map|any|all|for_each|position|flat_map|try_for_each)'
                          r'\s*\(\s*(?:move\s+)?\|\s*&*(?:mut\s+)?([A-Za-z_]\w*)')


def _first_arg(expr: str) -> str:
    """The first top-level argument of a call's argument text."""
    depth = 0
    for k, ch in enumerate(mask(expr)):
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        elif ch == ',' and depth == 0:
            return expr[:k].strip()
    return expr.strip()


def _path_value(expr: str) -> tuple[list[str], str] | None:
    """(relative path literals, shape) when expr is built only from string literals: a literal, PathBuf::from("…"),
    Path::new("…"), a w!/s! literal macro, or an array or vec! of those; None when anything else is in it. shape is
    'literal', 'array', 'vec!', the constructor ('PathBuf::from', 'Path::new' ...) or the macro ('w!')."""
    lits = [m.group(2) if m.group(2) is not None else m.group(3) for m in STR_LIT.finditer(expr)]
    if not lits:
        return None
    rest = STR_LIT.sub('""', expr)
    ctors = PATH_CTOR.findall(rest)
    macros = LIT_MACRO.findall(rest)
    rest = PATH_CTOR.sub('(', rest)
    rest = LIT_MACRO.sub('(', rest)
    rest = re.sub(r'\.\s*(?:to_path_buf|into|to_string|to_owned|as_ref|unwrap|expect\s*\(\s*""\s*|as_ptr|as_path|'
                  r'as_c_str)\s*\(\s*\)', '', rest)
    if re.sub(r'""|[\s\[\](),&]', '', rest):
        return None
    rel = [p for p in lits if _relative_literal(f'"{p}"')]
    if 'vec' in macros:
        shape = 'vec!'
    elif len(lits) > 1 or rest.strip().startswith(('[', '&[')):
        shape = 'array'
    elif ctors:
        shape = ctors[0]
    elif macros:
        shape = macros[0] + '!'
    else:
        shape = 'literal'
    return rel, shape


def _branch_paths(orig: str, masked: str) -> tuple[list[str], str] | None:
    """(relative literals, 'if'|'match') for an if/match value whose branches build candidate paths: the elements of
    every vec![...] or [...] made only of literals, and every PathBuf::from("…") / Path::new("…") on its own. A literal
    passed to a method of another value (base.join("x"), exe.with_file_name("x")) is not one: it is not resolved
    against the working directory."""
    rel: list[str] = []
    for mt in re.finditer(r'(?:\bvec!\s*)?\[', masked):
        if mt.group(0) == '[' and mt.start() and re.match(r'[\w)\]]', masked[mt.start() - 1]):
            continue   # indexing, not an array
        end = _balanced(masked, mt.end() - 1, '[', ']')
        for part in re.split(r',', orig[mt.end():end - 1]):
            pv = _path_value(part)
            rel += [p for p in (pv[0] if pv else []) if p not in rel]
    for mt in PATH_CTOR.finditer(masked):
        if mt.group(1) not in ('PathBuf::from', 'Path::new') or (mt.start() and masked[mt.start() - 1] == '.'):
            continue
        end = _balanced(masked, mt.end() - 1)
        pv = _path_value(orig[mt.start():end])
        rel += [p for p in (pv[0] if pv else []) if p not in rel]
    if not rel:
        return None
    return rel, re.match(r'\s*(\w+)', masked).group(1)


def _path_bindings(orig: str, masked: str) -> dict[str, list]:
    """{name: [(index, literals, via)]}: const, static and let bindings whose value is built only from relative path
    literals, and loop or closure variables that iterate over such an array."""
    out: dict[str, list] = {}
    for mt in BINDING.finditer(masked):
        start = mt.end()
        depth, end = 0, len(masked)
        for k in range(start, len(masked)):
            ch = masked[k]
            if ch in '([{':
                depth += 1
            elif ch in ')]}':
                depth -= 1
                if depth < 0:
                    end = k
                    break
            elif ch == ';' and depth == 0:
                end = k
                break
        pv = _path_value(orig[start:end])
        if not pv and re.match(r'\s*(?:if|match)\b', masked[start:end]):
            pv = _branch_paths(orig[start:end], masked[start:end])
        if not pv or not pv[0]:
            continue
        rel, shape = pv
        via = f'{mt.group(1)} {mt.group(2)}' + (f' = {shape}' if shape not in ('literal', 'array') else '')
        out.setdefault(mt.group(2), []).append((mt.start(), rel, via))
    for rx in (FOR_BIND, CLOSURE_BIND):
        for mt in rx.finditer(masked):
            var, src = (mt.group(1), mt.group(2)) if rx is FOR_BIND else (mt.group(2), mt.group(1))
            b = _binding_at(out, src, mt.start())
            if b:
                out.setdefault(var, []).append((mt.start(), b[1], f'{b[2]} (iterated as {var})'))
    return out


def _binding_at(bindings: dict, name: str, idx: int):
    """The binding of name in effect at idx: the last one before it, else the first after it (a const declared below
    its use)."""
    cands = bindings.get(name) or []
    before = [b for b in cands if b[0] < idx]
    return before[-1] if before else (cands[0] if cands else None)


def _resolve_path(arg: str, idx: int, bindings: dict) -> tuple[list[str], str] | None:
    """(relative literals, via) when a call argument that is not itself a relative literal reaches one through a
    binding, a path constructor or a literal macro."""
    e = re.sub(r'^[&*\s]+', '', arg.strip())
    e = re.sub(r'(?:\s*\.\s*(?:as_ref|as_path|as_os_str|clone|to_owned|to_path_buf|as_str|deref|as_ptr)\s*\(\s*\))+$',
               '', e)
    if re.fullmatch(r'(?:\w+::)*[A-Za-z_]\w*', e):
        b = _binding_at(bindings, e.split('::')[-1], idx)
        return (b[1], b[2]) if b else None
    pv = _path_value(arg)
    if pv and pv[0] and pv[1] != 'literal':
        return pv[0], pv[1]
    return None


def test_lines(text: str) -> set[int]:
    """Line numbers (1-based) of Rust test code in text: every item under a #[cfg(test)] attribute (a cfg expression
    naming `test` outside not(...), such as cfg(all(test, feature = "x"))), from the attribute to the item's closing
    brace or semicolon, and every #[test] (or #[tokio::test], #[rstest] ...) function with its attributes. Comments and
    strings are masked first, so braces inside them never count."""
    masked = mask(text)
    out: set[int] = set()
    inner = re.search(r'#!\[\s*cfg\s*\((.*?)\)\s*\]', masked)
    if inner and '{' not in masked[:inner.start()] and re.search(r'\btest\b', re.sub(r'\bnot\s*\([^()]*\)', '', inner.group(1))):
        return set(range(1, masked.count('\n') + 2))   # #![cfg(test)]: the whole file is test code
    for mt in re.finditer(r'#\[', masked):
        a_end = _balanced(masked, mt.start() + 1, '[', ']')
        attr = masked[mt.start():a_end]
        cm = re.match(r'#\[\s*cfg\s*\((.*)\)\s*\]$', attr, re.S)
        is_test = bool(TEST_ATTR.match(attr))
        if cm:
            expr = cm.group(1)
            while re.search(r'\bnot\s*\([^()]*\)', expr):
                expr = re.sub(r'\bnot\s*\([^()]*\)', '', expr)
            is_test = bool(re.search(r'\btest\b', expr))
        if not is_test:
            continue
        # the item: skip further attributes, then up to its `;` or its balanced `{...}`
        k, depth = a_end, 0
        end = len(masked)
        while k < len(masked):
            ch = masked[k]
            if ch == '#' and masked.startswith('#[', k) and depth == 0:
                k = _balanced(masked, k + 1, '[', ']')
                continue
            if ch in '([':
                depth += 1
            elif ch in ')]':
                depth -= 1
            elif ch == ';' and depth == 0:
                end = k
                break
            elif ch == '{' and depth == 0:
                end = _balanced(masked, k, '{', '}') - 1
                break
            k += 1
        out.update(range(_line_of(masked, mt.start()), _line_of(masked, end) + 1))
    return out


def scan_file(path: Path, rel: str) -> dict:
    """Items of one Rust file: mod declarations, tests, doc tests, clap derive and builder items, unsafe sites,
    process launches, library loads, env reads, working-directory-relative paths and use paths. Inline module names
    are kept per item (`inline`) so the caller can place it in the module tree."""
    try:
        orig = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    masked = mask(orig)
    olines, mlines = orig.splitlines(), masked.splitlines()
    res = {'mods': [], 'tests': [], 'doctests': [], 'derive': [], 'unsafe': [], 'process': [], 'libload': [],
           'env': [], 'relpaths': [], 'uses': [], 'builder': [], 'has_main': False}
    stack: list[dict] = []          # open brace scopes: {'kind', 'name', 'depth', 'cfg', 'item'}
    depth = 0
    pending: list[str] = []
    attr_buf = ''
    derive_cur = None               # the clap derive struct/enum being read
    variant_cur = None
    for i, (o, m) in enumerate(zip(olines, mlines + [''] * (len(olines) - len(mlines)))):
        ln = i + 1
        s = m.strip()
        if attr_buf:
            attr_buf += ' ' + o.strip()
            if attr_buf.count('[') <= attr_buf.count(']'):
                pending.append(attr_buf)
                attr_buf = ''
            depth += m.count('{') - m.count('}')
            continue
        if s.startswith('#[') or s.startswith('#!['):
            if s.count('[') > s.count(']'):
                attr_buf = o.strip()
            else:
                # an attribute and an item on one line: keep the attribute, read the rest as the item
                end = _balanced(m, m.index('['), '[', ']')
                pending.append(o[o.index('#'):end].strip())
                rest_m = m[end:]
                if not rest_m.strip():
                    continue
                o, m, s = o[end:], rest_m, rest_m.strip()
        inline = [x['name'] for x in stack if x['kind'] == 'mod']
        cfgs = [c for x in stack for c in x.get('cfg', [])]
        it = ITEM.match(m)
        if s.startswith('use ') or s.startswith('pub use ') or re.match(r'^pub\([^)]*\)\s+use ', s):
            res['uses'].append({'line': ln, 'path': re.sub(r'^(pub(\([^)]*\))?\s+)?use\s+', '', o.strip()).rstrip(';'),
                                'inline': inline})
        if it:
            kind, name = it.group(1), it.group(2)
            attrs = pending
            pending = []
            own_cfg = [CFG_ATTR.match(a).group(1).strip() for a in attrs if CFG_ATTR.match(a)]
            if kind == 'mod' and name:
                path_attr = next((re.search(r'"([^"]+)"', a).group(1) for a in attrs
                                  if re.match(r'#\[\s*path\s*=', a) and re.search(r'"([^"]+)"', a)), None)
                if '{' in m:
                    stack.append({'kind': 'mod', 'name': name, 'depth': depth + 1, 'cfg': own_cfg})
                elif ';' in m:
                    res['mods'].append({'name': name, 'line': ln, 'inline': inline, 'path_attr': path_attr,
                                        'cfg': cfgs + own_cfg})
            elif kind == 'fn':
                if name == 'main' and not inline and not stack:
                    res['has_main'] = True
                if any(TEST_ATTR.search(a) for a in attrs):
                    res['tests'].append({'name': name, 'line': ln, 'inline': inline,
                                         'ignored': any(re.match(r'#\[\s*ignore\b', a) for a in attrs),
                                         'should_panic': any(re.match(r'#\[\s*should_panic\b', a) for a in attrs),
                                         'cfg': cfgs + own_cfg})
            elif kind in ('struct', 'enum'):
                derive = ' '.join(a for a in attrs if 'derive' in a)
                ck = next((k for k in ('Parser', 'Subcommand', 'Args') if re.search(r'\b' + k + r'\b', derive)), None)
                if ck:
                    derive_cur = {'kind': ck, 'item': kind, 'name': name, 'line': ln, 'args': [], 'variants': [],
                                  'subcommand_field': None, 'flatten': [], 'depth': depth + 1,
                                  'command_name': _attr_value(attrs, 'name')}
                    res['derive'].append(derive_cur)
            # unsafe fn / unsafe impl / unsafe trait
            um = re.match(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+|async\s+)*unsafe\s+(fn|impl|trait)\b', m)
            if um:
                res['unsafe'].append({'line': ln, 'kind': um.group(1), 'safety_comment': _safety(olines, i, attrs)})
            if '{' in m and kind != 'mod':
                stack.append({'kind': kind, 'name': name, 'depth': depth + 1, 'cfg': own_cfg})
        elif s and not s.startswith('#'):
            # clap derive fields and variants
            if derive_cur and depth >= derive_cur['depth']:
                _derive_member(derive_cur, o, m, s, ln, depth, pending)
            pending = [] if (s.endswith((',', ';', '{', '}')) or s.endswith(')')) else pending
        for um in re.finditer(r'\bunsafe\s*\{', m):
            res['unsafe'].append({'line': ln, 'kind': 'block', 'safety_comment': _safety(olines, i, [])})
        depth += m.count('{') - m.count('}')
        while stack and depth < stack[-1]['depth']:
            stack.pop()
        if derive_cur and depth < derive_cur['depth']:
            derive_cur = None
    full_scan(orig, masked, res)
    res['doctests'] = _doctests(olines)
    return res


def _attr_value(attrs: list[str], key: str) -> str | None:
    for a in attrs:
        if re.match(r'#\[\s*(command|clap|arg)\s*\(', a):
            m = re.search(r'\b' + key + r'\s*=\s*"([^"]+)"', a)
            if m:
                return m.group(1)
    return None


def _derive_member(cur: dict, o: str, m: str, s: str, ln: int, depth: int, pending: list) -> None:
    attrs = ' '.join(pending)
    if cur['item'] == 'enum' and depth == cur['depth']:
        vm = re.match(r'^\s*([A-Z]\w*)\s*(\{|\(|,|$)', m)
        if vm:
            name = _attr_value(pending, 'name') or _kebab(vm.group(1))
            tup = re.match(r'^\s*\w+\s*\(\s*([\w:]+)\s*\)', m)
            cur['variants'].append({'variant': vm.group(1), 'name': name, 'line': ln, 'args': [],
                                    'args_struct': tup.group(1).split('::')[-1] if tup else None,
                                    'subcommand': bool(re.search(r'subcommand', attrs))})
        return
    fm = re.match(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(\w+)\s*:\s*([^,]+?)\s*,?\s*$', m)
    if not fm:
        return
    field, ftype = fm.group(1), fm.group(2)
    tname = re.findall(r'[A-Z]\w*', ftype)
    if re.search(r'#\[\s*(command|clap)\s*\(\s*subcommand', attrs):
        cur['subcommand_field'] = (tname[-1] if tname else None)
        if cur['item'] == 'enum' and cur['variants']:
            cur['variants'][-1]['nested'] = tname[-1] if tname else None
        return
    if re.search(r'#\[\s*(command|clap)\s*\(\s*flatten', attrs):
        cur['flatten'].append(tname[-1] if tname else None)
        return
    arg_attr = next((a for a in pending if re.match(r'#\[\s*(arg|clap)\s*\(', a)), '')
    long_ = short = env = None
    if arg_attr:
        lm = re.search(r'\blong\s*=\s*"([^"]+)"', arg_attr)
        long_ = lm.group(1) if lm else (_kebab(field) if re.search(r'\blong\b', arg_attr) else None)
        sm = re.search(r"\bshort\s*=\s*'(.)'", arg_attr)
        short = sm.group(1) if sm else (field[0] if re.search(r'\bshort\b', arg_attr) else None)
        em = re.search(r'\benv\s*=\s*"([^"]+)"', arg_attr)
        env = em.group(1) if em else (field.upper() if re.search(r'\benv\b', arg_attr) else None)
    entry = {'name': field, 'long': long_, 'short': short, 'env': env, 'line': ln,
             'positional': not (long_ or short)}
    if cur['item'] == 'enum' and cur['variants']:
        cur['variants'][-1]['args'].append(entry)
    else:
        cur['args'].append(entry)


def _safety(olines: list[str], i: int, attrs: list[str]) -> bool:
    """A SAFETY: comment on this line or within the three lines above (or a `# Safety` doc section above an unsafe
    fn)."""
    window = olines[max(0, i - 3):i + 1]
    if any(re.search(r'SAFETY\s*:', x, re.I) for x in window):
        return True
    j = i - 1
    while j >= 0 and (olines[j].strip().startswith(('///', '#[')) or not olines[j].strip()):
        if re.search(r'#\s*Safety', olines[j]):
            return True
        j -= 1
    return False


def full_scan(orig: str, masked: str, res: dict) -> None:
    clap_cmd = bool(re.search(r'\buse\s+clap::(?:\{[^}]*\bCommand\b|Command\b)|\bclap::Command\b', orig))
    proc_cmd = bool(re.search(r'\buse\s+(?:std|tokio|async_std)::process::(?:\{[^}]*\bCommand\b|Command\b)', orig))
    bindings = _path_bindings(orig, masked)

    def relpath(ln, idx, expr, used_for):
        first = _first_arg(expr)
        if _relative_literal(first):
            res['relpaths'].append({'line': ln, 'expr': expr, 'used_for': used_for})
            return
        hit = _resolve_path(first, idx, bindings)
        if hit:
            res['relpaths'].append({'line': ln, 'expr': expr, 'used_for': used_for, 'via': hit[1], 'literals': hit[0]})
    for mt in PROCESS_CALL.finditer(masked):
        open_idx = mt.end() - 1
        expr = _arg_text(orig, masked, open_idx)
        tail = masked[mt.end():mt.end() + 400]
        explicit = bool(mt.group(1))
        chain_proc = bool(re.search(r'\.(spawn|output|status|current_dir|stdin|stdout|stderr)\s*\(', tail[:300]))
        chain_clap = bool(re.search(r'\.(subcommand|about|version|arg\s*\(\s*Arg::|author)\b', tail[:300]))
        is_proc = explicit or (proc_cmd and not clap_cmd) or (proc_cmd and clap_cmd and chain_proc and not chain_clap) \
            or (not clap_cmd and not proc_cmd and chain_proc)
        ln = _line_of(orig, mt.start())
        if is_proc:
            res['process'].append({'line': ln, 'program_expr': expr})
            relpath(ln, mt.start(), expr, 'executable')
        elif clap_cmd or chain_clap:
            res['builder'].append({'kind': 'command', 'line': ln, 'name': re.sub(r'^"|"$', '', expr), 'idx': mt.start()})
    if clap_cmd or re.search(r'\bclap::', orig):
        for mt in re.finditer(r'\bArg::new\s*\(', masked):
            name = re.sub(r'^"|"$', '', _arg_text(orig, masked, mt.end() - 1))
            nxt = re.search(r'\bArg::new\s*\(|\bCommand::new\s*\(|;', masked[mt.end():])
            span = orig[mt.end(): mt.end() + (nxt.start() if nxt else 400)]
            lm, sm, em = (re.search(r'\.long\s*\(\s*"([^"]+)"', span), re.search(r"\.short\s*\(\s*'(.)'", span),
                          re.search(r'\.env\s*\(\s*"([^"]+)"', span))
            res['builder'].append({'kind': 'arg', 'line': _line_of(orig, mt.start()), 'name': name, 'idx': mt.start(),
                                   'long': lm.group(1) if lm else None, 'short': sm.group(1) if sm else None,
                                   'env': em.group(1) if em else None})
    for mt in LIB_LOAD.finditer(masked):
        expr = _arg_text(orig, masked, mt.end() - 1)
        ln = _line_of(orig, mt.start())
        res['libload'].append({'line': ln, 'call': mt.group(1), 'path_expr': expr})
        relpath(ln, mt.start(), expr, 'library')
    _nest(masked, res['builder'])
    for mt in ENV_READ.finditer(orig):
        if masked[mt.start()] == ' ':     # inside a comment or string
            continue
        name = mt.group(2) if mt.group(2) is not None else mt.group(4)
        kind = 'runtime' if mt.group(1) else 'compile_time'
        res['env'].append({'name': name, 'line': _line_of(orig, mt.start()), 'kind': kind})
    for mt in ENV_DYNAMIC.finditer(masked):
        res['env'].append({'name': None, 'expr': orig[mt.start(1):mt.end(1)].strip(), 'line': _line_of(orig, mt.start()),
                           'kind': 'runtime'})
    lines = orig.splitlines()
    for mt in CURRENT_DIR.finditer(masked):
        ln = _line_of(orig, mt.start())
        window = ' '.join(lines[ln - 1:ln + 5])
        used = ('executable' if re.search(r'Command::new|\.exe\b', window) else 'library'
                if re.search(r'Library::|dlopen|\.(dll|so|dylib)\b', window) else 'path')
        res['relpaths'].append({'line': ln, 'expr': lines[ln - 1].strip()[:160], 'used_for': used,
                                'base': 'current_dir()'})


def _nest(masked: str, items: list[dict]) -> None:
    """Give each clap builder item `nest`: the positions of the brackets open where it starts, so an item inside
    `.subcommand(Command::new("x").arg(...))` is known to sit inside that call and one chained after it is not."""
    want = sorted({x['idx'] for x in items})
    at: dict[int, list[int]] = {}
    stack: list[int] = []
    w = 0
    for k, ch in enumerate(masked):
        while w < len(want) and want[w] == k:
            at[k] = list(stack)
            w += 1
        if w >= len(want):
            break
        if ch in '([':
            stack.append(k)
        elif ch in ')]' and stack:
            stack.pop()
    for x in items:
        x['nest'] = at.get(x['idx'], [])


def _builder_owner(x: dict, commands: list[dict]):
    """The command an item belongs to: of the commands before it whose open brackets enclose it too, the most deeply
    nested, then the latest."""
    nest = x.get('nest') or []
    best = None
    for c in commands:
        cn = c.get('nest') or []
        if c is x or c['idx'] > x['idx'] or nest[:len(cn)] != cn:
            continue
        if best is None or (len(cn), c['idx']) > (len(best.get('nest') or []), best['idx']):
            best = c
    return best


def _cfg_parse(expr: str):
    """A cfg expression as a tree: ('pred', name, value or None) or (all|any|not, [children]); None when unreadable."""
    toks = re.findall(r'"(?:[^"\\]|\\.)*"|[A-Za-z_][\w]*|[(),=]', expr)
    pos = 0

    def node():
        nonlocal pos
        if pos >= len(toks):
            raise ValueError('end')
        name = toks[pos]
        pos += 1
        if name in ('all', 'any', 'not') and pos < len(toks) and toks[pos] == '(':
            pos += 1
            kids = []
            while pos < len(toks) and toks[pos] != ')':
                kids.append(node())
                if pos < len(toks) and toks[pos] == ',':
                    pos += 1
            pos += 1
            return (name, kids)
        if pos + 1 < len(toks) and toks[pos] == '=':
            pos += 2
            return ('pred', name, toks[pos - 1].strip('"'))
        return ('pred', name, None)
    try:
        tree = node()
        return tree if pos == len(toks) else None
    except (ValueError, IndexError):
        return None


def _cfg_need(n) -> tuple[set, list]:
    """(features every configuration that compiles this needs, [groups of features of which one is needed])."""
    if n[0] == 'pred':
        return ({n[2]} if n[1] == 'feature' and n[2] else set()), []
    if n[0] == 'not':
        return set(), []
    kids = [_cfg_need(k) for k in n[1]]
    if n[0] == 'all':
        req, alts = set(), []
        for r, a in kids:
            req |= r
            alts += a
        return req, alts
    # any(...): ungated when one branch needs no feature; else what every branch needs, and the rest as alternatives
    if not kids or any(not r and not a for r, a in kids):
        return set(), []
    common = set.intersection(*[r for r, _ in kids]) if all(r for r, _ in kids) else set()
    rest = sorted({f for r, a in kids for f in (r - common) | {x for g in a for x in g}})
    return common, ([rest] if rest else [])


def cfg_features(cfgs: list[str]) -> tuple[list[str], list[list[str]]]:
    """(features, features_any) for a list of cfg expressions that all apply (an item's own and its modules'):
    features every build that compiles the item enables (feature = "x", all(feature = "x", test)), and groups of
    which at least one must be enabled (any(feature = "a", feature = "b")). not(feature = "x") and
    any(feature = "x", test) gate on no feature. An expression that cannot be parsed falls back to every feature
    named in it."""
    req, alts = set(), []
    for cf in cfgs:
        tree = _cfg_parse(cf)
        if tree is None:
            req |= set(FEATURE.findall(cf))
            continue
        r, a = _cfg_need(tree)
        req |= r
        alts += [g for g in a if g not in alts]
    return sorted(req), [g for g in alts if not set(g) & req]


def _builder_items(text: str) -> list[dict]:
    """The clap builder items of one Rust source text (for tests)."""
    res = {'process': [], 'relpaths': [], 'builder': [], 'libload': [], 'env': []}
    full_scan(text, mask(text), res)
    return res['builder']


def _doctests(olines: list[str]) -> list[dict]:
    """Code blocks in /// and //! doc comments that rustdoc runs as tests (rust, or no language; not text/ignore)."""
    out, open_at, info = [], None, ''
    for i, line in enumerate(olines):
        s = line.strip()
        if not (s.startswith('///') or s.startswith('//!')):
            if open_at is not None:
                open_at = None
            continue
        body = s[3:].strip()
        if body.startswith('```'):
            if open_at is None:
                open_at, info = i + 1, body[3:].strip().lower()
            else:
                words = {w.strip() for w in re.split(r'[,\s]+', info) if w.strip()}
                rusty = not words or words & {'rust', 'ignore', 'no_run', 'should_panic', 'compile_fail', 'edition2018',
                                              'edition2021', 'edition2024'} or all(w.startswith('edition') for w in words)
                if rusty and not words & {'text', 'sh', 'bash', 'console', 'json', 'toml', 'yaml', 'plain'}:
                    out.append({'line': open_at, 'ignored': 'ignore' in words, 'no_run': 'no_run' in words})
                open_at = None
    return out


# ---------------------------------------------------------------- crates and targets
def _stem(p: str) -> str:
    return PurePosixPath(p).stem


def _auto_targets(cdir: Path, pkg: dict, man: dict) -> list[dict]:
    name = pkg.get('name', cdir.name)
    out = []

    def add(kind, tname, path, auto=True, **extra):
        out.append({'kind': kind, 'name': tname, 'path': path, 'required_features': [], 'harness': True,
                    'autodiscovered': auto, **extra})
    lib = man.get('lib') or {}
    if lib or (cdir / 'src/lib.rs').is_file():
        add('lib', (lib.get('name') or name).replace('-', '_'), lib.get('path', 'src/lib.rs'), not lib,
            harness=lib.get('harness', True))
    explicit = {k: man.get(k) or [] for k in ('bin', 'test', 'bench', 'example')}
    auto_flag = {k: pkg.get(f'auto{k}s', pkg.get(f'auto{k}', True)) for k in ('bin', 'test', 'bench', 'example')}
    auto_paths = {'bin': [], 'test': [], 'bench': [], 'example': []}
    if (cdir / 'src/main.rs').is_file():
        auto_paths['bin'].append((name, 'src/main.rs'))
    for kind, folder in (('bin', 'src/bin'), ('test', 'tests'), ('bench', 'benches'), ('example', 'examples')):
        d = cdir / folder
        if not d.is_dir():
            continue
        for e in sorted(d.iterdir(), key=lambda p: p.name):
            if e.is_file() and e.suffix == '.rs':
                auto_paths[kind].append((e.stem, f'{folder}/{e.name}'))
            elif e.is_dir() and (e / 'main.rs').is_file():
                auto_paths[kind].append((e.name, f'{folder}/{e.name}/main.rs'))
    default_dir = {'bin': 'src/bin', 'test': 'tests', 'bench': 'benches', 'example': 'examples'}
    for kind in ('bin', 'test', 'bench', 'example'):
        seen_paths = set()
        for t in explicit[kind]:
            tname = t.get('name')
            path = t.get('path') or next((p for n_, p in auto_paths[kind] if n_ == tname),
                                         f'{default_dir[kind]}/{tname}.rs' if kind != 'bin' or tname != name
                                         else 'src/main.rs')
            seen_paths.add(path)
            add(kind, tname or _stem(path), path, False, required_features=list(t.get('required-features') or []),
                harness=t.get('harness', True))
            if kind == 'test' and t.get('harness') is False:
                out[-1]['harness'] = False
        if auto_flag[kind] is not False:
            for tname, path in auto_paths[kind]:
                if path not in seen_paths and tname not in {t.get('name') for t in explicit[kind]}:
                    add(kind, tname, path)
    build = pkg.get('build')
    if build is not False and (build or (cdir / 'build.rs').is_file()):
        add('build', 'build-script-build', build if isinstance(build, str) else 'build.rs')
    return out


def _features(man: dict) -> dict:
    feats = man.get('features') or {}
    deps = {}
    for sect in ('dependencies', 'dev-dependencies', 'build-dependencies'):
        deps.update(man.get(sect) or {})
    for tgt in (man.get('target') or {}).values():
        if isinstance(tgt, dict):
            deps.update(tgt.get('dependencies') or {})
    referenced_dep = {x[4:] for v in feats.values() for x in (v or []) if isinstance(x, str) and x.startswith('dep:')}
    implicit = {k for k, v in deps.items() if isinstance(v, dict) and v.get('optional') and k not in referenced_dep}
    all_f = sorted((set(feats) - {'default'}) | implicit)
    closure, todo = set(), list(feats.get('default') or [])
    while todo:
        f = todo.pop()
        if f in closure or not isinstance(f, str):
            continue
        closure.add(f)
        todo.extend(x for x in feats.get(f, []) or [] if isinstance(x, str) and '/' not in x and not x.startswith('dep:'))
    return {'default': list(feats.get('default') or []), 'optional': sorted(set(all_f) - closure), 'all': all_f}


def _deps(man: dict, cdir: Path, root: Path, ws_deps: dict) -> tuple[list[str], list[dict]]:
    names, path_deps = set(), []
    tables = [(s, man.get(s) or {}) for s in ('dependencies', 'dev-dependencies', 'build-dependencies')]
    for tgt in (man.get('target') or {}).values():
        if isinstance(tgt, dict):
            tables += [(s, tgt.get(s) or {}) for s in ('dependencies', 'dev-dependencies', 'build-dependencies')]
    for sect, tab in tables:
        for k, v in tab.items():
            names.add(k)
            spec = v if isinstance(v, dict) else {}
            if spec.get('workspace') and isinstance(ws_deps.get(k), dict):
                spec = {**ws_deps[k], **{x: y for x, y in spec.items() if x != 'workspace'}}
                base = root
            else:
                base = cdir
            if spec.get('path'):
                p = (base / spec['path']).resolve()
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    rel = str(p)
                path_deps.append({'name': spec.get('package') or k, 'alias': k, 'dir': rel, 'kind': sect})
    return sorted(names), path_deps


def _module_walk(cdir: Path, root_file: str, cache: dict, file_cfg: dict | None = None) -> dict[str, str]:
    """{file (crate-relative): module path} for every file reached from a target's root file through `mod x;`.
    file_cfg, when given, is filled with {file: [cfg exprs of the `mod x;` declarations on the way to it]}."""
    out: dict[str, str] = {}
    todo = [(root_file, '', True, [])]
    while todo:
        rel, mod, is_root, cfg = todo.pop()
        if rel in out or not (cdir / rel).is_file():
            continue
        out[rel] = mod
        if file_cfg is not None:
            file_cfg.setdefault(rel, cfg)
        sc = cache.get(rel)
        if sc is None:
            sc = cache[rel] = scan_file(cdir / rel, rel)
        p = PurePosixPath(rel)
        base = p.parent if (is_root or p.name == 'mod.rs') else p.parent / p.stem
        for d in sc.get('mods', []):
            sub = base.joinpath(*d['inline']) if d['inline'] else base
            modpath = '::'.join(x for x in [mod, *d['inline'], d['name']] if x)
            if d.get('path_attr'):
                cand = [str(PurePosixPath(p.parent) / d['path_attr'])]
            else:
                cand = [str(sub / f'{d["name"]}.rs'), str(sub / d['name'] / 'mod.rs')]
            hit = next((c for c in cand if (cdir / c).is_file()), None)
            if hit:
                todo.append((os.path.normpath(hit).replace('\\', '/'), modpath, False, cfg + list(d.get('cfg') or [])))
    return out


def _crate(cdir: Path, root: Path, ws: dict, notes: list) -> dict | None:
    man = _read_manifest(cdir / 'Cargo.toml', notes)
    pkg = man.get('package')
    if not isinstance(pkg, dict):
        return None
    crel = cdir.relative_to(root).as_posix() if cdir != root else '.'
    pre = '' if crel == '.' else crel + '/'
    edition = pkg.get('edition')
    if isinstance(edition, dict) and edition.get('workspace'):
        edition = ((ws.get('package') or {}).get('edition'))
    ws_deps = ws.get('dependencies') or {}
    deps, path_deps = _deps(man, cdir, root, ws_deps)
    targets = _auto_targets(cdir, pkg, man)
    for t in targets:
        t['path'] = pre + t['path']
    c = {'name': pkg.get('name', cdir.name), 'dir': crel, 'manifest': pre + 'Cargo.toml', 'edition': edition,
         'features': _features(man), 'deps': deps, 'path_deps': path_deps, 'targets': targets,
         'fixture_bins': [], 'tests': [], 'clis': [], 'unsafe': [], 'process_launches': [], 'library_loads': [],
         'env_reads': [], 'relative_paths': [], 'modules': [], 'uses': [], 'unreached_files': []}
    cache: dict = {}
    file_targets: dict[str, list] = {}
    file_module: dict[str, str] = {}
    file_cfg: dict[str, list] = {}
    c['target_files'] = {}
    for t in targets:
        local = t['path'][len(pre):]
        walked = _module_walk(cdir, local, cache, file_cfg)
        c['target_files'][f"{t['kind']}:{t['name']}"] = sorted(pre + f for f in walked)
        for f, mod in walked.items():
            file_targets.setdefault(f, []).append(t)
            file_module.setdefault(f, mod)
    # every .rs file of the crate: scanned once; files no target reaches are listed
    all_rs = []
    for dp, dns, fns in os.walk(cdir):
        dns[:] = [d for d in dns if d not in PRUNE and not (Path(dp) / d / 'Cargo.toml').is_file()]
        for f in fns:
            if f.endswith('.rs'):
                all_rs.append((Path(dp) / f).relative_to(cdir).as_posix())
    target_paths = {t['path'][len(pre):] for t in targets}
    for f in sorted(all_rs):
        sc = cache.get(f) or scan_file(cdir / f, f)
        cache[f] = sc
        frel = pre + f
        tg = file_targets.get(f, [])
        if not tg:
            if f.startswith('tests/') and f.count('/') >= 2 and sc.get('has_main'):
                c['fixture_bins'].append({'file': frel, 'name': _stem(f) if _stem(f) != 'main' else
                                          PurePosixPath(f).parent.name, 'declared': False,
                                          'note': 'a helper binary under tests/ with fn main; not a test target'})
            elif not (f.startswith('tests/') and f.count('/') >= 2):
                c['unreached_files'].append(frel)
        mod = file_module.get(f, '')
        for d in sc.get('mods', []):
            c['modules'].append({'file': frel, 'line': d['line'], 'name': d['name'],
                                 'module': '::'.join(x for x in [mod, *d['inline'], d['name']] if x),
                                 'cfg': d['cfg']})
        for u in sc.get('uses', []):
            c['uses'].append({'file': frel, 'line': u['line'], 'path': u['path']})
        test_tg = next((t for t in tg if t['kind'] == 'lib'), None) or (tg[0] if tg else None)
        for t in sc.get('tests', []):
            modpath = '::'.join(x for x in [mod, *t['inline']] if x)
            full = '::'.join(x for x in [modpath, t['name']] if x)
            kind = 'unit' if test_tg and test_tg['kind'] in ('lib', 'bin') else (
                'integration' if test_tg and test_tg['kind'] == 'test' else (test_tg or {}).get('kind', 'unknown'))
            cfgs = list(file_cfg.get(f, [])) + list(t['cfg'])
            feats, feats_any = cfg_features(cfgs)
            c['tests'].append({'name': full, 'fn': t['name'], 'file': frel, 'line': t['line'], 'kind': kind,
                               'module': modpath, 'ignored': t['ignored'], 'should_panic': t['should_panic'],
                               'cfg': cfgs, 'features': feats, 'features_any': feats_any,
                               'target': test_tg['name'] if test_tg else None,
                               'target_kind': test_tg['kind'] if test_tg else None,
                               'id': f'{test_tg["name"]}::{full}' if test_tg else None})
        lib_t = next((t for t in tg if t['kind'] == 'lib'), None)
        if lib_t:
            for d in sc.get('doctests', []):
                c['tests'].append({'name': f'{f} - {mod or c["name"]} (line {d["line"]})', 'fn': None, 'file': frel,
                                   'line': d['line'], 'kind': 'doc', 'module': mod, 'ignored': d['ignored'],
                                   'no_run': d['no_run'], 'cfg': [], 'features': [], 'features_any': [],
                                   'target': lib_t['name'],
                                   'target_kind': 'lib', 'id': None})
        for u in sc.get('unsafe', []):
            c['unsafe'].append({'file': frel, **u})
        for p in sc.get('process', []):
            c['process_launches'].append({'file': frel, **p})
        for p in sc.get('libload', []):
            c['library_loads'].append({'file': frel, **p})
        for e in sc.get('env', []):
            c['env_reads'].append({'file': frel, **e})
        for r in sc.get('relpaths', []):
            c['relative_paths'].append({'file': frel, **r})
        bin_t = next((t for t in tg if t['kind'] == 'bin'), None)
        c['clis'] += _clis(sc, frel, bin_t, lib_t)
    for t in targets:
        if t['kind'] == 'bin' and t['path'].startswith(pre + 'tests/'):
            c['fixture_bins'].append({'file': t['path'], 'name': t['name'], 'declared': True,
                                      'note': 'a [[bin]] target kept under tests/: a helper binary for tests'})
    del target_paths
    return c


def _clis(sc: dict, frel: str, bin_t, lib_t) -> list[dict]:
    out = []
    derive = {d['name']: d for d in sc.get('derive', [])}
    owner = (bin_t or {}).get('name')

    def args_of(struct_name, parent, seen=()):
        d = derive.get(struct_name)
        if not d or struct_name in seen:
            return []
        res = [{**a, 'parent': parent, 'file': frel} for a in d['args']]
        for fl in d['flatten']:
            res += args_of(fl, parent, (*seen, struct_name))
        return res

    def commands_of(enum_name, parent, seen=()):
        e = derive.get(enum_name)
        if not e or enum_name in seen:
            return [], []
        cmds, args = [], []
        for v in e['variants']:
            cmds.append({'name': v['name'], 'line': v['line'], 'parent': parent, 'variant': v['variant'],
                         'args_struct': v['args_struct'], 'file': frel})
            args += [{**a, 'parent': v['name'], 'file': frel} for a in v['args']]
            if v['args_struct']:
                args += args_of(v['args_struct'], v['name'])
                sub = (derive.get(v['args_struct']) or {}).get('subcommand_field')
                if sub:
                    c2, a2 = commands_of(sub, v['name'], (*seen, enum_name))
                    cmds += c2
                    args += a2
            if v.get('nested'):
                c2, a2 = commands_of(v['nested'], v['name'], (*seen, enum_name))
                cmds += c2
                args += a2
        return cmds, args
    for d in sc.get('derive', []):
        if d['kind'] != 'Parser':
            continue
        name = d['command_name'] or (owner or d['name'])
        args = args_of(d['name'], name)
        cmds = []
        if d['subcommand_field']:
            cmds, a2 = commands_of(d['subcommand_field'], name)
            args += a2
        out.append({'bin': owner, 'target': (bin_t or lib_t or {}).get('name'), 'file': frel, 'line': d['line'],
                    'style': 'derive', 'name': name, 'struct': d['name'], 'parent': None, 'commands': cmds,
                    'args': args})
    b = sorted(sc.get('builder', []), key=lambda x: x['idx'])
    if b and any(x['kind'] == 'command' for x in b):
        root = next(x for x in b if x['kind'] == 'command')
        commands = [x for x in b if x['kind'] == 'command']
        cmds, args = [], []
        for x in b:
            # the owner follows the builder's nesting: an .arg chained on the root after a .subcommand(...) stays on
            # the root, and a command built inside .subcommand(...) of another is that one's child
            own = _builder_owner(x, commands) or root
            if x['kind'] == 'command' and x is not root:
                cmds.append({'name': x['name'], 'line': x['line'], 'parent': own['name'], 'file': frel})
            elif x['kind'] == 'arg':
                args.append({'name': x['name'], 'long': x['long'], 'short': x['short'], 'env': x['env'],
                             'line': x['line'], 'positional': not (x['long'] or x['short']), 'parent': own['name'],
                             'file': frel})
        out.append({'bin': owner, 'target': (bin_t or lib_t or {}).get('name'), 'file': frel, 'line': root['line'],
                    'style': 'builder', 'name': root['name'], 'struct': None, 'parent': None, 'commands': cmds,
                    'args': args})
    return out


def _members(root: Path, ws: dict, notes: list) -> list[str]:
    out = []
    for pat in ws.get('members') or []:
        hits = sorted(glob.glob(str(root / pat)))
        if not hits:
            notes.append(f'workspace member {pat!r} matches no directory')
        for h in hits:
            if (Path(h) / 'Cargo.toml').is_file():
                rel = Path(h).resolve().relative_to(root).as_posix()
                if rel not in out:
                    out.append(rel)
    exclude = set(ws.get('exclude') or [])
    return [m for m in out if m not in exclude]


def read_cargo(repo, metadata: dict | None = None) -> dict:
    """The Cargo model of repo:

    {tool, schema_version: 1, repo, is_cargo, notes,
     workspace: {root, members, default_members, virtual} | None,
     crates: [{name, dir, manifest, edition,
               features: {default, optional, all}, deps: [names], path_deps: [{name, alias, dir, kind}],
               targets: [{kind lib|bin|test|bench|example|build, name, path, required_features, harness,
                          autodiscovered}],
               target_files: {'<kind>:<name>': [files in that target's module tree]},
               fixture_bins: [{file, name, declared, note}],
               tests: [{name (libtest path), fn, file, line, kind unit|integration|doc, module, ignored,
                        should_panic, cfg: [cfg exprs of the fn, its inline modules and the `mod x;`
                        declarations that include its file], features (required: see cfg_features), features_any
                        ([[a, b]]: one of them needed), target, target_kind,
                        id: '<target>::<name>' (None for doc tests; the target part is qualified_target's:
                            '<crate>/<target>' or '<crate>/<kind>/<target>' when the name is not unique)}],
               clis: [{bin, target, file, line, style derive|builder, name, struct, parent,
                       commands: [{name, line, parent, file, ...}],
                       args: [{name, long, short, env, line, positional, parent, file}]}],
               unsafe: [{file, line, kind block|fn|impl|trait, safety_comment}],
               process_launches: [{file, line, program_expr}],
               library_loads: [{file, line, call, path_expr}],
               env_reads: [{file, line, name (None when not a literal, then expr), kind runtime|compile_time}],
               relative_paths: [{file, line, expr, used_for executable|library|path, base?,
                                 via?: how the literal reaches the call ('const NAME', 'let p = PathBuf::from',
                                       'Path::new', 'w!', 'let list = vec! (iterated as p)', ...),
                                 literals?: [the relative literals behind via]}],
               modules: [{file, line, name, module, cfg}],   # `mod x;` declarations
               uses: [{file, line, path}],
               unreached_files: [.rs files no target's module tree reaches]}]}

    Paths are repo-relative with '/'. metadata: parsed `cargo metadata` JSON; its targets and features replace the
    static ones for the packages it lists."""
    root = Path(repo).resolve()
    notes: list[str] = []
    out = {'tool': TOOL, 'schema_version': SCHEMA_VERSION, 'repo': str(root), 'is_cargo': False, 'notes': notes,
           'workspace': None, 'crates': []}
    top = root / 'Cargo.toml'
    if not top.is_file():
        notes.append('no Cargo.toml at the repository root')
        return out
    out['is_cargo'] = True
    man = _read_manifest(top, notes)
    ws = man.get('workspace') if isinstance(man.get('workspace'), dict) else None
    dirs = []
    if ws is not None:
        members = _members(root, ws, notes)
        out['workspace'] = {'root': '.', 'members': members,
                            'default_members': list(ws.get('default-members') or []) or None,
                            'virtual': not isinstance(man.get('package'), dict)}
        if isinstance(man.get('package'), dict):
            dirs.append(root)
        dirs += [root / m for m in members if m != '.']
    else:
        dirs.append(root)
    for d in dirs:
        c = _crate(d, root, ws or {}, notes)
        if c:
            out['crates'].append(c)
    if metadata:
        _apply_metadata(out, metadata, root, notes)
    for c in out['crates']:   # ids carry the crate (and kind) where a target name is not unique
        for t in c['tests']:
            if t.get('id') and t.get('target'):
                t['id'] = f"{qualified_target(out, c['name'], t['target_kind'], t['target'])}::{t['name']}"
    if not any(c['tests'] for c in out['crates']):
        notes.append('no #[test] function or doc test was found')
    return out


def _apply_metadata(out: dict, meta: dict, root: Path, notes: list) -> None:
    by_name = {c['name']: c for c in out['crates']}
    for p in meta.get('packages') or []:
        c = by_name.get(p.get('name'))
        if not c:
            continue
        cdir = root / c['dir']
        targets = []
        for t in p.get('targets') or []:
            kinds = t.get('kind') or []
            kind = 'build' if 'custom-build' in kinds else ('lib' if any(k in ('lib', 'rlib', 'cdylib', 'staticlib',
                                                                              'dylib', 'proc-macro') for k in kinds)
                                                            else (kinds[0] if kinds else 'unknown'))
            try:
                path = Path(t.get('src_path', '')).resolve().relative_to(root).as_posix()
            except ValueError:
                path = t.get('src_path')
            targets.append({'kind': kind, 'name': t.get('name'), 'path': path,
                            'required_features': t.get('required-features') or [], 'harness': t.get('test', True)
                            if kind != 'bench' else True, 'autodiscovered': None, 'from': 'cargo metadata'})
        if targets:
            c['targets'] = targets
            pre = '' if c['dir'] == '.' else c['dir'] + '/'
            cache: dict = {}
            c['target_files'] = {}
            for t in targets:
                local = t['path'][len(pre):] if isinstance(t['path'], str) and t['path'].startswith(pre) else None
                walked = _module_walk(cdir, local, cache) if local else {}
                c['target_files'][f"{t['kind']}:{t['name']}"] = sorted(pre + f for f in walked)
        feats = p.get('features')
        if isinstance(feats, dict):
            c['features'] = _features({'features': feats})
        del cdir
    notes.append('targets and features come from cargo metadata')


TEST_KINDS = ('lib', 'bin', 'test', 'bench', 'example')


def qualified_target(model: dict, crate: str, kind: str, name: str) -> str:
    """The target part of a test id: the target's name when no other lib/bin/test/bench/example target in the
    project has it; `<crate>/<name>` when another crate has one of that name (tests/cli.rs in two crates);
    `<crate>/<kind>/<name>` when the crate itself has two (a lib and tests/<lib name>.rs). parse_results.py keeps
    the same rule, so runtime ids equal these."""
    owners = [(c.get('name'), t.get('kind')) for c in model.get('crates') or [] for t in c.get('targets') or []
              if t.get('kind') in TEST_KINDS and t.get('name') == name]
    if len(owners) <= 1:
        return name
    if sum(1 for o, _ in owners if o == crate) <= 1:
        return f'{crate}/{name}'
    return f'{crate}/{kind}/{name}'


def test_ids(model: dict) -> list[str]:
    """Every libtest id ('<target>::<path::name>') the static scan found (doc tests left out)."""
    return [t['id'] for c in model.get('crates', []) for t in c['tests'] if t.get('id')]


def target_files(model: dict) -> dict[str, set[str]]:
    """{'<crate>:<kind>:<name>': {repo-relative files}}: the files each target's module tree holds (its root file and
    every file reached through `mod x;`). Read from the model (crates' `target_files`); for a model made before that
    field existed, walked again from the repository on disk."""
    out: dict[str, set[str]] = {}
    root = Path(model.get('repo') or '.')
    for c in model.get('crates', []):
        tf = c.get('target_files')
        if tf is None:
            pre = '' if c['dir'] == '.' else c['dir'] + '/'
            cache: dict = {}
            tf = {}
            for t in c['targets']:
                p = t.get('path') or ''
                tf[f"{t['kind']}:{t['name']}"] = sorted(pre + f for f in _module_walk(root / c['dir'], p[len(pre):], cache)) \
                    if p.startswith(pre) else []
        for k, files in tf.items():
            out[f"{c['name']}:{k}"] = set(files)
    return out


def targets_reaching(model: dict, file: str, include_bench_examples: bool = False) -> list[dict]:
    """[{crate, target, kind, why}] whose build includes file (repo-relative), for test selection:

    - 'contains': the targets whose module tree holds the file (target_files);
    - 'links_lib': when it is in the crate's library, every lib, bin and test target of that crate (they link it);
    - 'path_dependent': the lib, bin and test targets of the crates that depend on that crate by path, followed
      transitively through normal dependencies (a dev-dependency adds its crate's targets but goes no further);
    - 'runs_bin': when it is in a binary only (a module of src/main.rs, no library holding it), the crate's
      integration tests, which cargo builds that binary for and which may run it; never the dependents, which cannot
      link a binary.

    Bench and example targets are left out unless include_bench_examples (cargo test does not run them). The crate
    owning the file is the one with the deepest directory holding it."""
    file = file.replace('\\', '/').strip('/')
    crates = model.get('crates', [])
    holders = [c for c in crates if c['dir'] == '.' or file.startswith(c['dir'] + '/')]
    if not holders:
        return []
    owner = max(holders, key=lambda c: 0 if c['dir'] == '.' else len(c['dir']))
    tf = target_files(model)
    skip = set() if include_bench_examples else {'bench', 'example'}
    out, seen = [], set()

    def add(c, t, why):
        k = (c['name'], t['kind'], t['name'])
        if t['kind'] in skip or k in seen:
            return
        seen.add(k)
        out.append({'crate': c['name'], 'target': t['name'], 'kind': t['kind'], 'why': why})
    direct = [t for t in owner['targets'] if file == t.get('path')
              or file in tf.get(f"{owner['name']}:{t['kind']}:{t['name']}", ())]
    for t in direct:
        add(owner, t, 'contains')
    if any(t['kind'] == 'lib' for t in direct):
        for t in owner['targets']:
            if t['kind'] in ('lib', 'bin', 'test', 'bench', 'example'):
                add(owner, t, 'links_lib')
        todo, done = [owner['name']], {owner['name']}
        while todo:
            name = todo.pop()
            for c in crates:
                links = [d for d in c['path_deps'] if d['name'] == name]
                if not links or c['name'] in done:
                    continue
                for t in c['targets']:
                    if t['kind'] != 'build':
                        add(c, t, 'path_dependent')
                if any(d.get('kind') != 'dev-dependencies' for d in links):
                    done.add(c['name'])
                    todo.append(c['name'])
    elif any(t['kind'] == 'bin' for t in direct):
        for t in owner['targets']:
            if t['kind'] == 'test':
                add(owner, t, 'runs_bin')
    return out


# ---------------------------------------------------------------- self-test
FIXTURE = {
    'Cargo.toml': '[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n\n[workspace.package]\nedition = "2024"\n\n'
                  '[workspace.dependencies]\ntides-core = { path = "crates/tides-core" }\n',
    'crates/tides-core/Cargo.toml': (
        '[package]\nname = "tides-core"\nversion = "0.1.0"\nedition.workspace = true\n\n'
        '[features]\ndefault = ["metric"]\nmetric = []\nchart = ["dep:plotlib"]\n\n'
        '[dependencies]\nlibloading = "0.8"\nplotlib = { version = "0.5", optional = true }\n'),
    'crates/tides-core/src/lib.rs': (
        '//! Tide tables.\n'
        'pub mod height;\n'
        'mod plugin;\n'
        '\n'
        '/// Doubles a reading.\n'
        '///\n'
        '/// ```\n'
        '/// assert_eq!(tides_core::double(2), 4);\n'
        '/// ```\n'
        '///\n'
        '/// ```text\n'
        '/// not a test\n'
        '/// ```\n'
        'pub fn double(x: i32) -> i32 { x * 2 }\n'
        '\n'
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    use super::*;\n'
        '    #[test]\n'
        '    fn doubles() { assert_eq!(double(2), 4); }\n'
        '\n'
        '    #[test]\n'
        '    #[ignore = "needs a tide gauge"]\n'
        '    fn reads_gauge() {}\n'
        '\n'
        '    #[cfg(feature = "chart")]\n'
        '    #[test]\n'
        '    fn draws_chart() { let s = "fn fake() { #[test] }"; let _ = s; }\n'
        '}\n'
        'mod native;\n'
        '#[cfg(feature = "chart")]\n'
        'mod charting;\n'
        '#[cfg(any(feature = "metric", test))]\n'
        'mod either;\n'),
    'crates/tides-core/src/charting.rs': (
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    #[test]\n'
        '    fn plots() {}\n'
        '}\n'),
    'crates/tides-core/src/either.rs': (
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    #[test]\n'
        '    fn runs() {}\n'
        '}\n'
        '#[cfg(all(feature = "metric", test))]\n'
        'mod gated {\n'
        '    #[test]\n'
        '    fn gated() {}\n'
        '}\n'
        '#[cfg(test)]\n'
        '#[cfg(not(feature = "chart"))]\n'
        '#[test]\n'
        'fn no_chart() {}\n'
        '#[cfg(test)]\n'
        '#[cfg(any(feature = "metric", feature = "chart"))]\n'
        '#[test]\n'
        'fn either_feature() {}\n'),
    'crates/tides-core/src/native.rs': (
        'use std::path::{Path, PathBuf};\n'
        'use std::process::Command;\n'
        '\n'
        'const GAUGE_DRIVER: &str = "drivers/gauge.dll";\n'
        'static HELPER: &str = "./tools/tide-helper";\n'
        'const SEARCH: [&str; 2] = ["lib/libgauge.so", "../shared/libgauge.so"];\n'
        '\n'
        'pub fn open_windows() {\n'
        '    unsafe { let _ = LoadLibraryExW(w!("drivers\\\\gauge.dll"), None, 0); }\n'
        '    unsafe { let _ = LoadLibraryW(w!("gauge.dll")); }\n'
        '    unsafe { let _ = libc::dlopen(c"libgauge.so".as_ptr(), 1); }\n'
        '}\n'
        '\n'
        'pub fn open_const() { let _ = unsafe { libloading::Library::new(GAUGE_DRIVER) }; }\n'
        'pub fn helper() { let _ = Command::new(HELPER).status(); }\n'
        'pub fn helper_path() { let p = PathBuf::from("bin/tide-helper"); let _ = Command::new(&p).output(); }\n'
        'pub fn inline_path() { let _ = Command::new(Path::new("bin/other")).spawn(); }\n'
        'pub fn search() {\n'
        '    for cand in SEARCH.iter() {\n'
        '        let _ = unsafe { libloading::Library::new(cand) };\n'
        '    }\n'
        '    let paths = vec![PathBuf::from("plugins/a.so"), PathBuf::from("/usr/lib/a.so")];\n'
        '    let _ = paths.iter().find_map(|p| unsafe { libloading::Library::new(p) }.ok());\n'
        '}\n'
        'pub fn load_engine() {\n'
        '    let candidates: Vec<PathBuf> = if let Some(p) = std::env::var_os("GAUGE_ENGINE") {\n'
        '        vec![p.into()]\n'
        '    } else if cfg!(windows) {\n'
        '        let mut found = vec![PathBuf::from("engine/gauge.dll")];\n'
        '        if let Ok(exe) = std::env::current_exe() { found.push(exe.with_file_name("data/gauge.dll")); }\n'
        '        found\n'
        '    } else {\n'
        '        vec!["libgauge.so.1".into()]\n'
        '    };\n'
        '    for path in candidates {\n'
        '        let _ = unsafe { libloading::Library::new(&path) };\n'
        '    }\n'
        '}\n'),
    'crates/tides-core/src/height.rs': (
        'pub fn metres(ft: f64) -> f64 { ft * 0.3048 }\n'
        '\n'
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    #[test]\n'
        '    fn converts() { assert!(super::metres(1.0) > 0.3); }\n'
        '}\n'),
    'crates/tides-core/src/plugin.rs': (
        'use std::env;\n'
        '\n'
        'pub fn load() -> Option<libloading::Library> {\n'
        '    let dir = env::var("TIDES_PLUGIN_DIR").unwrap_or_default();\n'
        '    let _ = dir;\n'
        '    // SAFETY: the plugin is our own build\n'
        '    unsafe { libloading::Library::new("plugins/libtides_extra.so").ok() }\n'
        '}\n'
        '\n'
        'pub unsafe fn raw_read(p: *const u8) -> u8 {\n'
        '    unsafe { *p }\n'
        '}\n'),
    'crates/tides-core/src/orphan.rs': 'pub fn unused() {}\n',
    'crates/tides-cli/Cargo.toml': (
        '[package]\nname = "tides-cli"\nversion = "0.1.0"\nedition = "2024"\n\n'
        '[[bin]]\nname = "tides"\npath = "src/main.rs"\n\n'
        '[[bin]]\nname = "fake-plotter"\npath = "tests/fixtures/fake_plotter.rs"\nrequired-features = ["fixtures"]\n\n'
        '[[bench]]\nname = "lookup"\nharness = false\n\n'
        '[features]\nfixtures = []\n\n'
        '[dependencies]\nclap = { version = "4", features = ["derive", "env"] }\ntides-core = { workspace = true }\n'),
    'crates/tides-cli/src/main.rs': (
        'use clap::{Parser, Subcommand, Args};\n'
        'use std::process::Command;\n'
        '\n'
        '#[derive(Parser)]\n'
        '#[command(name = "tides", version)]\n'
        'struct Cli {\n'
        '    /// Station id\n'
        '    #[arg(long, short, env = "TIDES_STATION")]\n'
        '    station: String,\n'
        '    #[command(subcommand)]\n'
        '    command: Cmd,\n'
        '}\n'
        '\n'
        '#[derive(Subcommand)]\n'
        'enum Cmd {\n'
        '    /// Next high tide\n'
        '    NextHigh {\n'
        '        #[arg(long = "days")]\n'
        '        days: u32,\n'
        '    },\n'
        '    Plot(PlotArgs),\n'
        '}\n'
        '\n'
        '#[derive(Args)]\n'
        'struct PlotArgs {\n'
        '    #[arg(short = \'o\')]\n'
        '    output: String,\n'
        '}\n'
        '\n'
        'fn plot() {\n'
        '    let base = std::env::current_dir().unwrap();\n'
        '    let _ = Command::new("./bin/plotter").current_dir(base).status();\n'
        '}\n'
        '\n'
        'fn main() {\n'
        '    let _ = Cli::parse();\n'
        '    plot();\n'
        '}\n'
        '\n'
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    #[test]\n'
        '    fn parses() {}\n'
        '}\n'
        'mod runner;\n'),
    'crates/tides-cli/src/runner.rs': 'pub fn go() {}\n',
    'crates/tides-chart/Cargo.toml': ('[package]\nname = "tides-chart"\nversion = "0.1.0"\nedition = "2024"\n\n'
                                      '[dependencies]\ntides-core = { path = "../tides-core" }\n'),
    'crates/tides-chart/src/lib.rs': 'pub fn chart() {}\n',
    'crates/tides-app/Cargo.toml': ('[package]\nname = "tides-app"\nversion = "0.1.0"\nedition = "2024"\n\n'
                                    '[dependencies]\ntides-chart = { path = "../tides-chart" }\n'),
    'crates/tides-app/src/lib.rs': 'pub fn app() {}\n',
    'crates/tides-app/tests/app.rs': 'fn helper() {}\n',
    'crates/tides-cli/src/bin/tidectl.rs': (
        'use clap::{Arg, Command};\n'
        '\n'
        'fn main() {\n'
        '    let _m = Command::new("tidectl")\n'
        '        .subcommand(Command::new("sync").arg(Arg::new("force").long("force").short(\'f\')))\n'
        '        .arg(Arg::new("config").long("config").env("TIDECTL_CONFIG"))\n'
        '        .get_matches();\n'
        '}\n'),
    'crates/tides-cli/tests/cli.rs': (
        '#[test]\n'
        'fn prints_help() {\n'
        '    let exe = env!("CARGO_BIN_EXE_tides");\n'
        '    let _ = std::process::Command::new(exe).arg("--help").output();\n'
        '}\n'
        '\n'
        '#[test]\n'
        '#[ignore]\n'
        'fn slow_station() {}\n'),
    'crates/tides-cli/tests/fixtures/fake_plotter.rs': 'fn main() { println!("plotted"); }\n',
    'crates/tides-cli/tests/fixtures/echo_station.rs': 'fn main() {}\n',
    'crates/tides-cli/tests/common/mod.rs': 'pub fn setup() {}\n',
    'crates/tides-cli/benches/lookup.rs': 'fn main() {}\n',
    'crates/tides-cli/examples/demo.rs': 'fn main() {}\n',
}


def self_test() -> int:
    d = Path(tempfile.mkdtemp(prefix='cargo-project-'))
    for rel, body in FIXTURE.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(body, encoding='utf-8')
    checks = []

    def check(name, ok, extra=None):
        checks.append((name, bool(ok), extra))
    try:
        m = read_cargo(d)
    except Exception as e:   # noqa: BLE001
        import traceback
        traceback.print_exc()
        m = {'error': repr(e), 'crates': [], 'workspace': None}
    crates = {c['name']: c for c in m.get('crates', [])}
    core, cli = crates.get('tides-core', {}), crates.get('tides-cli', {})
    check('a virtual workspace with two member crates', m.get('is_cargo') and (m.get('workspace') or {}).get('virtual')
          and (m.get('workspace') or {}).get('members') == ['crates/tides-app', 'crates/tides-chart', 'crates/tides-cli', 'crates/tides-core'], m.get('workspace'))
    check('edition.workspace = true resolves to the workspace edition', core.get('edition') == '2024', core.get('edition'))
    check('features: default, optional (not enabled by default) and all',
          core.get('features') == {'default': ['metric'], 'optional': ['chart'], 'all': ['chart', 'metric']},
          core.get('features'))
    tg = {(t['kind'], t['name']): t for t in cli.get('targets', [])}
    check('targets: declared bins, src/bin autodiscovery, tests, a harness=false bench, an example',
          {('bin', 'tides'), ('bin', 'tidectl'), ('bin', 'fake-plotter'), ('test', 'cli'), ('bench', 'lookup'),
           ('example', 'demo')} <= set(tg) and tg[('bench', 'lookup')]['harness'] is False
          and tg[('bin', 'tidectl')]['autodiscovered'] is True
          and tg[('bin', 'fake-plotter')]['required_features'] == ['fixtures'], sorted(tg))
    check('lib target named with underscores', any(t['kind'] == 'lib' and t['name'] == 'tides_core'
                                                   for t in core.get('targets', [])), core.get('targets'))
    fb = {f['file']: f for f in cli.get('fixture_bins', [])}
    check('helper binaries under tests/fixtures are fixture_bins (declared or not), never tests',
          fb.get('crates/tides-cli/tests/fixtures/fake_plotter.rs', {}).get('declared') is True
          and fb.get('crates/tides-cli/tests/fixtures/echo_station.rs', {}).get('declared') is False
          and 'crates/tides-cli/tests/common/mod.rs' not in fb, fb)
    tests = {t['id']: t for c in m.get('crates', []) for t in c['tests'] if t.get('id')}
    want = {'tides_core::charting::tests::plots', 'tides_core::either::tests::runs', 'tides_core::either::gated::gated',
            'tides_core::either::no_chart', 'tides_core::either::either_feature', 'tides_core::tests::doubles', 'tides_core::tests::reads_gauge', 'tides_core::tests::draws_chart',
            'tides_core::height::tests::converts', 'tides::tests::parses', 'cli::prints_help', 'cli::slow_station'}
    check('test ids are <target>::<module path::name> for unit and integration tests', set(tests) == want, sorted(tests))
    check('#[ignore] (with or without a reason) is recorded',
          tests.get('tides_core::tests::reads_gauge', {}).get('ignored') and tests.get('cli::slow_station', {}).get('ignored')
          and not tests.get('tides_core::tests::doubles', {}).get('ignored'), None)
    dc = tests.get('tides_core::tests::draws_chart', {})
    check('a #[cfg(feature)] gate on a test is recorded with the enclosing cfg(test)',
          dc.get('features') == ['chart'] and 'test' in dc.get('cfg', []), dc)
    check('unit tests are kind unit, tests/*.rs are integration',
          tests.get('tides_core::tests::doubles', {}).get('kind') == 'unit'
          and tests.get('cli::prints_help', {}).get('kind') == 'integration', None)
    docs = [t for t in core.get('tests', []) if t['kind'] == 'doc']
    check('a doc test in /// is found; a ```text block is not', len(docs) == 1 and docs[0]['line'] == 7, docs)
    clis = {c['name']: c for c in cli.get('clis', [])}
    tc = clis.get('tides', {})
    cmds = {c['name']: c for c in tc.get('commands', [])}
    args = {(a['parent'], a['name']): a for a in tc.get('args', [])}
    check('clap derive: Parser, Subcommand variants in kebab case, a tuple variant\'s Args struct',
          tc.get('style') == 'derive' and tc.get('bin') == 'tides' and set(cmds) == {'next-high', 'plot'}
          and cmds['next-high']['line'] == 17, (tc.get('commands'),))
    check('clap derive args: long, short, env; explicit long and short; parent command',
          args.get(('tides', 'station'), {}).get('long') == 'station'
          and args.get(('tides', 'station'), {}).get('short') == 's'
          and args.get(('tides', 'station'), {}).get('env') == 'TIDES_STATION'
          and args.get(('next-high', 'days'), {}).get('long') == 'days'
          and args.get(('plot', 'output'), {}).get('short') == 'o', sorted(args))
    bc = clis.get('tidectl', {})
    bargs = {a['name']: a for a in bc.get('args', [])}
    check('clap builder: Command::new root, subcommand, Arg long/short/env',
          bc.get('style') == 'builder' and bc.get('bin') == 'tidectl'
          and [c['name'] for c in bc.get('commands', [])] == ['sync']
          and bargs.get('force', {}).get('short') == 'f' and bargs.get('config', {}).get('env') == 'TIDECTL_CONFIG', bc)
    check('clap builder: an .arg chained on the root after a .subcommand stays on the root; the nested one on its '
          'subcommand', bargs.get('config', {}).get('parent') == 'tidectl' and bargs.get('force', {}).get('parent') == 'sync'
          and [c['parent'] for c in bc.get('commands', [])] == ['tidectl'], bc.get('args'))
    nb = {c['name']: c for c in _clis({'builder': _builder_items(
        'use clap::{Arg, Command};\n'
        'fn cli() -> Command {\n'
        '    Command::new("gauge")\n'
        '        .subcommand(Command::new("remote").subcommand(Command::new("add").arg(Arg::new("url")))\n'
        '                    .arg(Arg::new("verbose").long("verbose")))\n'
        '        .arg(Arg::new("quiet").long("quiet"))\n'
        '}\n'
        'fn extra() -> Command { Command::new("prune").arg(Arg::new("dry").long("dry-run")) }\n')},
        'x.rs', None, None)}.get('gauge', {})
    na = {a['name']: a['parent'] for a in nb.get('args', [])}
    check('clap builder nesting: a sub-subcommand\'s parent is its subcommand; args follow their own command',
          na == {'url': 'add', 'verbose': 'remote', 'quiet': 'gauge', 'dry': 'prune'}
          and {c['name']: c['parent'] for c in nb.get('commands', [])} == {'remote': 'gauge', 'add': 'remote',
                                                                             'prune': 'gauge'}, nb)
    us = {(u['file'].rsplit('/', 1)[-1], u['line']): u for u in core.get('unsafe', [])}
    check('unsafe sites: a block with a SAFETY comment, an unsafe fn without one, the block inside it',
          us.get(('plugin.rs', 7), {}).get('safety_comment') is True and us.get(('plugin.rs', 10), {}).get('kind') == 'fn'
          and us.get(('plugin.rs', 10), {}).get('safety_comment') is False and ('plugin.rs', 11) in us, sorted(us))
    pl = cli.get('process_launches', [])
    check('std::process::Command launches are found (clap\'s Command::new is not)',
          {(p['file'].rsplit('/', 1)[-1], p['program_expr']) for p in pl}
          == {('main.rs', '"./bin/plotter"'), ('cli.rs', 'exe')}, pl)
    ll = core.get('library_loads', [])
    check('libloading Library::new is a dynamic library load with its path expression',
          [x['path_expr'] for x in ll if x['file'].endswith('plugin.rs')] == ['"plugins/libtides_extra.so"'], ll)
    calls = {x['call'] for x in ll if x['file'].endswith('native.rs')}
    check('LoadLibraryExW, LoadLibraryW (windows crates) and dlopen are library loads',
          {'LoadLibraryExW', 'LoadLibraryW', 'dlopen'} <= calls, calls)
    via = {(r['used_for'], tuple(r.get('literals') or []), r.get('via')) for r in core.get('relative_paths', [])
           if r['file'].endswith('native.rs')}
    check('relative paths reaching Library::new / Command::new through a const, a static, PathBuf::from, Path::new, '
          'a w! literal and an iterated array of candidates are recorded with `via`',
          {('library', ('drivers/gauge.dll',), 'const GAUGE_DRIVER'),
           ('executable', ('./tools/tide-helper',), 'static HELPER'),
           ('executable', ('bin/tide-helper',), 'let p = PathBuf::from'),
           ('executable', ('bin/other',), 'Path::new'),
           ('library', ('drivers\\\\gauge.dll',), 'w!'),
           ('library', ('lib/libgauge.so', '../shared/libgauge.so'), 'const SEARCH (iterated as cand)'),
           ('library', ('plugins/a.so',), 'let paths = vec! (iterated as p)')} <= via, sorted(via, key=str))
    check('a candidate list chosen by if/else branches (one a vec! of PathBuf::from) reaches the call it is iterated '
          'into; a literal joined onto another base does not count',
          ('library', ('engine/gauge.dll',), 'let candidates = if (iterated as path)') in via
          and not any('data/gauge.dll' in v[1] for v in via), sorted(via, key=str))
    tl = globals().get('test_lines')
    sample = ('pub fn a() {}\n'                       # 1
              '#[cfg(not(test))]\n'                   # 2
              'fn real() {}\n'                        # 3
              '#[cfg(test)]\n'                        # 4
              'mod unit;\n'                           # 5
              '#[cfg(all(test, feature = "x"))]\n'    # 6
              'mod tests {\n'                         # 7
              '    fn helper(a: [u8; 2]) {}\n'        # 8
              '}\n'                                   # 9
              'fn b() {}\n'                           # 10
              '#[test]\n'                             # 11
              '#[ignore]\n'                           # 12
              'fn top_level() {\n'                    # 13
              '    let s = "}";\n'                    # 14
              '}\n'                                   # 15
              'fn c() {}\n')                          # 16
    got = tl(sample) if tl else None
    check('test_lines(text): the lines of #[cfg(test)] items and #[test] functions (not cfg(not(test)))',
          got == {4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15}, got)
    ev = {(e['name'], e['kind']) for c in m.get('crates', []) for e in c['env_reads']}
    check('env reads: env::var at run time, env! at compile time', {('TIDES_PLUGIN_DIR', 'runtime'),
                                                                     ('CARGO_BIN_EXE_tides', 'compile_time')} <= ev, ev)
    rp = {(r['file'].rsplit('/', 1)[-1], r['used_for']) for c in m.get('crates', []) for r in c['relative_paths']}
    check('working-directory-relative executables and libraries, and current_dir() bases',
          {('main.rs', 'executable'), ('plugin.rs', 'library')} <= rp
          and any(r.get('base') == 'current_dir()' for r in cli.get('relative_paths', [])), rp)
    check('files no target reaches are listed', core.get('unreached_files') == ['crates/tides-core/src/orphan.rs'],
          core.get('unreached_files'))
    check('mod declarations give module paths', any(x['module'] == 'height' for x in core.get('modules', [])),
          core.get('modules'))
    check('path dependencies through workspace = true', any(p['name'] == 'tides-core' and p['dir'] == 'crates/tides-core'
                                                             for p in cli.get('path_deps', [])), cli.get('path_deps'))
    reach = {(r['crate'], r['target']) for r in targets_reaching(m, 'crates/tides-core/src/height.rs')}
    check('targets_reaching: a library file reaches its lib and the dependent crate\'s targets',
          ('tides-core', 'tides_core') in reach and ('tides-cli', 'cli') in reach, reach)
    check('targets_reaching: path-dependents are followed transitively; bench and example targets only on request',
          ('tides-app', 'app') in reach and ('tides-chart', 'tides_chart') in reach
          and not {('tides-cli', 'lookup'), ('tides-cli', 'demo')} & reach
          and {('tides-cli', 'lookup'), ('tides-cli', 'demo')} <= {
              (r['crate'], r['target']) for r in targets_reaching(m, 'crates/tides-core/src/height.rs',
                                                                  include_bench_examples=True)}, sorted(reach))
    rr = {(r['crate'], r['target']) for r in targets_reaching(m, 'crates/tides-cli/src/runner.rs')}
    check('targets_reaching: a module of a bin-only target is not a library file: its bin and the crate\'s '
          'integration tests (which may run the bin), never the lib\'s dependents or other bins',
          ('tides-cli', 'tides') in rr and ('tides-cli', 'cli') in rr and ('tides-cli', 'tidectl') not in rr
          and not any(c != 'tides-cli' for c, _ in rr), sorted(rr))
    tfs = globals().get('target_files')
    tf = tfs(m) if tfs else {}
    check('target_files(model): the files each target\'s module tree holds, keyed <crate>:<kind>:<name>',
          'crates/tides-core/src/height.rs' in tf.get('tides-core:lib:tides_core', set())
          and 'crates/tides-cli/src/runner.rs' in tf.get('tides-cli:bin:tides', set())
          and 'crates/tides-cli/src/runner.rs' not in tf.get('tides-cli:bin:tidectl', set()), sorted(tf))
    feats = {t['fn']: t['features'] for t in core.get('tests', []) if t.get('fn')}
    check('test features: the cfg on the `mod x;` that includes a file gates its tests; any(feature, test) does not '
          'gate, all(feature, test) does, not(feature) does not',
          feats.get('plots') == ['chart'] and feats.get('runs') == [] and feats.get('gated') == ['metric']
          and feats.get('no_chart') == [] and feats.get('draws_chart') == ['chart'], feats)
    ef = next((t for t in core.get('tests', []) if t.get('fn') == 'either_feature'), {})
    check('test features: any(feature = a, feature = b) is recorded as alternatives, not as both required',
          ef.get('features') == [] and ef.get('features_any') == [['chart', 'metric']], ef)
    check('test_ids lists the ids', set(test_ids(m)) == want, None) if 'error' not in m else None
    empty = Path(tempfile.mkdtemp(prefix='cargo-none-'))
    check('no Cargo.toml: is_cargo false and the CLI exits 3', read_cargo(empty)['is_cargo'] is False
          and main([str(empty)]) == 3, None)
    mm = read_cargo(d, metadata={'packages': [{'name': 'tides-core', 'features': {'default': [], 'gui': []},
                                               'targets': [{'kind': ['lib'], 'name': 'tides_core',
                                                            'src_path': str(d / 'crates/tides-core/src/lib.rs')}]}]})
    mc = next(c for c in mm['crates'] if c['name'] == 'tides-core')
    check('--metadata replaces targets and features with cargo\'s own', mc['features']['all'] == ['gui']
          and mc['targets'][0].get('from') == 'cargo metadata', mc['features'])
    # two crates with tests/cli.rs, and a crate whose lib and integration test share a name
    two = Path(tempfile.mkdtemp(prefix='cargo-two-'))
    for rel, body in {'Cargo.toml': '[workspace]\nmembers = ["recipe-scale", "pantry"]\n',
                      'recipe-scale/Cargo.toml': '[package]\nname = "recipe-scale"\nversion = "0.1.0"\n',
                      'recipe-scale/src/lib.rs': '#[cfg(test)]\nmod tests {\n    #[test]\n    fn halves() {}\n}\n',
                      'recipe-scale/tests/cli.rs': '#[test]\nfn prints_help() {}\n',
                      'recipe-scale/tests/recipe_scale.rs': '#[test]\nfn round_trip() {}\n',
                      'pantry/Cargo.toml': '[package]\nname = "pantry"\nversion = "0.1.0"\n',
                      'pantry/src/main.rs': 'fn main() {}\n',
                      'pantry/tests/cli.rs': '#[test]\nfn prints_help() {}\n'}.items():
        (two / rel).parent.mkdir(parents=True, exist_ok=True)
        (two / rel).write_text(body, encoding='utf-8')
    ids2 = sorted(test_ids(read_cargo(two)))
    check('test ids carry the crate when two crates share a target name (<crate>/<target>), and the kind when one '
          'crate has two targets of that name (<crate>/<kind>/<target>); unique names stay plain',
          ids2 == ['pantry/cli::prints_help', 'recipe-scale/cli::prints_help',
                   'recipe-scale/lib/recipe_scale::tests::halves', 'recipe-scale/test/recipe_scale::round_trip'], ids2)
    shutil.rmtree(two, ignore_errors=True)
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--metadata', type=Path, help='JSON from `cargo metadata --format-version 1 --no-deps`')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    meta = json.loads(args.metadata.read_text(encoding='utf-8')) if args.metadata else None
    m = read_cargo(args.repo, meta)
    text = json.dumps(m, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
    elif m['is_cargo']:
        sys.stdout.write(text)
    if not m['is_cargo']:
        print(f'{TOOL}: no Cargo.toml in {args.repo}: not a Cargo project', file=sys.stderr)
        return 3
    n_t = sum(len(c['tests']) for c in m['crates'])
    print(f'{len(m["crates"])} crate(s), {sum(len(c["targets"]) for c in m["crates"])} target(s), {n_t} test(s), '
          f'{sum(len(c["clis"]) for c in m["crates"])} CLI(s)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
