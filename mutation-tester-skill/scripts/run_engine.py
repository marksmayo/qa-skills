#!/usr/bin/env python3
"""Run a mutation engine against a scratch copy of a repo and keep its raw results (stdlib only).

    python run_engine.py mutmut --repo <repo> --scope scope.json --run-dir <dir> [--minutes 30]
        [--mutmut "mutmut"] [--source-paths fare_calc/] [--tests tests/]
    python run_engine.py builtin --repo <repo> --scope scope.json --run-dir <dir> [builtin_mutator options]

mutmut (3.x): the repo is copied to <dir>/work and mutmut runs there, so the repository itself
is never touched. A [tool.mutmut] or setup.cfg [mutmut] section in the repo is used as is
(config_source "repo"); otherwise a scratch section is added to the copy (config_source
"scratch") with the scoped source directories. Nothing is added to the repo's dependencies.
The raw results (statuses, each mutant's diff, the exit codes and mutmut's per-function test
map) go to <dir>/mutmut.json and the console log to <dir>/mutmut.log. mutmut 3 needs fork(), so
on Windows it is recorded as not_run: use WSL, or the builtin engine.

builtin: runs builtin_mutator.py (stdlib AST mutations, pytest only) in place; see that script.

Other engines (StrykerJS, Stryker.NET, PIT, cargo-mutants, ...) are run with their own commands
(SKILL.md lists them); normalize_results.py reads Stryker's mutation-testing-elements JSON.
"""
from __future__ import annotations
import argparse, configparser, json, os, re, shlex, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests  # noqa: E402

COPY_SKIP = {'.git', 'mutants', '__pycache__', '.pytest_cache', '.mypy_cache', '.tox', '.nox', '.venv', 'venv',
             'node_modules', 'htmlcov', '.mutmut-cache'}
RESULT = re.compile(r'^\s*([\w.ǁ]+__mutmut_\d+):\s*(.+?)\s*$')


def repo_config(repo: Path) -> str | None:
    pp = repo / 'pyproject.toml'
    if pp.exists() and re.search(r'^\[tool\.mutmut\]', pp.read_text(encoding='utf-8'), re.M):
        return 'pyproject.toml [tool.mutmut]'
    sc = repo / 'setup.cfg'
    if sc.exists():
        cp = configparser.ConfigParser()
        cp.read(sc, encoding='utf-8')
        if cp.has_section('mutmut'):
            return 'setup.cfg [mutmut]'
    return None


def scoped_source_dirs(scope: dict) -> list[str]:
    dirs = []
    for f in scope['files']:
        if f['kind'] == 'source' and f['path'].endswith('.py') and f['status'] != 'deleted' and f['ranges']:
            top = f['path'].split('/')[0]
            d = top + '/' if '/' in f['path'] else f['path']
            if d not in dirs:
                dirs.append(d)
    return dirs


def run(cmd: list[str], cwd: Path, timeout: float | None = None) -> tuple[int, str, bool]:
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode('utf-8', 'replace'), False
    except subprocess.TimeoutExpired:
        run_tests.kill_tree(proc)
        out, _ = proc.communicate()
        return proc.returncode, out.decode('utf-8', 'replace'), True


def clean_log(text: str) -> str:
    """mutmut redraws a progress line with \\r; keep the last state of each line."""
    lines = [l.split('\r')[-1] for l in text.replace('\r\n', '\n').split('\n')]
    return run_tests.redact_home('\n'.join(l for l in lines if l.strip())) + '\n'


