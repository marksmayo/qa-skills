#!/usr/bin/env python3
"""Run a test command once or many times under controlled variations (stdlib only).

    python run_tests.py --preset pytest --cwd <repo> --out runs.json [--tests ID ...] [--repeat 20]
        [--minutes 60] [--order given|reverse|shuffle] [--seed N] [--hash-seed N] [--workers N]
        [--tz Pacific/Kiritimati] [--locale tr_TR.UTF-8] [--cpu-load K] [--isolate] [--env K=V ...]
        [--timeout 600] [--label baseline] [--run-prefix R] [--check-tree]

    python run_tests.py --preset custom --cmd "npx mocha --reporter mocha-junit-reporter {tests}" \
        --env MOCHA_FILE={junit} --cwd <repo> --out runs.json

Each run is appended to `runs.json` in parse_results.py's format ({runs[], cases[]}). The recorded
command shows `python` for the interpreter and JUnit paths relative to runs.json. Extra fields
on the run:
    variation {order, seed, hash_seed, workers, tz, tz_applied, locale, cpu_load, isolate, env},
    duration_s, timed_out, code_hash, label, tests_selected, preset

`code_hash` fingerprints the source files under --cwd (VCS, dependency, cache and build
directories skipped), so "same code" can be checked between runs. Presets know each runner's
JUnit, ordering, seed and worker flags; a variation a preset can't apply is an error, never
silently ignored. `--minutes` caps the wall time: repeats stop early and the stop is recorded.
The user's home directory is replaced with ~ in JUnit files and output.
`--check-tree` hashes the working tree before and after and exits 3 if anything outside the
JUnit directory changed. The script never edits test files.

On Windows the C runtime only understands "XXX-hh:mm" TZ strings (a three-letter name, a fixed
offset, no DST rules), so IANA zone names are translated (Pacific/Kiritimati -> LIN-14) and the
applied value is recorded. LANG and LC_ALL are set as
given; many runtimes on Windows ignore them, and each run says so.
"""
from __future__ import annotations
import argparse, hashlib, json, os, random, shlex, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_results  # noqa: E402

SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
             '.tox', '.nox', '.venv', 'venv', 'env', 'dist', 'build', 'target', '.gradle', '.idea', '.vscode',
             'coverage', 'htmlcov', '.next', '.nuxt', 'test-results', 'playwright-report', '.junit'}
SKIP_SUFFIXES = {'.pyc', '.pyo', '.log', '.xml'}

# IANA name -> TZ string for the Windows C runtime, which reads only "XXX[+|-]hh[:mm]" with a three-letter
# name and no DST rules. These are fixed standard offsets; daylight saving is not modelled.
POSIX_TZ = {
    'UTC': 'UTC0', 'Etc/UTC': 'UTC0', 'Pacific/Kiritimati': 'LIN-14', 'Pacific/Pago_Pago': 'SST11',
    'Pacific/Honolulu': 'HST10', 'America/St_Johns': 'NST3:30', 'America/New_York': 'EST5',
    'Europe/London': 'GMT0', 'Asia/Kolkata': 'IST-5:30', 'Asia/Kathmandu': 'NPT-5:45',
    'Australia/Sydney': 'AES-10', 'Pacific/Auckland': 'NZS-12', 'Pacific/Chatham': 'CHA-12:45',
}

