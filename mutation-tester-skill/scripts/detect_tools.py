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

Nested sample and fixture projects (a folder under examples/, fixtures/, testdata/, samples/ ... with its own
manifest or app: repo_layout.fixture_projects) are not the product: they are left out of the walk and the
language census and listed under fixtures_excluded. --include-fixtures counts them; --fixture-dir names another
fixture folder.

    python detect_tools.py <repo> [--out tools.json] [--include-fixtures] [--fixture-dir DIR]
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import js_workspace  # noqa: E402
import lang_census  # noqa: E402
import repo_layout  # noqa: E402

LANG_DEFAULT = {'python': 'mutmut', 'javascript': 'stryker', 'typescript': 'stryker', 'csharp': 'stryker-net', 'jvm': 'pit', 'rust': 'cargo-mutants',
                'go': 'go-mutesting', 'swift': 'muter', 'dart': 'mutation_test'}
LICENCE = {'mutmut': 'BSD-3-Clause', 'cosmic-ray': 'MIT', 'stryker': 'Apache-2.0', 'stryker-net': 'Apache-2.0',
           'pit': 'Apache-2.0', 'cargo-mutants': 'MIT', 'go-mutesting': 'MIT', 'gremlins': 'Apache-2.0', 'muter': 'MIT', 'mutation_test': 'BSD-3-Clause',
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


def walk(repo: Path, skip_dirs=()) -> tuple[dict[str, int], set[str], list[str]]:
    """One pruned walk: file counts per language, the project markers seen, and stryker-config.json paths.
    skip_dirs: repository-relative folders left out (fixture projects)."""
    counts: dict[str, int] = {}
    marked: set[str] = set()
    net_cfgs: list[str] = []
    skip_dirs = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(repo):
        here = Path(dirpath).relative_to(repo).as_posix()
        if skip_dirs:
            dirnames[:] = [d for d in dirnames if (d if here == '.' else f'{here}/{d}') not in skip_dirs]
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


def detect(repo: Path, include_fixtures: bool = False, fixture_dirs=()) -> dict:
    tools = []
    fixtures = [] if include_fixtures else repo_layout.fixture_projects(repo, fixture_dirs)
    fixture_set = {f['dir'] for f in fixtures}

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
    scan = walk(repo, fixture_set)
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
    gr = next((p for p in ('.gremlins.yaml', '.gremlins.yml') if (repo / p).exists()), None)
    add('gremlins', gr, bool(shutil.which('gremlins')),
        note='no mutation-testing-elements output: convert its JSON and pass it to normalize_results.py --mte' if gr else '')
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
                         '; use the builtin engine (stdlib; pytest or unittest), or mutmut under WSL'})
        elif lang == 'go':
            go_ok = bool(shutil.which('go'))
            recs.append({'language': lang, 'engine': 'builtin' if go_ok else default,
                         'config_source': 'builtin' if go_ok else 'scratch',
                         'why': ('the builtin engine mutates Go (builtin_mutator.py: Go tokens, go test -overlay, '
                                 'per-test coverprofiles); gremlins and go-mutesting are alternatives whose results '
                                 'are converted (normalize_results.py --mte) and count as external evidence'
                                 if go_ok else 'go is not on PATH, so no Go engine can run here')})
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
            elif default == 'cargo-mutants':
                how = ('installed; run_engine.py cargo-mutants runs it on the scoped .rs files and '
                       'normalize_results.py --cargo-mutants reads its outcomes.json' if tool and tool['available'] else
                       'not installed; installing it needs the user\'s agreement (cargo install --locked cargo-mutants), '
                       'then run_engine.py cargo-mutants runs it; until then Rust is not mutated')
            else:
                how = 'not configured; run from a scratch config if installed (installing it needs the user\'s agreement)'
            recs.append({'language': lang, 'engine': default, 'config_source': 'scratch',
                         'why': f'standard open-source tool for {lang}; {how}'})
    lc, tooling, warnings = language_facts(repo, tools, not include_fixtures, fixture_dirs)
    return {'repo': repo.name, 'platform': sys.platform, 'languages': langs, 'files_by_language': scan[0],
            'without_project_marker': unmarked, 'tools': tools, 'recommended': recs, 'node': node,
            'language_coverage': lc, 'tooling': tooling, 'warnings': warnings,
            'fixtures_excluded': fixtures, 'fixtures_included': include_fixtures}


# census language -> the tool that must be available here for a mutation engine to run on it
ENGINE_FOR = {'JavaScript': 'stryker', 'TypeScript': 'stryker', 'Rust': 'cargo-mutants', 'C#': 'stryker-net',
              'Java': 'pit', 'Kotlin': 'pit', 'Swift': 'muter', 'Dart': 'mutation_test'}
WHY_NOT = {'Rust': 'cargo-mutants is not installed (run_engine.py cargo-mutants runs it once it is: '
                   '`cargo install --locked cargo-mutants`, with the user\'s agreement)',
           'Go': 'go is not on PATH, so the builtin Go engine cannot run',
           'JavaScript': 'StrykerJS is not installed for this package',
           'TypeScript': 'StrykerJS is not installed for this package'}


def runnable_languages(tools: list[dict]) -> set[str]:
    """Census language names a mutation engine can run on here (Python always: the builtin engine)."""
    avail = {t['name'] for t in tools if t['available']}
    out = {'Python'}
    if shutil.which('go') or avail & {'gremlins', 'go-mutesting'}:
        out.add('Go')
    out |= {lang for lang, tool in ENGINE_FOR.items() if tool in avail}
    return out


