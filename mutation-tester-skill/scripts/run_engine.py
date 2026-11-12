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

    python run_engine.py stryker --repo <repo> --scope scope.json --run-dir <dir> [--minutes 30]
        [--stryker "npx stryker"] [--test-runner vitest] [--concurrency 4] [--timeout-ms 20000]
    python run_engine.py ranges --scope scope.json      # print the scope as Stryker --mutate ranges

stryker (StrykerJS, which must already be installed in the repo): writes a config to <dir> that
mutates only the scope's changed lines ('file:start-end' ranges), uses perTest coverage, puts
Stryker's sandbox (tempDirName) and JSON report in <dir>, and runs `npx stryker run <config>` from
the repo. A JSON Stryker config in the repo is used with those settings on top (config_source
"repo"); otherwise the config is a scratch one with the test runner from package.json. Stryker
mutates only its sandbox; the repository tree is hashed before and after, and its code hash is
recorded, in <dir>/stryker.run.json, which normalize_results.py --stryker-run reads.

Other engines (Stryker.NET, PIT, cargo-mutants, ...) are run with their own commands (SKILL.md
lists them); normalize_results.py reads the mutation-testing-elements JSON.
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
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
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


JS_SUFFIXES = ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts', '.vue', '.svelte')
STRYKER_CONFIGS = ('stryker.config.json', 'stryker.conf.json', '.stryker.conf.json', 'stryker.config.mjs',
                   'stryker.config.js', 'stryker.config.cjs', 'stryker.conf.js', 'stryker.conf.mjs', 'stryker.conf.cjs')


def mutate_ranges(scope: dict) -> list[str]:
    """Stryker --mutate entries for the scope's changed lines: 'file:start-end' per range."""
    out = []
    for f in scope['files']:
        if f['kind'] == 'source' and f['status'] != 'deleted' and f['path'].endswith(JS_SUFFIXES):
            out += [f"{f['path']}:{a}-{b}" for a, b in f['ranges']]
    return out


