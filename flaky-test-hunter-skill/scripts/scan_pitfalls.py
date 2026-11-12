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
        '.next', '.nuxt', 'out', 'coverage', '.turbo', '.cache', '.stryker-tmp', '.mypy_cache', '.gradle', '.idea'}
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


def enclosing_test(lines: list[str], i: int, js: bool) -> str | None:
    """The test the line sits in. For JS/TS, the describe chain and title joined with ' > '."""
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
               'mit_rx': re.compile(s['mitigated_by']) if s.get('mitigated_by') else None}
              for s in data['smells'] if s.get('scope') not in ('thread_setup', 'reload_target')]
    project = [s for s in data['smells'] if s.get('scope') == 'thread_setup']
    reload_targets = [s for s in data['smells'] if s.get('scope') == 'reload_target']
    all_files = list(files(root))
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
        is_test = any(rx.search(rel) for rx in test_rx)
        lines = read(p)
        if lines is None:
            continue
        local_mit = {}
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(('#', '//', '*', '/*')):
                continue
            for sm in smells:
                if lang not in sm['languages']:
                    continue
                if (sm.get('scope') == 'source') == is_test:
                    continue
                if not sm['rx'].search(line):
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
                mitigation = None
                if sm['mit_rx'] is not None:
                    if sm['id'] not in local_mit:
                        n = first_match(sm['mit_rx'], lines)
                        local_mit[sm['id']] = f'{sm["mitigation"]} in this file (line {n})' if n else None
                    mitigation = global_mit.get(sm['id']) or local_mit[sm['id']]
                out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': i + 1,
                            'excerpt': s[:160], 'cause': sm['cause'], 'message': sm['message'], 'fix': sm['fix'],
                            'test': enclosing_test(lines, i, lang in JS_LANGS) if is_test else None,
                            'in_test_file': is_test, 'mitigation': mitigation})

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


def self_test() -> int:
    data = json.loads(DATA.read_text(encoding='utf-8'))
    ok = True
    lines = []

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
