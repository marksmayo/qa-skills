#!/usr/bin/env python3
"""Prove that a suggested test kills its mutants: it passes on the original code and fails on each mutant.

    python prove_kill.py --repo <repo> --mutants mutants.json --mutant B18 [--mutant M11 ...]
        --test-file tests/test_pricing.py --test-code new_test.py --test-name test_concession_starts_at_65
        --run-dir <dir> --out proofs.json [--finding F2 | --gap G1] [--in-place] [--timeout 60]

The test code is appended to --test-file (created if missing) in a scratch copy of the repo under
<dir>, and only the named test is run: once on the original code, then once per mutant with the
mutant's lines swapped in (after checking the file still holds the mutant's original text). The
verdict per mutant is "kills" (original passed, mutant failed), "does_not_kill" (both passed),
or "fails_on_original" (the new test is wrong). Timeouts on the mutant are recorded as such,
never as kills.

--in-place (write_tests: true) also appends the test to the real repository file and runs it
there on the original code; mutants are still only ever applied in the scratch copy. Proofs are
appended to --out; the repository is never committed to.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_results  # noqa: E402
import run_tests  # noqa: E402

COPY_SKIP = {'.git', 'mutants', '__pycache__', '.pytest_cache', '.mypy_cache', '.tox', '.nox', '.venv', 'venv',
             'node_modules'}


def run_test(cwd: Path, test_id: str, junit: Path, env: dict, timeout: float) -> dict:
    cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', f'--junitxml={junit}', test_id]
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        run_tests.kill_tree(proc)
        out, _ = proc.communicate()
        timed_out = True
    cases, _ = parse_results.parse_junit(junit, junit.stem) if junit.exists() else ([], [])
    mine = [c for c in cases if c['id'] == test_id or c['id'].endswith('::' + test_id.split('::')[-1])]
    status = 'timeout' if timed_out else (mine[0]['status'] if mine else 'not_run')
    rec = {'command': f'python -m pytest -q -p no:cacheprovider {test_id}', 'exit_code': proc.returncode,
           'test_status': status, 'duration_s': round(time.monotonic() - t0, 3)}
    if status in ('failed', 'error') and mine and mine[0].get('message'):
        rec['message'] = run_tests.redact_home(mine[0]['message'])[:300]
    if status == 'not_run':
        rec['output_tail'] = run_tests.redact_home(out.decode('utf-8', 'replace')[-400:])
    if junit.exists():
        junit.unlink()
    return rec


def apply_mutant(root: Path, m: dict) -> None:
    p = root / m['file']
    text = p.read_text(encoding='utf-8')
    lines = text.splitlines(keepends=True)
    current = ''.join(lines[m['line'] - 1:m['end_line']]).rstrip('\n')
    if current != m['original']:
        raise SystemExit(f'{m["id"]}: {m["file"]}:{m["line"]} no longer holds the mutant\'s original text')
    nl = '\n' if lines[m['end_line'] - 1].endswith('\n') else ''
    lines[m['line'] - 1:m['end_line']] = [m['mutated'] + nl]
    p.write_text(''.join(lines), encoding='utf-8')


def append_test(path: Path, code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding='utf-8') if path.exists() else ''
    sep = '' if not old else ('\n\n' if old.endswith('\n') else '\n\n\n')
    path.write_text(old + sep + code.strip('\n') + '\n', encoding='utf-8')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--mutants', type=Path, required=True)
    ap.add_argument('--mutant', action='append', required=True, help='mutant id (repeatable)')
    ap.add_argument('--test-file', required=True, help='test file, relative to the repo')
    ap.add_argument('--test-code', type=Path, required=True, help='file holding the test function(s) to append')
    ap.add_argument('--test-name', required=True, help='name of the new test function to run')
    ap.add_argument('--finding', help='finding id this proof supports')
    ap.add_argument('--gap', help='coverage gap id this proof supports')
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--in-place', action='store_true', help='write_tests: also add the test to the repository file')
    ap.add_argument('--timeout', type=float, default=60.0)
    args = ap.parse_args(argv)
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    data = json.loads(args.mutants.read_text(encoding='utf-8'))
    by_id = {m['id']: m for m in data['mutants']}
    missing = [i for i in args.mutant if i not in by_id]
    if missing:
        raise SystemExit(f'unknown mutant id(s): {", ".join(missing)}')
    code = args.test_code.read_text(encoding='utf-8')
    test_id = f'{args.test_file}::{args.test_name}'
    pycache = run_dir / '_pycache'
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': str(pycache)}
    work = run_dir / 'proof-work'
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(repo, work, ignore=lambda d, names: [n for n in names if n in COPY_SKIP or n.endswith('.egg-info')])
    append_test(work / args.test_file, code)
    before = run_tests.tree_snapshot(repo, {run_dir})
    if args.in_place:
        append_test(repo / args.test_file, code)
        original = run_test(repo, test_id, run_dir / 'proof-orig.xml', env, args.timeout)
    else:
        original = run_test(work, test_id, run_dir / 'proof-orig.xml', env, args.timeout)
    pristine = {m: (work / by_id[m]['file']).read_bytes() for m in args.mutant}
    results = []
    for mid in args.mutant:
        m = by_id[mid]
        apply_mutant(work, m)
        try:
            res = run_test(work, test_id, run_dir / f'proof-{mid}.xml', env, args.timeout)
        finally:
            (work / m['file']).write_bytes(pristine[mid])
        if original['test_status'] != 'passed':
            verdict = 'fails_on_original'
        elif res['test_status'] in ('failed', 'error'):
            verdict = 'kills'
        elif res['test_status'] == 'timeout':
            verdict = 'timeout'
        else:
            verdict = 'does_not_kill'
        results.append({'mutant_id': mid, 'engine': m['engine'], 'run': res, 'verdict': verdict})
        print(f'{mid:>5} {m["file"]}:{m["line"]} {verdict}')
    after = run_tests.tree_snapshot(repo, {run_dir})
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(pycache, ignore_errors=True)
    store = json.loads(args.out.read_text(encoding='utf-8')) if args.out.exists() else {'proofs': []}
    pid = f'P{len(store["proofs"]) + 1}'
    store['proofs'].append({
        'id': pid, 'finding': args.finding, 'gap': args.gap, 'test_id': test_id, 'test_file': args.test_file, 'test_code': code.strip('\n'),
        'test_code_sha256': hashlib.sha256(code.strip('\n').encode('utf-8')).hexdigest(),
        'applied_to': 'repo' if args.in_place else 'scratch', 'original_run': original, 'mutants': results,
        'repo_files_changed': changed, 'proved_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(store, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{pid}: original {original["test_status"]}; ' + ', '.join(f'{r["mutant_id"]} {r["verdict"]}' for r in results))
    unexpected = [c for c in changed if not (args.in_place and c == args.test_file)]
    if unexpected:
        print('ERROR: the repository changed: ' + ', '.join(unexpected), file=sys.stderr)
        return 3
    return 0 if all(r['verdict'] == 'kills' for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
