#!/usr/bin/env python3
"""Find mutation-testing tools a repo already configures, and which ones can run here (stdlib only).

    python detect_tools.py <repo> [--out tools.json]

Looks for: mutmut (pyproject [tool.mutmut], setup.cfg [mutmut]), Cosmic Ray (cosmic-ray.toml or
*.toml with [cosmic-ray]), StrykerJS (stryker.conf.* / stryker.config.*, @stryker-mutator in
package.json), Stryker.NET (stryker-config.json), PIT (pitest in pom.xml or Gradle files),
cargo-mutants (.cargo/mutants.toml, mutants.toml), Muter (muter.conf.yml), mutation_test for
Dart (mutation_test.xml or dev_dependencies), go-mutesting (go.mod). Each tool gets configured
(with the file), available (found on PATH or in node_modules, with the version when cheap to
ask) and a recommendation of which engine to use. Nothing is installed.

Languages come from one walk that prunes node_modules, build output and caches before
descending. A language is listed only with a project marker (pyproject.toml, setup.py,
requirements*.txt, package.json, tsconfig.json, go.mod, Cargo.toml, a .csproj, ...); files of a
language with no marker (a helper script) are counted under without_project_marker and get no
recommendation. JavaScript and TypeScript are listed separately; both use StrykerJS.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import js_workspace  # noqa: E402

LANG_DEFAULT = {'python': 'mutmut', 'javascript': 'stryker', 'typescript': 'stryker', 'csharp': 'stryker-net', 'jvm': 'pit', 'rust': 'cargo-mutants',
                'go': 'go-mutesting', 'swift': 'muter', 'dart': 'mutation_test'}
LICENCE = {'mutmut': 'BSD-3-Clause', 'cosmic-ray': 'MIT', 'stryker': 'Apache-2.0', 'stryker-net': 'Apache-2.0',
           'pit': 'Apache-2.0', 'cargo-mutants': 'MIT', 'go-mutesting': 'MIT', 'muter': 'MIT', 'mutation_test': 'BSD-3-Clause',
           'builtin': 'this skill (stdlib)'}


def read(p: Path) -> str:
    try:
        return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def version(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (res.stdout or res.stderr).strip()
    m = re.search(r'\d+\.\d+(\.\d+)?', out)
    return m.group(0) if res.returncode == 0 and m else None


SKIP = {'node_modules', '.git', '.venv', 'venv', 'build', 'dist', 'target', '.next', '.nuxt', 'out', 'coverage',
        '__pycache__', '.turbo', '.cache', '.stryker-tmp', 'mutants', '.gradle', '.idea', '.tox', '.mypy_cache',
        '.pytest_cache', 'Pods', 'DerivedData', 'bin', 'obj'}
LANG_BY_EXT = {'.py': 'python', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
               '.ts': 'typescript', '.tsx': 'typescript', '.mts': 'typescript', '.cts': 'typescript', '.cs': 'csharp',
               '.java': 'jvm', '.kt': 'jvm', '.rs': 'rust', '.go': 'go', '.swift': 'swift', '.dart': 'dart'}
# a language counts only with a project marker: one helper script is not a Python project
MARKERS = {'python': re.compile(r'^(pyproject\.toml|setup\.py|setup\.cfg|Pipfile|tox\.ini|requirements[\w.-]*\.txt)$'),
           'javascript': re.compile(r'^package\.json$'), 'typescript': re.compile(r'^(package\.json|tsconfig[\w.-]*\.json)$'),
           'csharp': re.compile(r'\.(csproj|sln)$'), 'jvm': re.compile(r'^(pom\.xml|build\.gradle(\.kts)?)$'),
           'rust': re.compile(r'^Cargo\.toml$'), 'go': re.compile(r'^go\.mod$'),
           'swift': re.compile(r'^Package\.swift$|\.xcodeproj$'), 'dart': re.compile(r'^pubspec\.yaml$')}


def walk(repo: Path) -> tuple[dict[str, int], set[str], list[str]]:
    """One pruned walk: file counts per language, the project markers seen, and stryker-config.json paths."""
    counts: dict[str, int] = {}
    marked: set[str] = set()
    net_cfgs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        names = dirnames + filenames
        for lang, rx in MARKERS.items():
            if any(rx.search(n) for n in names):
                marked.add(lang)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.endswith('.egg-info'))
        for f in filenames:
            lang = LANG_BY_EXT.get(os.path.splitext(f)[1])
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
            if f == 'stryker-config.json':
                net_cfgs.append(Path(dirpath, f).relative_to(repo).as_posix())
    return counts, marked, sorted(net_cfgs)


def languages(repo: Path, scan=None) -> tuple[list[str], dict[str, int]]:
    """Languages with source files and a project marker; and those with files but no marker."""
    counts, marked, _ = scan or walk(repo)
    return sorted(l for l in counts if l in marked), {l: n for l, n in sorted(counts.items()) if l not in marked}


def detect(repo: Path) -> dict:
    tools = []

    def add(name, configured, available, ver=None, note=''):
        tools.append({'name': name, 'licence': LICENCE[name], 'configured': configured, 'available': available,
                      'version': ver, 'note': note})

    pp, sc = read(repo / 'pyproject.toml'), read(repo / 'setup.cfg')
    cfg = 'pyproject.toml [tool.mutmut]' if re.search(r'^\[tool\.mutmut\]', pp, re.M) else (
        'setup.cfg [mutmut]' if re.search(r'^\[mutmut\]', sc, re.M) else None)
    mv = version(['mutmut', '--version']) if cfg else None
    avail = bool(shutil.which('mutmut'))
    add('mutmut', cfg, avail, mv, 'mutmut 3 needs fork(): Linux, macOS or WSL only' if os.name == 'nt' else '')
    cr = next((p.name for p in repo.glob('*.toml') if re.search(r'^\[cosmic-ray\]', read(p), re.M)), None)
    add('cosmic-ray', cr, bool(shutil.which('cosmic-ray')))
    sj = next((p.name for p in repo.glob('stryker.con*.*')), None)
    pkg = read(repo / 'package.json')
    if not sj and '@stryker-mutator' in pkg:
        sj = 'package.json (@stryker-mutator)'
    core = js_workspace.find_module(repo, '@stryker-mutator/core')
    add('stryker', sj, bool(core), js_workspace.module_version(repo, '@stryker-mutator/core') if core else None,
        note='run with npx stryker run --reporters json,clear-text' if sj else '')
    scan = walk(repo)
    sn = scan[2][0] if scan[2] else None
    add('stryker-net', sn, bool(shutil.which('dotnet-stryker')) or bool(version(['dotnet', 'stryker', '--version'])) if sn else False)
    pit = None
    for name in ('pom.xml', 'build.gradle', 'build.gradle.kts'):
        if re.search(r'pitest', read(repo / name), re.I):
            pit = name
    add('pit', pit, bool(pit), note='runs through the build (mvn org.pitest:pitest-maven:mutationCoverage or gradle pitest)' if pit else '')
    cm = next((p for p in ('.cargo/mutants.toml', 'mutants.toml') if (repo / p).exists()), None)
    add('cargo-mutants', cm, bool(version(['cargo', 'mutants', '--version'])) if (repo / 'Cargo.toml').exists() else False)
    add('go-mutesting', None, bool(shutil.which('go-mutesting')))
    mu = 'muter.conf.yml' if (repo / 'muter.conf.yml').exists() else None
    add('muter', mu, bool(shutil.which('muter')))
    mt = 'mutation_test.xml' if (repo / 'mutation_test.xml').exists() else (
        'pubspec.yaml (dev_dependencies)' if re.search(r'^\s+mutation_test:', read(repo / 'pubspec.yaml'), re.M) else None)
    add('mutation_test', mt, bool(mt))
    langs, unmarked = languages(repo, scan)
    node = node_facts(repo) if (repo / 'package.json').is_file() else None
    recs = []
    for lang in langs:
        default = LANG_DEFAULT[lang]
        configured = [t for t in tools if t['configured'] and (lang != 'python' or t['name'] in ('mutmut', 'cosmic-ray'))
                      and (lang == 'python' or t['name'] == default)]
        if configured:
            recs.append({'language': lang, 'engine': configured[0]['name'], 'config_source': 'repo',
                         'why': f'configured in {configured[0]["configured"]}'})
        elif lang == 'python':
            mm = next(t for t in tools if t['name'] == 'mutmut')
            ok = mm['available'] and os.name != 'nt'
            recs.append({'language': lang, 'engine': 'mutmut' if ok else 'builtin',
                         'config_source': 'scratch' if ok else 'builtin',
                         'why': 'mutmut is available; run it from a scratch config' if ok else
                         ('mutmut is not available here' if os.name != 'nt' else 'mutmut 3 does not run on Windows') +
                         '; use the builtin engine (stdlib, pytest only), or mutmut under WSL'})
        else:
            tool = next((t for t in tools if t['name'] == default), None)
            if default == 'stryker' and tool and tool['available']:
                how = 'installed; run_engine.py stryker runs it from a scratch config'
            elif default == 'stryker':
                runner = (node or {}).get('test_runner') or 'vitest'
                how = ('not installed for this package; run_engine.py stryker needs @stryker-mutator/core and the runner '
                       'plugin where the package resolves them. Installing changes package.json and the lockfile and '
                       'needs the user\'s agreement: ' + js_workspace.install_command(
                           repo, ['@stryker-mutator/core', f'@stryker-mutator/{runner}-runner']))
            else:
                how = 'not configured; run from a scratch config if installed (installing it needs the user\'s agreement)'
            recs.append({'language': lang, 'engine': default, 'config_source': 'scratch',
                         'why': f'standard open-source tool for {lang}; {how}'})
    return {'repo': repo.name, 'platform': sys.platform, 'languages': langs, 'files_by_language': scan[0],
            'without_project_marker': unmarked, 'tools': tools, 'recommended': recs, 'node': node}


def node_facts(repo: Path) -> dict:
    """What run_engine.py stryker and prove_kill.py will meet in a JS/TS package: the package manager,
    whether it sits inside a workspace (the sandbox is then mirrored), and the TypeScript and runner versions."""
    root = js_workspace.workspace_root(repo)
    pkg = js_workspace.read_json(repo / 'package.json')
    deps = {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}
    runner = next((r for r in ('vitest', 'jest', 'mocha', 'karma') if r in deps), None)
    ts = js_workspace.module_version(repo, 'typescript')
    out = {'package_manager': js_workspace.package_manager(repo),
           'workspace_root': os.path.relpath(root, repo).replace('\\', '/') if root else None,
           'test_runner': runner, 'test_runner_version': js_workspace.module_version(repo, runner) if runner else None,
           'typescript': ts, 'notes': []}
    if root:
        out['notes'].append('a package inside a workspace: run_engine.py stryker and prove_kill.py mirror the workspace '
                            'levels in the run directory so relative extends and pnpm links resolve')
    if (js_workspace.major(ts) or 0) >= 7:
        out['notes'].append(f'TypeScript {ts}: Stryker\'s tsconfig rewrite and the typescript checker cannot run; '
                            'run_engine.py disables them, so compile errors count as killed or survived')
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    out = detect(args.repo.resolve())
    text = json.dumps(out, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    for r in out['recommended']:
        print(f'{r["language"]}: {r["engine"]} ({r["config_source"]}) - {r["why"]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
