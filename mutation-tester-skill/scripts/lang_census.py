#!/usr/bin/env python3
"""Count a repository's source files per language, leaving out vendored and third-party trees (stdlib only).

    python lang_census.py <repo> [--supported Go,Python] [--tool scan_x.py] [--out census.json]
    python lang_census.py --self-test

Skills that analyse only some languages call this before they report, so that a handful of incidental
files in a supported language is never presented as an analysis of a repository written in another one.

What is left out, before any file is counted (directories are pruned before they are walked):
  * dependency, VCS, cache and build output directories: vendor/, node_modules/, third_party/, Pods/,
    Carthage/, bower_components/, .git, dist/, build/, out/, target/, DerivedData/, __pycache__, ...;
  * bundles and header trees: *.app, *.framework, *.xcframework, *.bundle, *.xcassets, Headers/,
    PrivateHeaders/;
  * git submodules (the paths in .gitmodules);
  * embedded third-party packages: a nested directory with its own package manifest (package.json, go.mod,
    Cargo.toml, pyproject.toml, setup.py, *.podspec, Package.swift, pom.xml, build.gradle) AND its own
    LICENSE/COPYING file, under a directory that holds drivers, SDKs or external code (drivers/, driver/,
    sdk/, sdks/, external/, extern/, deps/, contrib/, third-party/, vendored/). A workspace package
    (packages/web with a LICENSE) is product code and stays counted;
  * a header-only tree (10+ .h files, no .c/.m/.cpp next to them) under such a directory.

Every exclusion is listed with its reason and file count, so a reader can check none of it is product code.

Library use:
    import lang_census
    c = lang_census.census(repo)                     # {'languages': {'Go': 225, ...}, 'dominant': 'Go', ...}
    cov = lang_census.language_coverage(c, {'Python', 'JavaScript', 'TypeScript'}, tool='scan_x.py')
    if cov['status'] == 'unsupported_dominant':
        print(cov['warning'], file=sys.stderr)       # and put cov in the output as `language_coverage`
    lang_census.excluded_path(c, 'drivers/ios/Agent/x.ts')   # the exclusion reason, or None
"""
from __future__ import annotations
import argparse, json, os, re, sys, tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

LANG_BY_EXT = {
    '.py': 'Python', '.pyi': 'Python',
    '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript', '.mts': 'TypeScript', '.cts': 'TypeScript',
    '.vue': 'Vue', '.svelte': 'Svelte',
    '.go': 'Go', '.rs': 'Rust',
    '.java': 'Java', '.kt': 'Kotlin', '.kts': 'Kotlin', '.scala': 'Scala', '.groovy': 'Groovy',
    '.cs': 'C#', '.fs': 'F#', '.vb': 'Visual Basic',
    '.swift': 'Swift', '.m': 'Objective-C', '.mm': 'Objective-C',
    '.c': 'C', '.h': 'C/C++ header', '.cc': 'C++', '.cpp': 'C++', '.cxx': 'C++', '.hpp': 'C++', '.hh': 'C++',
    '.rb': 'Ruby', '.php': 'PHP', '.dart': 'Dart',
    '.ex': 'Elixir', '.exs': 'Elixir', '.erl': 'Erlang', '.clj': 'Clojure', '.cljs': 'Clojure',
    '.hs': 'Haskell', '.ml': 'OCaml', '.mli': 'OCaml', '.lua': 'Lua', '.pl': 'Perl', '.pm': 'Perl',
    '.r': 'R', '.R': 'R', '.jl': 'Julia', '.zig': 'Zig', '.nim': 'Nim', '.cr': 'Crystal', '.elm': 'Elm',
}

PRUNE_DIRS = {'.git', '.hg', '.svn', 'node_modules', 'vendor', 'vendored', 'third_party', 'third-party', 'thirdparty',
              'bower_components', 'jspm_packages', 'Pods', 'Carthage', '.venv', 'venv', '.tox', '.nox',
              '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'site-packages', 'dist',
              'target', 'DerivedData', '.gradle', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache',
              'coverage', 'htmlcov', '.idea', '.vscode', 'Headers', 'PrivateHeaders'}
