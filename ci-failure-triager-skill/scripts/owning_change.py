#!/usr/bin/env python3
"""Find the commit that owns each failure: the change since the last green run that touched what failed.

    python owning_change.py --run run/ --repo <checkout> --out run/changes.json

The range is the commits after the last green run of the same workflow on the branch, up to the failing
commit (`git rev-list last_green..commit`). For each failure, every commit in range is scored by the files
it shares with the failure: a traceback frame (3), the test file (2), a module the test imports (2; Python
imports and relative JavaScript/TypeScript imports), a data file the test names (2). Paths a test runner
prints relative to its package (a monorepo) are matched to the one file in the tree that ends with them.
In a monorepo only commits that touch the failing package (the nearest folder above the test file or frame
with a package manifest) can own it. The highest score owns it; ties go to the most recent commit and are
listed. A failure that shares no file with any commit in range is `not_identified`: it was there before the
range (a flaky test, an outage) or lies outside the repository.

A failing commit or last green commit that is not in the checkout (a pull request head, a shallow clone)
is not an error to hide: changes.json is still written, with `commits: null`, every failure
`not_identified`, and the git command that would fetch it.
"""
from __future__ import annotations
import argparse, ast, json, posixpath, re, subprocess, sys
from pathlib import Path, PurePosixPath

MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts')
JS_EXT = ('', '.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '/index.ts', '/index.tsx', '/index.js', '/index.mjs')


def git(repo: Path, *args, check: bool = True) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
    if r.returncode and check:
        raise SystemExit(f'git {" ".join(args)}: {r.stderr.strip()}')
    return r.stdout if not r.returncode else ''


def has_commit(repo: Path, sha: str) -> bool:
    return subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', f'{sha}^{{commit}}'], capture_output=True).returncode == 0


def imports(src: str, tree: set[str]) -> set[str]:
    out = set()
    try:
        mod = ast.parse(src)
    except SyntaxError:
        return out
    for n in ast.walk(mod):
        names = [n.module] if isinstance(n, ast.ImportFrom) and n.module else [a.name for a in n.names] if isinstance(n, ast.Import) else []
        for name in names:
            parts = name.split('.')
            for k in range(len(parts), 0, -1):
                for cand in ('/'.join(parts[:k]) + '.py', '/'.join(parts[:k]) + '/__init__.py', 'src/' + '/'.join(parts[:k]) + '.py'):
                    if cand in tree:
                        out.add(cand)
                        break
    return out


def js_imports(src: str, test_file: str, tree: set[str]) -> set[str]:
    """Relative imports of a JavaScript or TypeScript test, resolved against the tree."""
    out = set()
    base = posixpath.dirname(test_file)
    for spec in re.findall(r'''(?:from\s+|import\s*\(\s*|require\s*\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]''', src):
        p = posixpath.normpath(posixpath.join(base, spec))
        stem = re.sub(r'\.(m|c)?js$', '', p)
        for cand in [p + e for e in JS_EXT] + [stem + e for e in JS_EXT[1:]]:
            if cand in tree:
                out.add(cand)
                break
    return out


def data_files(src: str, test_file: str, tree: set[str]) -> set[str]:
    """Data files under the test's directory whose name appears in the test source."""
    base = str(PurePosixPath(test_file).parent)
    return {f for f in tree if f.startswith(base + '/') and re.search(r'\.(json|ya?ml|csv|sql|txt|xml)$', f) and PurePosixPath(f).name in src}


def in_tree(path: str | None, tree: set[str]) -> str | None:
    """The tree path for a path as a runner printed it: exact, or the one file ending with it."""
    if not path:
        return None
    p = path.replace('\\', '/').lstrip('./')
    if p in tree:
        return p
    ends = [t for t in tree if t.endswith('/' + p)]
    return ends[0] if len(ends) == 1 else None


def package_of(path: str, tree: set[str]) -> str | None:
    """The nearest folder above a file holding a package manifest ('' for the root), or None."""
    d = posixpath.dirname(path)
    while True:
        if any((f'{d}/{m}' if d else m) in tree for m in MANIFESTS):
            return d
        if not d:
            return None
        d = posixpath.dirname(d)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    fails = json.loads((a.run / 'failures.json').read_text(encoding='utf-8'))['failures']
    head, green = run['run']['commit'], (run.get('last_green') or {}).get('commit')
    rng = f'{green}..{head}' if green else head
    absent = [(what, sha) for what, sha in (('failing commit', head), ('last green commit', green)) if sha and not has_commit(a.repo, sha)]
    if absent:
        how = []
        for what, sha in absent:
            if what == 'failing commit' and run['run'].get('event') in ('pull_request', 'pull_request_target'):
                how.append(f'{what} {sha[:12]} is a pull request head: git fetch origin {sha} (or git fetch origin pull/<number>/head)')
            else:
                how.append(f'{what} {sha[:12]} is not in the checkout: git fetch origin {sha} (or git fetch --unshallow)')
        basis = '; '.join(how)
        out = {'range': {'from': green, 'to': head, 'spec': rng}, 'commits': None, 'problem': basis,
               'per_failure': {f['id']: {'status': 'not_identified', 'basis': f'owning change not assessed: {basis}', 'files_considered': []} for f in fails}}
        a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
        print(f'WARNING: {basis}; wrote {a.out} with every owning change not_identified', file=sys.stderr)
        return 0
    commits = []
    for sha in git(a.repo, 'rev-list', '--reverse', rng).split():
        subject = git(a.repo, 'show', '-s', '--format=%s', sha).strip()
        files = [f for f in git(a.repo, 'show', '--format=', '--name-only', sha).split('\n') if f]
        commits.append({'commit': sha, 'subject': subject, 'files': files})
    tree = set(git(a.repo, 'ls-tree', '-r', '--name-only', head).split('\n')) - {''}
    per = {}
    for f in fails:
        touch: dict[str, tuple[int, str]] = {}
        for fr in f.get('frames', []):
            p = in_tree(fr['file'], tree)
            if p:
                touch[p] = (3, 'traceback frame')
        tf = in_tree(f.get('test_file'), tree)
        if tf:
            touch.setdefault(tf, (2, 'test file'))
            src = git(a.repo, 'show', f'{head}:{tf}')
            for m in (imports(src, tree) if tf.endswith('.py') else js_imports(src, tf, tree)):
                touch.setdefault(m, (2, 'module the test imports'))
            for d in data_files(src, tf, tree):
                touch.setdefault(d, (2, 'data file the test reads'))
        anchors = [tf] if tf else [p for p, (w, _) in touch.items() if w == 3]
        pkgs = {package_of(p, tree) for p in anchors} - {None}
        pkg = next(iter(pkgs)) if len(pkgs) == 1 else None
        scored = []
        for c in commits:
            if pkg and not any(x.startswith(pkg + '/') for x in c['files']):
                continue  # a monorepo: only commits touching the failing package can own it
            shared = [{'file': x, 'why': touch[x][1]} for x in c['files'] if x in touch]
            if shared:
                scored.append((sum(touch[s['file']][0] for s in shared), commits.index(c), c, shared))
        extra = {'package': pkg} if pkg else {}
        if not scored:
            why = (f'no commit in {len(commits)} commit(s) since the last green run touches the files of this failure' if touch else
                   'no file of this failure is known (a step failure with no traceback or test file in its output), so no commit can be tied to it')
            per[f['id']] = {'status': 'not_identified', 'basis': why, 'files_considered': sorted(touch), **extra}
            continue
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        top = scored[0]
        per[f['id']] = {'status': 'identified', 'commit': top[2]['commit'], 'subject': top[2]['subject'], 'score': top[0], 'files_shared': top[3],
                        'ties': [s[2]['commit'] for s in scored[1:] if s[0] == top[0]], 'others': [{'commit': s[2]['commit'], 'score': s[0]} for s in scored[1:]],
                        'files_considered': sorted(touch), **extra}
    out = {'range': {'from': green, 'to': head, 'spec': rng}, 'commits': commits, 'per_failure': per}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(commits)} commit(s) in {rng[:20]}...; ' + ', '.join(f'{k}: {v.get("commit", "not identified")[:8]}' for k, v in per.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
