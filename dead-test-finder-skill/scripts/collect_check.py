#!/usr/bin/env python3
"""Compare the tests defined in the code with the tests each framework actually collects (stdlib only).

    python collect_check.py <repo> --candidates candidates.json --run-dir run [--cwd client=/path/with/node_modules]
        [--env DATABASE_URL=postgresql://fake:fake@localhost:5432/fake ...] [--package-env api:PYTHONPATH=/x ...]
        [--scope all|candidates] [--files F ...] [--reuse] [--split-on-error [MAX_RUNS]]

For every package and framework with tests, runs the framework's own list command and keeps its
raw output in the run directory:

    pytest      python -m pytest --collect-only -q -p no:cacheprovider
    vitest      npx vitest list --json=<file in the run directory> [files]
    jest        npx jest --listTests --json            (file level only)
    playwright  npx playwright test --list --reporter=json [files]

Vitest's --json takes an optional value, so `--json <file>` would write the listing over the
first test file; the output path is always joined with '='. Every list command runs inside a
guard (tree_guard.py) that keeps the bytes of the package's test files and compares them (and
`git status` of the directory) afterwards: when a command changed any, they are put back and the
script stops with exit code 3, recording nothing.

node:test (no list command), a repository's own test runner (definitions with framework
`custom`, see scan_tests.py --test-files) and a package whose package.json names no runner are
recorded as `no_list_command`, and their tests are not_assessed with that reason, never
not_collected. --split-on-error lists a package whose whole list failed again in halves, so one
file the runner can't list (a static check of options it doesn't know) leaves the other files
listed (status `partial`, `errored_files`); tests in the failing files are not_assessed. A test
skipped conditionally (skipIf/runIf) that is missing from the list is not_assessed too: the
runner may leave it out on this machine only.

A JavaScript test file that imports @playwright/test, or sits in a Playwright testDir, is listed
with Playwright, never with Vitest or Jest (which exclude e2e/ and would report it as not
collected). --cwd PACKAGE=DIR runs a package's commands in another directory holding the same
files (a copy with node_modules installed). --env K=V [K=V ...] sets environment variables the setup
files read at collection time (a fake DATABASE_URL, never a real one) for every package, and
--package-env PACKAGE:K=V for one package only (two services that each need their own PYTHONPATH).
Only the variable names are recorded, never the values.

pytest names tests relative to its rootdir, the directory of the nearest pytest.ini /
pyproject.toml / setup.cfg / tox.ini above the arguments, which can be an ancestor of the package
(`services/pytest.ini` for `services/api`). Every listed id is mapped back to a path relative to
the package by finding its file, so ids are never prefixed twice. A pytest package that contains
another one (`services/api` inside `services`) is listed with `--ignore` for the inner one, so no
test is collected twice.

--scope candidates lists only the files that hold a candidate, and --files only the files given
(repo-relative): a JavaScript list command imports every file it lists, so on a large suite it
costs as much as a run. Tests in files outside the scope are not_assessed, never not_collected.
--reuse keeps a package's earlier list when the command and the listed files' contents are
unchanged (an input hash recorded in collection.json).

Every definition from scan_tests.py that its framework's list doesn't include is `not_collected`,
which is what confirms a never_runs finding. A parametrised or computed name (it.each, a template
literal, describe.each) is matched by its name pattern. A list command that fails is recorded as
errored, and its tests stay not_assessed.
"""
from __future__ import annotations
import argparse, contextlib, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests, tree_guard  # noqa: E402


NO_LIST = {
    'node-test': 'node:test has no list command that names tests without running them',
    'custom': "the tests use the repository's own test runner (a custom test DSL); there is no list command for it here",
    'unknown': 'no test runner found in package.json (dependencies or scripts) for this package',
}


def framework_of(d: dict, pkg_dir: Path, root: Path | None = None) -> str:
    if d['lang'] == 'python':
        return 'pytest'
    if d['lang'] == 'go':
        return 'go-test'
    if d.get('framework') in ('playwright', 'node-test', 'custom'):
        return d['framework']
    if d.get('framework') in ('vitest', 'jest'):
        return d['framework']
    return js_runner(pkg_dir, root)


