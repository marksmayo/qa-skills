#!/usr/bin/env python3
"""Prove that a suggested test kills its mutants: it passes on the original code and fails on each mutant.

    python prove_kill.py --repo <repo> --mutants mutants.json --mutant B18 [--mutant M11 ...]
        --test-file tests/test_pricing.py --test-code new_test.py --test-name test_concession_starts_at_65
        --run-dir <dir> --out proofs.json [--finding F2 | --gap G1] [--in-place] [--timeout 60]

Runners: pytest (test_file::test_name), and for a JS/TS test file Vitest (default) or Jest
(--runner jest): the file plus `-t` with the test's full name, given in the " > "-joined id form
("describe > title"). The pattern accepts both separators the runners use (spaces for Jest and
Vitest up to 3, " > " for Vitest 4 and later), and the JUnit report confirms the test ran. The
runner is started through its own entry script under node where found (npx is a .cmd on Windows,
and cmd.exe would rewrite the pattern). The scratch copy links the repo's node_modules (a symlink,
or a junction on Windows) instead of copying it, and removes the link before deleting the copy.
A JS package inside a workspace (pnpm-workspace.yaml, package.json "workspaces") is copied to its
own depth in a mirror of the workspace levels (js_workspace.py), so relative tsconfig `extends`
and pnpm's relative links resolve. When the test does not run at all, its output (proof-*.log)
and JUnit are kept in the run directory and the first error line is recorded. --env and
--extra-args pass what the repo's own test command adds (TZ=..., --configLoader native).

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
import argparse, hashlib, json, os, re, shlex, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import js_workspace  # noqa: E402
import parse_results  # noqa: E402
import run_tests  # noqa: E402

COPY_SKIP = {'.git', 'mutants', '__pycache__', '.pytest_cache', '.mypy_cache', '.tox', '.nox', '.venv', 'venv',
             'node_modules', '.next', '.nuxt', '.turbo', '.cache', 'coverage', '.stryker-tmp', 'dist', 'build', 'out'}
JS_SUFFIXES = ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts')


def runner_for(test_file: str) -> str:
    return 'vitest' if test_file.endswith(JS_SUFFIXES) else 'pytest'


def name_pattern(test_name: str) -> str:
    """The -t pattern for one test. Jest and Vitest up to 3 match it against the describe chain and title
    joined by spaces; Vitest 4 and later join them with " > ". The pattern accepts either separator, so it
    selects the test on every version; the JUnit check below still confirms which test ran."""
    parts = [p for p in re.split(r' > ', test_name) if p]
    return '^' + '(?: > | )'.join(JS_SPECIAL.sub(r'\\\g<0>', p) for p in parts) + '$'


JS_SPECIAL = re.compile(r'[.*+?^${}()|\[\]\\/]')     # only these: other escapes are invalid in a unicode RegExp


ENTRY = {'vitest': ('vitest.mjs',), 'jest': ('bin/jest.js',)}


def test_command(runner: str, test_file: str, test_name: str, junit: Path, extra: list[str],
                 cwd: Path | None = None) -> tuple[list[str], str]:
    """The command that runs only the new test, and how it is recorded (no absolute paths)."""
    if runner == 'pytest':
        tid = f'{test_file}::{test_name}'
        return ([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', f'--junitxml={junit}', tid, *extra],
                f'python -m pytest -q -p no:cacheprovider {tid}' + (' ' + ' '.join(extra) if extra else ''))
    pattern = name_pattern(test_name)
    # the runner's own entry script under node where it can be found: npx is a .cmd on Windows, and cmd.exe
    # would rewrite a pattern's ^ and | characters
    entry = js_workspace.find_module(cwd, runner) if cwd else None
    script = entry and next((entry / s for s in ENTRY[runner] if (entry / s).is_file()), None)
    head = [shutil.which('node') or 'node', str(script)] if script else [shutil.which('npx') or 'npx', runner]
    if runner == 'vitest':
        cmd = [*head, 'run', test_file, '-t', pattern, '--reporter=junit', f'--outputFile={junit}', *extra]
    else:
        cmd = [*head, '--ci', test_file, '-t', pattern, '--reporters=default', '--reporters=jest-junit', *extra]
    return cmd, f'npx {runner} {"run " if runner == "vitest" else ""}{test_file} -t "{pattern}"' + (' ' + ' '.join(extra) if extra else '')


def link_node_modules(repo: Path, work: Path) -> Path | None:
    """Make the repo's node_modules visible in the scratch copy (a symlink, or a junction on Windows)."""
    src, dst = repo / 'node_modules', work / 'node_modules'
    if not src.is_dir():
        return None
    js_workspace.link_dir(src, dst)
    return dst


def unlink_dir(link: Path | None) -> None:
    """Remove a symlink or junction itself, never what it points at."""
    if link is not None and os.path.lexists(link):
        js_workspace.unlink(link)