# build output names that are also plausible Go package names: left out unless they hold .go files themselves
BUILD_OUTPUT = {'build', 'out', 'obj'}
BUNDLE_SUFFIXES = ('.app', '.framework', '.xcframework', '.bundle', '.xcassets', '.appex', '.dSYM', '.egg-info')
# directories under which a nested package with its own licence is someone else's code
EXTERNAL_PARENTS = {'drivers', 'driver', 'sdk', 'sdks', 'external', 'externals', 'extern', 'deps', 'contrib',
                    'third-party', 'third_party', 'thirdparty', 'vendored', 'lib-external'}
MANIFESTS = ('package.json', 'go.mod', 'Cargo.toml', 'pyproject.toml', 'setup.py', 'Package.swift', 'pom.xml',
             'build.gradle', 'build.gradle.kts', 'composer.json', 'Gemfile', 'mix.exs', 'pubspec.yaml')
LICENSE_RX = re.compile(r'^(LICEN[CS]E|COPYING|COPYRIGHT)([.-].*)?$', re.I)
HEADER_ONLY_MIN = 10


def submodules(root: Path) -> set[str]:
    try:
        text = (root / '.gitmodules').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return set()
    return {m.group(1).strip().strip('/') for m in re.finditer(r'^\s*path\s*=\s*(.+)$', text, re.M)}


def exclusion_reason(rel_dir: str, name: str, files: list[str], subs: set[str]) -> str | None:
    """Why the directory rel_dir (its last part is name; files are its file names) is not product code, or None."""
    if name in PRUNE_DIRS:
        return f'{name}/ is a dependency, VCS, cache, build-output or header directory'
    if name in BUILD_OUTPUT and not any(f.endswith('.go') for f in files):
        return f'{name}/ is a build-output directory'
    if name.endswith(BUNDLE_SUFFIXES):
        return f'{name} is a built bundle'
    if rel_dir in subs:
        return 'a git submodule (.gitmodules)'
    parts = rel_dir.split('/')
    under_external = any(p.lower() in EXTERNAL_PARENTS for p in parts[:-1])
    if under_external:
        manifest = [m for m in MANIFESTS if m in files] + sorted(f for f in files if f.endswith('.podspec'))
        licence = sorted(f for f in files if LICENSE_RX.match(f))
        if manifest and licence:
            return (f'an embedded third-party package (its own {manifest[0]} and {licence[0]}) under '
                    f'{"/".join(p for p in parts[:-1] if p.lower() in EXTERNAL_PARENTS)}/')
        exts = Counter(PurePosixPath(f).suffix for f in files)
        if exts['.h'] >= HEADER_ONLY_MIN and not (exts['.c'] or exts['.m'] or exts['.mm'] or exts['.cpp'] or exts['.cc']):
            return f'a header-only tree ({exts[".h"]} .h files, no implementation) under a driver/SDK directory'
    return None


def census(root: Path | str, extra_exclude: list[str] | None = None) -> dict:
    """Source files per language under root, with vendored and third-party trees left out (see the module doc).
    extra_exclude: repo-relative directories the caller also knows are not product code."""
    root = Path(root).resolve()
    subs = submodules(root)
    extra = {e.strip('/') for e in (extra_exclude or [])}
    langs: Counter = Counter()
    by_lang_dirs: dict[str, Counter] = {}
    excluded: list[dict] = []
    for dp, dns, fns in os.walk(root):
        rel = Path(dp).relative_to(root).as_posix()
        rel = '' if rel == '.' else rel
        keep = []
        for d in sorted(dns):
            sub = f'{rel}/{d}' if rel else d
            try:
                files = [f for f in os.listdir(os.path.join(dp, d))]
            except OSError:
                files = []
            why = 'excluded by the caller' if sub in extra else exclusion_reason(sub, d, files, subs)
            if why:
                if d not in ('.git', '.hg', '.svn', '__pycache__'):   # noise nobody needs listed
                    excluded.append({'path': sub, 'reason': why, 'source_files': count_sources(Path(dp) / d)})
            else:
                keep.append(d)
        dns[:] = keep
        for f in fns:
            lang = LANG_BY_EXT.get(PurePosixPath(f).suffix)
            if lang:
                langs[lang] += 1
                top = rel.split('/')[0] if rel else '.'
                by_lang_dirs.setdefault(lang, Counter())[top] += 1
    ordered = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    dominant = next(iter(ordered), None)
    return {'root': root.name, 'languages': ordered, 'total_source_files': sum(langs.values()),
            'dominant': dominant, 'dominant_files': ordered.get(dominant, 0) if dominant else 0,
            'top_dirs': {k: dict(v.most_common(5)) for k, v in by_lang_dirs.items()},
            'excluded': excluded,
            'excluded_source_files': sum(e['source_files'] for e in excluded)}


