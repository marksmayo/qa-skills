#!/usr/bin/env python3
"""List static flake smells in a repository's tests (stdlib only).

    python scan_pitfalls.py <repo> [--out smells.json]

Applies data/pitfalls.json to test files (and, for smells with scope "source", to non-test
source files that tests import). Every hit is a lead with a file, line and the cause class it
usually leads to. A smell is never a flake on its own: it becomes one only when runs show a
pass and a fail. Smells whose tests never failed stay in the report as smells.

For JavaScript and TypeScript tests a smell's `test` is the describe chain and title joined with
" > ", the way Vitest and Jest name tests in JUnit ("checkout > applies a code"), so a smell can
be matched to a flake by file and name. A smell whose risk the repo already handles (Math.random
stubbed, fake timers in the file or a setup file) is kept, with a `mitigation` saying where.
The `tz-in-thread-setup` smell is a project check: `process.env.TZ` assigned in a Vitest setup
file while the tests run in worker threads, where the assignment does not change the zone.
"""
from __future__ import annotations
import argparse, json, os, re, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / 'data' / 'pitfalls.json'
# pruned before descending (never walked, then filtered)
SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', 'target', '.pytest_cache', '.tox',
        '.next', '.nuxt', 'out', 'coverage', '.turbo', '.cache', '.stryker-tmp', '.mypy_cache', '.gradle', '.idea',
        'vendor', 'third_party', 'Pods', 'Carthage'}
TEST_NAME = re.compile(r'^\s*(?:async\s+)?def\s+(test_\w+)|'
                       r'^\s*(?:it|test)(?:\.(?:only|skip|concurrent|sequential|fails|todo|runIf\([^)]*\)|skipIf\([^)]*\)))*'
                       r'\s*\(\s*([\'"`])((?:(?!\2).)+)\2|'
                       r'^\s*(?:public\s+)?(?:void|fun)\s+(\w+)\s*\(|^func\s+(Test\w+)\s*\(|'
                       r'^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(')
DESCRIBE = re.compile(r'^(\s*)describe(?:\.(?:only|skip|concurrent|sequential|shuffle|todo|runIf\([^)]*\)|skipIf\([^)]*\)))*'
                      r'\s*\(\s*([\'"`])((?:(?!\2).)+)\2')
JS_LANGS = ('javascript', 'typescript')
SETUP_NAME = re.compile(r'(^|/)(?:vitest|jest)\.setup\.[cm]?[jt]sx?$|(^|/)setupTests\.[jt]sx?$|'
                        r'(^|/)test[-_.]?setup\.[cm]?[jt]sx?$')
CONFIG_NAME = re.compile(r'(^|/)(?:vitest|vite|jest)\.(?:config|workspace)\.[cm]?[jt]s$')
SETUP_LIST = re.compile(r'setupFiles(?:AfterEnv)?\s*:\s*(\[[^\]]*\]|[\'"][^\'"]+[\'"])')
POOL = re.compile(r'\bpool\s*:\s*[\'"](\w+)[\'"]')
# A broad suffix -> language map used only to name a repo's dominant language, so an unsupported
# language (no smell pack) is still recognised and reported rather than silently skipped.
LANG_BY_SUFFIX_BROAD = {
    '.py': 'python', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript', '.java': 'java', '.kt': 'kotlin', '.kts': 'kotlin',
    '.go': 'go', '.rs': 'rust', '.rb': 'ruby', '.php': 'php', '.cs': 'csharp', '.swift': 'swift',
    '.scala': 'scala', '.clj': 'clojure', '.ex': 'elixir', '.exs': 'elixir', '.dart': 'dart',
    '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp', '.c': 'c', '.h': 'c', '.m': 'objc'}


def files(root: Path):
    """Every file under root, pruning SKIP directories before descending into them."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        for f in sorted(filenames):
            yield Path(dirpath) / f


def _split_ini_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [v for v in re.split(r'[\s,]+', str(value or '')) if v]


def pytest_collection(root: Path) -> dict:
    """pytest's own collection settings at the repo root (pytest.ini, pyproject.toml, tox.ini, setup.cfg, in pytest's
    order): {'config': file, 'testpaths': [...], 'python_files': [...]}, or {} when no pytest section is found."""
    import configparser
    for name, section in (('pytest.ini', 'pytest'), ('pyproject.toml', None), ('tox.ini', 'pytest'),
                          ('setup.cfg', 'tool:pytest')):
        p = root / name
        if not p.is_file():
            continue
        try:
            if section is None:
                import tomllib
                opts = ((tomllib.loads(p.read_text(encoding='utf-8')).get('tool') or {}).get('pytest') or {}
                        ).get('ini_options')
                if opts is None:
                    continue
            else:
                cp = configparser.ConfigParser(interpolation=None)
                cp.read_string(p.read_text(encoding='utf-8', errors='replace'))
                if not cp.has_section(section):
                    if name == 'pytest.ini':      # an empty pytest.ini still marks the rootdir
                        return {'config': name, 'testpaths': [], 'python_files': []}
                    continue
                opts = dict(cp.items(section))
        except (OSError, ValueError, configparser.Error, ImportError):
            continue
        return {'config': name,
                'testpaths': [t.strip('/').replace('\\', '/') for t in _split_ini_list(opts.get('testpaths'))
                              if t.strip('/') not in ('', '.')],
                'python_files': _split_ini_list(opts.get('python_files'))}
    return {}


def collection_excluded(root: Path, all_files: list[Path] | None = None, data: dict | None = None) -> list[str]:
    """Python files the test-name patterns call tests but pytest's configuration never collects: outside every
    `testpaths` entry, or (with no testpaths) named outside `python_files` and not inside a test directory. A root
    script named *_test.py is then product tooling, not a test, and gets only the source-scope checks."""
    import fnmatch
    cfg = pytest_collection(root)
    if not cfg or not (cfg['testpaths'] or cfg['python_files']):
        return []
    data = data or json.loads(DATA.read_text(encoding='utf-8'))
    test_rx = [re.compile(p) for p in data['test_file_patterns']]
    out = []
    for p in all_files if all_files is not None else files(root):
        if p.suffix != '.py':
            continue
        rel = p.relative_to(root).as_posix()
        if not any(rx.search(rel) for rx in test_rx):
            continue
        if cfg['testpaths']:
            if not any(rel == t or rel.startswith(t + '/') for t in cfg['testpaths']):
                out.append(rel)
        elif not (p.name == 'conftest.py' or any(fnmatch.fnmatch(p.name, g) for g in cfg['python_files'])
                  or re.search(r'(^|/)tests?/', rel)):
            out.append(rel)
    return sorted(out)


PY_DEF = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)\s*\(')
RUST_FN = re.compile(r'^(\s*)(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+"[^"]*"\s+)?fn\s+(\w+)')
RUST_TEST_ATTR = re.compile(r'#\[\s*(?:[\w:]+::)?test\b|#\[\s*rstest\b|#\[\s*test_case\b|#\[\s*(?:[\w:]+::)?test\s*\(')
RUST_CFG_TEST_MOD = re.compile(r'#\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def rust_has_test_attr(lines: list[str], j: int) -> bool:
    """The fn at line j carries a test attribute (#[test], #[tokio::test], #[rstest], #[test_case(..)]) in the
    attribute and doc-comment lines right above it."""
    for k in range(j - 1, -1, -1):
        s = lines[k].strip()
        if not s or s.startswith(('//', '#[', '#![')) or s.endswith(')]') or s == ']':
            if RUST_TEST_ATTR.search(s):
                return True
            continue
        return False
    return False


def python_enclosing_test(lines: list[str], i: int) -> str | None:
    """The test function around line i: the nearest enclosing def named test*, looking outward by indentation through
    nested helpers. A line in a module-level helper (def make_x) is in no test: None."""
    indent = _indent(lines[i]) if lines[i].strip() else 10 ** 6
    m = PY_DEF.match(lines[i])
    if m and m.group(2).startswith('test'):
        return m.group(2)
    for j in range(i - 1, -1, -1):
        s = lines[j]
        if not s.strip():
            continue
        ind = _indent(s)
        if ind >= indent:
            continue
        m = PY_DEF.match(s)
        if m and m.group(2).startswith('test'):
            return m.group(2)
        indent = ind
        if ind == 0:
            return None
    return None


def rust_enclosing_test(lines: list[str], i: int) -> str | None:
    """The #[test] function around line i, looking outward by indentation; a line in a plain helper fn (a scratch
    folder maker, a fixture builder) is in no test: None."""
    m = RUST_FN.match(lines[i])
    if m and rust_has_test_attr(lines, i):
        return m.group(2)
    indent = _indent(lines[i]) if lines[i].strip() else 10 ** 6
    for j in range(i - 1, -1, -1):
        s = lines[j]
        if not s.strip() or s.strip().startswith('//'):
            continue
        ind = _indent(s)
        if ind >= indent:
            continue
        m = RUST_FN.match(s)
        if m and rust_has_test_attr(lines, j):
            return m.group(2)
        indent = ind
        if ind == 0:
            return None
    return None


def rust_test_lines(lines: list[str]) -> set[int]:
    """Line indexes inside #[cfg(test)] modules of a Rust source file (unit tests live there, beside the code)."""
    text = '\n'.join(lines)
    if 'cfg' not in text or 'test' not in text:
        return set()
    try:
        import cargo_project
        masked = cargo_project.mask(text)
    except Exception:          # cargo_project.py missing: comments and strings are not blanked
        masked = text
    out = set()
    for m in RUST_CFG_TEST_MOD.finditer(masked):
        depth, k = 0, m.end() - 1
        while k < len(masked):
            if masked[k] == '{':
                depth += 1
            elif masked[k] == '}':
                depth -= 1
                if depth == 0:
                    break
            k += 1
        a, b = masked.count('\n', 0, m.start()), masked.count('\n', 0, k)
        out.update(range(a, b + 1))
    return out


