#!/usr/bin/env python3
"""Find hard-coded secrets in a repository, and optionally in its git history, without printing them.

    python scan_secrets.py <repo> [--history] [--max-commits 5000] [--out secrets.json]

Output records give the type, file, line, variable name, length, entropy and a fingerprint
(the first 16 hex characters of the value's SHA-256). The value itself is never written: the
fingerprint is enough to tell whether the same secret appears elsewhere, including in history.

--history scans lines added in every commit (git log --all -p) and reports, for each secret at
HEAD, whether it is in history, plus secrets that were removed from HEAD but remain in history
(still exposed: they must be rotated). gitleaks and trufflehog are more thorough; use them when
available and merge their SARIF with merge_findings.py.
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', 'coverage', 'vendor', 'Pods', '.gradle',
             '__pycache__', '.venv', 'venv', '.tox', '.mypy_cache', '.pytest_cache', 'site-packages', 'target',
             '.terraform', '.idea', '.vscode'}
SKIP_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.pdf', '.zip', '.gz', '.tgz', '.jar', '.class', '.so',
            '.dll', '.exe', '.woff', '.woff2', '.ttf', '.eot', '.mp4', '.mp3', '.lock', '.map', '.sqlite', '.db', '.pyc'}
SKIP_FILES = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Cargo.lock', 'go.sum', 'composer.lock'}
TEST_PATH = re.compile(r'(^|/)(tests?|spec|__tests__|fixtures|testdata)(/|$)|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$', re.I)

# (type, regex, value group). Specific token formats first; the generic assignment last.
RULES = [
    ('aws_access_key_id', re.compile(r'\b((?:AKIA|ASIA)[0-9A-Z]{16})\b'), 1),
    ('github_token', re.compile(r'\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})\b'), 1),
    ('gitlab_token', re.compile(r'\b(glpat-[A-Za-z0-9_-]{20})\b'), 1),
    ('slack_token', re.compile(r'\b(xox[baprs]-[A-Za-z0-9-]{10,})'), 1),
    ('slack_webhook', re.compile(r'(https://hooks\.slack\.com/services/T\w+/B\w+/\w+)'), 1),
    ('stripe_key', re.compile(r'\b((?:sk|rk)_live_[A-Za-z0-9]{16,})'), 1),
    ('google_api_key', re.compile(r'\b(AIza[0-9A-Za-z_-]{35})\b'), 1),
    ('anthropic_api_key', re.compile(r'\b(sk-ant-[A-Za-z0-9_-]{20,})'), 1),
    ('openai_api_key', re.compile(r'\b(sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,})'), 1),
    ('private_key', re.compile(r'(-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----)'), 1),
    ('jwt', re.compile(r'\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})'), 1),
    ('connection_string_password', re.compile(r'\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|mssql)://[^:\s/@]+:([^@\s/]{3,})@'), 1),
]
SECRET_NAME = r'[A-Za-z0-9_.-]*?(?:secret|password|passwd|pwd|token|api_?key|apikey|private_?key|signing_?key|client_?secret|access_?key|auth_?key|credentials?)[A-Za-z0-9_]*'
GENERIC = re.compile(
    r'(?i)["\']?(' + SECRET_NAME + r')["\']?\s*\]?\s*(?::=|=>|[:=,])\s*'
    r'(?:(os\.environ\.get|os\.getenv|getenv|process\.env\.\w+\s*(?:\|\||\?\?)|ENV\.fetch|System\.getenv)\s*\(?[^,)]*,?\s*)?'
    r'["\']([^"\'\s]{8,})["\']')
DOTENV = re.compile(r'^\s*(?:export\s+)?(' + SECRET_NAME + r')\s*=\s*["\']?([^"\'\s#]{8,})', re.I)
PLACEHOLDER = re.compile(r'(?i)^(changeme|change_me|change-me|password|secret|example|placeholder|dummy|sample|redacted|none|null|'
                         r'undefined|todo|xxx+|\*+|\.+|<.*>|\$\{.*\}|\{\{.*\}\}|%\(.*\)s|your[-_].*|.*[-_]here|test|testing)$|'
                         r'example\.(com|org)|\$\{|\{\{|^process\.env|^os\.environ')

def fingerprint(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]

def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return round(-sum(n / len(s) * math.log2(n / len(s)) for n in counts.values()), 2)

def find_in_line(line: str, path_name: str = '') -> list[dict]:
    """Secrets on one line: [{type, key, value, column, fallback}]. Callers must not persist 'value'."""
    out, spans = [], []
    for kind, rx, grp in RULES:
        for m in rx.finditer(line):
            out.append({'type': kind, 'key': None, 'value': m.group(grp), 'column': m.start(grp) + 1, 'fallback': False})
            spans.append(m.span(grp))
    is_env = path_name.startswith('.env') and not path_name.endswith(('.example', '.sample', '.template', '.dist'))
    m = DOTENV.match(line) if is_env else None
    matches = [m] if m else list(GENERIC.finditer(line))
    for m in matches:
        if m is None:
            continue
        key, value = (m.group(1), m.group(2)) if is_env else (m.group(1), m.group(3))
        start = m.start(2) if is_env else m.start(3)
        if any(a <= start < b for a, b in spans) or PLACEHOLDER.search(value) or entropy(value) < 3.0:
            continue
        if not is_env and re.fullmatch(r'[A-Z_]+|[a-z_.]+\(?\)?', value):
            continue  # an identifier or constant name, not a literal secret
        out.append({'type': 'dotenv_secret' if is_env else 'generic_secret', 'key': key, 'value': value,
                    'column': start + 1, 'fallback': bool(not is_env and m.group(2))})
    return out

def files(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS:
                    stack.append(p)
            elif p.suffix.lower() not in SKIP_EXT and p.name not in SKIP_FILES:
                try:
                    if p.stat().st_size <= 1_000_000:
                        yield p
                except OSError:
                    pass

def scan_tree(root: Path) -> list[dict]:
    found = []
    for p in files(root):
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 3000:
                continue
            for s in find_in_line(line, p.name):
                found.append({'type': s['type'], 'file': rel, 'line': i, 'column': s['column'], 'key': s['key'],
                              'length': len(s['value']), 'entropy': entropy(s['value']),
                              'fingerprint': fingerprint(s['value']), 'hardcoded_fallback': s['fallback'],
                              'in_test_path': bool(TEST_PATH.search(rel))})
    return found

def scan_history(root: Path, max_commits: int) -> tuple[dict, str | None]:
    """fingerprint -> {type, commits: [sha], files: [path]} for secrets on lines added in history."""
    try:
        proc = subprocess.run(['git', '-C', str(root), 'log', '--all', '-p', '--no-color', '-U0', '--no-ext-diff',
                               f'--max-count={max_commits}', '--format=@@COMMIT %H'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, f'git log failed: {exc}'
    if proc.returncode != 0:
        return {}, (proc.stderr.strip().splitlines() or ['not a git repository'])[0]
    hist: dict[str, dict] = {}
    commit, file = None, ''
    for line in proc.stdout.splitlines():
        if line.startswith('@@COMMIT '):
            commit = line.split()[1]
        elif line.startswith('+++ '):
            file = line[6:] if line.startswith('+++ b/') else ''
        elif line.startswith('+') and not line.startswith('+++') and commit:
            for s in find_in_line(line[1:], Path(file).name):
                h = hist.setdefault(fingerprint(s['value']), {'type': s['type'], 'commits': [], 'files': []})
                if commit[:12] not in h['commits']:
                    h['commits'].append(commit[:12])
                if file and file not in h['files']:
                    h['files'].append(file)
    return hist, None

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--history', action='store_true', help='also scan lines added in git history')
    ap.add_argument('--max-commits', type=int, default=5000)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    head = scan_tree(root)
    history_status, removed = 'not_run', []
    note = None
    if args.history:
        hist, err = scan_history(root, args.max_commits)
        if err:
            history_status, note = 'errored', err
        else:
            history_status = 'ran'
            at_head = {s['fingerprint'] for s in head}
            for s in head:
                s['in_git_history'] = 'yes' if s['fingerprint'] in hist else 'no'
            removed = [{'fingerprint': fp, **h} for fp, h in sorted(hist.items()) if fp not in at_head]
    for s in head:
        s.setdefault('in_git_history', 'unknown')
    out = {'tool': 'scan_secrets', 'status': 'ran',
           'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'repository': root.name, 'history': {'status': history_status, 'note': note, 'removed_from_head': removed},
           'note': 'Values are never written. Rotate any real secret found here, including ones only in history.',
           'secrets': head}
    text = json.dumps(out, indent=2)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f'{len(head)} secret(s) at HEAD, {len(removed)} only in history (history: {history_status}) -> {args.out}')
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
