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
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

LANG_DEFAULT = {'python': 'mutmut', 'javascript': 'stryker', 'csharp': 'stryker-net', 'jvm': 'pit', 'rust': 'cargo-mutants',
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


def languages(repo: Path) -> list[str]:
    seen = set()
    for p in repo.rglob('*'):
        if any(part in ('node_modules', '.git', '.venv', 'venv', 'build', 'dist', 'target') for part in p.parts):
            continue
        ext = p.suffix
        seen.update({'.py': ['python'], '.js': ['javascript'], '.ts': ['javascript'], '.tsx': ['javascript'],
                     '.jsx': ['javascript'], '.cs': ['csharp'], '.java': ['jvm'], '.kt': ['jvm'], '.rs': ['rust'],
                     '.go': ['go'], '.swift': ['swift'], '.dart': ['dart']}.get(ext, []))
    return sorted(seen)


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
    add('stryker', sj, (repo / 'node_modules' / '@stryker-mutator' / 'core').exists(),
        note='run with npx stryker run --reporters json,clear-text' if sj else '')
    sn = next((str(p.relative_to(repo)) for p in repo.rglob('stryker-config.json') if 'node_modules' not in p.parts), None)
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
    langs = languages(repo)
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
            recs.append({'language': lang, 'engine': default, 'config_source': 'scratch',
                         'why': f'standard open-source tool for {lang}; not configured, run from a scratch config if installed'})
    return {'repo': repo.name, 'platform': sys.platform, 'languages': langs, 'tools': tools, 'recommended': recs}


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