def in_string(line: str, pos: int, lang: str) -> bool:
    """Position pos of the line sits inside a string literal (a command passed to a child process, a message)."""
    quotes = '"' if lang == 'rust' else '"\''
    q, k = None, 0
    while k < pos and k < len(line):
        c = line[k]
        if q:
            if c == '\\':
                k += 2
                continue
            if c == q:
                q = None
        elif c in quotes:
            q = c
        elif c == '#' and lang == 'python' or line.startswith('//', k) and lang != 'python':
            return False
        k += 1
    return q is not None


NAME_CONTEXT = re.compile(r'format!|\.join\(|\bPath(?:Buf)?\b|temp_?dir|gettempdir|mkdtemp|mkdir|create_dir|\bf["\']|'
                          r'\.with_(?:file_)?name\(|\b\w*(?:dir|path|name|folder|prefix|suffix|filename)\b|'
                          r'[\'"]\s*\+\s*\S|\S\s*\+\s*[\'"]', re.I)          # a literal prefix or suffix glued on
ASSERTISH = re.compile(r'\bassert|\bexpect\(|[=!]=|\s[<>]=?\s|\bsorted\(|\bmin\(|\bmax\(|\.cmp\(|\.sort')
ASSIGNED = re.compile(r'^\s*(?:let\s+(?:mut\s+)?)?([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)')


def statement(lines: list[str], i: int, lang: str) -> str:
    """Line i and its continuation lines: to the `;` in Rust, until the brackets close in Python (at most 8 lines)."""
    out, depth = [], 0
    for k in range(i, min(i + 8, len(lines))):
        s = lines[k]
        out.append(s)
        depth += sum(s.count(c) for c in '([{') - sum(s.count(c) for c in ')]}')
        if (lang == 'rust' and s.rstrip().endswith((';', '{', '}'))) or (lang != 'rust' and depth <= 0):
            break
    return '\n'.join(out)


def statement_start(lines: list[str], i: int, lang: str) -> int:
    """The first line of the statement line i belongs to (a clock read on its own line inside a multi-line
    format!(...) or a call spanning several lines)."""
    start = i
    for k in range(i - 1, max(-1, i - 9), -1):
        s = lines[k].rstrip()
        if not s.strip():
            break
        if lang == 'rust':
            if s.endswith((';', '{', '}')):
                break
            start = k
        else:
            depth = sum(l.count(c) for l in lines[k:i] for c in '([{') - sum(l.count(c) for l in lines[k:i] for c in ')]}')
            if depth > 0:
                start = k
    return start


def unique_name_only(lines: list[str], i: int, lang: str) -> bool:
    """The clock or random read only makes a unique folder or file name and never reaches an assertion: uuid4() in a
    path, SystemTime::now() as nanoseconds formatted into a scratch folder's name. Either the statement builds a name
    itself, or it assigns a variable whose every later use builds a name. That is the isolation the skill recommends,
    not a flake lead."""
    i = statement_start(lines, i, lang)
    stmt = statement(lines, i, lang)
    if ASSERTISH.search(stmt):
        return False
    m = ASSIGNED.match(lines[i])
    rhs = stmt[stmt.index('=', m.end() - 1) + 1:] if m else stmt
    if NAME_CONTEXT.search(rhs) or (m and NAME_CONTEXT.search(m.group(1)) and 'temp' not in m.group(1).lower()):
        return True
    if not m:
        return False
    var = re.compile(r'(?<![\w.])' + re.escape(m.group(1)) + r'\b')
    n_lines = len(stmt.split('\n'))
    uses = [k for k in range(i + n_lines, len(lines)) if var.search(lines[k])]
    return bool(uses) and all(NAME_CONTEXT.search(statement(lines, k, lang)) and
                              not ASSERTISH.search(statement(lines, k, lang)) for k in uses)


def helper_programs(root: Path, data: dict, all_files: list[Path] | None = None) -> set[str]:
    """Files in test directories that are programs the tests launch, not tests: a main entry point and no test in
    the file (a fake server binary under tests/fixtures/, a Python script run as a child). They get only the source
    checks, so their sleeps and clocks are not reported as test smells."""
    test_rx = [re.compile(p) for p in data['test_file_patterns']]
    out = set()
    for p in all_files if all_files is not None else files(root):
        lang = data['language_by_suffix'].get(p.suffix)
        if lang not in ('rust', 'python', 'go'):
            continue
        rel = p.relative_to(root).as_posix()
        if not any(rx.search(rel) for rx in test_rx):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if lang == 'rust':
            main = re.search(r'(?m)^\s*(?:pub\s+)?(?:async\s+)?fn\s+main\s*\(', text)
            tests = RUST_TEST_ATTR.search(text)
        elif lang == 'python':
            main = re.search(r'(?m)^if\s+__name__\s*==\s*[\'"]__main__[\'"]', text)
            tests = re.search(r'(?m)^\s*(?:async\s+)?def\s+test|\bTestCase\b', text)
        else:
            main = re.search(r'(?m)^func\s+main\s*\(', text)
            tests = re.search(r'(?m)^func\s+(?:Test|Benchmark|Fuzz|Example)', text)
        if main and not tests:
            out.add(rel)
    return out