def count_sources(d: Path, cap: int = 200000) -> int:
    n = 0
    for _, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in ('.git', 'node_modules')]
        n += sum(1 for f in fns if PurePosixPath(f).suffix in LANG_BY_EXT)
        if n > cap:
            break
    return n


def excluded_path(c: dict, rel: str) -> str | None:
    """The reason rel (a repo-relative path) lies in an excluded tree of census c, or None."""
    rel = rel.replace('\\', '/').strip('/')
    for e in c.get('excluded', []):
        if rel == e['path'] or rel.startswith(e['path'] + '/'):
            return e['reason']
    return None


def language_coverage(c: dict, supported, tool: str = 'this script', what: str = 'full analysis',
                      min_files: int = 3) -> dict:
    """Supported against unsupported source-file counts for a tool that analyses only `supported` languages.

    status 'unsupported_dominant' (with a loud `warning`) when the largest unsupported language has more files than
    all supported languages together (and at least min_files); 'partial' when some unsupported languages are present
    but do not dominate; 'ok' when everything counted is supported; 'no_source' when nothing was counted."""
    supported = set(supported)
    langs = c.get('languages', {})
    sup = {k: v for k, v in langs.items() if k in supported}
    uns = {k: v for k, v in langs.items() if k not in supported}
    n_sup, n_uns = sum(sup.values()), sum(uns.values())
    out = {'tool': tool, 'supported_languages': sorted(supported), 'supported': sup, 'unsupported': uns,
           'supported_files': n_sup, 'unsupported_files': n_uns, 'dominant': c.get('dominant'),
           'excluded_source_files': c.get('excluded_source_files', 0), 'status': 'ok', 'warning': None}
    if not langs:
        out['status'] = 'no_source'
        return out
    if uns:
        lang, n = max(uns.items(), key=lambda kv: (kv[1], kv[0]))
        if n > n_sup and n >= min_files:
            out['status'] = 'unsupported_dominant'
            out['dominant_unsupported_language'], out['dominant_unsupported_files'] = lang, n
            out['warning'] = (f'This repository is predominantly {lang} ({n} source files, vendored and third-party '
                              f'trees excluded), which {tool} does not analyse: it covers '
                              f'{", ".join(sorted(supported)) or "no language"} only. {n_sup} supported source '
                              f'file(s) were found, so its output covers that minority and is NOT a {what} of this '
                              f'repository\'s {lang} code. Do not present it as one; record {lang} as not assessed.')
        else:
            out['status'] = 'partial'
    return out