def js_runner(pkg_dir: Path, root: Path | None = None) -> str:
    """vitest, jest or node-test from the package's dependencies, then its scripts; a package without one of its own
    inherits the nearest workspace root's (a pnpm or npm workspace keeps Vitest at the root). 'unknown' when a
    package.json exists but names no runner anywhere; vitest when there is no package.json at all."""
    seen_manifest = False
    d = pkg_dir
    while True:
        try:
            pj = json.loads((d / 'package.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pj = None
        if pj is not None:
            seen_manifest = True
            deps = {**pj.get('dependencies', {}), **pj.get('devDependencies', {})}
            scripts = ' '.join(str(v) for v in (pj.get('scripts') or {}).values())
            if 'vitest' in deps:
                return 'vitest'
            if 'jest' in deps:
                return 'jest'
            if re.search(r'(?<![\w-])vitest\b', scripts):
                return 'vitest'
            if re.search(r'(?<![\w-])jest\b', scripts):
                return 'jest'
            if re.search(r'\bnode\b[^&|;]*\s--test\b', scripts):
                return 'node-test'
        if root is None or d.resolve() == root.resolve() or d.parent == d:
            break
        d = d.parent
    return 'unknown' if seen_manifest else 'vitest'


def norm_name(test_id: str) -> str:
    """A test id with backslash-escaped quotes and backslashes undone: Vitest's JSON list escapes a double quote in a
    test name a second time (`a target=\\"_blank\\" link`), so a name with quotes never matched its definition."""
    return re.sub(r'\\(["\'`\\])', r'\1', test_id)


def parse_env(pairs: list, per_package: list[str]) -> tuple[dict, dict[str, dict]]:
    """(--env pairs, {package: pairs} from --package-env PACKAGE:K=V). --env may hold several pairs per flag."""
    flat = [x for item in pairs for x in (item if isinstance(item, list) else [item])]
    bad = [x for x in flat if '=' not in x] + [x for x in per_package if ':' not in x or '=' not in x.split(':', 1)[1]]
    if bad:
        raise SystemExit(f'not K=V (or PACKAGE:K=V for --package-env): {bad[0].split("=", 1)[0]}')
    glob = dict(x.split('=', 1) for x in flat)
    per: dict[str, dict] = {}
    for x in per_package:
        pkg, _, kv = x.partition(':')
        k, _, v = kv.partition('=')
        per.setdefault(pkg.strip().strip('/') or '.', {})[k] = v
    return glob, per


def map_pytest_path(path: str, cwd: Path, known: set[str]) -> str:
    """A file path from a pytest node id, made relative to the package directory cwd. pytest writes it relative to
    its rootdir, which may be an ancestor of cwd (an ini file higher up) or below it; known holds the package's test
    files relative to cwd."""
    if path in known or (cwd / path).is_file():
        return path
    for anc in cwd.parents:
        full = anc / path
        if full.is_file():
            try:
                return full.resolve().relative_to(cwd.resolve()).as_posix()
            except ValueError:
                break                                            # outside the package: leave it as it is
    ends = [k for k in known if k.endswith('/' + path)] or [k for k in known if path.endswith('/' + k)]
    return ends[0] if len(ends) == 1 else path


def run(cmd: list[str], cwd: Path, env_extra: dict) -> tuple[int, str]:
    exe = cmd
    if os.name == 'nt':
        found = shutil.which(cmd[0])
        exe = [found, *cmd[1:]] if found else cmd
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '1', 'FORCE_COLOR': '0', **env_extra}
    try:
        res = subprocess.run(exe, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
    except OSError as exc:
        return 127, f'could not start {cmd[0]}: {exc}'
    return res.returncode, res.stdout + (('\n' + res.stderr) if res.stderr.strip() else '')


def playwright_ids(data: dict, cwd: Path, prefix: str) -> list[str]:
    """file::describe > title for every spec in Playwright's JSON list (one id per spec, whatever the projects)."""
    root = Path(data.get('config', {}).get('rootDir') or cwd)
    out = []

    def walk(suite: dict, titles: list[str], file: str | None):
        f = suite.get('file') or file
        for spec in suite.get('specs', []):
            path = Path(spec.get('file') or f or '')
            full = path if path.is_absolute() else root / path
            rel = Path(os.path.relpath(full, cwd)).as_posix()
            out.append(f'{prefix}{rel}::' + ' > '.join(titles + [spec['title']]))
        for s in suite.get('suites', []):
            is_file = s.get('file') and s.get('title') == s.get('file') and not titles and file is None
            walk(s, titles if is_file or not s.get('title') else titles + [s['title']], s.get('file') or f)
    for s in data.get('suites', []):
        walk(s, [], None)
    return out


def go_list_ids(out: str, cwd: Path, prefix: str, known: set[str]) -> tuple[list[str], list[str], bool]:
    """(ids, errored files, ok) from `go test -list .` output: test names, then `ok <import path>`, `? <path> [no test
    files]` or `FAIL <path> [build failed]` per package. An import path maps to its directory through cwd's go.mod; a
    name is given the id of the known test file in that directory that defines it."""
    m = re.search(r'(?m)^\s*module\s+"?([^\s"]+)', (cwd / 'go.mod').read_text(encoding='utf-8', errors='replace')) \
        if (cwd / 'go.mod').is_file() else None
    mod = m.group(1) if m else ''
    by_dir: dict[str, list[str]] = {}
    for f in known:
        by_dir.setdefault(PurePosixPath(f).parent.as_posix(), []).append(f)

    def dir_of(path: str) -> str:
        return (path[len(mod):].strip('/') or '.') if mod and (path == mod or path.startswith(mod + '/')) else path
    ids, errored, pending, any_pkg = [], [], [], False
    texts: dict[str, str] = {}
    for line in out.splitlines():
        s = line.strip()
        if re.fullmatch(r'(?:Test|Benchmark|Fuzz|Example)\w*', s):
            pending.append(s)
            continue
        pm = re.match(r'^(ok|FAIL|\?)\s+(\S+)(.*)$', s)
        if not pm:
            continue
        any_pkg = True
        d = dir_of(pm.group(2))
        if pm.group(1) == 'FAIL' and re.search(r'\[(?:build|setup) failed\]', pm.group(3)):
            errored += [prefix + f for f in sorted(by_dir.get(d, []))]
        elif pm.group(1) == 'ok':
            for name in pending:
                for f in sorted(by_dir.get(d, [])):
                    if f not in texts:
                        try:
                            texts[f] = (cwd / f).read_text(encoding='utf-8', errors='replace')
                        except OSError:
                            texts[f] = ''
                    if re.search(r'(?m)^func\s+' + re.escape(name) + r'\s*\(', texts[f]):
                        ids.append(f'{prefix}{f}::{name}')
                        break
        pending = []
    return ids, errored, any_pkg


def collect(pkg: str, fw: str, cwd: Path, run_dir: Path, files: list[str] | None, env: dict, reuse: dict | None,
            n_defined: int = 0, known: set[str] | None = None, ignore: list[str] | None = None,
            guard: tree_guard.TreeGuard | None = None, part: str = '') -> dict:
    prefix = '' if pkg == '.' else pkg + '/'
    tag = (pkg.replace('/', '_') if pkg != '.' else 'root') + ('' if fw in ('pytest', 'vitest', 'jest') else f'-{fw}') + part
    local = [f[len(prefix):] for f in files] if files is not None else []
    if fw in NO_LIST:
        return {'package': pkg, 'framework': fw, 'command': None, 'exit_code': None, 'status': 'no_list_command',
                'raw_output': None, 'scope': 'all' if files is None else f'{len(local)} files', 'note': NO_LIST[fw],
                'collected': []}
    listing = None                                   # a file the list command writes its JSON to, in the run directory
    if fw == 'pytest':
        ign = [f'--ignore={d}' for d in (ignore or [])] if files is None else []
        cmd = [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', *ign, *local]
        shown = 'python -m pytest --collect-only -q -p no:cacheprovider' + ''.join(f' {x}' for x in ign) + \
            (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.txt'
        hashed = cmd[1:]
    elif fw == 'go-test':
        # go test -list builds each test binary (vet included) and prints the test names, then one ok/FAIL line per
        # package; nothing runs
        pkgs = sorted({'./' + PurePosixPath(f).parent.as_posix() if PurePosixPath(f).parent.as_posix() != '.' else '.'
                       for f in local}) if files is not None else ['./...']
        cmd = ['go', 'test', '-list', '.', *pkgs]
        shown = 'go test -list . ' + (' '.join(pkgs) if len(pkgs) <= 5 else f'<{len(pkgs)} packages>')
        raw = run_dir / f'collect-{tag}.txt'
        hashed = cmd
    elif fw == 'playwright':
        cmd = ['npx', 'playwright', 'test', '--list', '--reporter=json', *local]
        shown = 'npx playwright test --list --reporter=json' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.json'
        hashed = cmd
    elif fw == 'vitest':
        # Vitest's --json takes an optional value: `--json <file>` writes the listing OVER <file>. The output path is
        # always given with '=' and points into the run directory, never at a file of the target.
        listing = (run_dir / f'.collect-{tag}.vitest.json').resolve()
        cmd = ['npx', 'vitest', 'list', f'--json={listing}', *local]
        shown = 'npx vitest list --json=<run-dir file>' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.json'
        hashed = ['npx', 'vitest', 'list', '--json=<run-dir file>', *local]
    else:
        cmd = ['npx', 'jest', '--listTests', '--json', *local]       # jest's --json is a plain switch
        shown = 'npx jest --listTests --json' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.json'
        hashed = cmd
    h = hashlib.sha256(' '.join(hashed).encode('utf-8'))
    for k in sorted(env):
        h.update(f'\0{k}'.encode('utf-8'))
    for f in sorted(local):
        try:
            h.update(b'\0' + f.encode('utf-8') + b'\0' + (cwd / f).read_bytes().replace(b'\r\n', b'\n'))
        except OSError:
            pass
    input_hash = h.hexdigest()
    if reuse and reuse.get('input_hash') == input_hash and reuse.get('status') == 'ran' and raw.is_file():
        return {**reuse, 'reused': True}
    if listing is not None:
        listing.unlink(missing_ok=True)
    with (guard.around(shown) if guard else contextlib.nullcontext()):
        code, out = run(cmd, cwd, env)
    if listing is not None:
        try:
            text = listing.read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = None
        listing.unlink(missing_ok=True)
        if text is not None:
            out = text + ('\n' + out if out.strip() and code else '')
    ids = []
    errored_files: list[str] = []
    if fw == 'go-test':
        ids, errored_files, ok = go_list_ids(out, cwd, prefix, known or set())
        code = code if ok else (code or 1)
    elif fw == 'pytest':
        for line in out.splitlines():
            line = line.strip()
            if '::' in line and not line.startswith(('=', '<')):
                path, _, rest = re.sub(r'\[.*\]$', '', line).partition('::')
                ids.append(prefix + map_pytest_path(path, cwd, known or set()) + '::' + rest)
        ok = code in (0, 5)
    else:
        start = out.find('{' if fw == 'playwright' else '[')
        end = out.rfind('}' if fw == 'playwright' else ']') + 1
        try:
            items = json.loads(out[start:end]) if start >= 0 else None
        except ValueError:
            items = None
        if items is None:
            code = code or 1
        elif fw == 'playwright':
            ids = playwright_ids(items, cwd, prefix)
            if items.get('errors'):
                code = code or 1
        else:
            for it in items:
                if isinstance(it, str):                                # jest: file paths only
                    ids.append(f'{prefix}{Path(os.path.relpath(it, cwd)).as_posix()}::*')
                else:
                    ids.append(norm_name(f'{prefix}{Path(os.path.relpath(it["file"], cwd)).as_posix()}::{it["name"]}'))
        ok = code == 0
    note = None
    if ok and not ids and n_defined:
        # a list that names nothing for files defining tests is a failed list (a filter that matched nothing, a worker
        # that died), never evidence that every test is not collected
        ok, note = False, f'listed no tests although the listed files define {n_defined}; treated as errored'
    raw.write_text(run_tests.redact_home(out.replace(str(cwd), f'<{pkg}>').replace(str(cwd).replace('\\', '/'), f'<{pkg}>')),
                   encoding='utf-8')
    if fw == 'go-test' and errored_files:
        # packages that did not build: their tests are not_assessed, the others were listed
        status = 'partial' if ids else 'errored'
        return {'package': pkg, 'framework': fw, 'command': shown, 'exit_code': code, 'status': status,
                'raw_output': raw.name, 'scope': 'all' if files is None else f'{len(local)} files', 'input_hash': input_hash,
                **({'env': sorted(env)} if env else {}), 'collected': sorted(set(ids)), 'errored_files': errored_files,
                'note': f'{len(errored_files)} test file(s) in packages that did not build here ([build failed]); '
                        'their tests are not assessed'}
    return {'package': pkg, 'framework': fw, 'command': shown, 'exit_code': code, 'status': 'ran' if ok else 'errored',
            'raw_output': raw.name, 'scope': 'all' if files is None else f'{len(local)} files', 'input_hash': input_hash,
            **({'env': sorted(env)} if env else {}), 'collected': sorted(set(ids)), **({'note': note} if note else {})}


def split_collect(rec: dict, pkg: str, fw: str, cwd: Path, run_dir: Path, files: list[str], env: dict,
                  guard: tree_guard.TreeGuard | None, budget: int, known: set[str]) -> dict:
    """A package whose list command failed, listed again in halves until each failing file stands alone: one file
    the runner cannot list (a config it reads statically, a syntax it rejects) no longer costs every other file of the
    package. At most `budget` extra list commands; files not settled within it stay errored."""
    ok_ids: list[str] = []
    bad: list[str] = []
    parts: list[dict] = []
    todo = [files]
    while todo:
        group = todo.pop(0)
        if len(parts) >= budget:
            bad += group
            continue
        r = collect(pkg, fw, cwd, run_dir, group, env, None, 1, known, None, guard, part=f'-part{len(parts) + 1}')
        parts.append({k: r[k] for k in ('command', 'exit_code', 'status', 'raw_output', 'scope')})
        if r['status'] == 'ran':
            ok_ids += r['collected']
        elif len(group) == 1:
            bad += group
        else:
            half = len(group) // 2
            todo += [group[:half], group[half:]]
    listed_ok = sorted(set(files) - set(bad))
    out = {**rec, 'status': 'partial' if listed_ok else 'errored', 'collected': sorted(set(ok_ids)),
           'errored_files': sorted(bad), 'split': parts,
           'note': (f'the list of the whole package failed; listed again in parts: {len(listed_ok)} of {len(files)} files '
                    f'listed, {len(bad)} failed on their own')}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--cwd', action='append', default=[], help='PACKAGE=DIR: run that package\'s list commands in DIR')
    ap.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V',
                    help='environment for every list command (several pairs per flag, or repeat the flag)')
    ap.add_argument('--package-env', action='append', default=[], metavar='PACKAGE:K=V',
                    help="environment for one package's list command only (repeat)")
    ap.add_argument('--scope', choices=['all', 'candidates'], default='all',
                    help='candidates: list only the files that hold a candidate (the other tests are not_assessed)')
    ap.add_argument('--files', nargs='+', help='list only these repo-relative test files (the other tests are not_assessed)')
    ap.add_argument('--reuse', action='store_true', help='keep an earlier list when its command and files are unchanged')
    ap.add_argument('--split-on-error', type=int, nargs='?', const=40, default=0, metavar='MAX_RUNS',
                    help='when a package\'s list fails, list it again in halves so one file the runner cannot list does '
                         'not cost the others (at most MAX_RUNS extra list commands, 40 by default)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    data = json.loads(args.candidates.read_text(encoding='utf-8'))
    defs = data['definitions']
    overrides = dict(x.split('=', 1) for x in args.cwd)
    env, pkg_env = parse_env(args.env, args.package_env)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or args.run_dir / 'collection.json'
    previous = {}
    if args.reuse and out_path.is_file():
        previous = {(p['package'], p['framework']): p for p in json.loads(out_path.read_text(encoding='utf-8')).get('packages', [])}
    scope = set(args.files) if args.files else {c['file'] for c in data['candidates']} if args.scope == 'candidates' else None
    groups: dict[tuple[str, str], list[dict]] = {}
    for d in defs:
        pdir = root / d['package'] if d['package'] != '.' else root
        groups.setdefault((d['package'], framework_of(d, pdir, root)), []).append(d)
    unknown = sorted(set(pkg_env) - {p for p, _ in groups})
    if unknown:
        print(f'--package-env names no package with tests: {", ".join(unknown)}', file=sys.stderr)
        return 2
    pkgs = []
    try:
        for (pkg, fw), ds in sorted(groups.items()):
            cwd = Path(overrides[pkg]).resolve() if pkg in overrides else (root / pkg if pkg != '.' else root)
            files = sorted({d['file'] for d in ds if d['file'] in scope}) if scope is not None else None
            if files == []:
                continue
            prefix = '' if pkg == '.' else pkg + '/'
            # every test file of the package, where the command runs: a list command must never change one
            guard = tree_guard.TreeGuard.of_files(sorted({cwd / d['file'][len(prefix):] for d in ds}), [cwd])
            # a package holding another package of the same framework leaves the inner one to its own list command
            inner = sorted({p[len(prefix):] for p, f in groups if f == fw and p != pkg and (pkg == '.' or p.startswith(prefix))})
            penv = {**env, **pkg_env.get(pkg, {})}
            known = {d['file'][len(prefix):] for d in ds}
            rec = collect(pkg, fw, cwd, args.run_dir, files, penv, previous.get((pkg, fw)),
                          len([d for d in ds if files is None or d['file'] in files]), known, inner, guard)
            all_files = files if files is not None else sorted({d['file'] for d in ds})
            if rec['status'] == 'errored' and args.split_on_error and len(all_files) > 1 and fw != 'pytest':
                rec = split_collect(rec, pkg, fw, cwd, args.run_dir, all_files, penv, guard, args.split_on_error, known)
            pkgs.append(rec)
    except tree_guard.GuardError as exc:
        tree_guard.report(exc)
        return 3
    ran = {(p['package'], p['framework']): p for p in pkgs if p['status'] in ('ran', 'partial')}
    errored_files = {f for p in pkgs for f in p.get('errored_files', [])}
    collected = {i for p in ran.values() for i in p['collected']}
    files_listed = {i.split('::')[0] for i in collected if i.endswith('::*')}
    by_file: dict[str, list[str]] = {}
    for i in collected:
        by_file.setdefault(i.split('::', 1)[0], []).append(i)
    matched: set[str] = set()

    def is_collected(d: dict) -> bool:
        if d['id'] in collected or d['file'] in files_listed:
            matched.add(d['id'])
            return True
        if norm_name(d['id']) in collected:
            matched.add(norm_name(d['id']))
            return True
        if d.get('name_pattern'):
            rx = re.compile(re.escape(d['file'] + '::') + d['name_pattern'] + '$', re.S)
            hits = [i for i in by_file.get(d['file'], []) if rx.match(i)]
            matched.update(hits)
            return bool(hits)
        return False
    not_collected, not_assessed, n_collected, reasons = [], [], 0, {}
    for d in defs:
        pdir = root / d['package'] if d['package'] != '.' else root
        key = (d['package'], framework_of(d, pdir, root))
        if scope is not None and d['file'] not in scope:
            not_assessed.append(d['id'])
            reasons[d['id']] = 'outside the collect scope'
        elif key[1] in NO_LIST:
            not_assessed.append(d['id'])
            reasons[d['id']] = NO_LIST[key[1]] + (f' ({d["runner_module"]})' if d.get('runner_module') else '')
        elif key not in ran:
            not_assessed.append(d['id'])
            reasons[d['id']] = f'{key[1]} list command did not run or errored'
        elif d['file'] in errored_files:
            not_assessed.append(d['id'])
            reasons[d['id']] = f'{key[1]} could not list this file on its own (see the package\'s split list outputs)'
        elif is_collected(d):
            n_collected += 1
        elif d.get('skip_condition'):
            # skipIf/runIf: a runner may leave a conditionally skipped test out of its list on this machine (a
            # win32-only skip on Windows); that is not evidence that it never runs where the condition is false
            not_assessed.append(d['id'])
            reasons[d['id']] = f'not listed, but it is skipped conditionally ({d["skip_condition"]}) and may run elsewhere'
        else:
            not_collected.append(d['id'])
    res = {'packages': pkgs, 'defined': len(defs), 'collected': n_collected, 'not_collected': not_collected,
           'not_assessed': not_assessed, 'collected_not_defined': sorted(collected - matched - {f'{f}::*' for f in files_listed})}
    if reasons:
        res['not_assessed_reasons'] = reasons
    out_path.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    print(f'{res["collected"]} of {res["defined"]} defined tests collected; not collected: {len(not_collected)}'
          + (f'; {len(not_assessed)} not assessed' if not_assessed else '') + f' -> {out_path}')
    for p in pkgs:
        if p['status'] == 'no_list_command':
            print(f'  {p["package"]} {p["framework"]}: not listed: {p["note"]}', file=sys.stderr)
        elif p['status'] != 'ran':
            print(f'  {p["package"]} {p["framework"]}: {p["status"]} (exit {p["exit_code"]}), see {p["raw_output"]}'
                  + (f'; {p["note"]}' if p.get('note') else ''), file=sys.stderr)
    for t in not_collected:
        print(f'  not collected: {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