def test_bodies(lines: list[str], lang: str, test_lines: set[int] | None = None) -> list[tuple[str, int, int]]:
    """(name, def line, last line) of each test function: Python def test*, Rust fns with a test attribute (body by
    indentation: to the closing brace at the fn's own indentation)."""
    out = []
    for j, line in enumerate(lines):
        if lang == 'python':
            m = PY_DEF.match(line)
            if not m or not m.group(2).startswith('test'):
                continue
        else:
            m = RUST_FN.match(line)
            if not m or not rust_has_test_attr(lines, j):
                continue
        ind, end = len(m.group(1)), j
        for k in range(j + 1, len(lines)):
            s = lines[k]
            if not s.strip():
                continue
            if _indent(s) <= ind:
                end = k if lang == 'rust' and s.strip().startswith('}') else end
                break
            end = k
        out.append((m.group(2), j, end))
    return out


def body_hits(lines: list[str], lang: str, sm: dict) -> list[tuple[int, str]]:
    """(line index, test name) for a test-body smell. count_over_duration: a lower bound on a count asserted in a test
    whose body also runs or waits for a fixed duration (one hit per test). py_helper: a module-level def test_* that
    other code in the file calls (pytest would collect the helper as a test)."""
    rx = re.compile(sm['pattern'])
    code = lambda s: not s.strip().startswith(('#', '//'))  # noqa: E731
    hits = []
    if sm['scope'] == 'count_over_duration':
        dur = re.compile(sm['duration_pattern'])
        for name, a, b in test_bodies(lines, lang):
            body = [k for k in range(a, b + 1) if code(lines[k])]
            if not any(dur.search(lines[k]) for k in body):
                continue
            k = next((k for k in body if rx.search(lines[k])), None)
            if k is not None:
                hits.append((k, name))
    elif sm['scope'] == 'py_helper' and lang == 'python':
        for j, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if re.search(r'\b' + re.escape(name) + r'\.__test__\s*=\s*False', '\n'.join(lines)):
                continue
            call = re.compile(r'(?<![\w.])' + re.escape(name) + r'\s*\(')
            if any(k != j and code(l) and call.search(l) for k, l in enumerate(lines)):
                hits.append((j, name))
    return hits


def enclosing_test(lines: list[str], i: int, js: bool, lang: str | None = None) -> str | None:
    """The test the line sits in. For JS/TS, the describe chain and title joined with ' > '. Python and Rust: the
    enclosing test function only (a line in a helper is in no test)."""
    if lang == 'python':
        return python_enclosing_test(lines, i)
    if lang == 'rust':
        return rust_enclosing_test(lines, i)
    for j in range(i, -1, -1):
        m = TEST_NAME.match(lines[j])
        if not m:
            continue
        name = m.group(1) or m.group(3) or m.group(4) or m.group(5) or m.group(6)
        if not js or not m.group(3):
            return name
        chain, indent = [name], len(lines[j]) - len(lines[j].lstrip())
        for k in range(j - 1, -1, -1):
            d = DESCRIBE.match(lines[k])
            if d and len(d.group(1)) < indent:
                chain.insert(0, d.group(3))
                indent = len(d.group(1))
                if indent == 0:
                    break
        return ' > '.join(chain)
    return None