def do_mutmut(args) -> int:
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    scope = json.loads(args.scope.read_text(encoding='utf-8'))
    exe = shlex.split(args.mutmut)
    record = {'engine': {'name': 'mutmut', 'version': None, 'command': 'mutmut run', 'config_source': None,
                         'config': None}, 'status': 'ran', 'reason': '',
              'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    out_path = run_dir / 'mutmut.json'
    if os.name == 'nt':
        record.update(status='not_run', reason='mutmut 3 needs fork(); run it under WSL, Linux or macOS, or use the builtin engine')
        out_path.write_text(json.dumps(record, indent=1) + '\n', encoding='utf-8')
        print(record['reason'])
        return 0
    before = run_tests.tree_snapshot(repo, {run_dir})
    work = run_dir / 'work'
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(repo, work, ignore=lambda d, names: [n for n in names if n in COPY_SKIP or n.endswith('.egg-info')])
    cfg = repo_config(repo)
    if cfg:
        record['engine']['config_source'] = 'repo'
        record['engine']['config'] = cfg
    else:
        paths = args.source_paths or scoped_source_dirs(scope)
        tests = args.tests or ['tests/']
        section = ('\n[tool.mutmut]\n' + f'source_paths = {json.dumps(paths)}\n' +
                   f'pytest_add_cli_args_test_selection = {json.dumps(tests)}\n')
        pp = work / 'pyproject.toml'
        pp.write_text((pp.read_text(encoding='utf-8') if pp.exists() else '') + section, encoding='utf-8')
        record['engine']['config_source'] = 'scratch'
        record['engine']['config'] = section.strip()
    code, out, _ = run(exe + ['--version'], work)   # in the configured copy: mutmut reads its config even for --version
    if code != 0:
        record.update(status='not_run', reason=f'mutmut not available: {out.strip()[-200:]}')
        out_path.write_text(json.dumps(record, indent=1) + '\n', encoding='utf-8')
        print(record['reason'])
        return 0
    record['engine']['version'] = out.strip().split()[-1]
    t0 = time.monotonic()
    code, out, timed_out = run(exe + ['run'], work, timeout=args.minutes * 60)
    record['duration_s'] = round(time.monotonic() - t0, 1)
    record['exit_code'] = code
    (run_dir / 'mutmut.log').write_text(clean_log(out), encoding='utf-8')
    if timed_out:
        record['budget_note'] = f'mutmut run stopped at the {args.minutes}-minute budget; unchecked mutants stay "not checked"'
    _, res, _ = run(exe + ['results', '--all', 'true'], work)
    results = {}
    for line in res.splitlines():
        m = RESULT.match(line)
        if m:
            results[m.group(1)] = m.group(2)
    diffs = {}
    for key in results:
        _, show, _ = run(exe + ['show', key], work)
        diffs[key] = '\n'.join(l for l in show.splitlines() if not l.startswith(('/', '  warnings.warn')) and
                               'UserWarning' not in l)
    exit_codes, durations = {}, {}
    for meta in (work / 'mutants').rglob('*.meta'):
        try:
            d = json.loads(meta.read_text(encoding='utf-8'))
        except ValueError:
            continue
        exit_codes.update(d.get('exit_code_by_key', {}))
        durations.update(d.get('durations_by_key', {}))
    stats_path = work / 'mutants' / 'mutmut-stats.json'
    stats = json.loads(stats_path.read_text(encoding='utf-8')) if stats_path.exists() else {}
    record.update(results=results, diffs=diffs, exit_codes=exit_codes, durations=durations,
                  tests_by_function=stats.get('tests_by_mangled_function_name', {}))
    if not results:
        record.update(status='errored', reason='mutmut reported no mutants; see mutmut.log')
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    after = run_tests.tree_snapshot(repo, {run_dir})
    record['tree_changed'] = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    out_path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    counts: dict[str, int] = {}
    for v in results.values():
        counts[v] = counts.get(v, 0) + 1
    print(f'mutmut {record["engine"]["version"]} ({record["engine"]["config_source"]} config): ' +
          ', '.join(f'{v} {k}' for k, v in sorted(counts.items())) + f' -> {out_path}')
    if record['tree_changed']:
        print('ERROR: the repository changed during the run: ' + ', '.join(record['tree_changed']), file=sys.stderr)
        return 3
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('engine', choices=['mutmut', 'builtin'])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--scope', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--minutes', type=float, default=30.0)
    ap.add_argument('--mutmut', default='mutmut', help='command that runs mutmut (default: mutmut)')
    ap.add_argument('--source-paths', nargs='*', help='scratch config: directories to mutate (default: from the scope)')
    ap.add_argument('--tests', nargs='*', help='scratch config: test paths (default: tests/)')
    ap.add_argument('--keep-work', action='store_true', help='keep the scratch copy')
    args, rest = ap.parse_known_args(argv)
    if args.engine == 'builtin':
        import builtin_mutator
        return builtin_mutator.main(['--repo', str(args.repo), '--scope', str(args.scope), '--run-dir', str(args.run_dir),
                                     '--minutes', str(args.minutes), *rest])
    if rest:
        ap.error(f'unrecognised arguments: {" ".join(rest)}')
    return do_mutmut(args)


if __name__ == '__main__':
    sys.exit(main())
