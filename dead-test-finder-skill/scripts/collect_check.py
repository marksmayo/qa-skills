#!/usr/bin/env python3
"""Compare the tests defined in the code with the tests each framework actually collects (stdlib only).

    python collect_check.py <repo> --candidates candidates.json --run-dir run [--cwd client=/path/with/node_modules]
        [--env DATABASE_URL=postgresql://fake:fake@localhost:5432/fake ...] [--scope all|candidates] [--files F ...]
        [--reuse]

For every package and framework with tests, runs the framework's own list command and keeps its
raw output in the run directory:

    pytest      python -m pytest --collect-only -q -p no:cacheprovider
    vitest      npx vitest list --json [files]
    jest        npx jest --listTests --json            (file level only)
    playwright  npx playwright test --list --reporter=json [files]

A JavaScript test file that imports @playwright/test, or sits in a Playwright testDir, is listed
with Playwright, never with Vitest or Jest (which exclude e2e/ and would report it as not
collected). --cwd PACKAGE=DIR runs a package's commands in another directory holding the same
files (a copy with node_modules installed). --env K=V sets environment variables the setup files
read at collection time (a fake DATABASE_URL, never a real one).

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
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests  # noqa: E402


def framework_of(d: dict, pkg_dir: Path) -> str:
    if d['lang'] == 'python':
        return 'pytest'
    if d.get('framework') == 'playwright':
        return 'playwright'
    return js_runner(pkg_dir)


def js_runner(pkg_dir: Path) -> str:
    try:
        pj = json.loads((pkg_dir / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'vitest'
    deps = {**pj.get('dependencies', {}), **pj.get('devDependencies', {})}
    return 'jest' if 'jest' in deps and 'vitest' not in deps else 'vitest'


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


def collect(pkg: str, fw: str, cwd: Path, run_dir: Path, files: list[str] | None, env: dict, reuse: dict | None,
            n_defined: int = 0) -> dict:
    prefix = '' if pkg == '.' else pkg + '/'
    tag = (pkg.replace('/', '_') if pkg != '.' else 'root') + ('' if fw in ('pytest', 'vitest', 'jest') else f'-{fw}')
    local = [f[len(prefix):] for f in files] if files is not None else []
    if fw == 'pytest':
        cmd = [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', *local]
        shown = 'python -m pytest --collect-only -q -p no:cacheprovider' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.txt'
    elif fw == 'playwright':
        cmd = ['npx', 'playwright', 'test', '--list', '--reporter=json', *local]
        shown = 'npx playwright test --list --reporter=json' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.json'
    elif fw == 'vitest':
        cmd = ['npx', 'vitest', 'list', '--json', *local]
        shown = 'npx vitest list --json' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.json'
    else:
        cmd = ['npx', 'jest', '--listTests', '--json', *local]
        shown = 'npx jest --listTests --json' + (f' <{len(local)} files>' if local else '')
        raw = run_dir / f'collect-{tag}.json'
    h = hashlib.sha256(' '.join(cmd[1:] if fw == 'pytest' else cmd).encode('utf-8'))
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
    code, out = run(cmd, cwd, env)
    ids = []
    if fw == 'pytest':
        for line in out.splitlines():
            line = line.strip()
            if '::' in line and not line.startswith(('=', '<')):
                ids.append(prefix + re.sub(r'\[.*\]$', '', line))
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
                    ids.append(f'{prefix}{Path(os.path.relpath(it["file"], cwd)).as_posix()}::{it["name"]}')
        ok = code == 0
    note = None
    if ok and not ids and n_defined:
        # a list that names nothing for files defining tests is a failed list (a filter that matched nothing, a worker
        # that died), never evidence that every test is not collected
        ok, note = False, f'listed no tests although the listed files define {n_defined}; treated as errored'
    raw.write_text(run_tests.redact_home(out.replace(str(cwd), f'<{pkg}>').replace(str(cwd).replace('\\', '/'), f'<{pkg}>')),
                   encoding='utf-8')
    return {'package': pkg, 'framework': fw, 'command': shown, 'exit_code': code, 'status': 'ran' if ok else 'errored',
            'raw_output': raw.name, 'scope': 'all' if files is None else f'{len(local)} files', 'input_hash': input_hash,
            'collected': sorted(set(ids)), **({'note': note} if note else {})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--cwd', action='append', default=[], help='PACKAGE=DIR: run that package\'s list commands in DIR')
    ap.add_argument('--env', action='append', default=[], metavar='K=V', help='environment for the list commands (repeat)')
    ap.add_argument('--scope', choices=['all', 'candidates'], default='all',
                    help='candidates: list only the files that hold a candidate (the other tests are not_assessed)')
    ap.add_argument('--files', nargs='+', help='list only these repo-relative test files (the other tests are not_assessed)')
    ap.add_argument('--reuse', action='store_true', help='keep an earlier list when its command and files are unchanged')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    data = json.loads(args.candidates.read_text(encoding='utf-8'))
    defs = data['definitions']
    overrides = dict(x.split('=', 1) for x in args.cwd)
    env = dict(x.split('=', 1) for x in args.env)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or args.run_dir / 'collection.json'
    previous = {}
    if args.reuse and out_path.is_file():
        previous = {(p['package'], p['framework']): p for p in json.loads(out_path.read_text(encoding='utf-8')).get('packages', [])}
    scope = set(args.files) if args.files else {c['file'] for c in data['candidates']} if args.scope == 'candidates' else None
    groups: dict[tuple[str, str], list[dict]] = {}
    for d in defs:
        pdir = root / d['package'] if d['package'] != '.' else root
        groups.setdefault((d['package'], framework_of(d, pdir)), []).append(d)
    pkgs = []
    for (pkg, fw), ds in sorted(groups.items()):
        cwd = Path(overrides[pkg]).resolve() if pkg in overrides else (root / pkg if pkg != '.' else root)
        files = sorted({d['file'] for d in ds if d['file'] in scope}) if scope is not None else None
        if files == []:
            continue
        pkgs.append(collect(pkg, fw, cwd, args.run_dir, files, env, previous.get((pkg, fw)),
                            len([d for d in ds if files is None or d['file'] in files])))
    ran = {(p['package'], p['framework']): p for p in pkgs if p['status'] == 'ran'}
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
        if d.get('name_pattern'):
            rx = re.compile(re.escape(d['file'] + '::') + d['name_pattern'] + '$', re.S)
            hits = [i for i in by_file.get(d['file'], []) if rx.match(i)]
            matched.update(hits)
            return bool(hits)
        return False
    not_collected, not_assessed, n_collected, reasons = [], [], 0, {}
    for d in defs:
        pdir = root / d['package'] if d['package'] != '.' else root
        key = (d['package'], framework_of(d, pdir))
        if scope is not None and d['file'] not in scope:
            not_assessed.append(d['id'])
            reasons[d['id']] = 'outside the collect scope'
        elif key not in ran:
            not_assessed.append(d['id'])
            reasons[d['id']] = f'{key[1]} list command did not run or errored'
        elif is_collected(d):
            n_collected += 1
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
        if p['status'] != 'ran':
            print(f'  {p["package"]} {p["framework"]}: errored (exit {p["exit_code"]}), see {p["raw_output"]}'
                  + (f'; {p["note"]}' if p.get('note') else ''), file=sys.stderr)
    for t in not_collected:
        print(f'  not collected: {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
