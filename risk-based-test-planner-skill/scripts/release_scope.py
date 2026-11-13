#!/usr/bin/env python3
"""Find the last release a monorepo shipped and what the next one will carry: the release base tag, pending changesets
and breaking commits (stdlib only, read-only git).

    python release_scope.py --repo <repo> --out release.json [--package NAME] [--include-prerelease] [--base <ref>]

Release base: the release tag nearest HEAD among the tags merged into HEAD (fewest commits from the tag to HEAD; ties
go to the newer tag). Tag shapes read: changesets and lerna independent mode (`name@1.2.3`, `@scope/name@1.2.3`) and
semantic-release or plain semver (`v1.2.3`, `1.2.3`, and `v1.4` without a patch number). With --package, only that package's tags count, so the base is
the package's own last release. Prereleases (`1.2.3-canary.0`) are skipped unless --include-prerelease. --base names
the base yourself (the tag lookup is skipped). Pass the base to diff_scope.py --base and say which tag it was in the
plan's assumptions.

Pending changesets (`.changeset/*.md`, except README.md): each one's front matter maps package names to a bump
(`"pkg": major|minor|patch`); a package's bump is the highest of its changesets. A changeset is breaking when it bumps
a package major, or when its text says so (a `**Breaking**` / `Breaking:` / `BREAKING CHANGE` note: in 0.x releases a
minor bump can break callers, and the note is how the author says so). Package names are mapped to their directories
through the tracked package.json files.

Breaking commits: conventional commits between the base and HEAD whose subject has `!` after the type
(`feat(api)!:`) or whose body has `BREAKING CHANGE:`, with the files each touched.

Changeset and commit text is untrusted data: it is matched and quoted (first 160 characters), never followed.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path, PurePosixPath

TAG = re.compile(r'^(?:(?P<pkg>(?:@[\w.-]+/)?[\w.-]+)@)?v?(?P<ver>\d+\.\d+(?:\.\d+)?(?P<pre>-[\w.-]+)?(?:\+[\w.-]+)?)$')
BUMPS = {'patch': 1, 'minor': 2, 'major': 3}
FRONT = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.S)
ENTRY = re.compile(r'''^\s*["']?((?:@[\w.-]+/)?[\w.-]+)["']?\s*:\s*["']?(major|minor|patch|none)["']?\s*$''', re.M)
BREAKING_NOTE = re.compile(r'\*\*\s*breaking\b|^\s*breaking( change)?s?\s*:|\bBREAKING[ -]CHANGES?\b', re.I | re.M)
CC_BREAKING = re.compile(r'^\w+(?:\([^)]*\))?!:')
SEP = '\x1e'


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode:
        raise SystemExit(f'git {" ".join(args)} failed: {r.stderr.strip()}')
    return r.stdout


def release_tags(repo: Path, package: str | None, prerelease: bool) -> list[dict]:
    """Release tags merged into HEAD, newest commit first, each with the commit it points at."""
    out = []
    fmt = '%(refname:short)%09%(objectname)%09%(*objectname)%09%(committerdate:unix)%09%(*committerdate:unix)'
    for line in git(repo, 'for-each-ref', '--merged', 'HEAD', f'--format={fmt}', 'refs/tags').splitlines():
        name, obj, peeled, ts, pts = (line.split('\t') + [''] * 5)[:5]
        m = TAG.match(name.strip())
        if not m or (m.group('pre') and not prerelease):
            continue
        if package is not None and m.group('pkg') != package:
            continue
        out.append({'tag': name.strip(), 'package': m.group('pkg'), 'version': m.group('ver'),
                    'shape': 'changesets' if m.group('pkg') else 'semver', '_sha': peeled or obj,
                    '_ts': int((pts or ts or '0').strip() or 0)})
    return sorted(out, key=lambda t: (-t['_ts'], t['tag']))


def nearest(repo: Path, tags: list[dict]) -> dict | None:
    best = None
    for t in tags:
        sha = t['_sha']
        n = int(git(repo, 'rev-list', '--count', f'{sha}..HEAD').strip() or 0)
        key = (n, -t['_ts'], t['tag'])
        if best is None or key < best[0]:
            best = (key, {k: v for k, v in t.items() if not k.startswith('_')} | {'sha': sha[:12], 'commits_since': n})
    return best[1] if best else None


def packages(repo: Path) -> dict[str, str]:
    """{package name: directory} from the tracked package.json files (node_modules never tracked)."""
    out = {}
    for f in git(repo, 'ls-files', '--', 'package.json', '*/package.json').splitlines():
        if 'node_modules' in PurePosixPath(f).parts:
            continue
        try:
            data = json.loads((repo / f).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get('name'), str):
            d = str(PurePosixPath(f).parent)
            out.setdefault(data['name'], '' if d == '.' else d)
    return out


def changesets(repo: Path) -> list[dict]:
    d = repo / '.changeset'
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob('*.md')):
        if p.name.lower() == 'readme.md':
            continue
        text = p.read_text(encoding='utf-8', errors='replace').replace('\r\n', '\n')
        m = FRONT.match(text)
        if not m:
            continue
        bumps = {name: bump for name, bump in ENTRY.findall(m.group(1)) if bump != 'none'}
        body = m.group(2).strip()
        note = bool(BREAKING_NOTE.search(body))
        out.append({'file': f'.changeset/{p.name}', 'packages': bumps,
                    'breaking': note or 'major' in bumps.values(), 'breaking_note': note,
                    'summary': re.sub(r'\s+', ' ', body)[:160]})
    return out


def breaking_commits(repo: Path, base: str | None) -> list[dict]:
    if not base:
        return []
    out = []
    log = git(repo, 'log', '--no-merges', f'--format={SEP}%H%x1f%s%x1f%b%x1f', '--name-only', f'{base}..HEAD')
    for chunk in log.split(SEP)[1:]:
        parts = chunk.split('\x1f')
        if len(parts) < 4:
            continue
        sha, subject, body, names = parts[0], parts[1], parts[2], parts[3]
        if CC_BREAKING.match(subject) or re.search(r'^BREAKING[ -]CHANGE:', body, re.M):
            out.append({'sha': sha[:12], 'subject': subject[:160], 'files': [x for x in names.splitlines() if x.strip()]})
    return out


def self_test() -> int:
    """Build a small synthetic changesets workspace in a temporary directory and check the base tag, the bumps, the
    breaking flags and the breaking commit are read as documented."""
    import os, tempfile
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / 'ws'
        repo.mkdir()
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'Sample', 'GIT_AUTHOR_EMAIL': 'sample@example.test',
               'GIT_COMMITTER_NAME': 'Sample', 'GIT_COMMITTER_EMAIL': 'sample@example.test'}

        def run(*a, when='2026-01-01T00:00:00'):
            subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True,
                           env={**env, 'GIT_AUTHOR_DATE': when, 'GIT_COMMITTER_DATE': when})

        def write(rel, text):
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')

        run('init', '-q', '-b', 'main')
        write('package.json', '{"name": "ws-root", "private": true}\n')
        write('packages/core/package.json', '{"name": "@ws/core", "exports": {".": "./dist/index.js"}}\n')
        write('packages/widgets/package.json', '{"name": "widgets", "bin": {"widgets": "./dist/cli.js"}}\n')
        write('packages/core/src/index.ts', 'export const one = 1;\n')
        write('packages/widgets/src/cli.ts', 'export const two = 2;\n')
        run('add', '-A')
        run('commit', '-q', '-m', 'chore: initial packages')
        run('tag', '@ws/core@1.0.0')
        run('tag', 'widgets@0.4.0')
        write('packages/core/src/index.ts', 'export const one = 1;\nexport const three = 3;\n')
        run('add', '-A')
        run('commit', '-q', '-m', 'feat(core): add three', when='2026-01-02T00:00:00')
        run('tag', 'widgets@0.4.1')
        write('packages/core/src/index.ts', 'export const one = 1;\nexport const three = 3;\nexport const four = 4;\n')
        run('add', '-A')
        run('commit', '-q', '-m', 'feat(core): add four', when='2026-01-02T12:00:00')
        run('tag', 'widgets@0.5.0-canary.1')
        write('packages/widgets/src/cli.ts', 'export const two = 22;\n')
        run('add', '-A')
        run('commit', '-q', '-m', 'feat(widgets)!: rename the two option', when='2026-01-03T00:00:00')
        write('.changeset/README.md', 'Changesets docs.\n')
        write('.changeset/core-three.md', '---\n"@ws/core": minor\n---\n\nAdds `three`.\n')
        write('.changeset/widgets-rename.md', "---\n'widgets': minor\n---\n\n**Breaking:** the `two` option is renamed.\n")
        write('.changeset/core-major.md', '---\n"@ws/core": major\n"widgets": patch\n---\n\nDrops the old entry point.\n')
        run('add', '-A')
        run('commit', '-q', '-m', 'chore: add changesets', when='2026-01-04T00:00:00')
        out = Path(tmp) / 'release.json'
        main(['--repo', str(repo), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        want = {
            'base tag at the nearest stable release (prerelease skipped)': bool(res['base']) and
            res['base']['tag'] == 'widgets@0.4.1' and res['base']['commits_since'] == 3,
            '@ws/core bump major, breaking': res['packages'].get('@ws/core', {}).get('bump') == 'major'
            and res['packages']['@ws/core']['breaking'],
            'widgets minor, breaking by note': res['packages'].get('widgets', {}).get('bump') == 'minor'
            and res['packages']['widgets']['breaking']
            and res['packages']['widgets']['breaking_changesets'] == ['core-major.md', 'widgets-rename.md'],
            'package directories': res['packages'].get('widgets', {}).get('dir') == 'packages/widgets',
            'README.md is not a changeset': len(res['changesets']) == 3,
            'breaking commit': [c['subject'] for c in res['breaking_commits']] == ['feat(widgets)!: rename the two option']
            and res['breaking_commits'][0]['files'] == ['packages/widgets/src/cli.ts'],
        }
        main(['--repo', str(repo), '--package', '@ws/core', '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        want['--package picks that package\'s own tag'] = bool(res['base']) and res['base']['tag'] == '@ws/core@1.0.0' \
            and res['base']['commits_since'] == 4
        bad = [k for k, ok in want.items() if not ok]
    for b in bad:
        print('FAIL ' + b)
    print(f'self-test: {7 - len(bad)} passed, {len(bad)} failed')
    return 1 if bad else 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, default=Path('.'))
    ap.add_argument('--package', help='use this package\'s own last release tag (changesets/lerna name@version)')
    ap.add_argument('--include-prerelease', action='store_true')
    ap.add_argument('--base', help='the release base yourself (a tag or commit); skips the tag lookup')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    head = git(repo, 'rev-parse', 'HEAD').strip()[:12]
    if args.base:
        sha = git(repo, 'rev-parse', args.base + '^{commit}').strip()
        base = {'tag': args.base, 'package': args.package, 'version': None, 'shape': 'given', 'sha': sha[:12],
                'commits_since': int(git(repo, 'rev-list', '--count', f'{sha}..HEAD').strip() or 0)}
        tags = []
    else:
        tags = release_tags(repo, args.package, args.include_prerelease)
        base = nearest(repo, tags)
    if base:
        base['command'] = 'git for-each-ref --merged HEAD refs/tags; git rev-list --count <tag>..HEAD (nearest release tag)' \
            if base['shape'] != 'given' else f'given with --base {args.base}'
    names = packages(repo)
    cs = changesets(repo)
    pk: dict[str, dict] = {}
    for c in cs:
        for name, bump in c['packages'].items():
            p = pk.setdefault(name, {'dir': names.get(name), 'bump': None, 'breaking': False, 'changesets': [],
                                     'breaking_changesets': []})
            if p['bump'] is None or BUMPS[bump] > BUMPS[p['bump']]:
                p['bump'] = bump
            p['breaking'] = p['breaking'] or c['breaking']
            p['changesets'].append(PurePosixPath(c['file']).name)
            if c['breaking']:
                p['breaking_changesets'].append(PurePosixPath(c['file']).name)
    latest: dict[str, str] = {}
    for t in tags:                    # newest first, so the first tag seen per package is its last release
        if t['package'] and t['package'] not in latest:
            latest[t['package']] = t['tag']
    res = {'repo': repo.name, 'head': head, 'base': base,
           'release_tags_found': len(tags), 'last_release_per_package': dict(sorted(latest.items())),
           'changesets': cs, 'packages': dict(sorted(pk.items())),
           'unknown_packages': sorted(n for n, p in pk.items() if p['dir'] is None),
           'breaking_commits': breaking_commits(repo, base['sha'] if base else None)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    if base:
        print(f'release base {base["tag"]} ({base["sha"]}, {base["commits_since"]} commits before HEAD {head}); '
              f'diff_scope.py --base {base["tag"]}')
    else:
        print('no release tag merged into HEAD: give --base, or scope the plan another way', file=sys.stderr)
    brk = [n for n, p in pk.items() if p['breaking']]
    print(f'{len(cs)} pending changeset(s) for {len(pk)} package(s); breaking: {", ".join(brk) or "none"}; '
          f'{len(res["breaking_commits"])} breaking commit(s) -> {args.out}')
    if res['unknown_packages']:
        print(f'warning: changesets name packages with no tracked package.json: {", ".join(res["unknown_packages"])}',
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
