#!/usr/bin/env python3
"""Find the commit that owns each failure: the change since the last green run that touched what failed.

    python owning_change.py --run run/ --repo <checkout> --out run/changes.json

The range is the commits after the last green run on the branch, up to the failing commit
(`git rev-list last_green..commit`). For each failure, every commit in range is scored by the files it
shares with the failure: a traceback frame (3), the test file (2), a module the test imports (2), a data
file the test names (2). The highest score owns it; ties go to the most recent commit and are listed.
A failure that shares no file with any commit in range is `not_identified`: it was there before the
range (a flaky test, an outage) or lies outside the repository.
"""
from __future__ import annotations
import argparse, ast, json, re, subprocess, sys
from pathlib import Path, PurePosixPath


def git(repo: Path, *args) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
    if r.returncode:
        raise SystemExit(f'git {" ".join(args)}: {r.stderr.strip()}')
    return r.stdout


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


def data_files(src: str, test_file: str, tree: set[str]) -> set[str]:
    """Data files under the test's directory whose name appears in the test source."""
    base = str(PurePosixPath(test_file).parent)
    return {f for f in tree if f.startswith(base + '/') and re.search(r'\.(json|ya?ml|csv|sql|txt|xml)$', f) and PurePosixPath(f).name in src}


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
            touch[fr['file']] = (3, 'traceback frame')
        tf = f.get('test_file')
        if tf and tf in tree:
            touch.setdefault(tf, (2, 'test file'))
            src = git(a.repo, 'show', f'{head}:{tf}')
            for m in imports(src, tree):
                touch.setdefault(m, (2, 'module the test imports'))
            for d in data_files(src, tf, tree):
                touch.setdefault(d, (2, 'data file the test reads'))
        scored = []
        for c in commits:
            shared = [{'file': x, 'why': touch[x][1]} for x in c['files'] if x in touch]
            if shared:
                scored.append((sum(touch[s['file']][0] for s in shared), commits.index(c), c, shared))
        if not scored:
            per[f['id']] = {'status': 'not_identified', 'basis': f'no commit in {len(commits)} commit(s) since the last green run touches the files of this failure',
                            'files_considered': sorted(touch)}
            continue
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        top = scored[0]
        per[f['id']] = {'status': 'identified', 'commit': top[2]['commit'], 'subject': top[2]['subject'], 'score': top[0], 'files_shared': top[3],
                        'ties': [s[2]['commit'] for s in scored[1:] if s[0] == top[0]], 'others': [{'commit': s[2]['commit'], 'score': s[0]} for s in scored[1:]],
                        'files_considered': sorted(touch)}
    out = {'range': {'from': green, 'to': head, 'spec': rng}, 'commits': commits, 'per_failure': per}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(commits)} commit(s) in {rng[:20]}...; ' + ', '.join(f'{k}: {v.get("commit", "not identified")[:8]}' for k, v in per.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