def first_match(rx: re.Pattern, lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if rx.search(line) and not line.strip().startswith(('//', '#', '*', '/*')):
            return i + 1
    return None


def setup_files(root: Path, all_files: list[Path]) -> tuple[set[str], dict[str, str]]:
    """Setup files (named in a Vitest/Jest config or package.json, or by convention) and each config's pool."""
    setups, pools = set(), {}
    for p in all_files:
        rel = p.relative_to(root).as_posix()
        if SETUP_NAME.search(rel):
            setups.add(rel)
        if not (CONFIG_NAME.search(rel) or rel == 'package.json'):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for m in SETUP_LIST.finditer(text):
            for name in re.findall(r'[\'"]([^\'"]+)[\'"]', m.group(1)):
                name = name.replace('<rootDir>/', '')
                cand = (p.parent / name).resolve()
                try:
                    setups.add(cand.relative_to(root).as_posix())
                except ValueError:
                    pass
        if re.search(r'(^|/)vit(?:est|e)\.(?:config|workspace)\.', rel):
            pm = POOL.search(text)
            pools[rel] = pm.group(1) if pm else ''
    return setups, pools


def vitest_major(root: Path) -> int | None:
    try:
        pkg = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    for sec in ('devDependencies', 'dependencies'):
        v = (pkg.get(sec) or {}).get('vitest')
        if v:
            m = re.search(r'(\d+)', v)
            return int(m.group(1)) if m else None
    return None


def thread_pool(root: Path, pools: dict[str, str]) -> str | None:
    """'threads' when Vitest runs tests in worker threads: set in a config, or the default before Vitest 2."""
    for cfg, pool in pools.items():
        if pool in ('threads', 'vmThreads'):
            return f"{cfg} sets pool: '{pool}'"
    if pools and not any(pools.values()):
        major = vitest_major(root)
        if major is not None and major < 2:
            return f'Vitest {major}.x runs tests in worker threads by default'
    return None


def scan(root: Path, data: dict) -> list[dict]:
    test_rx = [re.compile(p) for p in data['test_file_patterns']]
    smells = [{**s, 'rx': re.compile(s['pattern']),
               'prev_rx': re.compile(s['skip_if_prev']) if s.get('skip_if_prev') else None,
               'next_rx': re.compile(s['skip_if_next']) if s.get('skip_if_next') else None,
               'mit_rx': re.compile(s['mitigated_by']) if s.get('mitigated_by') else None,
               'req_rx': re.compile(s['requires_in_file']) if s.get('requires_in_file') else None,
               'skip_rx': re.compile(s['skip_if_in_file']) if s.get('skip_if_in_file') else None}
              for s in data['smells']
              if s.get('scope') not in ('thread_setup', 'reload_target', 'module_let', *GO_SCOPES, *BODY_SCOPES)]
    module_let = [s for s in data['smells'] if s.get('scope') == 'module_let']
    body_smells = [s for s in data['smells'] if s.get('scope') in BODY_SCOPES]
    go_body = {s['scope']: s for s in data['smells'] if s.get('scope') in GO_SCOPES}
    project = [s for s in data['smells'] if s.get('scope') == 'thread_setup']
    reload_targets = [s for s in data['smells'] if s.get('scope') == 'reload_target']
    all_files = list(files(root))
    not_collected = set(collection_excluded(root, all_files, data))
    helpers = helper_programs(root, data, all_files)
    setups, pools = setup_files(root, all_files)
    texts: dict[str, list[str]] = {}

    def read(p: Path) -> list[str] | None:
        rel = p.relative_to(root).as_posix()
        if rel not in texts:
            try:
                texts[rel] = p.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                return None
        return texts[rel]

    # global mitigations: a setup file that stubs the risk for every test
    global_mit = {}
    for sm in smells:
        if not sm['mit_rx']:
            continue
        for rel in sorted(setups):
            lines = read(root / rel) if (root / rel).is_file() else None
            n = first_match(sm['mit_rx'], lines) if lines else None
            if n:
                global_mit[sm['id']] = f'{sm["mitigation"]} globally in {rel}:{n}'
                break

    out = []
    for p in all_files:
        lang = data['language_by_suffix'].get(p.suffix)
        if not lang:
            continue
        rel = p.relative_to(root).as_posix()
        is_test = any(rx.search(rel) for rx in test_rx) and rel not in not_collected and rel not in helpers
        lines = read(p)
        if lines is None:
            continue
        # Rust unit tests live in #[cfg(test)] modules of source files: those lines are test code
        test_lines = rust_test_lines(lines) if lang == 'rust' and not is_test else set()
        local_mit, has_req, has_skip, once = {}, {}, {}, set()
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(('#', '//', '*', '/*')) and not s.startswith(('#[', '#![')):
                continue
            line_test = is_test or i in test_lines
            for sm in smells:
                if lang not in sm['languages']:
                    continue
                if (sm.get('scope') == 'source') == line_test:
                    continue
                if sm.get('code_only'):
                    if not any(not in_string(line, mm.start(), lang) for mm in sm['rx'].finditer(line)):
                        continue          # only inside a string: a command for a child process, a message
                elif not sm['rx'].search(line):
                    continue
                if sm['skip_rx'] is not None:
                    if sm['id'] not in has_skip:
                        has_skip[sm['id']] = first_match(sm['skip_rx'], lines) is not None
                    if has_skip[sm['id']]:
                        continue
                if sm.get('skip_if_unique_name') and unique_name_only(lines, i, lang):
                    continue              # only makes a unique folder or file name
                if sm['req_rx'] is not None:
                    if sm['id'] not in has_req:
                        has_req[sm['id']] = first_match(sm['req_rx'], lines) is not None
                    if not has_req[sm['id']]:
                        continue
                if sm['prev_rx'] is not None:
                    prev = next((lines[k] for k in range(i - 1, -1, -1) if lines[k].strip()), '')
                    if sm['prev_rx'].search(prev):
                        continue
                if sm['next_rx'] is not None:
                    nxt = next((lines[k] for k in range(i + 1, len(lines)) if lines[k].strip()), '')
                    if sm['next_rx'].search(nxt):
                        continue
                if sm.get('skip_unless_compared') and fills_unused_field(lines, i, sm['rx']):
                    continue
                if sm['id'] == 'shared-env-go' and is_test and enclosing_test(lines, i, False) == 'TestMain':
                    continue          # TestMain sets the environment once for the whole test binary, before any test
                mitigation = None
                if sm['mit_rx'] is not None:
                    if sm['id'] not in local_mit:
                        n = first_match(sm['mit_rx'], lines)
                        local_mit[sm['id']] = f'{sm["mitigation"]} in this file (line {n})' if n else None
                    mitigation = global_mit.get(sm['id']) or local_mit[sm['id']]
                test = enclosing_test(lines, i, lang in JS_LANGS, lang) if line_test else None
                if sm.get('once_per_test'):
                    if (sm['id'], test) in once:
                        continue          # one lead per test: the first read is enough to point at it
                    once.add((sm['id'], test))
                if sm.get('once_per_file'):
                    if sm['id'] in once:
                        continue          # one lead per file: the file never cleans up, wherever it creates
                    once.add(sm['id'])
                out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': i + 1,
                            'excerpt': s[:160], 'cause': sm['cause'], 'message': sm['message'], 'fix': sm['fix'],
                            'test': test, 'in_test_file': line_test, 'mitigation': mitigation})
        if (is_test or test_lines) and lang in ('python', 'rust'):
            for sm in body_smells:
                if lang not in sm['languages']:
                    continue
                for k, name in body_hits(lines, lang, sm):
                    if is_test or k in test_lines:
                        out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': k + 1,
                                    'excerpt': lines[k].strip()[:160], 'cause': sm['cause'], 'message': sm['message'],
                                    'fix': sm['fix'], 'test': name, 'in_test_file': True, 'mitigation': None})
        if is_test and lang == 'go' and go_body:
            for scope, line, test, name in go_body_hits(lines):
                sm = go_body.get(scope)
                if sm:
                    out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': line + 1,
                                'excerpt': lines[line].strip()[:160], 'cause': sm['cause'],
                                'message': sm['message'] + (f' ({name})' if name else ''), 'fix': sm['fix'],
                                'test': test, 'in_test_file': True, 'mitigation': None})
        if is_test and lang in JS_LANGS:
            for sm in module_let:
                for name, line, test, mitigation in module_let_hits(lines, re.compile(sm['pattern'])):
                    out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': line + 1,
                                'excerpt': lines[line].strip()[:160], 'cause': sm['cause'],
                                'message': f"{sm['message']} ({name})", 'fix': sm['fix'], 'test': test,
                                'in_test_file': True, 'mitigation': mitigation})

    # project checks: process.env.TZ assigned in a setup file that runs in worker threads
    why = thread_pool(root, pools) if project else None
    if why:
        for sm in project:
            rx = re.compile(sm['pattern'])
            for rel in sorted(setups):
                lines = read(root / rel) if (root / rel).is_file() else None
                for i, line in enumerate(lines or []):
                    if rx.search(line) and not line.strip().startswith(('//', '*', '/*')):
                        out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': i + 1,
                                    'excerpt': line.strip()[:160], 'cause': sm['cause'],
                                    'message': f"{sm['message']} ({why})", 'fix': sm['fix'], 'test': None,
                                    'in_test_file': False, 'mitigation': None})

    # project checks: environment read at import in a module that a test reloads
    reloaded = {m.group(1).split('.')[-1] for h in out if h['smell'] == 'module-reload'
                for m in [RELOAD_ARG.search(h['excerpt'])] if m}
    if reload_targets and reloaded:
        for p in all_files:
            rel = p.relative_to(root).as_posix()
            if p.suffix != '.py' or p.stem not in reloaded or any(re.compile(x).search(rel) for x in data['test_file_patterns']):
                continue
            for sm in reload_targets:
                rx = re.compile(sm['pattern'])
                for i, line in enumerate(read(p) or []):
                    if rx.search(line):
                        out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': i + 1,
                                    'excerpt': line.strip()[:160], 'cause': sm['cause'],
                                    'message': f"{sm['message']} ({p.stem} is reloaded by a test)", 'fix': sm['fix'],
                                    'test': None, 'in_test_file': False, 'mitigation': None})
    return out


GO_SCOPES = ('go_parallel', 'go_goroutine')
BODY_SCOPES = ('count_over_duration', 'py_helper')
# presets of run_tests.py per language, and the CI gate runners detect_conventions.py names
PRESETS_BY_LANG = {'python': ['pytest'], 'rust': ['cargo', 'nextest'], 'go': ['go-test', 'gotestsum'],
                   'javascript': ['vitest', 'jest', 'node-test', 'playwright'],
                   'typescript': ['vitest', 'jest', 'node-test', 'playwright']}
GATE_LANG = {'cargo-llvm-cov': 'rust', 'cargo-tarpaulin': 'rust', 'cargo-nextest': 'rust', 'cargo-test': 'rust',
             'coverage-unittest': 'python', 'coverage-pytest': 'python', 'unittest': 'python', 'pytest': 'python',
             'go-test': 'go'}
STATIC_TEST = {'python': re.compile(r'^\s*(?:async\s+)?def\s+test\w*\s*\('),
               'go': re.compile(r'^func\s+Test\w*\s*\(\s*\w+\s+\*testing\.T\b'),
               'javascript': re.compile(r'^\s*(?:it|test)(?:\.\w+)*\s*\(\s*[\'"`]'),
               'typescript': re.compile(r'^\s*(?:it|test)(?:\.\w+)*\s*\(\s*[\'"`]')}


