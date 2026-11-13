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
        [--sandbox auto|mirror|run-dir|in-package] [--stryker-option key=value ...] [--keep-work]
    python run_engine.py ranges --scope scope.json      # print the scope as Stryker --mutate ranges

stryker (StrykerJS, which must already be installed for the package): writes a config to
<dir>/qa-stryker.config.json that mutates only the scope's changed lines ('file:start-end'
ranges), uses perTest coverage, names the runner (and checker) plugins so pnpm's isolated
node_modules cannot hide them, puts the JSON report in <dir>, and runs `npx stryker run <config>`
from the package. A JSON Stryker config in the repo is used with those settings on top
(config_source "repo"); otherwise the config is a scratch one with the test runner from
package.json. --stryker-option sets any other Stryker option (dotted keys nest, values are JSON
when they parse): `vitest.configFile=vitest.unit.config.ts`, `vitest.related=false`,
`ignoreStatic=true`. The scope, reporters and sandbox location cannot be overridden.

The sandbox (--sandbox, default auto): a package inside a workspace (pnpm-workspace.yaml, package.json
"workspaces", ...) gets `mirror`: the workspace's directory levels above the package are recreated
in <dir>/ws (files copied, directories linked, see js_workspace.py) and Stryker's sandbox sits at the
package's own depth there, so `extends: "../../tsconfig.base.json"` and pnpm's relative links resolve
as in the repo. Otherwise `run-dir` (<dir>/stryker-tmp). `in-package` keeps Stryker's default
.stryker-tmp inside the package (left out of the tree check). Stryker's tsconfig rewrite assumes
that default, and TypeScript 7 lacks the API it calls, so it is disabled (tsconfigFile set to a
file that does not exist) for the mirror and run-dir sandboxes and for TypeScript >= 7; the
typescript checker, which needs it, is then removed and the record says so.

Stryker mutates only its sandbox; the repository tree is hashed before and after, and its code
hash is recorded, in <dir>/stryker.run.json, which normalize_results.py --stryker-run reads. An
engine crash (no JSON report) is recorded as `errored` with the first error line from the log and,
for known integration problems, a hint; it is never a run with zero mutants.

