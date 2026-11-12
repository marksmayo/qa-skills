#!/usr/bin/env python3
"""Find hard-coded secrets in a repository, and optionally in its git history, without printing them.

    python scan_secrets.py <repo> [--history] [--history-budget 300] [--history-exclude '*.html' ...]
                           [--max-commits 5000] [--out secrets.json]

Output records give the type, file, line, variable name, length, entropy and a fingerprint
(the first 16 hex characters of the value's SHA-256). The value itself is never written: the
fingerprint is enough to tell whether the same secret appears elsewhere, including in history.

Each secret's file gets a git status (tracked, untracked, ignored, or unknown outside git) and a
cheap per-path history check (git log --all -- <path>), so a key that is present locally,
gitignored and never committed reads git_status "ignored", in_git_history "no", history_basis
"path". Paths to credential files (GOOGLE_APPLICATION_CREDENTIALS=keys/sa.json) are not secrets:
they, and the credential files found (service-account JSON, private-key files), are listed under
credential_files with the same git facts. Well-known public fields of service-account and OAuth
configuration (token_uri, auth_uri, certificate URLs) are not reported.

--history scans the lines added in each commit, but only file changes that touch a secret keyword
or token prefix (git log -G), leaving out binary, lock, vendored and minified files and any
--history-exclude globs (for example '*.html' on a site with thousands of generated pages). Commits
go to git in batches within a time budget; when it runs out, history.status is "partial" and
history.coverage says how many commits were scanned, instead of hanging. It reports, for each
secret at HEAD, whether it is in history, plus secrets removed from HEAD that remain in history
(still exposed: they must be rotated). gitleaks and trufflehog are more thorough; use them when
available and merge their SARIF with merge_findings.py.
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', 'coverage', 'vendor', 'Pods', '.gradle',
             '__pycache__', '.venv', 'venv', '.tox', '.mypy_cache', '.pytest_cache', 'site-packages', 'target',
             '.terraform', '.idea', '.vscode'}
SKIP_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.pdf', '.zip', '.gz', '.tgz', '.jar', '.class', '.so',
            '.dll', '.exe', '.woff', '.woff2', '.ttf', '.eot', '.mp4', '.mp3', '.lock', '.map', '.sqlite', '.db', '.pyc'}
SKIP_FILES = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Cargo.lock', 'go.sum', 'composer.lock'}
# also left out of the history scan: more media and bundled output
HISTORY_SKIP_EXT = SKIP_EXT | {'.avif', '.svg', '.bmp', '.tif', '.tiff', '.psd', '.webm', '.mov', '.wav', '.ogg',
                               '.otf', '.bin', '.7z', '.rar', '.bz2', '.xz', '.min.js', '.min.css'}
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

# Fields of service-account and OAuth/OIDC configuration that are public by design.
PUBLIC_KEYS = {'token_uri', 'auth_uri', 'auth_provider_x509_cert_url', 'client_x509_cert_url', 'universe_domain',
               'token_url', 'token_endpoint', 'authorization_endpoint', 'jwks_uri', 'userinfo_endpoint',
               'revocation_endpoint', 'introspection_endpoint', 'issuer', 'token_type'}
PUBLIC_URL_KEY = re.compile(r'(?i)(?:_|^)(?:uri|url|endpoint|domain|host)$')
# A key naming where a credential lives, and values that look like file paths.
PATH_KEY = re.compile(r'(?i)(?:file|path|dir|directory|location|_credentials)$|^google_application_credentials$')
CRED_EXT = r'(?:json|pem|p12|pfx|key|crt|cer|der|jks|keystore|p8|ppk|asc|gpg|kdbx)'
PATH_VALUE = re.compile(r'(?:~|\.{1,2}|[A-Za-z]:)?[\\/]?(?:[\w.@ -]+[\\/])*[\w.@-]+')
CRED_FILE_VALUE = re.compile(r'(?:~|\.{1,2}|[A-Za-z]:)?[\\/]?(?:[\w.@ -]+[\\/])*[\w.@-]+\.' + CRED_EXT, re.I)
CRED_FILE_NAME = re.compile(r'(?i)^(id_(rsa|dsa|ecdsa|ed25519)|.*\.(pem|p12|pfx|key|jks|keystore|p8|ppk|kdbx))$')

def fingerprint(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]

def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return round(-sum(n / len(s) * math.log2(n / len(s)) for n in counts.values()), 2)

def is_public_field(key: str | None, value: str) -> bool:
    """Public endpoints: token_uri, certificate URLs, and plain https URLs under *_uri/*_url keys."""
    if not key:
        return False
    k = re.split(r'[.\[\]]', key.strip('"\'').lower())[-1] or key.lower()
    if k in PUBLIC_KEYS:
        return True
    m = re.fullmatch(r'https://[^/@:?#\s]+(?::\d+)?(/[^?#@\s]*)?', value)
    if m and PUBLIC_URL_KEY.search(k):
        segments = [s for s in (m.group(1) or '').split('/') if s]
        return not any(len(s) >= 20 and entropy(s) >= 3.5 for s in segments)  # a token in the path is still a secret
    return False

def credential_path(key: str | None, value: str) -> bool:
    """True when the value is a path to a credential file (not a secret)."""
    if '://' in value:
        return False
    if CRED_FILE_VALUE.fullmatch(value):
        return True
    return bool(key and PATH_KEY.search(key) and PATH_VALUE.fullmatch(value) and re.search(r'[\\/]', value)
                and re.search(r'^(?:~|\.{1,2}[\\/]|[A-Za-z]:|[\\/])|\.\w{1,8}$', value))

def find_in_line(line: str, path_name: str = '', refs: list | None = None) -> list[dict]:
    """Secrets on one line: [{type, key, value, column, fallback}]. Callers must not persist 'value'.

    Paths to credential files are not secrets; when `refs` is given they are appended to it."""
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
        if any(a <= start < b for a, b in spans) or PLACEHOLDER.search(value):
            continue
        if credential_path(key, value):
            if refs is not None:
                refs.append({'key': key, 'path': value})
            continue
        if entropy(value) < 3.0 or is_public_field(key, value):
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

def scan_tree(root: Path) -> tuple[list[dict], list[dict]]:
    """(secrets, credential files and references to them) in the working tree."""
    found, creds = [], []
    for p in files(root):
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            if CRED_FILE_NAME.match(p.name):
                creds.append({'kind': 'credential_file', 'path': rel, 'referenced_from': None, 'key': None})
            continue
        if CRED_FILE_NAME.match(p.name):
            creds.append({'kind': 'credential_file', 'path': rel, 'referenced_from': None, 'key': None})
        elif p.suffix.lower() == '.json' and re.search(r'"type"\s*:\s*"service_account"', text) and '"private_key"' in text:
            creds.append({'kind': 'service_account_json', 'path': rel, 'referenced_from': None, 'key': None})
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 3000:
                continue
            refs: list[dict] = []
            for s in find_in_line(line, p.name, refs):
                found.append({'type': s['type'], 'file': rel, 'line': i, 'column': s['column'], 'key': s['key'],
                              'length': len(s['value']), 'entropy': entropy(s['value']),
                              'fingerprint': fingerprint(s['value']), 'hardcoded_fallback': s['fallback'],
                              'in_test_path': bool(TEST_PATH.search(rel))})
            for r in refs:
                target = Path(r['path'].replace('\\', '/')).expanduser()
                resolved = target if target.is_absolute() else root / target
                try:
                    in_repo = resolved.resolve().relative_to(root).as_posix()
                except (ValueError, OSError):
                    in_repo = None
                creds.append({'kind': 'reference', 'path': r['path'], 'referenced_from': f'{rel}:{i}', 'key': r['key'],
                              'exists': resolved.exists(), 'repo_path': in_repo})
    return found, creds

def git(root: Path, args: list[str], timeout: float = 60, stdin: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout, input=stdin)
    except (OSError, subprocess.TimeoutExpired):
        return None

def is_git(root: Path) -> bool:
    probe = git(root, ['rev-parse', '--is-inside-work-tree'])
    return bool(probe and probe.returncode == 0 and probe.stdout.strip() == 'true')

def git_statuses(root: Path, paths: list[str]) -> dict[str, str]:
    """path (relative to root) -> tracked | untracked | ignored | unknown (outside git)."""
    paths = sorted(set(paths))
    if not paths or not is_git(root):
        return {p: 'unknown' for p in paths}
    ls = git(root, ['ls-files', '-z', '--', *paths])
    tracked = set(ls.stdout.split('\0')) if ls and ls.returncode == 0 else set()
    rest = [p for p in paths if p not in tracked]
    ignored: set[str] = set()
    if rest:
        ci = git(root, ['check-ignore', '--stdin', '-z'], stdin='\0'.join(rest) + '\0')
        if ci and ci.returncode in (0, 1):
            ignored = set(ci.stdout.split('\0'))
    return {p: 'tracked' if p in tracked else 'ignored' if p in ignored else 'untracked' for p in paths}

def path_history(root: Path, path: str, fingerprints: set[str]) -> tuple[str, dict[str, list[str]] | None]:
    """(was the path ever committed: yes/no/unknown, fingerprint -> commits that added it at this path).

    The second value is None when the path's diffs could not be read."""
    log = git(root, ['log', '--all', '--format=%H', '--max-count=1', '--', path])
    if not log or log.returncode != 0:
        return 'unknown', None
    if not log.stdout.strip():
        return 'no', {}
    if not fingerprints:
        return 'yes', {}
    diff = git(root, ['log', '--all', '-p', '--no-color', '-U0', '--no-ext-diff', '--no-renames',
                      '--format=@@COMMIT %H', '--', path], timeout=120)
    if not diff or diff.returncode != 0:
        return 'yes', None
    hits: dict[str, list[str]] = {}
    commit = None
    for line in diff.stdout.splitlines():
        if line.startswith('@@COMMIT '):
            commit = line.split()[1][:12]
        elif line.startswith('+') and not line.startswith('+++') and commit and len(line) <= 3000:
            for s in find_in_line(line[1:], Path(path).name):
                fp = fingerprint(s['value'])
                if fp in fingerprints and commit not in hits.setdefault(fp, []):
                    hits[fp].append(commit)
    return 'yes', hits

# The history scan only reads file changes that add or remove a line matching one of these (git log -i -G).
# It must stay a superset of what find_in_line can match: RULES' prefixes and SECRET_NAME's words.
HISTORY_KEYWORDS = ('AKIA|ASIA|gh[pousr]_|github_pat_|glpat-|xox[baprs]-|hooks[.]slack[.]com|_live_|AIza|sk-ant-|'
                    'sk-proj-|sk-svcacct-|PRIVATE KEY|eyJ|://[^:/@ ]+:[^@ /]+@|secret|passw|pwd|token|api_?key|apikey|'
                    'private_?key|signing_?key|access_?key|auth_?key|credential')

def history_pathspecs(extra: list[str]) -> list[str]:
    dirs = sorted(SKIP_DIRS - {'.git'})
    return (['.'] + [f':(exclude,icase)*{ext}' for ext in sorted(HISTORY_SKIP_EXT)]
            + [f':(exclude){n}' for n in sorted(SKIP_FILES)] + [f':(exclude)*/{n}' for n in sorted(SKIP_FILES)]
            + [f':(exclude){d}/*' for d in dirs] + [f':(exclude)*/{d}/*' for d in dirs]
            + [f':(exclude){g}' for g in extra])