def suite_inventory(root: Path, data: dict | None = None) -> list[dict]:
    """One entry per language that has tests: {language, test_files, tests, count_source, presets, ci_gates}. The
    languages and test files come from detect_conventions.py (lines, test files, CI gates); Rust tests are counted
    from the Cargo model (cargo_project.py: #[test] functions in #[cfg(test)] modules and tests/*.rs, doc tests
    apart), other languages by a static count of test definitions in test files. A report covers each of these
    suites in metadata.suites, as run or not run with the reason, so a language half of the suite with no
    per-test runs lowers evidence completeness instead of disappearing."""
    data = data or json.loads(DATA.read_text(encoding='utf-8'))
    try:
        import detect_conventions
        res = detect_conventions.Detector(root, 50_000).run(None)
    except Exception as exc:                  # noqa: BLE001 - the inventory is a lead, never fatal
        res = {'languages': {}, 'ci_gates': [], 'error': repr(exc)}
    langs = res.get('languages') or {}
    gates = res.get('ci_gates') or []
    rust = None
    if (root / 'Cargo.toml').is_file():
        try:
            import cargo_project
            model = cargo_project.read_cargo(root)
            tests = [t for c in model.get('crates', []) for t in c.get('tests', [])]
            rust = {'tests': sum(t.get('kind') != 'doc' for t in tests), 'doc_tests': sum(t.get('kind') == 'doc' for t in tests),
                    'ignored': sum(bool(t.get('ignored')) for t in tests)}
        except Exception:                     # noqa: BLE001
            rust = None
    test_rx = [re.compile(p) for p in data['test_file_patterns']]
    all_files = list(files(root))
    helpers = helper_programs(root, data, all_files)
    counts: dict[str, int] = {}
    for p in all_files:
        lang = data['language_by_suffix'].get(p.suffix)
        rel = p.relative_to(root).as_posix()
        if lang not in STATIC_TEST or rel in helpers or not any(rx.search(rel) for rx in test_rx):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        counts[lang] = counts.get(lang, 0) + sum(bool(STATIC_TEST[lang].match(l)) for l in text.splitlines())
    out = []
    for lang, info in langs.items():
        tf = (info or {}).get('test_files', 0)
        if lang == 'rust' and rust is not None and (rust['tests'] or tf):
            entry = {'language': lang, 'test_files': tf, 'tests': rust['tests'], 'doc_tests': rust['doc_tests'],
                     'ignored': rust['ignored'], 'count_source': 'cargo_project.py (#[test] fns; doc tests apart)'}
        elif tf:
            entry = {'language': lang, 'test_files': tf, 'tests': counts.get(lang),
                     'count_source': 'static count of test definitions' if lang in STATIC_TEST else None}
        else:
            continue
        entry['presets'] = PRESETS_BY_LANG.get(lang, ['custom'])
        entry['ci_gates'] = [f"{g.get('file')}:{g.get('line')} {g.get('command')}" for g in gates
                             if GATE_LANG.get(g.get('runner')) == lang]
        out.append(entry)
    return out
GO_TEST_FUNC = re.compile(r'^func\s+(Test\w*|Benchmark\w*|Fuzz\w*|Example\w*)\s*\(')
GO_WAITS = re.compile(r'\.Wait\(\)|<-|\berrgroup\b|\bEventually\w*\(|\bsynctest\.(?:Run|Test|Wait)\(')
GO_GOROUTINE = re.compile(r'^\s*go\s+(?:func\b|[\w.]+\()')


def go_package_vars(lines: list[str]) -> set[str]:
    """Names declared by package-level var statements (`var x int`, `var x, y = ...` and `var ( ... )` blocks)."""
    names, block = set(), False
    for line in lines:
        if block:
            if line.startswith(')'):
                block = False
                continue
            m = re.match(r'^\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\b', line)
            if m and not line.strip().startswith('//'):
                names.update(x.strip() for x in m.group(1).split(','))
            continue
        if re.match(r'^var\s*\(\s*$', line):
            block = True
            continue
        m = re.match(r'^var\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\b', line)
        if m:
            names.update(x.strip() for x in m.group(1).split(','))
    return names - {'_'}


def go_body_hits(lines: list[str]) -> list[tuple[str, int, str, str | None]]:
    """Go test-body checks, as (scope, line index, test name, detail): a package-level variable written in a test
    that calls t.Parallel() (go_parallel, one hit per variable and test), and a goroutine started in a test whose
    body never waits for anything (go_goroutine, at the first `go` statement). A test body runs from its
    `func TestX(` line to the next line that is a closing brace at column 0 (gofmt layout)."""
    pkg_vars = go_package_vars(lines)
    hits = []
    i = 0
    while i < len(lines):
        m = GO_TEST_FUNC.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = i, i + 1
        while end < len(lines) and not lines[end].startswith('}'):
            end += 1
        body = [(k, lines[k]) for k in range(start + 1, end) if not lines[k].strip().startswith('//')]
        name = m.group(1)
        if any('.Parallel()' in l for _, l in body):
            for var in sorted(pkg_vars):
                write = re.compile(r'(?<![\w.])' + re.escape(var) +
                                   r'(?:\[[^\]]*\])?\s*(?:=(?!=)|\+\+|--|[-+*/%|&^]=|<<=|>>=)')
                k = next((k for k, l in body if write.search(l)), None)
                if k is not None:
                    hits.append(('go_parallel', k, name, var))
        if not any(GO_WAITS.search(l) for _, l in body):
            k = next((k for k, l in body if GO_GOROUTINE.match(l)), None)
            if k is not None:
                hits.append(('go_goroutine', k, name, None))
        i = end + 1
    return sorted(hits, key=lambda h: h[1])


HOOK = re.compile(r'^\s*(beforeEach|afterEach|beforeAll|afterAll)\s*\(')


def enclosing_block(lines: list[str], i: int) -> tuple[str, int] | None:
    """The innermost test or hook callback around line i, by indentation: ('test', j) or (hook name, j)."""
    indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i - 1, -1, -1):
        s = lines[j]
        if not s.strip():
            continue
        ind = len(s) - len(s.lstrip())
        if ind >= indent:
            continue
        indent = ind
        h = HOOK.match(s)
        if h:
            return h.group(1), j
        if TEST_NAME.match(s) and TEST_NAME.match(s).group(3):
            return 'test', j
        if ind == 0:
            return None
    return None


def module_let_hits(lines: list[str], decl: re.Pattern) -> list[tuple[str, int, str | None, str | None]]:
    """A module-level `let`/`var` (no indentation) reassigned inside a test body: the next test in the file sees the
    value it left. One hit per variable and test, at the first reassignment; a reset in beforeEach/afterEach is
    noted as the mitigation."""
    hits = []
    for d, line in enumerate(lines):
        m = decl.match(line)
        if not m:
            continue
        name = m.group(1)
        assign = re.compile(r'(?<![\w$.])' + re.escape(name) + r'\s*(?:=(?![=>])|\+\+|--|(?:[-+*/%|&^]|\?\?|\|\||&&)=)')
        shadow = re.compile(r'\b(?:let|const|var)\s+' + re.escape(name) + r'\b')
        in_tests, reset = {}, None
        for i, l in enumerate(lines):
            if i == d or not l[:1].isspace() or l.strip().startswith(('//', '*', '/*')):
                continue
            if not assign.search(l) or shadow.search(l):
                continue
            where = enclosing_block(lines, i)
            if where is None:
                continue
            kind, j = where
            if kind in ('beforeEach', 'afterEach'):
                reset = reset or f'{name} is reset in {kind} (line {i + 1})'
            elif kind == 'test':
                in_tests.setdefault(j, i)
        for j, i in in_tests.items():
            hits.append((name, i, enclosing_test(lines, i, True), reset))
    return sorted(hits, key=lambda h: h[1])


RELOAD_ARG = re.compile(r'reload\(\s*([A-Za-z_][\w.]*)\s*\)')
# created_at=dt.datetime.now(...) as a keyword argument, or "created_at": datetime.now() in a dict literal
KWARG_BEFORE = re.compile(r'(?:(?:^|[(,])\s*([A-Za-z_]\w*)=(?!=)|(?:^|[{,])\s*[\'"]([A-Za-z_]\w*)[\'"]\s*:)\s*[\w.]*$')
# what may follow the clock read: the end of the argument, optionally shifted by a timedelta
KWARG_AFTER = re.compile(r'\s*(?:[-+]\s*[\w.]*timedelta\([^()]*\)\s*)?(?:,|\)|\}|$)')


