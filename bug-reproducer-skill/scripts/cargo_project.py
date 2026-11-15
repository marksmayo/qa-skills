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
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys, tempfile
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
LIB_LOAD = re.compile(r'\b((?:libloading::)?(?:os::\w+::)?Library::(?:new|open)|dlopen|LoadLibrary[AW]?)\s*\(')
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
            rel = _relative_literal(expr)
            if rel:
                res['relpaths'].append({'line': ln, 'expr': expr, 'used_for': 'executable'})
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
        if _relative_literal(expr):
            res['relpaths'].append({'line': ln, 'expr': expr, 'used_for': 'library'})
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


def _module_walk(cdir: Path, root_file: str, cache: dict) -> dict[str, str]:
    """{file (crate-relative): module path} for every file reached from a target's root file through `mod x;`."""
    out: dict[str, str] = {}
    todo = [(root_file, '', True)]
    while todo:
        rel, mod, is_root = todo.pop()
        if rel in out or not (cdir / rel).is_file():
            continue
        out[rel] = mod
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
                todo.append((os.path.normpath(hit).replace('\\', '/'), modpath, False))
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
    for t in targets:
        local = t['path'][len(pre):]
        for f, mod in _module_walk(cdir, local, cache).items():
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
            feats = sorted({x for cf in t['cfg'] for x in FEATURE.findall(cf)})
            c['tests'].append({'name': full, 'fn': t['name'], 'file': frel, 'line': t['line'], 'kind': kind,
                               'module': modpath, 'ignored': t['ignored'], 'should_panic': t['should_panic'],
                               'cfg': t['cfg'], 'features': feats,
                               'target': test_tg['name'] if test_tg else None,
                               'target_kind': test_tg['kind'] if test_tg else None,
                               'id': f'{test_tg["name"]}::{full}' if test_tg else None})
        lib_t = next((t for t in tg if t['kind'] == 'lib'), None)
        if lib_t:
            for d in sc.get('doctests', []):
                c['tests'].append({'name': f'{f} - {mod or c["name"]} (line {d["line"]})', 'fn': None, 'file': frel,
                                   'line': d['line'], 'kind': 'doc', 'module': mod, 'ignored': d['ignored'],
                                   'no_run': d['no_run'], 'cfg': [], 'features': [], 'target': lib_t['name'],
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
        cmds, args, current = [], [], root['name']
        for x in b:
            if x['kind'] == 'command' and x is not root:
                cmds.append({'name': x['name'], 'line': x['line'], 'parent': root['name'], 'file': frel})
                current = x['name']
            elif x['kind'] == 'arg':
                args.append({'name': x['name'], 'long': x['long'], 'short': x['short'], 'env': x['env'],
                             'line': x['line'], 'positional': not (x['long'] or x['short']), 'parent': current,
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
               fixture_bins: [{file, name, declared, note}],
               tests: [{name (libtest path), fn, file, line, kind unit|integration|doc, module, ignored,
                        should_panic, cfg: [cfg exprs of the fn and its modules], features, target, target_kind,
                        id: '<target>::<name>' (None for doc tests)}],
               clis: [{bin, target, file, line, style derive|builder, name, struct, parent,
                       commands: [{name, line, parent, file, ...}],
                       args: [{name, long, short, env, line, positional, parent, file}]}],
               unsafe: [{file, line, kind block|fn|impl|trait, safety_comment}],
               process_launches: [{file, line, program_expr}],
               library_loads: [{file, line, call, path_expr}],
               env_reads: [{file, line, name (None when not a literal, then expr), kind runtime|compile_time}],
               relative_paths: [{file, line, expr, used_for executable|library|path, base?}],
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
        feats = p.get('features')
        if isinstance(feats, dict):
            c['features'] = _features({'features': feats})
        del cdir
    notes.append('targets and features come from cargo metadata')


def test_ids(model: dict) -> list[str]:
    """Every libtest id ('<target>::<path::name>') the static scan found (doc tests left out)."""
    return [t['id'] for c in model.get('crates', []) for t in c['tests'] if t.get('id')]


def targets_reaching(model: dict, file: str) -> list[dict]:
    """[{crate, target, kind}] whose build includes file (repo-relative): the crate's targets whose module tree holds
    it, plus every test, bench and example target of that crate and of the crates that depend on it by path when it
    belongs to a library (they link the library)."""
    file = file.replace('\\', '/').strip('/')
    owner = next((c for c in model.get('crates', []) if c['dir'] == '.' or file.startswith(c['dir'] + '/')), None)
    if not owner:
        return []
    out = []
    in_lib = file not in owner['unreached_files'] and not any(
        file == t['path'] for t in owner['targets'] if t['kind'] in ('test', 'bench', 'example', 'bin'))
    for t in owner['targets']:
        if file == t['path'] or (in_lib and t['kind'] in ('lib', 'test', 'bench', 'example')):
            out.append({'crate': owner['name'], 'target': t['name'], 'kind': t['kind']})
    if in_lib:
        for c in model.get('crates', []):
            if any(d['name'] == owner['name'] for d in c['path_deps']):
                out += [{'crate': c['name'], 'target': t['name'], 'kind': t['kind']} for t in c['targets']
                        if t['kind'] != 'build']
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
        '}\n'),
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
          and (m.get('workspace') or {}).get('members') == ['crates/tides-cli', 'crates/tides-core'], m.get('workspace'))
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
    want = {'tides_core::tests::doubles', 'tides_core::tests::reads_gauge', 'tides_core::tests::draws_chart',
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
          len(ll) == 1 and ll[0]['path_expr'] == '"plugins/libtides_extra.so"', ll)
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