def scan_history(root: Path, max_commits: int, budget: float = 300, exclude: list[str] | None = None,
                 batch: int = 100, max_file_kb: int = 256) -> tuple[dict, dict]:
    """(fingerprint -> {type, commits, files}, coverage) for secrets on lines added in history."""
    t0 = time.monotonic()
    exclude = list(exclude or [])
    cov = {'commits_total': None, 'commits_scanned': 0, 'max_commits': max_commits, 'time_budget_s': budget,
           'elapsed_s': 0.0, 'complete': False, 'stopped': None,
           'method': 'git log -i -G<secret keywords> -p -U0 --no-renames, in batches of commits; merge commits not diffed',
           'excluded': ['binary, media and font files', 'lock files', 'vendored, dependency and build directories',
                        'minified files', f'added text over {max_file_kb} KB per file per commit'] + exclude,
           'large_diffs_skipped': 0}
    revs = git(root, ['rev-list', '--all'], timeout=120)
    if not revs or revs.returncode != 0:
        msg = (revs.stderr.strip().splitlines() or ['git rev-list failed'])[0] if revs else 'git rev-list failed'
        return {}, {**cov, 'error': msg}
    commits = revs.stdout.split()
    cov['commits_total'] = len(commits)
    todo = commits[:max_commits]
    hist: dict[str, dict] = {}
    specs = history_pathspecs(exclude)
    cap = max_file_kb * 1024
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        if time.monotonic() - t0 > budget:
            cov['stopped'] = f'time budget of {budget:g}s ran out'
            break
        cmd = ['git', '-C', str(root), 'log', '--no-walk=unsorted', '--stdin', '-p', '--no-color', '-U0', '--no-ext-diff',
               '--no-renames', '-i', f'-G{HISTORY_KEYWORDS}', '--format=@@COMMIT %H', '--', *specs]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', errors='replace')
        except OSError as exc:
            return hist, {**cov, 'error': f'git log failed: {exc}'}
        proc.stdin.write('\n'.join(chunk) + '\n')
        proc.stdin.close()
        commit, file, size, skip = None, '', 0, False
        for line in proc.stdout:
            if time.monotonic() - t0 > budget:
                proc.kill()
                cov['stopped'] = f'time budget of {budget:g}s ran out'
                break
            line = line.rstrip('\n')
            if line.startswith('@@COMMIT '):
                commit = line.split()[1]
            elif line.startswith('+++ '):
                file = line[6:] if line.startswith('+++ b/') else ''
                size, skip = 0, False
            elif line.startswith('+') and commit and not skip:
                size += len(line)
                if size > cap:
                    skip = True
                    cov['large_diffs_skipped'] += 1
                    continue
                if len(line) > 3000:
                    continue
                for s in find_in_line(line[1:], Path(file).name):
                    h = hist.setdefault(fingerprint(s['value']), {'type': s['type'], 'commits': [], 'files': []})
                    if commit[:12] not in h['commits']:
                        h['commits'].append(commit[:12])
                    if file and file not in h['files']:
                        h['files'].append(file)
        err = proc.stderr.read()
        proc.wait()
        if cov['stopped']:
            break
        if proc.returncode != 0:
            return hist, {**cov, 'elapsed_s': round(time.monotonic() - t0, 1),
                          'error': (err.strip().splitlines() or [f'git log exited with {proc.returncode}'])[0]}
        cov['commits_scanned'] += len(chunk)
    if not cov['stopped'] and len(todo) < len(commits):
        cov['stopped'] = f'--max-commits {max_commits} reached'
    cov['elapsed_s'] = round(time.monotonic() - t0, 1)
    cov['complete'] = cov['commits_scanned'] == len(commits)
    return hist, cov

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--history', action='store_true', help='also scan lines added in git history')
    ap.add_argument('--max-commits', type=int, default=5000)
    ap.add_argument('--history-budget', type=float, default=300, help='seconds for the history scan (default 300)')
    ap.add_argument('--history-exclude', action='append', default=[], metavar='GLOB',
                    help="git pathspec glob to leave out of the history scan, e.g. '*.html' (repeatable)")
    ap.add_argument('--history-max-file-kb', type=int, default=256,
                    help='stop reading one file\'s added lines in one commit after this many KB (default 256)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    head, creds = scan_tree(root)

    # git facts for every file that holds a secret or is a credential file
    paths = {s['file'] for s in head} | {c['path'] for c in creds if c['kind'] != 'reference'} \
        | {c['repo_path'] for c in creds if c['kind'] == 'reference' and c.get('repo_path')}
    status = git_statuses(root, sorted(paths))
    in_git = any(v != 'unknown' for v in status.values()) or is_git(root)
    by_path: dict[str, tuple[str, dict | None]] = {}
    for path in sorted(paths):
        fps = {s['fingerprint'] for s in head if s['file'] == path}
        by_path[path] = path_history(root, path, fps) if in_git else ('unknown', None)

    history_status, removed, note, coverage = 'not_run', [], None, None
    hist: dict = {}
    if args.history:
        hist, coverage = scan_history(root, args.max_commits, args.history_budget, args.history_exclude,
                                      max_file_kb=args.history_max_file_kb)
        if coverage.get('error'):
            history_status, note = 'errored', coverage['error']
        elif not coverage['complete']:
            history_status = 'partial'
            note = (f"{coverage['commits_scanned']} of {coverage['commits_total']} commits scanned "
                    f"({coverage['stopped']}); secrets only in older commits may be missing")
        else:
            history_status = 'ran'
        at_head = {s['fingerprint'] for s in head}
        removed = [{'fingerprint': fp, **h} for fp, h in sorted(hist.items()) if fp not in at_head]

    for s in head:
        committed, hits = by_path.get(s['file'], ('unknown', None))
        s['git_status'] = status.get(s['file'], 'unknown')
        s['path_in_history'] = committed
        commits = list((hits or {}).get(s['fingerprint']) or []) + [c for c in hist.get(s['fingerprint'], {}).get('commits', [])]
        s['history_commits'] = sorted(set(commits))
        if commits:
            s['in_git_history'], s['history_basis'] = 'yes', 'full' if s['fingerprint'] in hist else 'path'
        elif history_status == 'ran':
            s['in_git_history'], s['history_basis'] = 'no', 'full'
        elif committed == 'no' or (committed == 'yes' and hits is not None):
            s['in_git_history'], s['history_basis'] = 'no', 'path'  # never added at this path; --history checks elsewhere
        else:
            s['in_git_history'], s['history_basis'] = 'unknown', None

    for c in creds:
        target = c['path'] if c['kind'] != 'reference' else c.get('repo_path')
        c['git_status'] = status.get(target, 'unknown') if target else 'outside_repository'
        c['path_in_history'] = by_path.get(target, ('unknown', None))[0] if target else 'unknown'

    out = {'tool': 'scan_secrets', 'status': 'ran',
           'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'repository': root.name, 'git': in_git,
           'history': {'status': history_status, 'note': note, 'coverage': coverage, 'removed_from_head': removed},
           'note': 'Values are never written. Rotate any real secret found here, including ones only in history. '
                   "in_git_history 'no' with history_basis 'path' means never committed at that path; "
                   "with 'full' it means not found in any scanned commit.",
           'secrets': head, 'credential_files': creds}
    text = json.dumps(out, indent=2)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f'{len(head)} secret(s) at HEAD, {len(removed)} only in history (history: {history_status}), '
              f'{len(creds)} credential file(s) or reference(s) -> {args.out}')
        if note:
            print(f'  history: {note}')
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
