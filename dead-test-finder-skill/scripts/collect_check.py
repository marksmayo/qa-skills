#!/usr/bin/env python3
"""Compare the tests defined in the code with the tests each framework actually collects (stdlib only).

    python collect_check.py <repo> --candidates candidates.json --run-dir run [--cwd client=/path/with/node_modules]

For every package with tests, runs the framework's own list command and keeps its raw output in
the run directory:

    pytest   python -m pytest --collect-only -q -p no:cacheprovider
    vitest   npx vitest list --json
    jest     npx jest --listTests --json            (file level only)

--cwd PACKAGE=DIR runs a package's command in another directory holding the same files (a copy
with node_modules installed, for example). Every definition from scan_tests.py that no list
includes is `not_collected`, which is what confirms a never_runs finding. A package whose list
command fails is recorded as errored, and its tests stay not assessed.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests  # noqa: E402


def frameworks(defs: list[dict]) -> dict[str, str]:
    out = {}
    for d in defs:
        out.setdefault(d['package'], 'pytest' if d['lang'] == 'python' else 'js')
    return out


def js_runner(pkg_dir: Path) -> str:
    try:
        pj = json.loads((pkg_dir / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'vitest'
    deps = {**pj.get('dependencies', {}), **pj.get('devDependencies', {})}
    return 'jest' if 'jest' in deps and 'vitest' not in deps else 'vitest'


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    exe = cmd
    if os.name == 'nt':
        found = shutil.which(cmd[0])
        exe = [found, *cmd[1:]] if found else cmd
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '1', 'FORCE_COLOR': '0'}
    res = subprocess.run(exe, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
    return res.returncode, res.stdout + (('\n' + res.stderr) if res.stderr.strip() else '')


def collect(root: Path, pkg: str, fw: str, cwd: Path, run_dir: Path) -> dict:
    pdir = root / pkg if pkg != '.' else root
    prefix = '' if pkg == '.' else pkg + '/'
    tag = pkg.replace('/', '_') if pkg != '.' else 'root'
    if fw == 'pytest':
        cmd = [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider']
        code, out = run(cmd, cwd)
        ids = []
        for line in out.splitlines():
            line = line.strip()
            if '::' in line and not line.startswith(('=', '<')):
                ids.append(prefix + re.sub(r'\[.*\]$', '', line))
        raw = run_dir / f'collect-{tag}.txt'
        shown = 'python -m pytest --collect-only -q -p no:cacheprovider'
        ok = code in (0, 5)
    else:
        runner = js_runner(pdir)
        cmd = ['npx', 'vitest', 'list', '--json'] if runner == 'vitest' else ['npx', 'jest', '--listTests', '--json']
        code, out = run(cmd, cwd)
        raw = run_dir / f'collect-{tag}.json'
        shown = ' '.join(cmd)
        ids = []
        start = out.find('[')
        try:
            items = json.loads(out[start:out.rfind(']') + 1]) if start >= 0 else []
        except ValueError:
            items, code = [], code or 1
        for it in items:
            if isinstance(it, str):                                    # jest: file paths only
                rel = Path(os.path.relpath(it, cwd)).as_posix()
                ids.append(f'{prefix}{rel}::*')
            else:
                rel = Path(os.path.relpath(it['file'], cwd)).as_posix()
                ids.append(f'{prefix}{rel}::{it["name"]}')
        ok = code == 0
        fw = runner
    raw.write_text(run_tests.redact_home(out.replace(str(cwd), f'<{pkg}>').replace(str(cwd).replace('\\', '/'), f'<{pkg}>')),
                   encoding='utf-8')
    return {'package': pkg, 'framework': fw, 'command': shown, 'exit_code': code, 'status': 'ran' if ok else 'errored',
            'raw_output': raw.name, 'collected': sorted(set(ids))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--cwd', action='append', default=[], help='PACKAGE=DIR: run that package\'s list command in DIR')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    data = json.loads(args.candidates.read_text(encoding='utf-8'))
    defs = data['definitions']
    overrides = dict(x.split('=', 1) for x in args.cwd)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    pkgs = []
    for pkg, fw in sorted(frameworks(defs).items()):
        cwd = Path(overrides[pkg]).resolve() if pkg in overrides else (root / pkg if pkg != '.' else root)
        pkgs.append(collect(root, pkg, fw, cwd, args.run_dir))
    collected = {i for p in pkgs if p['status'] == 'ran' for i in p['collected']}
    ran_pkgs = {p['package'] for p in pkgs if p['status'] == 'ran'}
    files_listed = {i.split('::')[0] for i in collected if i.endswith('::*')}
    defined = [d['id'] for d in defs]
    not_collected = [d['id'] for d in defs if d['package'] in ran_pkgs and d['id'] not in collected and d['file'] not in files_listed]
    not_assessed = [d['id'] for d in defs if d['package'] not in ran_pkgs]
    res = {'packages': pkgs, 'defined': len(defined), 'collected': len([d for d in defined if d in collected or
                                                                         d.split('::')[0] in files_listed]),
           'not_collected': not_collected, 'not_assessed': not_assessed,
           'collected_not_defined': sorted(collected - set(defined) - {f'{f}::*' for f in files_listed})}
    out = args.out or args.run_dir / 'collection.json'
    out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    print(f'{res["collected"]} of {res["defined"]} defined tests collected; not collected: {len(not_collected)}'
          + (f'; {len(not_assessed)} not assessed' if not_assessed else '') + f' -> {out}')
    for t in not_collected:
        print(f'  not collected: {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
