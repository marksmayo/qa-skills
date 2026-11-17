#!/usr/bin/env python3
"""Turn a selection into ready-to-run commands, one per package and framework (stdlib only).

    python build_command.py selection.json [--max-chars 8000] [--ci github|gitlab --ci-out snippet.yml]

Each command runs in its package directory with test paths relative to it: pytest and Vitest/Jest
take files, Playwright takes files, Cypress takes `--spec a,b`, Go takes package directories,
Gradle and Maven take test classes, xcodebuild takes `-only-testing:` targets. A command longer
than --max-chars (the Windows command-line limit is 8191) is split into several. When a package
runs its whole suite, the command has no test arguments (Go: `./...`). A Go command keeps the
`go test` flags the repository's CI or Makefile uses (-race, -timeout, -tags, -count, -short,
...; not coverage, -run or -json), or select_tests.py --go-test-flags; `flags_from` says where
they came from and `args` counts its package arguments. The CI snippet is printed or written
where you say, for you to add yourself; this script never edits CI configuration.

A JavaScript package's own `test` script decides the runner when it names one (vitest, jest,
mocha, playwright test, cypress run, node --test); a package whose script runs something else (a
repository's own CLI) gets no command, its selected tests are listed in no_runner, and
no_runner_reasons says why. build_first lists workspace packages to build before the commands,
when selected tests load a changed package through its build output (package.json main/exports
pointing at dist/).

Tests that are commands run from the repository root, one command each: a script's own self-test
(`python x.py --self-test`, `python scripts/self_test.py`; framework `selftest`) and a check the
repository's guide documents (`python scripts/validate_report.py examples/report.json`; framework
`command`). Python tests governed by a pytest configuration in a folder of their own
(benchmarks/pytest.ini) run from that folder. A command without test arguments (the whole suite)
is given only when nothing excluded (a fixture project with planted failing tests) lies below its
folder; otherwise the selected files are named. A unittest command taken from CI or detect_conventions
is relative to the repository root, so it runs from there.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path, PurePosixPath

CYPRESS_RX = re.compile(r'\.cy\.[jt]sx?$')


def framework_for(test: str, level: str | None, frameworks: list[str], python_runner: str | None = None) -> str | None:
    ext = PurePosixPath(test).suffix
    fw = set(frameworks)
    if ext == '.py':
        return 'unittest' if python_runner == 'unittest' else 'pytest'
    if ext == '.rs':
        return 'cargo-test'
    if ext in ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts'):
        if CYPRESS_RX.search(test) and 'cypress' in fw:
            return 'cypress'
        if 'playwright' in fw and (level == 'e2e' or level == 'visual') and '.spec.' in test:
            return 'playwright'
        for name in ('vitest', 'jest', 'mocha', 'playwright'):
            if name in fw:
                return name
        return 'vitest'
    if ext == '.go':
        return 'go-testing'
    if ext in ('.java', '.kt', '.kts', '.scala'):
        return 'maven' if 'maven' in fw else 'gradle'
    if ext == '.swift':
        return 'xctest'
    if ext == '.dart':
        return 'flutter_test'
    if ext == '.rb':
        return 'rspec'
    return None


def java_class(rel: str) -> str:
    p = PurePosixPath(rel)
    parts = list(p.with_suffix('').parts)
    for marker in ('java', 'kotlin', 'scala'):
        if marker in parts:
            parts = parts[parts.index(marker) + 1:]
            break
    return '.'.join(parts)


GO_KEEP_BOOL = {'-race', '-short', '-failfast', '-trimpath', '-msan', '-asan'}
GO_KEEP_VALUE = {'-timeout', '-count', '-tags', '-p', '-cpu', '-vet', '-mod', '-parallel'}
GO_CI_FILES = ('.github/workflows/*.yml', '.github/workflows/*.yaml', 'Makefile', 'makefile', 'GNUmakefile',
               '.gitlab-ci.yml', 'azure-pipelines*.yml', 'Taskfile.yml', 'Taskfile.yaml', 'justfile', 'Justfile',
               '.circleci/config.yml')


def go_flags_of(line: str) -> list[str]:
    """The flags of one `go test` command line worth keeping for a selected run: -race, -timeout, -tags,
    -count, -short ... (not coverage, -run/-skip, -json or -v, which belong to that CI job's own purpose)."""
    m = re.search(r'\bgo\s+test\b(.*)', line)
    if not m:
        return []
    import shlex
    try:
        words = shlex.split(m.group(1).split('#')[0], posix=True)
    except ValueError:
        words = m.group(1).split()
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w in ('&&', '||', '|', ';') or w.startswith(('>', '2>')):
            break
        name = '-' + w.lstrip('-').split('=', 1)[0] if w.startswith('-') else None
        if name in GO_KEEP_BOOL:
            out.append(name)
        elif name in GO_KEEP_VALUE:
            if '=' in w:
                out.append(f'{name}={w.split("=", 1)[1]}')
            elif i + 1 < len(words):
                out += [name, words[i + 1]]
                i += 1
        elif name and '=' not in w and i + 1 < len(words) and not words[i + 1].startswith(('-', './', '.')) and \
                name in ('-run', '-skip', '-coverprofile', '-coverpkg', '-covermode', '-o', '-bench', '-benchtime',
                         '-exec', '-ldflags', '-gcflags'):
            i += 1                       # a dropped flag's value
        i += 1
    return out


def ci_go_flags(root: Path) -> tuple[list[str], str | None]:
    """go test flags from the repository's CI or Makefile (the first `go test` line that has any), and where."""
    import fnmatch, os
    for pat in GO_CI_FILES:
        d, _, name = pat.rpartition('/')
        base = root / d if d else root
        if not base.is_dir():
            continue
        for f in sorted(os.listdir(base)):
            p = base / f
            if not p.is_file() or not fnmatch.fnmatch(f, name):
                continue
            try:
                lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                flags = go_flags_of(line)
                if flags:
                    return flags, f'{(d + "/") if d else ""}{f}:{n}'
    return [], None


def argv_for(fw: str, tests: list[str], full: bool, go_flags: list[str] | None = None) -> list[str]:
    base = {
        'pytest': ['python', '-m', 'pytest'], 'vitest': ['npx', 'vitest', 'run'], 'jest': ['npx', 'jest'],
        'mocha': ['npx', 'mocha'], 'playwright': ['npx', 'playwright', 'test'], 'cypress': ['npx', 'cypress', 'run'],
        'go-testing': ['go', 'test'], 'gradle': ['./gradlew', 'test'], 'maven': ['mvn', 'test'],
        'xctest': ['xcodebuild', 'test'], 'flutter_test': ['flutter', 'test'], 'rspec': ['bundle', 'exec', 'rspec'],
        'node-test': ['node', '--test'], 'unittest': ['python', '-m', 'unittest'],
    }[fw]
    if fw == 'unittest' and full:
        return base + ['discover']
    if fw == 'go-testing' and go_flags:
        base = base + list(go_flags)
    if full:
        return base + (['./...'] if fw == 'go-testing' else [])
    if fw == 'jest':
        return base + ['--runTestsByPath'] + tests
    if fw == 'cypress':
        return base + ['--spec', ','.join(tests)]
    if fw == 'go-testing':
        dirs = sorted({'./' + str(PurePosixPath(t).parent) if str(PurePosixPath(t).parent) != '.' else '.' for t in tests})
        return base + dirs
    if fw == 'gradle':
        return base + [x for t in tests for x in ('--tests', java_class(t))]
    if fw == 'maven':
        return base + ['-Dtest=' + ','.join(java_class(t).rsplit('.', 1)[-1] for t in tests)]
    if fw == 'xctest':
        return base + [f'-only-testing:{PurePosixPath(t).parts[0]}/{PurePosixPath(t).stem}' for t in tests]
    return base + tests


def split(fw: str, tests: list[str], max_chars: int, go_flags: list[str] | None = None) -> list[list[str]]:
    chunks, cur = [], []
    for t in tests:
        trial = cur + [t]
        if cur and len(' '.join(argv_for(fw, trial, False, go_flags))) > max_chars:
            chunks.append(cur)
            cur = [t]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    return chunks


SCRIPT_RUNNERS = (('vitest', re.compile(r'\bvitest\b')), ('jest', re.compile(r'\bjest\b')),
                  ('playwright', re.compile(r'\bplaywright\s+test\b')), ('cypress', re.compile(r'\bcypress\s+run\b')),
                  ('mocha', re.compile(r'\bmocha\b')), ('node-test', re.compile(r'\bnode\b[^&|;]*\s--test\b')))
JS_FWS = {'vitest', 'jest', 'mocha', 'playwright', 'cypress', 'node-test'}


def script_runner(fw: str, script: str | None) -> tuple[str | None, str | None]:
    """Check a JS framework guess against the package's own test script: (framework, why not runnable).
    A script that runs a tool this script does not know (a repository's own CLI) gets no command."""
    if script is None or fw not in JS_FWS:
        return fw, None
    named = [name for name, rx in SCRIPT_RUNNERS if rx.search(script)]
    if fw in named:
        return fw, None
    if named:
        return named[0], None
    return None, f'its test script runs `{script.strip()[:120]}`, not a runner this selector can pass files to'


def hard_gates(t: dict) -> list[dict]:
    """The gates that keep a test out of a plain `cargo test` on every platform (required-features of its target,
    #[ignore], a feature the defaults do not enable, a cfg false everywhere); [] when it runs somewhere by default."""
    gates = [g for g in t.get('why') or [] if 'platforms' not in g]
    if not gates and t.get('features'):
        gates = [{'gate': 'feature', 'features': list(t['features']),
                  'detail': f'needs --features {",".join(t["features"])}, not enabled by default'}]
    return gates


def not_run_groups(selected: list[dict]) -> list[dict]:
    """command.not_run_by_default: the selected Rust tests a plain `cargo test` does not run, or runs only on some
    platforms or cannot be decided, grouped by crate and gates, with how to run them when a flag does it."""
    groups: dict[tuple, dict] = {}
    for f, t in selected:
        hard = hard_gates(t)
        if hard:
            gates = list(t.get('why') or hard)
        elif t.get('runs_by_default') is False:
            gates = [g for g in t.get('why') or []]
        elif 'runs_by_default' in t and t['runs_by_default'] is None:
            gates = [{'gate': 'undecided', 'detail': 'a cfg this reader cannot decide: '
                                                     + (t.get('why_undecided') or 'unknown')}]
        else:
            continue
        names = sorted({g['gate'] for g in gates})
        feats = sorted({x for g in gates for x in g.get('features') or []})
        plats = sorted(t['platforms']) if t.get('platforms') is not None else None
        detail = '; '.join(dict.fromkeys(g.get('detail') or g['gate'] for g in gates))
        key = (t['crate'], tuple(names), tuple(feats), tuple(plats or ()), detail)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {'crate': t['crate'], 'package': t['package'], 'gates': names, 'detail': detail,
                               'tests': [], 'files': []}
            if feats:
                g['features'] = feats
            if plats is not None:
                g['platforms'] = plats
            run_with = ['cargo', 'test', '-p', t['crate']]
            if feats:
                run_with += ['--features', ','.join(feats)]
            if 'ignored' in names and hard:
                run_with += ['--', '--ignored']
            if (feats or 'ignored' in names) and hard and not set(names) - {'feature', 'required_features', 'ignored'}:
                g['run_with'] = run_with
        if t['id'] not in g['tests']:
            g['tests'].append(t['id'])
        if f not in g['files']:
            g['files'].append(f)
    return sorted(groups.values(), key=lambda g: (g['crate'], g['gates'], g['tests']))


def not_run_lines(command: dict) -> list[str]:
    """One line per not_run_by_default group, for the console and the report: what the commands do not run and why."""
    out = []
    for g in command.get('not_run_by_default') or []:
        n = len(g['tests'])
        shown = ', '.join(g['tests'][:3]) + (f' and {n - 3} more' if n > 3 else '')
        if set(g['gates']) == {'platform'}:
            line = (f'{n} selected test(s) of {g["crate"]} run only on {", ".join(g.get("platforms") or []) or "some platforms"}'
                    f' ({shown}): a run elsewhere passes without running them')
        elif g['gates'] == ['undecided']:
            line = f'{n} selected test(s) of {g["crate"]} may not run ({shown}): {g["detail"]}'
        else:
            line = f'{n} selected test(s) of {g["crate"]} are not run by the commands above ({shown}): {g["detail"]}'
            if g.get('run_with'):
                line += f'. Run them with `{" ".join(g["run_with"])}`'
        out.append(line)
    return out


def cargo_commands(files: list[str], cargo: dict, max_chars: int) -> tuple[list[dict], list[dict], list[str], list[dict]]:
    """(commands, feature_gated, files with no test id, not_run_by_default) for the selected Rust test files.

    Every Rust test file of the workspace selected: one workspace-wide command (the CI's own `cargo test
    --workspace ...` line when there is one). Every test file of a crate: `cargo test -p <crate>`. Otherwise
    `cargo test -p <crate> --lib|--bin <b>|--test <t> -- --exact <paths>` from the files' libtest ids
    (`<target>::<path>`), the same arguments run_tests.py's cargo preset builds from those ids (`test_ids`).

    Tests a plain `cargo test` does not run are never silently counted as run: a test behind a feature the crate does
    not enable by default, in a target with required-features the defaults do not enable (cargo skips the whole
    target), marked #[ignore], or compiled on some platforms only (cargo_project's runs_by_default and why). They are
    listed under not_run_by_default (and, for features, feature_gated). A test no platform runs by default is left
    out of the --exact names (cargo would run nothing and pass, or refuse the target), a command's `tests` counts
    only the files it runs a test of, `not_run` the selected tests of its crates it does not run and
    `platform_dependent` those it runs on some platforms only."""
    tests, crates = cargo['tests'], cargo['crates']
    unknown, by_crate = [], {}
    gated: dict[tuple[str, str], dict] = {}
    for f in files:
        ts = tests.get(f)
        if not ts:
            unknown.append(f)
            continue
        by_crate.setdefault(ts[0]['crate'], []).append(f)
        for t in ts:
            for feat in sorted({x for g in hard_gates(t) for x in g.get('features') or []} | set(t['features'])):
                g = gated.setdefault((t['crate'], feat), {'package': t['package'], 'crate': t['crate'], 'feature': feat,
                                                          'tests': []})
                g['tests'].append(t['id'])
    not_run = not_run_groups([(f, t) for f in files for t in tests.get(f) or []])

    def counts(mine: list[str]) -> dict:
        """What a whole-crate or workspace command over these files runs: the files with a test that runs somewhere
        by default, the tests it never runs, the tests that depend on the platform."""
        ts = [t for f in mine for t in tests.get(f) or []]
        out = {'tests': len([f for f in mine if any(not hard_gates(t) for t in tests.get(f) or [])])}
        hard = len([t for t in ts if hard_gates(t)])
        plat = len([t for t in ts if not hard_gates(t) and t.get('runs_by_default') is False])
        if hard:
            out['not_run'] = hard
        if plat:
            out['platform_dependent'] = plat
        return out
    commands = []
    roots = {crates[c]['root'] for c in by_crate}
    all_rust = {f for c in crates.values() for f in c['test_files']}
    if by_crate and len(roots) == 1 and all_rust <= set(files) and \
            len({c['root'] for c in crates.values()}) == 1:
        root = roots.pop()
        ws = cargo.get('workspace_command')
        cmd = {'package': root, 'cwd': root, 'framework': 'cargo-test', 'full_suite': True,
               'argv': list(ws['argv']) if ws else ['cargo', 'test', '--workspace'],
               **counts([f for f in files if f not in unknown]), 'part': 1, 'parts': 1}
        if ws:
            cmd['command_from'] = ws['from']
        commands.append(cmd)
    else:
        for crate in sorted(by_crate):
            mine, info = by_crate[crate], crates[crate]
            base = {'package': info['dir'], 'cwd': info['root'], 'framework': 'cargo-test'}
            if not any(not hard_gates(t) for f in mine for t in tests[f]):
                continue                # nothing of this crate runs by default: listed under not_run_by_default
            if set(info['test_files']) <= set(mine):
                commands.append({**base, 'full_suite': True, 'argv': ['cargo', 'test', '-p', crate], **counts(mine),
                                 'part': 1, 'parts': 1})
                continue
            ids, param = [], set()
            for f in mine:
                ids += [t['id'] for t in tests[f] if t['id'] not in ids and not hard_gates(t)]
                param |= {t['id'] for t in tests[f] if t.get('parameterised')}
            kind_of = {t['id']: (t['target_kind'], t['target']) for f in mine for t in tests[f]}
            dependent = {t['id'] for f in mine for t in tests[f] if not hard_gates(t) and t.get('runs_by_default') is False}

            def argv_of(chunk: list[str], exact: bool) -> list[str]:
                sel: list[list[str]] = []
                for i in chunk:
                    kind, target = kind_of[i]
                    flag = ['--lib'] if kind == 'lib' else [f'--{kind}', target]
                    if flag not in sel:
                        sel.append(flag)
                return ['cargo', 'test', '-p', crate, *[x for f in sel for x in f], '--',
                        *(['--exact'] if exact else []), *[i.partition('::')[2] for i in chunk]]
            # #[rstest] / #[test_case] functions are modules of generated tests (`fn::case_1`): --exact would match
            # none, so they run as name filters (a substring match) in commands of their own
            chunks = []
            for exact, group in ((True, [i for i in ids if i not in param]), (False, sorted(i for i in ids if i in param))):
                cur: list[str] = []
                for i in group:
                    if cur and len(' '.join(argv_of(cur + [i], exact))) > max_chars:
                        chunks.append((exact, cur))
                        cur = [i]
                    else:
                        cur.append(i)
                if cur:
                    chunks.append((exact, cur))
            for n, (exact, chunk) in enumerate(chunks, 1):
                files_in = sorted({f for f in mine if any(t['id'] in chunk for t in tests[f])})
                cmd = {**base, 'full_suite': False, 'argv': argv_of(chunk, exact), 'tests': len(files_in),
                       'test_ids': chunk, 'part': n, 'parts': len(chunks)}
                if dependent & set(chunk):
                    cmd['platform_dependent'] = len(dependent & set(chunk))
                if not exact:
                    cmd['note'] = ('parameterised tests (#[rstest], #[test_case]) generate child tests named '
                                   '<fn>::<case>, which --exact <fn> would not match: these names are substring '
                                   'filters without --exact, so they run every case and may also run other tests '
                                   'whose names contain them')
                commands.append(cmd)
    return commands, sorted(gated.values(), key=lambda g: (g['crate'], g['feature'])), unknown, not_run


def safe_command(cmd: str | None) -> list[str] | None:
    """A CI command line as argv when it is one plain command (no pipes, redirects, chaining or variables)."""
    import shlex
    if not cmd or re.search(r'[|&;<>`$]', cmd):
        return None
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def build(selected: list[dict], packages: list[dict], full_packages: set[str], tests_by_package: dict[str, list[str]],
          levels: dict[str, str | None], max_chars: int = 8000, scripts: dict[str, str] | None = None,
          builds: list[dict] | None = None, go_flags: list[str] | None = None, go_flags_from: str | None = None,
          cargo: dict | None = None, python_runners: dict[str, dict] | None = None,
          self_tests: dict[str, dict] | None = None, run_roots: dict[str, str] | None = None,
          excluded: list[str] | None = None) -> dict:
    """selected: [{test_id, file, package}], ordered. full_packages: packages whose whole suite is selected.
    scripts: each JS package's own `test` script (package.json), which decides the runner where it names
    one; a package whose script runs something else gets no command, and its tests go to no_runner.
    builds: workspace packages to build first (their tests load another package's build output).
    cargo: select_tests.py's cargo_info() (libtest ids per Rust test file, crates, the CI's workspace command).
    python_runners: detect_conventions' python_runner per package: unittest runs `python -m unittest <files>`
    (the CI's own command for the whole suite), pytest `python -m pytest`.
    self_tests: {test: {command, framework: selftest|command}} for tests that are commands (run from the root).
    run_roots: {python test: folder of the pytest configuration that governs it} where that is not its package.
    excluded: folders left out of the tests (fixture projects): no whole-suite command runs over them."""
    self_tests, run_roots, excluded = self_tests or {}, run_roots or {}, excluded or []
    by_pkg: dict[tuple[str, str, str], list[str]] = {}
    own: list[dict] = []
    unknown = []
    reasons: dict[str, str] = {}
    rust: list[str] = []
    fws = {p['root']: p.get('frameworks', []) for p in packages}
    runners = python_runners or {}
    for s in selected:
        pkg = s['package']
        st = self_tests.get(s['file'])
        if st:
            import shlex
            own.append({'package': pkg, 'cwd': '.', 'framework': st['framework'], 'full_suite': False,
                        'argv': shlex.split(st['command']), 'tests': 1, 'part': 1, 'parts': 1})
            continue
        root = run_roots.get(s['file'], pkg)
        rel = s['file'] if root == '.' else s['file'][len(root) + 1:]
        fw = framework_for(s['file'], levels.get(s['file']), fws.get(pkg, []), (runners.get(pkg) or {}).get('runner'))
        if fw == 'cargo-test':
            if cargo:
                rust.append(s['file'])
            else:
                unknown.append(s['file'])
                reasons.setdefault(pkg, 'Rust test files with no Cargo model (no Cargo.toml was read), so no cargo '
                                        'command can name their tests')
            continue
        if fw is not None and scripts is not None:
            fw, why = script_runner(fw, scripts.get(pkg))
            if why:
                reasons[pkg] = why
        if fw is None:
            unknown.append(s['file'])
            ext = PurePosixPath(s['file']).suffix
            if pkg not in reasons:
                reasons[pkg] = f'no runner this selector knows for {ext or "these"} test files: run them with the package\'s own test command'
            continue
        by_pkg.setdefault((pkg, fw, root), []).append(rel)
    commands = []
    for (pkg, fw, root), tests in by_pkg.items():
        pr = (runners.get(pkg) or {}).get('runner')
        all_fw = [t for t in tests_by_package.get(pkg, []) if t not in self_tests and run_roots.get(t, pkg) == root
                  and framework_for(t, levels.get(t), fws.get(pkg, []), pr) == fw]
        # a bare command collects everything below its folder: never over an excluded fixture project
        holds_excluded = any(root == '.' or d == root or d.startswith(root + '/') for d in excluded)
        full = (pkg in full_packages or root != pkg) and len(tests) >= len(all_fw) and not holds_excluded
        flags = go_flags if fw == 'go-testing' else None
        chunks = [tests] if full else split(fw, tests, max_chars, flags)
        for i, chunk in enumerate(chunks, 1):
            argv = argv_for(fw, chunk, full, flags)
            from_ci = None
            cwd = root
            if fw == 'unittest' and full:
                ci_cmd = safe_command((runners.get(pkg) or {}).get('command'))
                if ci_cmd:
                    # CI's line and detect_conventions' discover command name folders from the repository root
                    argv, cwd = ci_cmd, '.'
                    from_ci = next((e for e in (runners.get(pkg) or {}).get('evidence', []) if ':' in e), None)
            cmd = {'package': pkg, 'cwd': cwd, 'framework': fw, 'full_suite': full,
                   'argv': argv, 'tests': len(chunk), 'part': i, 'parts': len(chunks)}
            if from_ci:
                cmd['command_from'] = from_ci
            if fw == 'go-testing':
                # go test takes package directories, not test files: `args` counts the trailing arguments
                cmd['args'] = len(argv) - 2 - len(flags or [])
                if flags:
                    cmd['flags_from'] = go_flags_from
            commands.append(cmd)
    commands += own
    gated: list[dict] = []
    not_run: list[dict] = []
    if rust:
        cargo_cmds, gated, no_ids, not_run = cargo_commands(rust, cargo, max_chars)
        commands += cargo_cmds
        unknown += no_ids
    out = {'commands': commands, 'split_into': len(commands), 'max_chars': max_chars, 'no_runner': unknown}
    if gated:
        out['feature_gated'] = gated
    if not_run:
        out['not_run_by_default'] = not_run
    if reasons:
        out['no_runner_reasons'] = dict(sorted(reasons.items()))
    if builds:
        out['build_first'] = builds
    return out


def ci_snippet(command: dict, fmt: str) -> str:
    if fmt == 'github':
        lines = ['# Selected tests, from test-impact-selector. Add these steps to a job yourself.', 'steps:']
        for b in command.get('build_first', []):
            lines += [f'  - name: Build {b["package"]} first', f'    working-directory: {b["cwd"]}',
                      f'    run: {" ".join(b["argv"])}']
        for c in command['commands']:
            lines += [f'  - name: Selected {c["framework"]} tests ({c["package"]}, part {c["part"]} of {c["parts"]})',
                      f'    working-directory: {c["cwd"]}',
                      f'    run: {" ".join(c["argv"])}']
        return '\n'.join(lines) + '\n'
    lines = ['# Selected tests, from test-impact-selector. Add this job yourself.', 'selected-tests:', '  script:']
    for b in command.get('build_first', []):
        lines.append(f'    - cd "$CI_PROJECT_DIR/{b["cwd"]}" && {" ".join(b["argv"])}')
    for c in command['commands']:
        lines.append(f'    - cd "$CI_PROJECT_DIR/{c["cwd"]}" && {" ".join(c["argv"])}')
    return '\n'.join(lines) + '\n'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('selection', type=Path)
    ap.add_argument('--max-chars', type=int)
    ap.add_argument('--ci', choices=('github', 'gitlab'))
    ap.add_argument('--ci-out', type=Path)
    args = ap.parse_args(argv)
    sel = json.loads(args.selection.read_text(encoding='utf-8'))
    command = sel['command']
    if args.max_chars:
        levels = {s['file']: s.get('test_level') for s in sel['selected']}
        full = {p['root'] for p in sel['packages'] if p.get('full_suite')}
        go = next((c for c in command['commands'] if c['framework'] == 'go-testing'), None)
        flags = go['argv'][2:len(go['argv']) - go['args']] if go and 'args' in go and not go['full_suite'] else \
            (go['argv'][2:-1] if go else [])
        # cargo and unittest commands need the selection's Cargo model and runner detection: keep them as they are
        kept = [c for c in command['commands'] if c['framework'] in ('cargo-test', 'unittest', 'selftest', 'command')
                or (c['framework'] == 'pytest' and c['cwd'] != c['package'])]
        done = {c['package'] for c in kept if c['framework'] in ('unittest', 'pytest')}
        own = {' '.join(c['argv']) for c in kept if c['framework'] in ('selftest', 'command')}
        rest = [s for s in sel['selected'] if not s['file'].endswith('.rs') and
                not (s['file'].endswith('.py') and s['package'] in done) and
                not any(s['file'] in a.split() for a in own) and ':' not in s['file']]
        command = {**build(rest, sel['packages'], full, {p['root']: p.get('test_files', []) for p in sel['packages']},
                           levels, args.max_chars, go_flags=flags, go_flags_from=(go or {}).get('flags_from')),
                   **{k: v for k, v in command.items() if k in ('no_runner_reasons', 'build_first', 'feature_gated', 'not_run_by_default')}}
        command['commands'] += kept
        command['split_into'] = len(command['commands'])
    if args.ci:
        text = ci_snippet(command, args.ci)
        if args.ci_out:
            args.ci_out.write_text(text, encoding='utf-8')
            print(f'{args.ci} snippet -> {args.ci_out}')
        else:
            sys.stdout.write(text)
        return 0
    for b in command.get('build_first', []):
        print(f'(cd {b["cwd"]} && {" ".join(b["argv"])})    # build first: {b["why"]}')
    for c in command['commands']:
        print(f'(cd {c["cwd"]} && {" ".join(c["argv"])})')
    for pkg, why in command.get('no_runner_reasons', {}).items():
        print(f'# {pkg}: no command, {why}; run its selected tests with its own script')
    for line in not_run_lines(command):
        print('# ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