def detect_runner(repo: Path) -> str | None:
    try:
        pkg = json.loads((repo / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    deps = {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}
    for name in ('vitest', 'jest', 'mocha', 'karma'):
        if name in deps:
            return name
    return None


def do_stryker(args) -> int:
    """StrykerJS in place: Stryker mutates in its own sandbox; its sandbox and reports go to the run
    directory, and the repository tree is hashed before and after to show it was not changed."""
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    scope = json.loads(args.scope.read_text(encoding='utf-8'))
    exe = shlex.split(args.stryker)
    exe[0] = shutil.which(exe[0]) or exe[0]     # npx is npx.cmd on Windows
    out_path = run_dir / 'stryker.run.json'
    report = run_dir / 'stryker-report.json'
    record = {'engine': {'name': 'stryker', 'version': None, 'command': None, 'config_source': None, 'config': None},
              'status': 'ran', 'reason': '', 'report': report.name,
              'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

    def finish(status: str, reason: str) -> int:
        record.update(status=status, reason=reason)
        out_path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'stryker {status}: {reason}')
        return 0

    mutate = mutate_ranges(scope)
    if not mutate:
        return finish('not_run', 'no JavaScript/TypeScript source lines in scope')
    if not (repo / 'node_modules' / '@stryker-mutator' / 'core').exists():
        return finish('not_run', '@stryker-mutator/core is not installed in the repo (and the runner plugin, e.g. '
                      '@stryker-mutator/vitest-runner); install it only with the user\'s agreement')
    config: dict = {}
    repo_cfg = next((n for n in STRYKER_CONFIGS if (repo / n).is_file()), None)
    if repo_cfg and repo_cfg.endswith('.json'):
        config = json.loads((repo / repo_cfg).read_text(encoding='utf-8'))
        record['engine']['config_source'] = 'repo'
    else:
        record['engine']['config_source'] = 'scratch'
        if repo_cfg:
            record['scratch_note'] = f'{repo_cfg} is JavaScript and cannot be merged here; a scratch config was used'
        runner = args.test_runner or detect_runner(repo)
        if runner:
            config['testRunner'] = runner
        if runner == 'vitest':
            config['vitest'] = {'related': True}
    sandbox = run_dir / 'stryker-tmp'
    config.update({'mutate': mutate, 'reporters': ['json', 'clear-text'], 'jsonReporter': {'fileName': str(report)},
                   'coverageAnalysis': 'perTest', 'tempDirName': str(sandbox), 'cleanTempDir': True,
                   'incremental': False})
    if args.concurrency:
        config['concurrency'] = args.concurrency
    if args.timeout_ms:
        config['timeoutMS'] = args.timeout_ms
    cfg_path = run_dir / 'stryker.config.json'
    cfg_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    record['engine']['config'] = (f'{repo_cfg} + ' if record['engine']['config_source'] == 'repo' else '') + \
        f'{cfg_path.name} (mutate: {len(mutate)} line ranges from the scope)'
    record['engine']['command'] = f'{args.stryker} run <run>/{cfg_path.name}'
    record['mutate'] = mutate
    skip = {run_dir}
    before = run_tests.tree_snapshot(repo, skip)
    record['code_hash'] = run_tests.code_hash(repo)
    t0 = time.monotonic()
    code, out, timed_out = run(exe + ['run', str(cfg_path)], repo, timeout=args.minutes * 60)
    record['duration_s'] = round(time.monotonic() - t0, 1)
    record['exit_code'] = code
    (run_dir / 'stryker.log').write_text(clean_log(out), encoding='utf-8')
    after = run_tests.tree_snapshot(repo, skip)
    record['tree_changed'] = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    try:
        record['engine']['version'] = json.loads((repo / 'node_modules' / '@stryker-mutator' / 'core' / 'package.json')
                                                 .read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        pass
    if timed_out:
        record.update(status='errored', reason=f'stopped at the {args.minutes}-minute budget; see stryker.log')
    elif not report.exists():
        record.update(status='errored', reason=f'Stryker exited {code} without a JSON report; see stryker.log')
    out_path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'stryker ({record["engine"]["config_source"]} config, {len(mutate)} ranges): {record["status"]} '
          f'in {record["duration_s"]} s -> {report if report.exists() else "no report"}')
    if record['tree_changed']:
        print('ERROR: the repository changed during the run: ' + ', '.join(record['tree_changed'][:10]), file=sys.stderr)
        return 3
    return 0 if record['status'] == 'ran' else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('engine', choices=['mutmut', 'builtin', 'stryker', 'ranges'])
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--scope', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path)
    ap.add_argument('--stryker', default='npx stryker', help='command that runs StrykerJS (default: npx stryker)')
    ap.add_argument('--test-runner', help='Stryker testRunner for a scratch config (default: from package.json)')
    ap.add_argument('--concurrency', type=int)
    ap.add_argument('--timeout-ms', type=int, help='Stryker timeoutMS')
    ap.add_argument('--minutes', type=float, default=30.0)
    ap.add_argument('--mutmut', default='mutmut', help='command that runs mutmut (default: mutmut)')
    ap.add_argument('--source-paths', nargs='*', help='scratch config: directories to mutate (default: from the scope)')
    ap.add_argument('--tests', nargs='*', help='scratch config: test paths (default: tests/)')
    ap.add_argument('--keep-work', action='store_true', help='keep the scratch copy')
    args, rest = ap.parse_known_args(argv)
    if args.engine == 'ranges':
        for r in mutate_ranges(json.loads(args.scope.read_text(encoding='utf-8'))):
            print(r)
        return 0
    if args.repo is None or args.run_dir is None:
        ap.error(f'{args.engine} needs --repo and --run-dir')
    if args.engine == 'stryker':
        if rest:
            ap.error(f'unrecognised arguments: {" ".join(rest)}')
        return do_stryker(args)
    if args.engine == 'builtin':
        import builtin_mutator
        return builtin_mutator.main(['--repo', str(args.repo), '--scope', str(args.scope), '--run-dir', str(args.run_dir),
                                     '--minutes', str(args.minutes), *rest])
    if rest:
        ap.error(f'unrecognised arguments: {" ".join(rest)}')
    return do_mutmut(args)


if __name__ == '__main__':
    sys.exit(main())