FIRST_ERROR = re.compile(r'\b(?:TypeError|ReferenceError|SyntaxError|Error)\b:\s*\S.*|Cannot find (?:package|module) .*'
                         r'|\[TSCONFIG_ERROR\].*|No test files? found.*|No tests? found.*|FAIL\s+\S.*')


def run_test(cwd: Path, runner: str, test_file: str, test_name: str, junit: Path, env: dict, timeout: float,
             extra: list[str]) -> dict:
    cmd, shown = test_command(runner, test_file, test_name, junit, extra, cwd)
    if runner == 'jest':
        env = {**env, 'JEST_JUNIT_OUTPUT_FILE': str(junit)}
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
    tail = test_name.split('::')[-1]
    mine = [c for c in cases if c['status'] != 'skipped' and (
        c['id'] == f'{test_file}::{test_name}' or c['id'].endswith('::' + tail) or c['id'].endswith(' > ' + tail))]
    status = 'timeout' if timed_out else (mine[0]['status'] if mine else 'not_run')
    rec = {'command': shown, 'exit_code': proc.returncode,
           'test_status': status, 'duration_s': round(time.monotonic() - t0, 3)}
    if status in ('failed', 'error') and mine and mine[0].get('message'):
        rec['message'] = run_tests.redact_home(mine[0]['message'])[:300]
    if status == 'not_run':
        # the test never ran: keep the whole output and the JUnit beside it, and name the first error line
        text = out.decode('utf-8', 'replace')
        log = junit.with_suffix('.log')
        log.write_text(run_tests.redact_home(text), encoding='utf-8')
        rec['output_log'] = log.name
        if junit.exists():
            rec['junit_kept'] = junit.name
        cause = [c.get('message') for c in cases if c['status'] in ('failed', 'error') and c.get('message')]
        m = next((m for m in map(FIRST_ERROR.search, text.splitlines()) if m), None)
        first = cause[0] if cause else (m.group(0).strip() if m else None)
        if first:
            rec['first_error'] = run_tests.redact_home(first.splitlines()[0])[:300]
        rec['output_tail'] = run_tests.redact_home(text[-400:])
        print(f'  {test_name}: not run' + (f' - {rec["first_error"]}' if first else '') + f' (see {log.name})',
              file=sys.stderr)
        return rec
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
    ap.add_argument('--runner', choices=['pytest', 'vitest', 'jest'],
                    help='default: vitest for a JS/TS test file, else pytest')
    ap.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V',
                    help="environment the repo's test command sets (e.g. TZ=...); several pairs per flag, or repeat the flag")
    ap.add_argument('--extra-args', default='', help="extra runner arguments the repo's test command adds")
    args = ap.parse_args(argv)
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    runner = args.runner or runner_for(args.test_file)
    extra = shlex.split(args.extra_args)
    data = json.loads(args.mutants.read_text(encoding='utf-8'))
    by_id = {m['id']: m for m in data['mutants']}
    missing = [i for i in args.mutant if i not in by_id]
    if missing:
        raise SystemExit(f'unknown mutant id(s): {", ".join(missing)}')
    code = args.test_code.read_text(encoding='utf-8')
    test_id = f'{args.test_file}::{args.test_name}'
    pycache = run_dir / '_pycache'
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': str(pycache),
           **dict(kv.split('=', 1) for kv in run_tests.env_pairs(args.env))}
    scratch = run_dir / 'proof-work'
    js_workspace.remove_tree(scratch)    # left by an interrupted run: links are unlinked, never followed
    # a JS package inside a workspace is copied to its own depth in a mirror of the workspace levels, so
    # relative tsconfig extends and pnpm's relative links resolve as in the repository
    root = js_workspace.workspace_root(repo) if runner != 'pytest' else None
    work = js_workspace.build_mirror(repo, root, scratch) if root else scratch
    shutil.copytree(repo, work, ignore=lambda d, names: [n for n in names if n in COPY_SKIP or n.endswith('.egg-info')])
    link = link_node_modules(repo, work) if runner != 'pytest' else None
    append_test(work / args.test_file, code)
    before = run_tests.tree_snapshot(repo, {run_dir})
    t = (args.test_file, args.test_name, env, args.timeout, extra)

    def go(cwd: Path, junit: Path) -> dict:
        return run_test(cwd, runner, t[0], t[1], junit, t[2], t[3], t[4])

    if args.in_place:
        append_test(repo / args.test_file, code)
        original = go(repo, run_dir / 'proof-orig.xml')
    else:
        original = go(work, run_dir / 'proof-orig.xml')
    pristine = {m: (work / by_id[m]['file']).read_bytes() for m in args.mutant}
    results = []
    for mid in args.mutant:
        m = by_id[mid]
        apply_mutant(work, m)
        try:
            res = go(work, run_dir / f'proof-{mid}.xml')
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
    unlink_dir(link)          # the link first, so removing the copy never reaches the repo's node_modules
    js_workspace.remove_tree(scratch)
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