Other engines (Stryker.NET, PIT, cargo-mutants, ...) are run with their own commands (SKILL.md
lists them); normalize_results.py reads the mutation-testing-elements JSON.
"""
from __future__ import annotations
import argparse, configparser, json, os, re, shlex, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import js_workspace  # noqa: E402
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


STRYKER_RUNNERS = ('vitest', 'jest', 'mocha', 'karma', 'jasmine', 'tap', 'cucumber')
SCRATCH_CFG = 'qa-stryker.config.json'
TS_DISABLED = 'tsconfig.qa-stryker-disabled.json'     # a file that does not exist: Stryker's tsconfig rewrite is skipped
RESERVED = {'mutate', 'reporters', 'jsonReporter', 'tempDirName'}
STRYKER_ERROR = re.compile(r'\b(?:TypeError|ReferenceError|SyntaxError|RangeError|Error)\b:\s*\S.*'
                           r'|There were failed tests in the initial test run.*|Unknown stryker config option.*'
                           r'|Cannot find (?:package|module) \S+.*|\[TSCONFIG_ERROR\].*|No tests were (?:found|executed).*')
STRYKER_HINTS = (
    ('parseConfigFileTextToJson', 'TypeScript 7 does not export the API that Stryker\'s tsconfig rewrite uses; '
     'run_engine.py disables that rewrite for TypeScript 7 (tsconfig_preprocessor in the record)'),
    ('failed tests in the initial test run', 'Stryker\'s own dry run had failing tests (its related test set can be '
     'wider than the baseline, and its runner uses worker threads): give it a Stryker-only test config with '
     '--stryker-option vitest.configFile=<file> (or jest.configFile) and name what was left out in the limitations'),
    ('Unknown stryker config option', 'a Stryker plugin was not loaded: list it in "plugins" (run_engine.py does for '
     'the runner and checkers of a config without "plugins")'),
    ('Cannot find package', 'a dependency did not resolve from the sandbox: for a workspace package use --sandbox '
     'mirror (the default when a workspace root is found)'),
    ('TSCONFIG_ERROR', 'a tsconfig "extends" or reference did not resolve from the sandbox: use --sandbox mirror'),
)


def set_option(config: dict, kv: str) -> str:
    """--stryker-option key=value: dotted keys nest (vitest.configFile=...), values are JSON when they parse."""
    if '=' not in kv:
        raise SystemExit(f'--stryker-option {kv!r}: expected key=value')
    key, raw = kv.split('=', 1)
    if key.split('.')[0] in RESERVED:
        raise SystemExit(f'--stryker-option {key}: run_engine.py sets {key.split(".")[0]} itself (the scope and the report)')
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    *path, last = key.split('.')
    node = config
    for p in path:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            raise SystemExit(f'--stryker-option {key}: {p} is not an object')
    node[last] = value
    return key


def first_error(log: str) -> tuple[str | None, str | None]:
    """The first error line in Stryker's log and, where it is a known integration problem, what to do."""
    line = next((m.group(0).strip() for m in map(STRYKER_ERROR.search, log.splitlines()) if m), None)
    hint = next((h for k, h in STRYKER_HINTS if k in log), None)
    return (line[:300] if line else None), hint


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
              'status': 'ran', 'reason': '', 'report': report.name, 'notes': [],
              'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

    def finish(status: str, reason: str) -> int:
        record.update(status=status, reason=reason)
        out_path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'stryker {status}: {reason}')
        return 0

    mutate = mutate_ranges(scope)
    if not mutate:
        return finish('not_run', 'no JavaScript/TypeScript source lines in scope')
    if not js_workspace.find_module(repo, '@stryker-mutator/core'):
        return finish('not_run', '@stryker-mutator/core is not installed for this package (nor the runner plugin, e.g. '
                      '@stryker-mutator/vitest-runner); install it only with the user\'s agreement: ' +
                      js_workspace.install_command(repo, ['@stryker-mutator/core', '@stryker-mutator/vitest-runner']))
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
    # where the sandbox goes: a package inside a workspace needs the workspace's levels around it
    root = js_workspace.workspace_root(repo) if args.sandbox in ('auto', 'mirror') else None
    mode = args.sandbox if args.sandbox != 'auto' else ('mirror' if root else 'run-dir')
    if mode == 'mirror' and root is None:
        return finish('not_run', '--sandbox mirror: no workspace root (pnpm-workspace.yaml, package.json "workspaces", '
                      'lerna/nx/turbo/rush) above the package')
    skip = {run_dir}
    mirror = run_dir / 'ws'
    if mode == 'mirror':
        sandbox = js_workspace.build_mirror(repo, root, mirror).parent
        record['sandbox'] = {'mode': 'mirror', 'workspace_root': os.path.relpath(root, repo).replace('\\', '/'),
                             'note': 'the workspace levels above the package are recreated in <run>/ws (files copied, '
                                     'directories linked) so relative extends and pnpm links resolve from the sandbox'}
    elif mode == 'run-dir':
        sandbox = run_dir / 'stryker-tmp'
        record['sandbox'] = {'mode': 'run-dir'}
    else:
        sandbox = None
        skip.add(repo / '.stryker-tmp')
        record['sandbox'] = {'mode': 'in-package', 'note': 'Stryker\'s default .stryker-tmp inside the package '
                                                           '(left out of the tree check)'}
    ts = js_workspace.module_version(repo, 'typescript')
    record['typescript'] = ts
    ts7 = (js_workspace.major(ts) or 0) >= 7
    if ts7 or mode != 'in-package':
        why = (f'TypeScript {ts} does not export the API it uses') if ts7 else \
            'it assumes the sandbox is .stryker-tmp inside the package'
        config['tsconfigFile'] = TS_DISABLED
        record['tsconfig_preprocessor'] = f'disabled: {why}'
        checkers = [c for c in config.get('checkers') or [] if c != 'typescript']
        if checkers != list(config.get('checkers') or []):
            config['checkers'] = checkers
            record['notes'].append('the typescript checker was removed (it needs the tsconfig rewrite): a mutant that '
                                   'does not compile counts as killed or survived, never compile_error')
    else:
        record['tsconfig_preprocessor'] = 'enabled'
    if 'plugins' not in config and config.get('testRunner') in STRYKER_RUNNERS:
        # pnpm's isolated node_modules hide plugins from Stryker core's own lookup; name them
        config['plugins'] = [f'@stryker-mutator/{config["testRunner"]}-runner'] + \
            [f'@stryker-mutator/{c}-checker' for c in config.get('checkers') or []]
    for plugin in config.get('plugins') or []:
        if isinstance(plugin, str) and plugin.startswith('@stryker-mutator/') and not js_workspace.find_module(repo, plugin):
            return finish('not_run', f'{plugin} is not installed for this package; install it only with the user\'s '
                          f'agreement: {js_workspace.install_command(repo, [plugin])}')
    config.update({'mutate': mutate, 'reporters': ['json', 'clear-text'], 'jsonReporter': {'fileName': str(report)},
                   'coverageAnalysis': 'perTest', 'cleanTempDir': True, 'incremental': False})
    if sandbox is not None:
        config['tempDirName'] = str(sandbox)
    if args.concurrency:
        config['concurrency'] = args.concurrency
    if args.timeout_ms:
        config['timeoutMS'] = args.timeout_ms
    record['stryker_options'] = [set_option(config, kv) for kv in args.stryker_option]
    record['coverage_analysis'] = config.get('coverageAnalysis')
    cfg_path = run_dir / SCRATCH_CFG
    cfg_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    record['engine']['config'] = (f'{repo_cfg} + ' if record['engine']['config_source'] == 'repo' else '') + \
        f'{cfg_path.name} (mutate: {len(mutate)} line ranges from the scope)'
    record['engine']['command'] = f'{args.stryker} run <run>/{cfg_path.name}'
    record['mutate'] = mutate
    if report.exists():
        report.unlink()           # never read a report left by an earlier run
    before = run_tests.tree_snapshot(repo, skip)
    record['code_hash'] = run_tests.code_hash(repo)
    t0 = time.monotonic()
    try:
        code, out, timed_out = run(exe + ['run', str(cfg_path)], repo, timeout=args.minutes * 60)
    finally:
        if mode == 'mirror' and not args.keep_work:
            js_workspace.remove_tree(mirror)
    record['duration_s'] = round(time.monotonic() - t0, 1)
    record['exit_code'] = code
    log = clean_log(out)
    (run_dir / 'stryker.log').write_text(log, encoding='utf-8')
    after = run_tests.tree_snapshot(repo, skip)
    record['tree_changed'] = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    core = js_workspace.find_module(repo, '@stryker-mutator/core')
    record['engine']['version'] = js_workspace.read_json(core / 'package.json').get('version') if core else None
    err, hint = first_error(log)
    if timed_out:
        record.update(status='errored', reason=f'stopped at the {args.minutes}-minute budget; see stryker.log')
    elif not report.exists():
        # an engine crash is never a run: no report means no mutant results at all
        record.update(status='errored', reason=f'Stryker exited {code} without a JSON report' +
                      (f': {err}' if err else '') + '; see stryker.log')
    elif code != 0:
        record['notes'].append(f'Stryker exited {code} after writing its report (a score threshold, or a late error'
                               + (f': {err}' if err else '') + ')')
    if hint and record['status'] != 'ran':
        record['hint'] = hint
    out_path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'stryker ({record["engine"]["config_source"]} config, {mode} sandbox, {len(mutate)} ranges): '
          f'{record["status"]} in {record["duration_s"]} s -> {report if report.exists() else "no report"}')
    for n in record['notes']:
        print('  note: ' + n)
    if record['status'] != 'ran':
        print('  ' + record['reason'] + (f'\n  hint: {hint}' if hint else ''))
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
    ap.add_argument('--sandbox', choices=['auto', 'mirror', 'run-dir', 'in-package'], default='auto',
                    help='where Stryker\'s sandbox goes (default auto: mirror inside a workspace, else run-dir)')
    ap.add_argument('--stryker-option', action='append', default=[], metavar='KEY=VALUE',
                    help='any other Stryker option, e.g. vitest.configFile=vitest.unit.config.ts (repeatable)')
    ap.add_argument('--minutes', type=float, default=30.0)
    ap.add_argument('--mutmut', default='mutmut', help='command that runs mutmut (default: mutmut)')
    ap.add_argument('--source-paths', nargs='*', help='mutmut scratch config only: directories to mutate (default: from the scope)')
    ap.add_argument('--tests', nargs='*', help='mutmut scratch config only: test paths (default: tests/); for Stryker '
                    'use --stryker-option vitest.configFile=... (or jest.configFile)')
    ap.add_argument('--keep-work', action='store_true', help='keep the scratch copy (mutmut) or workspace mirror (stryker)')
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
