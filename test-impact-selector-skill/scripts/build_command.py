#!/usr/bin/env python3
"""Turn a selection into ready-to-run commands, one per package and framework (stdlib only).

    python build_command.py selection.json [--max-chars 8000] [--ci github|gitlab --ci-out snippet.yml]

Each command runs in its package directory with test paths relative to it: pytest and Vitest/Jest
take files, Playwright takes files, Cypress takes `--spec a,b`, Go takes package directories,
Gradle and Maven take test classes, xcodebuild takes `-only-testing:` targets. A command longer
than --max-chars (the Windows command-line limit is 8191) is split into several. When a package
runs its whole suite, the command has no test arguments. The CI snippet is printed or written
where you say, for you to add yourself; this script never edits CI configuration.

A JavaScript package's own `test` script decides the runner when it names one (vitest, jest,
mocha, playwright test, cypress run, node --test); a package whose script runs something else (a
repository's own CLI) gets no command, its selected tests are listed in no_runner, and
no_runner_reasons says why. build_first lists workspace packages to build before the commands,
when selected tests load a changed package through its build output (package.json main/exports
pointing at dist/).
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path, PurePosixPath

CYPRESS_RX = re.compile(r'\.cy\.[jt]sx?$')


def framework_for(test: str, level: str | None, frameworks: list[str]) -> str | None:
    ext = PurePosixPath(test).suffix
    fw = set(frameworks)
    if ext == '.py':
        return 'pytest'
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


def argv_for(fw: str, tests: list[str], full: bool) -> list[str]:
    base = {
        'pytest': ['python', '-m', 'pytest'], 'vitest': ['npx', 'vitest', 'run'], 'jest': ['npx', 'jest'],
        'mocha': ['npx', 'mocha'], 'playwright': ['npx', 'playwright', 'test'], 'cypress': ['npx', 'cypress', 'run'],
        'go-testing': ['go', 'test'], 'gradle': ['./gradlew', 'test'], 'maven': ['mvn', 'test'],
        'xctest': ['xcodebuild', 'test'], 'flutter_test': ['flutter', 'test'], 'rspec': ['bundle', 'exec', 'rspec'],
        'node-test': ['node', '--test'],
    }[fw]
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


def split(fw: str, tests: list[str], max_chars: int) -> list[list[str]]:
    chunks, cur = [], []
    for t in tests:
        trial = cur + [t]
        if cur and len(' '.join(argv_for(fw, trial, False))) > max_chars:
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


def build(selected: list[dict], packages: list[dict], full_packages: set[str], tests_by_package: dict[str, list[str]],
          levels: dict[str, str | None], max_chars: int = 8000, scripts: dict[str, str] | None = None,
          builds: list[dict] | None = None) -> dict:
    """selected: [{test_id, file, package}], ordered. full_packages: packages whose whole suite is selected.
    scripts: each JS package's own `test` script (package.json), which decides the runner where it names
    one; a package whose script runs something else gets no command, and its tests go to no_runner.
    builds: workspace packages to build first (their tests load another package's build output)."""
    by_pkg: dict[tuple[str, str], list[str]] = {}
    unknown = []
    reasons: dict[str, str] = {}
    fws = {p['root']: p.get('frameworks', []) for p in packages}
    for s in selected:
        pkg = s['package']
        rel = s['file'] if pkg == '.' else s['file'][len(pkg) + 1:]
        fw = framework_for(s['file'], levels.get(s['file']), fws.get(pkg, []))
        if fw is not None and scripts is not None:
            fw, why = script_runner(fw, scripts.get(pkg))
            if why:
                reasons[pkg] = why
        if fw is None:
            unknown.append(s['file'])
            continue
        by_pkg.setdefault((pkg, fw), []).append(rel)
    commands = []
    for (pkg, fw), tests in by_pkg.items():
        all_fw = [t for t in tests_by_package.get(pkg, []) if framework_for(t, levels.get(t), fws.get(pkg, [])) == fw]
        full = pkg in full_packages and len(tests) >= len(all_fw)
        chunks = [tests] if full else split(fw, tests, max_chars)
        for i, chunk in enumerate(chunks, 1):
            commands.append({'package': pkg, 'cwd': pkg, 'framework': fw, 'full_suite': full,
                             'argv': argv_for(fw, chunk, full), 'tests': len(chunk), 'part': i, 'parts': len(chunks)})
    out = {'commands': commands, 'split_into': len(commands), 'max_chars': max_chars, 'no_runner': unknown}
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
        command = {**build(sel['selected'], sel['packages'], full, {p['root']: p.get('test_files', []) for p in sel['packages']},
                           levels, args.max_chars),
                   **{k: v for k, v in command.items() if k in ('no_runner_reasons', 'build_first')}}
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