def self_test() -> int:
    checks = []

    def tree(files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp(prefix='lang-census-'))
        for rel, body in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(body, encoding='utf-8')
        return d

    # a Go CLI that embeds a third-party iOS driver (TypeScript tooling, Objective-C, a header tree), a vendored Go
    # module, a built app bundle and node_modules
    files = {'go.mod': 'module example.test/kiosk\n', 'main.go': 'package main\n'}
    files.update({f'pkg/runner/f{i}.go': 'package runner\n' for i in range(12)})
    files.update({f'pkg/runner/f{i}_test.go': 'package runner\n' for i in range(6)})
    files.update({'drivers/ios/AgentKit/package.json': '{"name": "agent-kit"}', 'drivers/ios/AgentKit/LICENSE': 'BSD'})
    files.update({f'drivers/ios/AgentKit/lib/m{i}.ts': '' for i in range(30)})
    files.update({f'drivers/ios/AgentKit/Agent/Lib/c{i}.m': '' for i in range(25)})
    files.update({f'drivers/ios/AgentKit/PrivateHeaders/XCTest/XC{i}.h': '' for i in range(60)})
    files.update({f'drivers/ios/prebuilt/Runner.app/Frameworks/XCTest.framework/Headers/H{i}.h': '' for i in range(50)})
    files.update({f'drivers/ios/headers-only/XCT{i}.h': '' for i in range(40)})
    files.update({f'vendor/example.test/dep/d{i}.go': 'package dep\n' for i in range(40)})
    files.update({f'npm/wrapper/node_modules/x/i{i}.js': '' for i in range(40)})
    files.update({'npm/wrapper/package.json': '{"name": "kiosk"}', 'npm/wrapper/install.js': ''})
    root = tree(files)
    c = census(root)
    checks.append(('a Go repo with a big vendored driver tree is Go-dominant', c['dominant'] == 'Go'
                   and c['languages'].get('Go') == 19, (c['dominant'], c['languages'])))
    reasons = {e['path']: e['reason'] for e in c['excluded']}
    checks.append(('the embedded third-party package is excluded with its manifest and licence named',
                   'embedded third-party package' in reasons.get('drivers/ios/AgentKit', ''), reasons))
    checks.append(('a built .app bundle, vendor/ and node_modules/ are excluded',
                   all(p in reasons for p in ('drivers/ios/prebuilt/Runner.app', 'vendor', 'npm/wrapper/node_modules')),
                   sorted(reasons)))
    checks.append(('a header-only tree under drivers/ is excluded', 'header-only' in reasons.get('drivers/ios/headers-only', ''),
                   reasons))
    checks.append(('excluded_path finds a file inside an excluded tree',
                   bool(excluded_path(c, 'drivers/ios/AgentKit/lib/m1.ts')) and not excluded_path(c, 'pkg/runner/f1.go'),
                   None))
    cov = language_coverage(c, {'Python', 'JavaScript', 'TypeScript'}, tool='scan_x.py')
    checks.append(('a JS/Python-only tool on the Go repo is flagged unsupported_dominant',
                   cov['status'] == 'unsupported_dominant' and cov['dominant_unsupported_language'] == 'Go'
                   and 'NOT a full analysis' in cov['warning'], cov))
    cov = language_coverage(c, {'Go'}, tool='scan_go.py')
    checks.append(('a Go tool on the Go repo is ok or partial, never unsupported_dominant',
                   cov['status'] in ('ok', 'partial'), cov['status']))
    # a JavaScript workspace whose packages carry their own LICENSE: product code, kept
    ws = {'package.json': '{"workspaces": ["packages/*"]}'}
    for p in ('web', 'api', 'ui'):
        ws.update({f'packages/{p}/package.json': '{}', f'packages/{p}/LICENSE': 'MIT'})
        ws.update({f'packages/{p}/src/f{i}.ts': '' for i in range(5)})
    ws.update({f'scripts/s{i}.py': '' for i in range(4)})
    c2 = census(tree(ws))
    checks.append(('workspace packages with their own LICENSE stay counted', c2['languages'].get('TypeScript') == 15
                   and not c2['excluded'], (c2['languages'], c2['excluded'])))
    # a git submodule is excluded
    sm = {'.gitmodules': '[submodule "ext"]\n\tpath = libs/ext\n\turl = https://example.test/ext.git\n',
          'app.py': '', 'core.py': ''}
    sm.update({f'libs/ext/e{i}.c': '' for i in range(9)})
    c3 = census(tree(sm))
    checks.append(('a git submodule is excluded', c3['dominant'] == 'Python' and 'libs/ext' in
                   {e['path'] for e in c3['excluded']}, (c3['languages'], c3['excluded'])))
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--supported', help='comma-separated languages the calling tool analyses, e.g. "Go,Python"')
    ap.add_argument('--tool', default='this tool')
    ap.add_argument('--exclude', action='append', default=[], metavar='DIR', help='also leave out this directory')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    c = census(args.repo, args.exclude)
    if args.supported is not None:
        c['language_coverage'] = language_coverage(c, [s.strip() for s in args.supported.split(',') if s.strip()],
                                                   tool=args.tool)
    text = json.dumps(c, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    langs = ', '.join(f'{k} {v}' for k, v in list(c['languages'].items())[:6]) or 'none'
    print(f'dominant: {c["dominant"]}; {langs}; {len(c["excluded"])} tree(s) excluded '
          f'({c["excluded_source_files"]} source files)', file=sys.stderr)
    cov = c.get('language_coverage')
    if cov and cov['warning']:
        print('WARNING: ' + cov['warning'], file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