PRESETS = {
    'pytest': {
        'cmd': [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--junitxml={junit}'],
        'tests': 'args', 'workers': ['-n', '{n}'], 'workers_check': 'xdist', 'order': 'explicit_ids',
        'collect': [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider'],
        'seed_env': 'PYTHONHASHSEED',
    },
    'jest': {
        'cmd': ['npx', 'jest', '--ci', '--reporters=default', '--reporters=jest-junit'],
        'env': {'JEST_JUNIT_OUTPUT_FILE': '{junit}'}, 'tests': 'args', 'workers': ['--maxWorkers={n}'],
        'shuffle': ['--randomize', '--seed={seed}'],
    },
    'vitest': {
        'cmd': ['npx', 'vitest', 'run', '--reporter=junit', '--outputFile={junit}'], 'tests': 'args',
        'workers': ['--maxWorkers={n}'], 'shuffle': ['--sequence.shuffle', '--sequence.seed={seed}'],
    },
    'playwright': {
        'cmd': ['npx', 'playwright', 'test', '--reporter=junit'], 'env': {'PLAYWRIGHT_JUNIT_OUTPUT_NAME': '{junit}'},
        'tests': 'args', 'workers': ['--workers={n}'],
    },
    'gotestsum': {
        'cmd': ['gotestsum', '--junitfile', '{junit}', '--', '-count=1'], 'tests': 'args', 'workers': ['-p', '{n}'],
        'shuffle': ['-shuffle={seed}'],
    },
    'custom': {'cmd': None, 'tests': 'placeholder'},
}


def redact_home(text: str) -> str:
    """Replace the user's home directory with ~ (both slash styles), so reports don't carry it."""
    home = str(Path.home())
    for h in sorted({home, home.replace('\\', '/'), home.replace('\\', '\\\\')}, key=len, reverse=True):
        if len(h) > 3:
            text = text.replace(h, '~')
    return text


def code_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(iter_files(root)):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode('utf-8') + b'\0')
        h.update(p.read_bytes().replace(b'\r\n', b'\n'))
        h.update(b'\0')
    return h.hexdigest()


def iter_files(root: Path, extra_skip: set[Path] | None = None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith('.egg-info')
                       and not (extra_skip and (Path(dirpath) / d).resolve() in extra_skip)]
        for f in filenames:
            p = Path(dirpath) / f
            if p.suffix not in SKIP_SUFFIXES:
                yield p


def tree_snapshot(root: Path, skip: set[Path]) -> dict[str, str]:
    out = {}
    for p in iter_files(root, skip):
        try:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
    return out


def tz_value(tz: str) -> tuple[str, str | None]:
    if os.name == 'nt' and tz in POSIX_TZ:
        return POSIX_TZ[tz], f'TZ {tz} applied as {POSIX_TZ[tz]!r}, a fixed standard offset (the Windows C runtime has no IANA zones or DST rules)'
    if os.name == 'nt' and '/' in tz:
        return tz, f'TZ {tz} has no POSIX translation here; the Windows C runtime may treat it as UTC'
    return tz, None


def collect_pytest(cwd: Path, tests: list[str], env: dict) -> list[str]:
    res = subprocess.run(PRESETS['pytest']['collect'] + tests, cwd=cwd, env=env, capture_output=True, text=True,
                         encoding='utf-8', errors='replace')
    ids = [l.strip() for l in res.stdout.splitlines() if '::' in l and not l.startswith(' ')]
    if not ids:
        raise SystemExit(f'pytest collected no tests:\n{res.stdout[-800:]}{res.stderr[-800:]}')
    return ids


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def start_hogs(k: int) -> list[subprocess.Popen]:
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    return [subprocess.Popen([sys.executable, '-c', 'while True: pass'], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kw) for _ in range(k)]


def has_module(name: str, cwd: Path, env: dict) -> bool:
    return subprocess.run([sys.executable, '-c', f'import {name}'], cwd=cwd, env=env, capture_output=True).returncode == 0


def build(args, preset: dict, junit: Path, tests: list[str], seed: int | None) -> tuple[list[str], dict]:
    fill = lambda s: s.replace('{junit}', str(junit)).replace('{seed}', str(seed)).replace('{n}', str(args.workers))  # noqa: E731
    if preset['cmd'] is None:
        cmd = []
        for tok in shlex.split(args.cmd):
            if tok == '{tests}':
                cmd += tests  # the token expands to one argument per test
            else:
                cmd.append(fill(tok))
        if tests and '{tests}' not in args.cmd:
            cmd += tests
    else:
        cmd = [fill(x) for x in (shlex.split(args.cmd) if args.cmd else preset['cmd'])]
        if args.cmd and '{junit}' not in args.cmd and preset is PRESETS['pytest']:
            cmd += [f'--junitxml={junit}']
        if args.workers:
            cmd += [fill(x) for x in preset['workers']]
        if args.order == 'shuffle' and 'shuffle' in preset:
            cmd += [fill(x) for x in preset['shuffle']]
        cmd += tests
    env = {k: fill(v) for k, v in preset.get('env', {}).items()}
    return cmd, env


def run_one(args, preset, run_id: str, tests: list[str], base_env: dict, junit_dir: Path, cwd: Path,
            seed: int | None, notes: list[str]) -> dict:
    junit = (junit_dir / f'{run_id}.xml').resolve()
    if junit.exists():
        junit.unlink()
    cmd, extra_env = build(args, preset, junit, tests, seed)
    env = {**base_env, **extra_env}
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    hogs = start_hogs(args.cpu_load) if args.cpu_load else []
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        try:
            out, _ = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            out, _ = proc.communicate()
            timed_out = True
        code = proc.returncode
    finally:
        for h in hogs:
            kill_tree(h)
    duration = round(time.monotonic() - t0, 3)
    if junit.exists():
        junit.write_text(redact_home(junit.read_text(encoding='utf-8', errors='replace')), encoding='utf-8')
    cases, pnotes = parse_results.parse_junit(junit, run_id) if junit.exists() else ([], [f'{junit.name}: not written'])
    run_notes = list(notes) + pnotes
    if timed_out:
        run_notes.append(f'Timed out after {args.timeout}s; the process tree was killed.')
    if not cases:
        tail = redact_home((out or b'').decode('utf-8', 'replace')[-600:])
        run_notes.append('No test cases parsed. Treat this run as not having run the tests. Output tail: ' + tail)
    shown = ' '.join('python' if c == sys.executable else c for c in cmd)
    rel = os.path.relpath(junit, args.out.resolve().parent).replace(os.sep, '/')
    shown = shown.replace(str(junit), rel)
    return {'id': run_id, 'command': shown, 'exit_code': code, 'junit_paths': [rel],
            'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'totals': parse_results.totals(cases), 'file_hashes': {}, 'notes': run_notes,
            'duration_s': duration, 'timed_out': timed_out, 'label': args.label, 'preset': args.preset,
            'tests_selected': tests}, cases


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--preset', choices=sorted(PRESETS), default='pytest')
    ap.add_argument('--cmd', help='base command; required for --preset custom ({junit}, {tests}, {seed}, {n} placeholders)')
    ap.add_argument('--cwd', type=Path, default=Path('.'))
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--junit-dir', type=Path, help='default: <out dir>/junit')
    ap.add_argument('--tests', nargs='*', default=[])
    ap.add_argument('--tests-file', type=Path, help='one test id per line')
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--minutes', type=float, default=60.0, help='wall-time cap for all repeats (default 60)')
    ap.add_argument('--timeout', type=int, default=900, help='per-run timeout in seconds')
    ap.add_argument('--order', choices=['given', 'reverse', 'shuffle'], default='given')
    ap.add_argument('--seed', type=int, help='shuffle seed (default: a recorded random seed per run)')
    ap.add_argument('--hash-seed', help='PYTHONHASHSEED for every run ("random" leaves Python to choose)')
    ap.add_argument('--workers', type=int)
    ap.add_argument('--tz')
    ap.add_argument('--locale')
    ap.add_argument('--cpu-load', type=int, default=0, help='number of busy sibling processes during each run')
    ap.add_argument('--isolate', action='store_true', help='each test in its own process')
    ap.add_argument('--env', nargs='*', default=[], metavar='K=V')
    ap.add_argument('--label', default='run')
    ap.add_argument('--run-prefix', default='R')
    ap.add_argument('--check-tree', action='store_true')
    args = ap.parse_args(argv)
    preset = PRESETS[args.preset]
    if preset['cmd'] is None and not args.cmd:
        ap.error('--preset custom needs --cmd')
    cwd = args.cwd.resolve()
    junit_dir = (args.junit_dir or args.out.parent / 'junit').resolve()
    junit_dir.mkdir(parents=True, exist_ok=True)

    tests = list(args.tests) + ([l.strip() for l in args.tests_file.read_text(encoding='utf-8').splitlines() if l.strip()]
                                if args.tests_file else [])
    env = dict(os.environ)
    notes, variation = [], {'order': args.order, 'seed': None, 'hash_seed': args.hash_seed, 'workers': args.workers,
                            'tz': args.tz, 'tz_applied': None, 'locale': args.locale, 'cpu_load': args.cpu_load,
                            'isolate': args.isolate, 'env': {}}
    for kv in args.env:
        k, _, v = kv.partition('=')
        env[k] = v
        variation['env'][k] = v
    if args.tz:
        env['TZ'], note = tz_value(args.tz)
        variation['tz_applied'] = env['TZ']
        if note:
            notes.append(note)
    if args.locale:
        env['LANG'] = env['LC_ALL'] = args.locale
        if os.name == 'nt':
            notes.append(f'LANG/LC_ALL set to {args.locale}; many runtimes on Windows ignore them.')
    if args.hash_seed and args.hash_seed != 'random':
        if preset.get('seed_env') != 'PYTHONHASHSEED' and args.preset != 'custom':
            ap.error(f'--hash-seed applies to Python runners only, not {args.preset}')
        env['PYTHONHASHSEED'] = str(args.hash_seed)
    if args.workers:
        if 'workers' not in preset:
            ap.error(f'{args.preset} has no worker flag; use --preset custom')
        if preset.get('workers_check') and not has_module(preset['workers_check'], cwd, env):
            ap.error(f'--workers needs {preset["workers_check"]} installed for {args.preset}; record the parallel variation as not_run')
    if args.order != 'given' and preset.get('order') != 'explicit_ids' and 'shuffle' not in preset:
        ap.error(f'{args.preset} cannot reorder tests; record the order variation as not_run')
    if args.order == 'reverse' and preset.get('order') != 'explicit_ids':
        ap.error(f'{args.preset} supports shuffle, not reverse')
    if preset.get('order') == 'explicit_ids' and (args.order != 'given' or args.isolate):
        if args.preset == 'pytest' and has_module('pytest_randomly', cwd, env):
            preset = {**preset, 'cmd': preset['cmd'] + ['-p', 'no:randomly']}
        tests = collect_pytest(cwd, tests, env)

    data = {'runs': [], 'cases': []}
    if args.out.exists():
        data = json.loads(args.out.read_text(encoding='utf-8'))
    existing = {r['id'] for r in data['runs']}
    n0 = sum(1 for r in data['runs'] if r['id'].startswith(args.run_prefix + '-'))
    chash = code_hash(cwd)
    before = tree_snapshot(cwd, {junit_dir, args.out.resolve().parent}) if args.check_tree else None
    deadline = time.monotonic() + args.minutes * 60
    batches = [[t] for t in tests] if args.isolate else [tests]
    done, planned, stopped = 0, args.repeat * len(batches), False
    for i in range(args.repeat):
        for batch in batches:
            if time.monotonic() > deadline:
                stopped = True
                break
            seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2**31)
            order = list(batch)
            if args.order == 'reverse':
                order.reverse()
            elif args.order == 'shuffle' and preset.get('order') == 'explicit_ids':
                random.Random(seed).shuffle(order)
            run_id = f'{args.run_prefix}-{n0 + done + 1}'
            while run_id in existing:
                n0 += 1
                run_id = f'{args.run_prefix}-{n0 + done + 1}'
            run, cases = run_one(args, preset, run_id, order, env, junit_dir, cwd,
                                 seed if args.order == 'shuffle' else None, notes)
            run['variation'] = {**variation, 'seed': seed if args.order == 'shuffle' else None}
            run['code_hash'] = chash
            data['runs'].append(run)
            data['cases'] += cases
            existing.add(run_id)
            done += 1
            t = run['totals']
            print(f"{run_id} [{args.label}] {t['passed']} passed, {t['failed']} failed, {t['error']} error, "
                  f"{t['skipped']} skipped; exit {run['exit_code']}; {run['duration_s']}s"
                  + (' TIMEOUT' if run['timed_out'] else ''))
        if stopped:
            break
    if stopped:
        print(f'Stopped at the {args.minutes}-minute cap after {done} of {planned} planned run(s).')
        data.setdefault('stops', []).append({'label': args.label, 'completed': done, 'planned': planned,
                                             'minutes': args.minutes})
    if code_hash(cwd) != chash:
        data.setdefault('warnings', []).append(f'{args.label}: source files changed during the runs')
        print('WARN: source files changed during the runs; these runs do not share one code version.')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if before is not None:
        after = tree_snapshot(cwd, {junit_dir, args.out.resolve().parent})
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if changed:
            print('Working tree changed: ' + ', '.join(changed[:20]))
            return 3
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
