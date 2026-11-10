#!/usr/bin/env python3
"""Map a repository's attack surface for a security review.

    python recon.py <repo> [--out recon.json]

Finds languages and frameworks, entry points (HTTP routes, GraphQL operations, webhooks, queue
consumers, CLI commands, mobile deep links and exported components, CI workflow triggers), a
first guess at each entry point's authentication requirement, data stores, outbound calls, secret
configuration, dependency manifests and infrastructure files. It also lists review leads: routes
taking object ids (IDOR), upload handlers, auth flows and admin surfaces.

Everything here is a regex-level first pass. The agent confirms entry points and auth by reading
the code; an 'unknown' auth value means the script could not tell, not that the route is public.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', 'coverage', 'vendor', 'Pods', '.gradle',
             '__pycache__', '.venv', 'venv', '.tox', 'site-packages', 'target', 'bin', 'obj', '.terraform', '.idea'}
LANG = {'.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
        '.ts': 'TypeScript', '.tsx': 'TypeScript', '.java': 'Java', '.kt': 'Kotlin', '.go': 'Go', '.rb': 'Ruby',
        '.php': 'PHP', '.cs': 'C#', '.swift': 'Swift', '.m': 'Objective-C', '.c': 'C', '.h': 'C/C++', '.cc': 'C++',
        '.cpp': 'C++', '.rs': 'Rust', '.scala': 'Scala', '.dart': 'Dart', '.vue': 'Vue', '.svelte': 'Svelte',
        '.tf': 'Terraform', '.sql': 'SQL'}
TEST_PATH = re.compile(r'(^|/)(tests?|spec|__tests__|e2e|cypress|fixtures|testdata)(/|$)|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$', re.I)
MANIFESTS = {'requirements.txt', 'requirements-dev.txt', 'pyproject.toml', 'Pipfile', 'Pipfile.lock', 'poetry.lock',
             'setup.py', 'setup.cfg', 'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'go.mod',
             'go.sum', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'Gemfile', 'Gemfile.lock', 'composer.json',
             'composer.lock', 'Cargo.toml', 'Cargo.lock', 'Podfile', 'Package.swift', 'pubspec.yaml'}
FRAMEWORKS = [  # (name, regex over source)
    ('Flask', r'\bfrom flask import|\bimport flask\b'), ('FastAPI', r'\bfrom fastapi import'),
    ('Django', r'\bfrom django\b'), ('Express', r'require\(["\']express["\']\)|from ["\']express["\']'),
    ('Next.js', r'from ["\']next(/|["\'])'), ('NestJS', r'from ["\']@nestjs/'), ('Koa', r'from ["\']koa["\']|require\(["\']koa["\']\)'),
    ('Spring', r'org\.springframework'), ('Gin', r'github\.com/gin-gonic/gin'), ('Echo', r'github\.com/labstack/echo'),
    ('chi', r'github\.com/go-chi/chi'), ('Rails', r'Rails\.application|ActionController'), ('Laravel', r'Illuminate\\'),
    ('ASP.NET Core', r'Microsoft\.AspNetCore'), ('GraphQL', r'\btype\s+(Query|Mutation)\s*\{|graphql'),
    ('SQLAlchemy', r'\bsqlalchemy\b'), ('Prisma', r'@prisma/client'), ('Celery', r'\bcelery\b'),
]
ROUTES = [  # (framework, regex, method group, path group)
    ('flask', re.compile(r'@\s*(\w+)\.(route|get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']([^)]*)'), 2, 3),
    ('fastapi', re.compile(r'@\s*(\w+)\.(api_route|get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']()'), 2, 3),
    ('django', re.compile(r'\b(re_)?path\(\s*r?["\']([^"\']*)["\']\s*,\s*([\w.]+)()'), None, 2),
    ('express', re.compile(r'(?<!@)(?<!@\s)\b(app|router|\w+Router|server)\.(get|post|put|patch|delete|all)\(\s*["\'`]([^"\'`]+)["\'`]([^\n]*)'), 2, 3),
    ('spring', re.compile(r'@(Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*"([^"]*)"()()'), 1, 2),
    ('go', re.compile(r'\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\(\s*"([^"]+)"()()'), 1, 2),
    ('laravel', re.compile(r'Route::(get|post|put|patch|delete|any|match)\(\s*[\'"]([^\'"]+)[\'"]([^\n]*)()'), 1, 2),
    ('rails', re.compile(r'^\s*(get|post|put|patch|delete|resources|resource)\s+[\'":]([\w/:{}.-]+)()()'), 1, 2),
    ('nestjs', re.compile(r'@(Get|Post|Put|Patch|Delete|All)\(\s*[\'"]?([^\'")]*)[\'"]?\s*\)()()'), 1, 2),
]
PREFIX = [re.compile(r'Blueprint\([^)]*url_prefix\s*=\s*["\']([^"\']+)'), re.compile(r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']+)'),
          re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]+)"'), re.compile(r'@Controller\(\s*[\'"]([^\'"]+)')]
AUTH_ADMIN = re.compile(r'(?i)admin_required|roles?_required\(\s*["\']admin|is_?admin|require_?admin|hasRole\(\s*["\']ADMIN|'
                        r'@PreAuthorize\([^)]*ADMIN|staff_member_required|superuser|RolesGuard|role:admin')
AUTH_USER = re.compile(r'(?i)login_required|jwt_required|auth_required|requires_auth|permission_required|Depends\(\s*\w*(current_user|auth|token)\w*|'
                       r'authenticate|isAuthenticated|requireAuth|ensureAuth|verifyToken|passport\.authenticate|@PreAuthorize|@Secured|'
                       r'@RolesAllowed|UseGuards|AuthGuard|before_action\s*:authenticate|middleware\([\'"]auth|\[Authorize\]|'
                       r'authorize|withAuth|getServerSession|currentUser')
AUTH_PUBLIC = re.compile(r'(?i)@permission_classes\(\s*\[\s*AllowAny|AllowAnonymous|@PermitAll|skip_before_action\s*:authenticate|@Public\(\)|\.permitAll\(\)')
ID_PARAM = re.compile(r'<(int:|uuid:|string:)?\w*id>|:(\w*id)\b|\{\w*[iI]d\}|\[\w*[iI]d\]', re.I)
STORES = [('sqlite', r'\bsqlite3\b'), ('postgres', r'\bpsycopg2?\b|\bpg\b\s*=|from ["\']pg["\']|postgres(ql)?://|asyncpg'),
          ('mysql', r'\bmysql\b|pymysql|mysql2'), ('mongodb', r'\bpymongo\b|\bmongoose\b|mongodb(\+srv)?://|MongoClient'),
          ('redis', r'\bredis\b'), ('sqlalchemy', r'\bsqlalchemy\b'), ('prisma', r'@prisma/client'),
          ('jdbc', r'\bjdbc:'), ('gorm', r'gorm\.io'), ('activerecord', r'ActiveRecord'), ('s3', r'\bboto3\b.*s3|S3Client|@aws-sdk/client-s3'),
          ('filesystem', r'\bUPLOAD_(DIR|FOLDER)\b|multer\(|MultipartFile|request\.files')]
OUTBOUND = re.compile(r'\b(requests|httpx|aiohttp\.ClientSession|urllib\.request|urlopen|axios|fetch|got|http\.Get|http\.Post|'
                      r'RestTemplate|WebClient|HttpClient|Net::HTTP|Faraday|HTTParty|curl_exec|Guzzle)\b[.(]')
SECRET_CFG = re.compile(r'\b(os\.environ|os\.getenv|process\.env|System\.getenv|ENV\[|ENV\.fetch|getenv\(|Configuration\[)'
                        r'[^\n]{0,40}(SECRET|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DSN|DATABASE_URL)', re.I)
QUEUE = re.compile(r'@(app\.task|shared_task|celery\.task)|@KafkaListener|@RabbitListener|@SqsListener|\.subscribe\(|'
                   r'channel\.consume\(|receive_message\(|@JmsListener|\bconsumer\s*=|Sidekiq::Worker|ApplicationJob')
CLI = re.compile(r'@click\.command|@\w+\.command\(|typer\.Typer\(|argparse\.ArgumentParser\(|cobra\.Command\{|commander')
UPLOAD = re.compile(r'request\.files\b|\bUploadFile\b|multer\(|MultipartFile|IFormFile|\$_FILES|upload\.(single|array|fields)\(')
GLOBAL_AUTH = re.compile(r'before_request|before_app_request|app\.use\([^)]*(auth|session|jwt|passport)|dependencies\s*=\s*\[\s*Depends|'
                         r'MIDDLEWARE\s*=|SecurityFilterChain|authorizeHttpRequests')
AUTH_FLOW = re.compile(r'(?i)(login|logout|signin|signup|register|password|reset|token|oauth|session|mfa|otp|jwt)')

def walk(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and (not p.name.startswith('.') or p.name == '.github'):
                    stack.append(p)
            elif p.is_file():
                yield p

def read(p: Path) -> str:
    try:
        if p.stat().st_size > 1_000_000:
            return ''
        return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''

def guess_auth(lines: list[str], i: int, framework: str, file_text: str) -> tuple[str, str | None]:
    """Look at decorators/middleware on the route declaration itself."""
    if framework in ('flask', 'fastapi', 'spring', 'nestjs'):
        # the decorator/annotation block around the route: contiguous @ lines above, @ lines below up to the signature
        start = i
        while start > 0 and lines[start - 1].lstrip().startswith('@'):
            start -= 1
        end = i + 1
        while end < len(lines) and lines[end].lstrip().startswith('@'):
            end += 1
        ctx_text = '\n'.join(lines[start:end + 1])  # include the signature (FastAPI Depends(...) lives there)
    else:
        ctx_text = lines[i]
    for rx, label in ((AUTH_PUBLIC, 'public'), (AUTH_ADMIN, 'admin'), (AUTH_USER, 'authenticated')):
        m = rx.search(ctx_text)
        if m and not (label == 'public' and framework in ('express', 'laravel')):
            return label, m.group(0)
    if framework in ('express', 'go', 'laravel', 'rails') and re.search(r'(?i)router\.use\([^)]*(auth|session|jwt)|middleware.*auth', file_text):
        return 'unknown', 'file-level auth middleware may apply'
    return 'unknown', None

def handler_after(lines: list[str], i: int) -> str | None:
    for l in lines[i:i + 8]:
        m = re.search(r'\b(?:async\s+)?def\s+(\w+)|\bfunction\s+(\w+)|\bpublic\s+\S+\s+(\w+)\s*\(|\bfunc\s+(\w+)', l)
        if m:
            return next(g for g in m.groups() if g)
    return None

def routes_in(rel: str, text: str) -> list[dict]:
    out = []
    lines = text.splitlines()
    prefix = ''
    for rx in PREFIX:
        m = rx.search(text)
        if m:
            prefix = m.group(1).rstrip('/')
            break
    for fw, rx, mgrp, pgrp in ROUTES:
        if fw == 'rails' and not rel.endswith('routes.rb'):
            continue
        if fw == 'django' and not rel.endswith('urls.py'):
            continue
        for i, line in enumerate(lines):
            m = rx.search(line)
            if not m:
                continue
            path = m.group(pgrp)
            method = (m.group(mgrp) if mgrp else 'ANY').upper()
            if fw == 'flask' and method == 'ROUTE':
                mm = re.search(r'methods\s*=\s*\[([^\]]+)\]', m.group(4) or '')
                method = ','.join(x.strip(' "\'').upper() for x in mm.group(1).split(',')) if mm else 'GET'
            if fw in ('spring', 'nestjs', 'go'):
                method = {'REQUEST': 'ANY', 'HANDLEFUNC': 'ANY', 'HANDLE': 'ANY', 'ALL': 'ANY'}.get(method, method)
            if fw == 'django':
                path = '/' + path.lstrip('^').rstrip('$')
            full = (prefix + ('/' + path.lstrip('/') if path not in ('', '/') else '')) if fw in ('flask', 'fastapi', 'spring', 'nestjs') else path
            if fw in ('spring', 'nestjs') and re.search(r'\bclass\s', '\n'.join(lines[i + 1:i + 4])):
                continue  # a class-level mapping is the prefix, not a route
            if any(r['handler'] == f'{rel}:{i + 1}' for r in out):
                continue  # the same declaration matched by another framework's pattern
            auth, evidence = guess_auth(lines, i, fw, text)
            out.append({'kind': 'webhook' if 'webhook' in full.lower() else 'http_route', 'framework': fw, 'method': method,
                        'path': full or '/', 'handler': f'{rel}:{i + 1}', 'function': handler_after(lines, i),
                        'auth': auth, 'auth_evidence': evidence})
    return out

def nextjs_routes(rel: str, text: str) -> list[dict]:
    m = re.search(r'(?:^|/)app/(.*)/route\.(ts|js|tsx|jsx)$', rel) or re.search(r'(?:^|/)pages/(api/.*)\.(ts|js)$', rel)
    if not m:
        return []
    path = '/' + re.sub(r'\[(\.\.\.)?(\w+)\]', r':\2', m.group(1)).replace('/index', '')
    methods = re.findall(r'export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE)\b', text) or ['ANY']
    hint = AUTH_USER.search(text)  # file-level only: each exported handler must be read to know which ones check it
    auth, evidence = 'unknown', (f'the file uses {hint.group(0)}; check each handler' if hint else None)
    return [{'kind': 'http_route', 'framework': 'nextjs', 'method': meth, 'path': path, 'handler': f'{rel}:1',
             'function': meth, 'auth': auth, 'auth_evidence': evidence} for meth in methods]

def graphql_ops(rel: str, text: str) -> list[dict]:
    out = []
    for block in re.finditer(r'\btype\s+(Query|Mutation|Subscription)\s*\{([^}]*)\}', text):
        base = text[:block.start()].count('\n')
        for fm in re.finditer(r'^\s*(\w+)\s*[(:]', block.group(2), re.M):
            line = base + block.group(0)[:block.group(0).find(fm.group(0))].count('\n') + fm.group(0).count('\n') + 1
            out.append({'kind': 'graphql', 'framework': 'graphql', 'method': block.group(1).upper(), 'path': fm.group(1),
                        'handler': f'{rel}:{line}', 'function': fm.group(1), 'auth': 'unknown', 'auth_evidence': None})
    return out

def android_entries(rel: str, text: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<(activity|service|receiver|provider)\b([^>]*)>(.*?)</\1>|<(activity|service|receiver|provider)\b([^>]*)/>', text, re.S):
        tag, attrs, body = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), '')
        name = (re.search(r'android:name\s*=\s*"([^"]+)"', attrs) or [None, '?'])[1]
        exported = re.search(r'android:exported\s*=\s*"true"', attrs) or ('<intent-filter' in body and 'android:exported="false"' not in attrs)
        if not exported:
            continue
        line = text[:m.start()].count('\n') + 1
        schemes = re.findall(r'android:scheme\s*=\s*"([^"]+)"', body)
        perm = re.search(r'android:permission\s*=\s*"([^"]+)"', attrs)
        out.append({'kind': 'deep_link' if schemes else 'exported_component', 'framework': 'android', 'method': tag.upper(),
                    'path': name + (f" ({', '.join(sorted(set(schemes)))})" if schemes else ''), 'handler': f'{rel}:{line}',
                    'function': name, 'auth': 'authenticated' if perm else 'public', 'auth_evidence': perm.group(1) if perm else None})
    return out

def ios_entries(rel: str, text: str) -> list[dict]:
    m = re.search(r'CFBundleURLSchemes</key>\s*<array>(.*?)</array>', text, re.S)
    if not m:
        return []
    line = text[:m.start()].count('\n') + 1
    return [{'kind': 'deep_link', 'framework': 'ios', 'method': 'URL_SCHEME', 'path': s + '://', 'handler': f'{rel}:{line}',
             'function': None, 'auth': 'public', 'auth_evidence': None} for s in re.findall(r'<string>([^<]+)</string>', m.group(1))]

def workflow_info(rel: str, text: str) -> dict:
    triggers = re.findall(r'^\s{0,4}(push|pull_request_target|pull_request|issue_comment|issues|workflow_run|workflow_dispatch|'
                          r'schedule|release|discussion|discussion_comment|pull_request_review|pull_request_review_comment)\s*:', text, re.M)
    on_inline = re.search(r'^on\s*:\s*\[?([\w, ]+)\]?\s*$', text, re.M)
    if on_inline:
        triggers += [t.strip() for t in on_inline.group(1).split(',')]
    risky = sorted({t for t in triggers if t in ('pull_request_target', 'issue_comment', 'workflow_run', 'issues',
                                                  'discussion', 'discussion_comment', 'pull_request_review_comment')})
    perms = re.findall(r'^\s*(contents|pull-requests|issues|packages|id-token|actions|deployments)\s*:\s*write', text, re.M)
    return {'file': rel, 'triggers': sorted(set(triggers)), 'untrusted_input_triggers': risky,
            'write_permissions': sorted(set(perms)) + (['write-all'] if 'write-all' in text else []),
            'uses_secrets': bool(re.search(r'\$\{\{\s*secrets\.', text))}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    langs: Counter = Counter()
    frameworks: set[str] = set()
    entries, stores, outbound, secret_cfg, queues, clis, uploads = [], [], [], [], [], [], []
    manifests, iac, workflows, env_files = [], [], [], []
    files_total = 0
    global_hooks: list[str] = []
    for p in walk(root):
        rel = p.relative_to(root).as_posix()
        if p.name in MANIFESTS:
            manifests.append(rel)
        if p.name.startswith('.env'):
            env_files.append(rel)
        if p.name in ('Dockerfile', 'Containerfile', 'docker-compose.yml', 'docker-compose.yaml', 'compose.yaml') or p.suffix in ('.tf', '.bicep') or \
                re.search(r'(^|/)(k8s|kubernetes|helm|charts|deploy)/.*\.ya?ml$', rel):
            iac.append(rel)
        if '.github/workflows/' in '/' + rel and p.suffix in ('.yml', '.yaml'):
            workflows.append(workflow_info(rel, read(p)))
            continue
        if p.name == 'AndroidManifest.xml':
            entries += android_entries(rel, read(p))
            continue
        if p.name == 'Info.plist':
            entries += ios_entries(rel, read(p))
            continue
        lang = LANG.get(p.suffix)
        if p.suffix in ('.graphql', '.gql'):
            entries += graphql_ops(rel, read(p))
            continue
        if not lang:
            continue
        files_total += 1
        if TEST_PATH.search(rel):
            continue
        langs[lang] += 1
        text = read(p)
        for i, line in enumerate(text.splitlines(), 1):
            if GLOBAL_AUTH.search(line):
                global_hooks.append(f'{rel}:{i}')
        for name, rx in FRAMEWORKS:
            if re.search(rx, text):
                frameworks.add(name)
        entries += routes_in(rel, text) + nextjs_routes(rel, text)
        if 'gql`' in text or 'typeDefs' in text:
            entries += graphql_ops(rel, text)
        for i, line in enumerate(text.splitlines(), 1):
            for kind, rx in STORES:
                if re.search(rx, line) and not any(s['kind'] == kind and s['file'] == rel for s in stores):
                    stores.append({'kind': kind, 'file': rel, 'line': i})
            if OUTBOUND.search(line) and not line.lstrip().startswith(('import ', 'from ', '#', '//')):
                outbound.append({'file': rel, 'line': i, 'snippet': line.strip()[:160]})
            if SECRET_CFG.search(line):
                secret_cfg.append({'file': rel, 'line': i, 'snippet': line.strip()[:160]})
            if QUEUE.search(line):
                queues.append({'kind': 'queue_consumer', 'framework': None, 'method': 'CONSUME', 'path': line.strip()[:80],
                               'handler': f'{rel}:{i}', 'function': None, 'auth': 'unknown', 'auth_evidence': None})
            if CLI.search(line):
                clis.append({'kind': 'cli', 'framework': None, 'method': 'CLI', 'path': rel, 'handler': f'{rel}:{i}',
                             'function': None, 'auth': 'unknown', 'auth_evidence': 'local invocation'})
            if UPLOAD.search(line) and not line.lstrip().startswith(('import ', 'from ')):
                uploads.append(f'{rel}:{i}')
    entries += queues + clis
    for e in entries:  # decorator frameworks: no decorator and no app-wide hook means no auth check
        if e['framework'] in ('flask', 'fastapi') and e['auth'] == 'unknown' and not global_hooks:
            e['auth'], e['auth_evidence'] = 'public', 'no auth decorator on the route and no app-wide auth hook found'
    for n, e in enumerate(entries, 1):
        e['id'] = f'EP-{n}'
    leads = []
    for e in entries:
        if e['kind'] in ('http_route', 'webhook', 'graphql') and ID_PARAM.search(e['path']):
            leads.append({'area': 'access_control', 'entry_point': e['id'], 'why': f"{e['method']} {e['path']} takes an object id: check ownership (IDOR/BOLA)"})
        if e['kind'] in ('http_route', 'webhook') and AUTH_FLOW.search(e['path']):
            leads.append({'area': 'authentication', 'entry_point': e['id'], 'why': f"{e['method']} {e['path']} is part of an auth flow: tokens, rate limits, enumeration"})
        if e['kind'] in ('http_route',) and re.search(r'(?i)admin', e['path']):
            leads.append({'area': 'access_control', 'entry_point': e['id'], 'why': f"{e['path']} is an admin surface: check function-level authorisation"})
        if e['kind'] == 'webhook':
            leads.append({'area': 'server_side', 'entry_point': e['id'], 'why': 'webhook: check signature verification, replay and any outbound fetch'})
        if e['kind'] in ('deep_link', 'exported_component'):
            leads.append({'area': 'mobile', 'entry_point': e['id'], 'why': f"{e['path']} is reachable by other apps: validate every intent/URL parameter"})
    for w in workflows:
        if w['untrusted_input_triggers']:
            leads.append({'area': 'supply_chain_ci', 'entry_point': None,
                          'why': f"{w['file']} runs on {', '.join(w['untrusted_input_triggers'])}: check for untrusted checkout and expression injection"})
    if uploads:
        leads.append({'area': 'server_side', 'entry_point': None, 'why': f'file uploads at {", ".join(uploads[:5])}: parsing, storage path, type checks'})
    if outbound:
        leads.append({'area': 'server_side', 'entry_point': None, 'why': f'{len(outbound)} outbound call(s): check for request-controlled URLs (SSRF)'})
    counts = Counter(e['auth'] for e in entries)
    out = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repository': root.name,
        'languages': dict(langs.most_common()),
        'frameworks': sorted(frameworks),
        'source_files': files_total,
        'entry_points': entries,
        'entry_point_auth': dict(counts),
        'global_auth_hooks': global_hooks,
        'data_stores': stores,
        'outbound_calls': outbound,
        'secret_config': secret_cfg,
        'env_files': env_files,
        'uploads': uploads,
        'dependency_manifests': sorted(manifests),
        'iac': sorted(iac),
        'ci_workflows': workflows,
        'review_leads': leads,
        'note': "Regex-level first pass. Confirm entry points and auth by reading the code; 'unknown' auth is not 'public'.",
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f"{len(entries)} entry point(s) {dict(counts)}, {len(stores)} data store(s), {len(outbound)} outbound call(s), "
              f"{len(workflows)} workflow(s), {len(leads)} review lead(s) -> {args.out}")
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