def language_facts(repo: Path, tools: list[dict], exclude_fixtures: bool = True,
                   fixture_dirs=()) -> tuple[dict, dict, list[str]]:
    """(language coverage, tooling languages, warnings). The language with the most lines of product code (test
    files and tooling folders such as scripts/ counted apart: lang_census.py) decides: when no engine can run on
    it here, that is a loud warning, because mutating the other languages does not test the product. A language
    whose only non-test code sits in tooling folders while the product is elsewhere is named as tooling."""
    c = lang_census.census(repo, exclude_fixtures=exclude_fixtures, fixture_dirs=fixture_dirs)
    run_here = runnable_languages(tools)
    lc = lang_census.language_coverage(c, run_here, tool='the mutation engines that can run here',
                                       what='mutation test')
    lc.update(runnable_languages=sorted(run_here), product_lines=c.get('lines') or {}, dominant_by=c.get('dominant_by'))
    warnings = []
    if lc['status'] == 'unsupported_dominant':
        lang = lc['dominant_unsupported_language']
        n = (c.get('lines') or {}).get(lang, 0)
        why = WHY_NOT.get(lang) or ('this skill has no scripted engine for it: run the tool from SKILL.md\'s table by '
                                    'hand and convert its results (normalize_results.py --mte)')
        warnings.append(f'{lang} has the most product code ({n} lines; test code and tooling folders counted apart) '
                        f'and no mutation engine can run on it here: {why}. Mutating the other languages does not '
                        f'test the product; record {lang} as not mutated in the report\'s limitations.')
    tooling = {}
    if c.get('total_product_lines'):
        for lang, tl in (c.get('tooling_lines') or {}).items():
            if tl and not (c.get('lines') or {}).get(lang):
                dirs = sorted({d for d in (c.get('top_dirs') or {}).get(lang, {}) if d.lower() in lang_census.TOOLING_DIRS})
                tooling[lang] = {'tooling_files': (c.get('tooling_files') or {}).get(lang, 0), 'tooling_lines': tl,
                                 'test_lines': (c.get('test_lines') or {}).get(lang, 0), 'product_lines': 0,
                                 'dirs': [d + '/' for d in dirs]}
                warnings.append(f'the {lang} here is tooling, not the product: {tl} lines under '
                                f'{", ".join(d + "/" for d in dirs) or "tooling folders"} and no {lang} product code '
                                f'(the product is {c.get("dominant")}). Mutating it tests helper scripts; say so in '
                                'the report\'s limitations.')
    return lc, tooling, warnings


def under(path: str, dirs) -> bool:
    return any(path == d or path.startswith(d.rstrip('/') + '/') for d in dirs)


def report_language_coverage(repo: Path, mutated_files, scope_paths, product_paths=()) -> dict:
    """The report's metadata.language_coverage: the language with the most product lines, the languages this
    report's mutants are in, `unmutated_dominant` when the product's main language was not mutated (the report
    must then say so in a limitation), and the scoped files that sit in tooling folders while the product is
    elsewhere (`tooling_scope_files`: mutating them tests helper scripts, which a limitation must say).
    product_paths: folders the user declares are the product although their names read as tooling (a repository
    whose product is its scripts/); recorded as `declared_product`. Fixture projects are left out of the census."""
    product_paths = sorted({p.replace('\\', '/').strip('/') for p in product_paths or ()})
    c = lang_census.census(repo, exclude_fixtures=True)
    mutated = sorted({lang_census.LANG_BY_EXT.get(Path(f).suffix) for f in mutated_files} - {None})
    lc = lang_census.language_coverage(c, mutated, tool="this report's mutation engines", what='mutation test')
    tooling = sorted(f for f in scope_paths if lang_census.is_tooling_file(f) and not under(f, product_paths))         if c.get('total_product_lines') else []
    return {**({'declared_product': product_paths} if product_paths else {}),'dominant': c.get('dominant'), 'dominant_by': c.get('dominant_by'), 'product_lines': c.get('lines') or {},
            'mutated_languages': mutated, 'status': lc['status'],
            'unmutated_dominant': lc.get('dominant_unsupported_language') if lc['status'] == 'unsupported_dominant' else None,
            'tooling_scope_files': tooling, 'warning': lc.get('warning')}


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
    ap.add_argument('--include-fixtures', action='store_true',
                    help='count nested sample and fixture projects (under examples/, fixtures/, ...) as the product')
    ap.add_argument('--fixture-dir', action='append', default=[],
                    help='another folder (name or repository-relative path) whose projects are fixtures (repeatable)')
    args = ap.parse_args(argv)
    out = detect(args.repo.resolve(), args.include_fixtures, args.fixture_dir)
    text = json.dumps(out, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    for r in out['recommended']:
        print(f'{r["language"]}: {r["engine"]} ({r["config_source"]}) - {r["why"]}', file=sys.stderr)
    for w in out['warnings']:
        print(f'WARNING: {w}', file=sys.stderr)
    if out['fixtures_excluded']:
        print(f'left out {len(out["fixtures_excluded"])} fixture project(s): ' +
              ', '.join(f['dir'] for f in out['fixtures_excluded'][:5]) + ' (--include-fixtures counts them)',
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