def fills_unused_field(lines: list[str], i: int, rx: re.Pattern) -> bool:
    """The clock read only fills a keyword argument (created_at=datetime.now() in a fixture or constructor call),
    and no line of the file compares or asserts on that name: a timestamp nobody compares can't make a test flaky."""
    line = lines[i]
    m = rx.search(line)
    kw = KWARG_BEFORE.search(line[:m.start()])
    if not kw or not KWARG_AFTER.match(line[m.end():]):
        return False   # not a keyword argument or dict value, or the value is used further on the same line
    name = re.escape(kw.group(1) or kw.group(2))
    named = re.compile(r'(?:\.|\[[\'"])' + name + r'\b|\b' + name + r'\b')
    compared = re.compile(r'\bassert\b|[<>]=?|==|!=|\bsorted\(|\bmin\(|\bmax\(|timedelta|\.days\b|total_seconds')
    return not any(j != i and named.search(l) and compared.search(l) for j, l in enumerate(lines))


def coverage_warning(root: Path, data: dict) -> str | None:
    """A loud warning when the repo's dominant language has no smell pack, so scanning it found
    ~nothing of the primary language. Returns None when the dominant language is supported (so it
    never fires on a Python, JS/TS, Java/Kotlin, Go or Rust repo) or no source files were seen.
    This stops an unsupported-language skip from reading as a clean '0 smells' result."""
    supported = set(data['language_by_suffix'].values())
    counts: dict[str, int] = {}
    for p in files(root):
        lang = LANG_BY_SUFFIX_BROAD.get(p.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    dominant = max(counts, key=lambda k: (counts[k], k))
    if dominant in supported:
        return None
    return (f"WARNING: '{dominant}' is this repo's most common language ({counts[dominant]} file(s)) "
            f"but has no smell pack, so 0 of its files were scanned. This is NOT a clean result: "
            f"the {dominant} tests were skipped, not found flake-free. "
            f"Supported languages: {', '.join(sorted(supported))}.")


RUST_SAMPLE_TEST = """\
use std::thread;
use std::time::{Duration, SystemTime};

#[test]
fn test_waits() {
    thread::sleep(Duration::from_millis(100));
    let _ = SystemTime::now();
    let mut rng = rand::rng();
    let _ = rng.random_range(0..10);
    let _ = std::net::TcpListener::bind("127.0.0.1:8080");
    let _ = "/tmp/fixed-file";
    std::env::set_var("FOO", "bar");
}
"""
RUST_SAMPLE_SRC = "pub static mut COUNTER: u32 = 0;\n"


JS_LET_SAMPLE = """\
import { describe, it, expect } from 'vitest'

let loadError: Error | null = null
const reader = () => loadError

describe('decoder', () => {
  it('decodes a small image', () => {
    expect(reader()).toBe(null)
  })

  it('reports a load failure', () => {
    loadError = new Error('missing codec')
    expect(reader()).not.toBe(null)
  })
})
"""
JS_LET_RESET_SAMPLE = """\
import { beforeEach, it, expect } from 'vitest'

let visits = 0

beforeEach(() => {
  visits = 0
})

it('counts a visit', () => {
  visits += 1
  expect(visits).toBe(1)
})
"""


GO_SAMPLE_TEST = """\
package cart

import (
\t"math/rand"
\t"net"
\t"net/http"
\t"os"
\t"sync"
\t"testing"
\t"time"
)

var hits int

func TestWaits(t *testing.T) {
\ttime.Sleep(50 * time.Millisecond)
\tstart := time.Now()
\t_ = start
\tn := rand.Intn(10)
\t_ = n
\tln, _ := net.Listen("tcp", "127.0.0.1:8080")
\t_ = ln
\tos.Setenv("CART_MODE", "test")
\t_ = os.TempDir()
}

func TestParallelShared(t *testing.T) {
\tt.Parallel()
\thits++
}

func TestGoroutine(t *testing.T) {
\tgo func() {
\t\thits = 2
\t}()
}

func TestGoroutineSynced(t *testing.T) {
\tvar wg sync.WaitGroup
\twg.Add(1)
\tgo func() {
\t\tdefer wg.Done()
\t}()
\twg.Wait()
}

func TestMain(m *testing.M) {
\tos.Setenv("CART_TELEMETRY", "off")
\tos.Exit(m.Run())
}

func TestStub(t *testing.T) {
\tsrv := startOn(2000)
\tcases := []struct{ port int }{{port: 2000}}
\t_ = http.ListenAndServe
\t_, _ = srv, cases
}
"""


PY_SAMPLES = {
    'pytest.ini': '[pytest]\naddopts = -v\ntestpaths = tests\npython_files = test_*.py\n',
    'tests/test_links.py': (
        'import requests\n\n\nclass TestLinks:\n    def setup_method(self):\n'
        '        self.session = requests.Session()\n\n    def test_head(self):\n'
        '        r = self.session.head(self.url, timeout=5)\n        assert r.status_code < 400\n'),
    'tests/test_db.py': (
        'from app import db, User\n\n\ndef test_lookup():\n    user = db.session.get(User, 1)\n'
        '    db.session.delete(user)\n    assert user\n'),
    'tests/test_speed.py': (
        'import time\n\n\ndef test_page_is_fast(client):\n    start = time.time()\n    client.get("/")\n'
        '    load_time = time.time() - start\n    assert load_time < 2.0\n\n\n'
        'def test_budget_unittest(self):\n    elapsed = time.perf_counter() - self.t0\n'
        '    self.assertLess(elapsed, 0.5)\n\n\ndef test_count():\n    assert len(items) < 3\n'),
    'tests/test_mtime.py': (
        'import os\nfrom datetime import datetime\n\n\ndef test_modified_today(tmp_path):\n'
        '    p = tmp_path / "a.txt"\n    p.write_text("x")\n'
        '    modified = datetime.fromtimestamp(os.path.getmtime(p))\n'
        '    assert modified.date() == datetime.now().date()\n\n\n'
        'def test_formatted_mtime(tmp_path):\n    p = tmp_path / "b.txt"\n    p.write_text("x")\n'
        '    assert last_modified_day(p) == datetime.now().strftime("%Y-%m-%d")\n\n\n'
        'def test_last_modified_formats(tmp_path):\n    p = tmp_path / "c.txt"\n    p.write_text("x")\n'
        '    day = last_modified_day(p)\n    stamp = last_modified_stamp(p)\n'
        '    assert stamp.startswith(day)\n'),
    'perf_test.py': 'import requests\n\n\ndef test_live():\n    requests.get(URL)\n',
    'tools/load_test.py': 'import time\n\n\ndef test_soak():\n    time.sleep(1)\n',
}


# A synthetic tide-table workspace: a clap CLI crate with integration tests in tests/*.rs, a helper binary under
# tests/fixtures that the tests launch, and unit tests in a #[cfg(test)] module of the library.
RUST_TIDE = {
    'Cargo.toml': '[package]\nname = "tidectl"\nversion = "0.1.0"\nedition = "2024"\n',
    'src/lib.rs': (
        'use std::time::Duration;\n\n'
        'pub fn settle() {\n    std::thread::sleep(Duration::from_millis(5));\n}\n\n'
        'pub fn height(hour: u32) -> f64 {\n    f64::from(hour) * 0.5\n}\n\n'
        '#[cfg(test)]\nmod tests {\n    use super::*;\n\n'
        '    fn helper_mode() -> String {\n        std::env::var("TIDE_MODE").unwrap_or_default()\n    }\n\n'
        '    #[test]\n    fn reads_mode_from_env() {\n        std::env::set_var("TIDE_MODE", "spring");\n'
        '        assert_eq!(helper_mode(), "spring");\n    }\n\n'
        '    #[test]\n    fn height_is_linear() {\n        assert_eq!(height(2), 1.0);\n    }\n}\n'),
    'tests/cli.rs': (
        'use std::process::Command;\nuse std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};\n\n'
        'fn scratch_dir(tag: &str) -> std::path::PathBuf {\n'
        '    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();\n'
        '    let dir = std::env::temp_dir().join(format!("tide-{tag}-{nanos}"));\n'
        '    std::fs::create_dir_all(&dir).unwrap();\n    dir\n}\n\n'
        '#[test]\nfn logs_samples_for_a_duration() {\n    let dir = scratch_dir("log");\n'
        '    let out = Command::new(env!("CARGO_BIN_EXE_tidectl"))\n'
        '        .args(["log", "--duration", "0.7", "--interval-ms", "100"])\n'
        '        .current_dir(&dir)\n        .output()\n        .unwrap();\n'
        '    let samples = String::from_utf8_lossy(&out.stdout).lines().count();\n'
        '    assert!(samples >= 2, "only {samples} samples");\n}\n\n'
        '#[test]\nfn reports_samples_in_json() {\n    let dir = std::env::temp_dir().join(format!(\n'
        '        "tide-report-{}-{}",\n        std::process::id(),\n        SystemTime::now()\n'
        '            .duration_since(UNIX_EPOCH)\n            .unwrap()\n            .as_nanos()\n    ));\n'
        '    std::fs::create_dir(&dir).unwrap();\n'
        '    let out = Command::new(env!("CARGO_BIN_EXE_tidectl"))\n'
        '        .args(["log", "--json", "--duration", "0.5"])\n        .output()\n        .unwrap();\n'
        '    let report: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();\n'
        '    assert!(report["samples"].as_array().unwrap().len() >= 2);\n}\n\n'
        '#[test]\nfn answers_quickly() {\n    let start = Instant::now();\n'
        '    let _ = Command::new(env!("CARGO_BIN_EXE_tidectl")).arg("--help").output();\n'
        '    assert!(start.elapsed() < Duration::from_millis(500));\n}\n\n'
        '#[test]\nfn reads_today() {\n    let today = SystemTime::now();\n'
        '    assert!(today > UNIX_EPOCH);\n}\n'),
    'tests/fixtures/slow_station.rs': (
        'use std::{thread, time::Duration};\n\nfn main() {\n'
        '    thread::sleep(Duration::from_millis(200));\n    println!("station ready");\n}\n'),
}
# A unittest-style Python suite (as CI runs it with python -m unittest; pytest collects it too).
PY_LOGGER = {
    'tests/test_logger.py': (
        'import subprocess\nimport sys\nimport tempfile\nimport unittest\nimport uuid\nfrom pathlib import Path\n\n\n'
        'def test_directory(name):\n    return Path(tempfile.gettempdir()) / f"{name}-{uuid.uuid4().hex}"\n\n\n'
        'class LoggerTest(unittest.TestCase):\n'
        '    def test_samples_over_duration(self):\n'
        '        out = subprocess.run([sys.executable, "logger.py", "--duration", "3", "--interval-ms", "100"],\n'
        '                             capture_output=True, text=True).stdout\n'
        '        samples = out.splitlines()\n        self.assertGreaterEqual(len(samples), 2)\n\n'
        '    def test_child_waits(self):\n'
        '        subprocess.run([sys.executable, "-c", "import time; time.sleep(0.2)"], check=True)\n\n'
        '    def test_scratch(self):\n        scratch_dir = test_directory("scratch")\n'
        '        self.assertTrue(str(scratch_dir))\n\n'
        '    def test_report_folder(self):\n        scratch = Path(tempfile.gettempdir())\n'
        '        tmp = scratch / ("report-" + uuid.uuid4().hex)\n        tmp.mkdir()\n'
        '        self.assertTrue(tmp.is_dir())\n\n'
        '    def test_ids_sort(self):\n        ids = sorted(uuid.uuid4() for _ in range(3))\n'
        '        self.assertEqual(len(ids), 3)\n'),
    'tests/fixtures/fake_station.py': (
        'import time\n\n\ndef main():\n    time.sleep(1)\n    print("ready")\n\n\n'
        'if __name__ == "__main__":\n    main()\n'),
}


def self_test() -> int:
    data = json.loads(DATA.read_text(encoding='utf-8'))
    ok = True
    lines = []

    # 6) Rust in a realistic layout: unique-name clock reads, helper binaries under tests/fixtures, unit tests in a
    #    #[cfg(test)] module, a sample count over a fixed duration, an elapsed-time budget, a scratch folder never
    #    removed.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, text in RUST_TIDE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        hits = scan(root, data)
        got = {(h['smell'], h['file'], h['test']) for h in hits}
        want = {('shared-env-rust', 'src/lib.rs', 'reads_mode_from_env'),
                ('wall-time-count-assertion', 'tests/cli.rs', 'logs_samples_for_a_duration'),
                ('wall-time-count-assertion', 'tests/cli.rs', 'reports_samples_in_json'),
                ('wall-time-assertion-rust', 'tests/cli.rs', 'answers_quickly'),
                ('wall-clock-rust', 'tests/cli.rs', 'answers_quickly'),
                ('wall-clock-rust', 'tests/cli.rs', 'reads_today'),
                ('scratch-dir-leak-rust', 'tests/cli.rs', None)}
        unwanted = {h for h in got if h[1] == 'tests/fixtures/slow_station.rs'
                    or (h[1] == 'src/lib.rs' and h[0] == 'fixed-sleep-rust')            # product code, not a test
                    or (h[0] == 'wall-clock-rust' and h not in want)                    # unique folder name
                    or h[0] == 'shared-temp-path-rust'}                                  # temp_dir + unique name
        leaks = [h for h in hits if h['smell'] == 'scratch-dir-leak-rust']
        if len(leaks) != 1:                                                     # one lead per file is enough
            unwanted.add(('scratch-dir-leak-rust x' + str(len(leaks)), 'tests/cli.rs', None))
        inv = suite_inventory(root)
        rust = next((s for s in inv if s['language'] == 'rust'), None)
        good = want <= got and not unwanted and rust is not None and rust['tests'] == 6 \
            and 'tests/fixtures/slow_station.rs' in helper_programs(root, data)
        lines.append('rust layout (unique names, fixtures, cfg(test), counts over a duration, inventory): ' + (
            'ok' if good else f'FAILED, missing {sorted(want - got, key=str)}, unwanted {sorted(unwanted, key=str)}, '
                              f'rust suite {rust}'))
        ok = ok and good

    # 7) Python: uuid4 that only names a folder, a sleep inside a child's -c string, a helper binary under
    #    tests/fixtures, a sample count over a fixed duration, and a module-level test_* helper pytest would collect.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, text in PY_LOGGER.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        hits = scan(root, data)
        got = {(h['smell'], h['file'], h['test']) for h in hits}
        want = {('wall-time-count-assertion', 'tests/test_logger.py', 'test_samples_over_duration'),
                ('unseeded-random', 'tests/test_logger.py', 'test_ids_sort'),
                ('pytest-collects-helper', 'tests/test_logger.py', 'test_directory')}
        unwanted = {h for h in got if h[1] == 'tests/fixtures/fake_station.py'
                    or (h[0] == 'fixed-sleep' and h[2] == 'test_child_waits')
                    or (h[0] == 'unseeded-random' and h[2] != 'test_ids_sort')}
        good = want <= got and not unwanted
        lines.append('python unique names, child strings, fixtures, counts over a duration, collected helpers: ' + (
            'ok' if good else f'FAILED, missing {sorted(want - got, key=str)}, unwanted {sorted(unwanted, key=str)}'))
        ok = ok and good

    # 1) The Rust pack fires on a sample Rust test and source file.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / 'tests').mkdir()
        (root / 'src').mkdir()
        (root / 'tests' / 'test_sample.rs').write_text(RUST_SAMPLE_TEST, encoding='utf-8')
        (root / 'src' / 'lib.rs').write_text(RUST_SAMPLE_SRC, encoding='utf-8')
        hits = scan(root, data)
        found = {h['smell'] for h in hits}
        expect = {'fixed-sleep-rust', 'wall-clock-rust', 'unseeded-random-rust', 'fixed-port-rust',
                  'shared-temp-path-rust', 'shared-env-rust', 'static-mut-rust'}
        missing = expect - found
        named = any(h.get('test') == 'test_waits' for h in hits if h['in_test_file'])
        no_warn_rust = coverage_warning(root, data) is None
        if missing:
            ok = False
        lines.append(f"rust pack: {'ok' if not missing else 'FAILED, missing ' + str(sorted(missing))} "
                     f"({len(found)} smell id(s) hit); enclosing test named: {named}; "
                     f"warning silent on rust repo: {no_warn_rust}")
        ok = ok and named and no_warn_rust

    # 2) The coverage warning fires for an unsupported dominant language, not for a supported one.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(3):
            (root / f'app{i}.rb').write_text('x = rand\n', encoding='utf-8')
        (root / 'helper.py').write_text('x = 1\n', encoding='utf-8')
        w = coverage_warning(root, data)
        fires = w is not None and 'ruby' in w
        lines.append(f"warning on unsupported dominant (ruby): {'ok' if fires else 'FAILED'}")
        ok = ok and fires
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(3):
            (root / f'mod{i}.py').write_text('x = 1\n', encoding='utf-8')
        silent = coverage_warning(root, data) is None
        lines.append(f"warning silent on python repo: {'ok' if silent else 'FAILED'}")
        ok = ok and silent

    # 3) A module-level let reassigned in a test is a smell; one reset in beforeEach is its mitigation.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / 'tests').mkdir()
        (root / 'tests' / 'loader.test.ts').write_text(JS_LET_SAMPLE, encoding='utf-8')
        (root / 'tests' / 'reset.test.ts').write_text(JS_LET_RESET_SAMPLE, encoding='utf-8')
        hits = [h for h in scan(root, data) if h['smell'] == 'module-let-reassigned']
        got = {(h['file'], h['test'], bool(h['mitigation'])) for h in hits}
        want = {('tests/loader.test.ts', 'decoder > reports a load failure', False),
                ('tests/reset.test.ts', 'counts a visit', True)}
        good = got == want
        lines.append(f"module-level let reassigned in a test: {'ok' if good else 'FAILED, got ' + str(sorted(got))}")
        ok = ok and good

    # 4) The Go pack: each smell once in the test that has it, and no fixed-port hit on a stub or a table field.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / 'go.mod').write_text('module example.test/harbour\n\ngo 1.26\n', encoding='utf-8')
        (root / 'cart').mkdir()
        (root / 'cart' / 'cart_test.go').write_text(GO_SAMPLE_TEST, encoding='utf-8')
        hits = [h for h in scan(root, data) if h['file'].endswith('.go')]
        got = {(h['smell'], h['test']) for h in hits}
        want = {('fixed-sleep-go', 'TestWaits'), ('wall-clock-go', 'TestWaits'), ('unseeded-random-go', 'TestWaits'),
                ('fixed-port-go', 'TestWaits'), ('shared-env-go', 'TestWaits'), ('shared-temp-path-go', 'TestWaits'),
                ('parallel-shared-state-go', 'TestParallelShared'), ('goroutine-unsynced-go', 'TestGoroutine')}
        good = got == want and len(hits) == len(want)
        lines.append(f"go pack: {'ok' if good else 'FAILED, missing ' + str(sorted(want - got)) + ', extra ' + str(sorted(got - want))}")
        ok = ok and good

    # 5) Python: a requests.Session verb is a network call (an ORM session is not), a wall-time budget assertion,
    #    a file mtime compared with the wall clock, and pytest's own collection config decides what is a test file.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, text in PY_SAMPLES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        hits = scan(root, data)
        got = {(h['smell'], h['file'], h['test']) for h in hits}
        want = {('real-network-session', 'tests/test_links.py', 'test_head'),
                ('wall-time-assertion', 'tests/test_speed.py', 'test_page_is_fast'),
                ('wall-time-assertion', 'tests/test_speed.py', 'test_budget_unittest'),
                ('mtime-vs-wall-clock', 'tests/test_mtime.py', 'test_modified_today'),
                ('mtime-vs-wall-clock', 'tests/test_mtime.py', 'test_formatted_mtime'),
                ('mtime-vs-wall-clock', 'tests/test_mtime.py', 'test_last_modified_formats')}
        unwanted = {h for h in got if h[1] in ('tests/test_db.py', 'perf_test.py', 'tools/load_test.py')
                    or h[0] in ('real-network-session', 'wall-time-assertion', 'mtime-vs-wall-clock') and h not in want}
        # one lead per test, at a read (never the def line that names the test after the helper)
        per_test = [h for h in hits if h['smell'] == 'mtime-vs-wall-clock' and h['test'] == 'test_last_modified_formats']
        unwanted |= {('mtime-vs-wall-clock x' + str(len(per_test)), h['file'], h['excerpt']) for h in per_test
                     if len(per_test) != 1 or h['excerpt'].startswith('def ')}
        excluded = collection_excluded(root)
        good = want <= got and not unwanted and excluded == ['perf_test.py', 'tools/load_test.py']
        lines.append('python session/wall-time/mtime smells and pytest collection: ' + (
            'ok' if good else f'FAILED, missing {sorted(want - got)}, unwanted {sorted(unwanted)}, excluded {excluded}'))
        ok = ok and good

    print('self-test ' + ('passed' if ok else 'FAILED') + ':\n  ' + '\n  '.join(lines),
          file=sys.stderr)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path, nargs='?')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--self-test', action='store_true',
                    help='run the built-in Rust-pack and coverage-warning checks and exit')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.repo is None:
        ap.error('repo is required')
    data = json.loads(DATA.read_text(encoding='utf-8'))
    root = args.repo.resolve()
    hits = scan(root, data)
    result = {'pitfalls_version': data['version'], 'smells': hits}
    cfg = pytest_collection(root)
    if cfg:
        result['pytest_collection'] = {**cfg, 'not_collected': collection_excluded(root, None, data)}
    helpers = sorted(helper_programs(root, data))
    if helpers:
        result['helper_programs'] = helpers
    result['suites'] = suite_inventory(root, data)
    text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    by: dict[str, int] = {}
    for h in hits:
        by[h['smell']] = by.get(h['smell'], 0) + 1
    mitigated = sum(1 for h in hits if h['mitigation'])
    warning = coverage_warning(root, data)
    if warning:
        bar = '!' * 72
        print(f"\n{bar}\n{warning}\n{bar}", file=sys.stderr)
    print(f"{len(hits)} smell(s): " + (', '.join(f'{v} {k}' for k, v in sorted(by.items())) or 'none')
          + (f'; {mitigated} with a mitigation noted' if mitigated else ''), file=sys.stderr)
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
