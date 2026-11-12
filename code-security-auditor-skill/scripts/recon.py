#!/usr/bin/env python3
"""Map a repository's attack surface for a security review.

    python recon.py <repo> [--out recon.json]

Finds languages and frameworks, entry points (HTTP routes, GraphQL operations, webhooks, queue
consumers, CLI commands, mobile deep links and exported components, CI workflow triggers, Next.js
route handlers, server actions and middleware, and client-side sources in browser JavaScript and
inline scripts: location.search/hash, useSearchParams, postMessage, document.referrer, window.name,
web storage and cookies; server code, config files and test setup are not client sources), a
first guess at each entry point's authentication requirement, data stores, outbound calls, secret
configuration, dependency manifests and infrastructure files. It also lists review leads: routes
taking object ids (IDOR), upload handlers, auth flows and admin surfaces.

For Next.js, each exported handler's auth is read from its body (see nextjs_auth.py): a session or
guard call, one of the repository's helpers that wraps one (auth_helpers), a machine credential
(cron_secret, api_key, signature, token), middleware coverage (middleware), or none ('public').

Python routes are read from the syntax tree (see py_routes.py), so multi-line decorators are found
and decorators quoted in docstrings are not: FastAPI routers with their prefixes and dependencies
(APIRouter(prefix=, dependencies=), include_router(prefix=, dependencies=), Depends/Security
parameters), Flask blueprints, and Azure Functions HTTP routes and triggers (blob, queue, Service
Bus, Event Hub, timer). Azure DevOps pipelines are listed in ci_workflows next to GitHub Actions,
with their triggers, runtime parameters, service connections and environments.

Everything here is a regex-level first pass. The agent confirms entry points and auth by reading
the code; an 'unknown' auth value means the script could not tell, not that the route is public.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # importing a sibling must not write __pycache__ into the skill folder
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nextjs_auth  # noqa: E402
import py_routes  # noqa: E402

SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache', 'coverage',
             'vendor', 'Pods', '.gradle', '__pycache__', '.venv', 'venv', '.tox', 'site-packages', 'target', 'bin', 'obj',
             '.terraform', '.idea'}
LANG = {'.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
        '.ts': 'TypeScript', '.tsx': 'TypeScript', '.java': 'Java', '.kt': 'Kotlin', '.go': 'Go', '.rb': 'Ruby',
        '.php': 'PHP', '.cs': 'C#', '.swift': 'Swift', '.m': 'Objective-C', '.c': 'C', '.h': 'C/C++', '.cc': 'C++',
        '.cpp': 'C++', '.rs': 'Rust', '.scala': 'Scala', '.dart': 'Dart', '.vue': 'Vue', '.svelte': 'Svelte',
        '.tf': 'Terraform', '.sql': 'SQL'}
TEST_PATH = re.compile(r'(^|/)(tests?|spec|__tests__|__mocks__|e2e|cypress|playwright|fixtures|testdata)(/|$)|(^|/)test_[^/]*$|'
                       r'_test\.\w+$|\.(test|spec)\.\w+$|(^|/)(vitest|jest|playwright)\.(setup|config)\.\w+$|(^|/)setupTests\.\w+$', re.I)
CONFIG_FILE = re.compile(r'(^|/)[\w-]+\.config\.\w+$')
MANIFESTS = {'requirements.txt', 'requirements-dev.txt', 'pyproject.toml', 'Pipfile', 'Pipfile.lock', 'poetry.lock',
             'setup.py', 'setup.cfg', 'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'go.mod',
             'go.sum', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'Gemfile', 'Gemfile.lock', 'composer.json',
             'composer.lock', 'Cargo.toml', 'Cargo.lock', 'Podfile', 'Package.swift', 'pubspec.yaml'}
FRAMEWORKS = [  # (name, regex over source)
    ('Flask', r'\bfrom flask import|\bimport flask\b'), ('FastAPI', r'\bfrom fastapi\b|\bimport fastapi\b'),
    ('Azure Functions', r'\bimport azure\.functions\b|\bfrom azure\.functions\b|\bfrom azure import functions\b'),
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
# Client-side sources: values a visitor's browser reads that another page, a crafted link or stored state controls.
# (name, regex, auth, why). URL, message and referrer sources are attacker-controlled without an account.
CLIENT_SOURCES = [
    ('location.search', re.compile(r'\blocation\.search\b|\bnew\s+URLSearchParams\s*\(\s*(?:window\.|document\.)?location\b|\buseSearchParams\s*\(\s*\)'),
     'public', 'query string: a crafted link sets it'),
    ('location.hash', re.compile(r'\blocation\.hash\b'), 'public', 'URL fragment: a crafted link sets it, and it never reaches the server'),
    ('location.href', re.compile(r'\b(?:document\.URL|document\.documentURI|location\.href|location\.pathname)\b(?!\s*=[^=])'), 'public', 'the page URL: a crafted link sets it'),
    ('postMessage', re.compile(r'addEventListener\(\s*["\']message["\']|\bonmessage\s*='), 'public', 'message events: any window can post one unless event.origin is checked'),
    ('document.referrer', re.compile(r'\bdocument\.referrer\b'), 'public', 'referrer: the linking page sets it'),
    ('window.name', re.compile(r'\bwindow\.name\b(?!\s*=[^=])'), 'public', 'window.name: survives navigation from another site'),
    ('web storage', re.compile(r'\b(?:localStorage|sessionStorage)\.getItem\s*\(|\b(?:localStorage|sessionStorage)\[\s*["\']'), 'unknown', 'stored values: controlled by any script on the origin, so they carry stored XSS'),
    ('document.cookie', re.compile(r'\bdocument\.cookie\b(?!\s*=[^=])'), 'unknown', 'cookies read by script: sibling subdomains can set them'),
]
CLIENT_EXT = {'.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.vue', '.svelte'}
HTML_EXT = {'.html', '.htm'}
SCRIPT_BLOCK = re.compile(r'<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>', re.S | re.I)
# CI triggers: who can start the workflow. pull_request_target, issue_comment and workflow_run run with
# the base repository's secrets and token on input from anyone who can open a pull request or comment.
CI_TRIGGER_AUTH = {
    'pull_request_target': ('public', 'any GitHub user who can open a pull request; runs with secrets and a write token'),
    'issue_comment': ('public', 'any GitHub user who can comment; runs with secrets'),
    'issues': ('public', 'any GitHub user who can open an issue'),
    'discussion': ('public', 'any GitHub user who can start a discussion'),
    'discussion_comment': ('public', 'any GitHub user who can comment on a discussion'),
    'pull_request_review_comment': ('public', 'any GitHub user who can review'),
    'pull_request_review': ('public', 'any GitHub user who can review'),
    'workflow_run': ('unknown', 'runs after another workflow, with secrets; public if that workflow runs on fork pull requests'),
    'pull_request': ('public', 'fork pull requests: no secrets and a read-only token by default'),
    'push': ('authenticated', 'repository write access'),
    'release': ('authenticated', 'repository write access'),
    'workflow_dispatch': ('authenticated', 'repository write access'),
    'schedule': ('authenticated', 'runs on a timer from the default branch'),
}
# Azure DevOps pipelines: who can start a run.
ADO_TRIGGER_AUTH = {
    'trigger': ('authenticated', 'anyone who can push to a matching branch'),
    'pr': ('unknown', 'anyone who can open a pull request into a matching branch; for GitHub repositories, fork builds get '
                      'secrets only if "make secrets available to fork builds" is on'),
    'schedule': ('authenticated', 'runs on a timer'),
    'pipeline_completion': ('unknown', 'runs after another pipeline completes, with this pipeline\'s secrets'),
    'manual': ('authenticated', 'anyone with queue permission; runtime parameters are caller input'),
}
ADO_DIRS = {'.azuredevops', '.pipelines', '.azure-pipelines', '.ado'}
ADO_TOP = re.compile(r'^(trigger|pr|schedules|stages|jobs|steps|pool|extends|resources)\s*:', re.M)
ADO_STEP = re.compile(r'^\s*-?\s*(task|script|bash|pwsh|powershell|checkout|template|deployment|job|stage)\s*:', re.M)
ADO_SERVICE_CONN = re.compile(r'^\s*(azureSubscription|connectedServiceName\w*|azureResourceManagerConnection|serviceConnection|'
                              r'dockerRegistryServiceConnection|kubernetesServiceConnection|externalEndpoint)\s*:\s*["\']?([^"\'#\n]+)', re.M)
ADO_TEMPLATE_REF = re.compile(r'^\s*-?\s*(?:template|extends)\s*:\s*["\']?([^"\'@#\s]+)', re.M)

def is_ado_pipeline(rel: str, text: str) -> bool:
    if '.github/workflows/' in '/' + rel or not rel.endswith(('.yml', '.yaml')):
        return False
    if re.search(r'(^|/)azure-pipelines[\w.-]*\.ya?ml$', rel):
        return True
    if re.search(r'^(apiVersion|services|kind)\s*:', text, re.M) or re.search(r'^\s*runs-on\s*:', text, re.M):
        return False  # Kubernetes, Compose, GitHub Actions
    return bool(ADO_TOP.search(text) and ADO_STEP.search(text))

def _yaml_block(lines: list[str], key: str) -> tuple[int, str, list[str]] | None:
    """(line number, inline value, indented lines below) for a top-level `key:`."""
    for i, l in enumerate(lines):
        m = re.match(rf'^{key}\s*:\s*(.*?)\s*(#.*)?$', l)
        if m:
            body = []
            for x in lines[i + 1:]:
                if x.strip() and not x.startswith((' ', '\t', '-')):
                    break
                body.append(x)
            return i + 1, m.group(1), body
    return None

def _branches(inline: str, body: list[str]) -> list[str]:
    if inline.startswith('['):
        return [b.strip(' "\'') for b in inline.strip('[]').split(',') if b.strip()]
    if inline:
        return [inline.strip('"\'')]
    out, in_exclude = [], False
    for l in body:
        if re.match(r'^\s*(exclude|paths|tags)\s*:', l):
            in_exclude = True
        elif re.match(r'^\s*(include|branches)\s*:', l):
            in_exclude = False
        m = re.match(r'^\s*-\s*["\']?([^"\'#]+?)["\']?\s*(#.*)?$', l)
        if m and not in_exclude:
            out.append(m.group(1))
    return out

def ado_pipeline_info(rel: str, text: str, template: bool) -> tuple[dict, list[dict]]:
    """ci_workflows row and entry points for an Azure DevOps pipeline (or template) file."""
    lines = text.splitlines()
    triggers, entries, all_branches, branches = [], [], False, []
    def entry(kind, line, why_extra=''):
        auth, why = ADO_TRIGGER_AUTH[kind]
        entries.append({'kind': 'ci_workflow', 'framework': 'azure_pipelines', 'method': kind, 'path': rel, 'handler': f'{rel}:{line}',
                        'function': None, 'auth': auth, 'auth_evidence': why + why_extra})
    if not template:
        t = _yaml_block(lines, 'trigger')
        if t is None:
            triggers.append('trigger')
            all_branches = True
            entry('trigger', 1, '; no trigger key, so Azure Pipelines runs on pushes to every branch')
        elif t[1].strip('"\'').lower() != 'none':
            branches = _branches(t[1], t[2])
            all_branches = not branches or any(b in ('*', 'refs/heads/*') for b in branches) or t[1].lower() == 'true'
            triggers.append('trigger')
            entry('trigger', t[0], f" ({'every branch' if all_branches else 'branches ' + ', '.join(branches[:5])})")
        pr = _yaml_block(lines, 'pr')
        if pr and pr[1].strip('"\'').lower() != 'none':
            triggers.append('pr')
            entry('pr', pr[0])
        sch = _yaml_block(lines, 'schedules')
        if sch:
            triggers.append('schedule')
            entry('schedule', sch[0])
        res = _yaml_block(lines, 'resources')
        if res and re.search(r'^\s+pipelines\s*:', '\n'.join(res[2]), re.M):
            triggers.append('pipeline_completion')
            entry('pipeline_completion', res[0])
    params = []
    pb = _yaml_block(lines, 'parameters')
    if pb:
        params = re.findall(r'^\s*-\s*name\s*:\s*["\']?([\w.-]+)', '\n'.join(pb[2]), re.M)
        if params and not template:
            triggers.append('manual')
            entry('manual', pb[0], f" ({', '.join(params[:6])})")
    conns = sorted({m.group(2).strip() for m in ADO_SERVICE_CONN.finditer(text)})
    envs = sorted({m.strip(' "\'') for m in re.findall(r'^\s*environment\s*:[ \t]*([^\n#{]+)', text, re.M) if m.strip(' "\'')} |
                  set(re.findall(r'^\s*environment\s*:\s*\n\s+name\s*:\s*["\']?([^"\'\s#]+)', text, re.M)))
    info = {'file': rel, 'system': 'azure_pipelines', 'template': template, 'triggers': triggers, 'all_branches': all_branches,
            'branches': branches, 'runtime_parameters': params, 'service_connections': conns, 'environments': envs,
            'deployment_jobs': len(re.findall(r'^\s*-\s*deployment\s*:', text, re.M)),
            'untrusted_input_triggers': ['pr'] if 'pr' in triggers else [], 'write_permissions': [],
            'uses_secrets': bool(re.search(r'^\s*-\s*group\s*:|AzureKeyVault@|\$\(\w*(secret|password|token|key)\w*\)', text, re.M | re.I))}
    return info, entries

def client_sources(rel: str, text: str, base_line: int = 0) -> list[dict]:
    """One entry per client-side source kind per file (first line where it is read)."""
    out, seen = [], set()
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith(('//', '*', '/*')) or len(line) > 3000:
            continue
        for name, rx, auth, why in CLIENT_SOURCES:
            if name in seen or not rx.search(line):
                continue
            seen.add(name)
            out.append({'kind': 'client_source', 'framework': 'browser', 'method': name, 'path': rel,
                        'handler': f'{rel}:{base_line + i}', 'function': None, 'auth': auth, 'auth_evidence': why,
                        'snippet': s[:160]})
    return out

def html_client_sources(rel: str, text: str) -> list[dict]:
    out, seen = [], set()
    for m in SCRIPT_BLOCK.finditer(text):
        base = text[:m.start(1)].count('\n')
        for e in client_sources(rel, m.group(1), base):
            if e['method'] not in seen:
                seen.add(e['method'])
                out.append(e)
    return out

def ci_trigger_entries(rel: str, text: str, triggers: list[str]) -> list[dict]:
    out = []
    lines = text.splitlines()
    for t in triggers:
        rx = re.compile(r'^\s*' + re.escape(t) + r'\s*:|^on\s*:.*\b' + re.escape(t) + r'\b')
        line = next((i for i, l in enumerate(lines, 1) if rx.search(l)), 1)
        auth, why = CI_TRIGGER_AUTH.get(t, ('unknown', None))
        out.append({'kind': 'ci_workflow', 'framework': 'github_actions', 'method': t, 'path': rel,
                    'handler': f'{rel}:{line}', 'function': None, 'auth': auth, 'auth_evidence': why})
    return out

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
                if p.name not in SKIP_DIRS and (not p.name.startswith('.') or p.name == '.github' or p.name in ADO_DIRS):
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

def classify_decorators(text: str) -> tuple[str, str | None]:
    """Auth from the decorators stacked on a Python view (Flask login_required, roles_required...)."""
    for rx, label in ((AUTH_PUBLIC, 'public'), (AUTH_ADMIN, 'admin'), (AUTH_USER, 'authenticated')):
        m = rx.search(text)
        if m:
            return label, m.group(0)
    return 'unknown', None

def handler_after(lines: list[str], i: int) -> str | None:
    for l in lines[i:i + 8]:
        m = re.search(r'\b(?:async\s+)?def\s+(\w+)|\bfunction\s+(\w+)|\bpublic\s+\S+\s+(\w+)\s*\(|\bfunc\s+(\w+)', l)
        if m:
            return next(g for g in m.groups() if g)
    return None

def routes_in(rel: str, text: str, python_parsed: bool = False) -> list[dict]:
    """Regex routes. Flask and FastAPI come from py_routes (the syntax tree) unless the file did not parse."""
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
        if rel.endswith('.py') and (fw not in ('django', 'flask', 'fastapi') or (python_parsed and fw != 'django')):
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
    client, ci_entries = [], []
    html_seen: dict = {}
    js_files: dict[str, str] = {}
    py_files: dict[str, str] = {}
    ado_files: dict[str, str] = {}
    for p in walk(root):
        rel = p.relative_to(root).as_posix()
        if p.name in MANIFESTS:
            manifests.append(rel)
        if p.name.startswith('.env'):
            env_files.append(rel)
        if p.name in ('Dockerfile', 'Containerfile') or p.name.startswith('Dockerfile.') or \
                re.match(r'(docker-)?compose([.-][\w-]+)?\.ya?ml$', p.name) or p.suffix in ('.tf', '.bicep', '.bicepparam') or \
                re.search(r'(^|/)(k8s|kubernetes|helm|charts|deploy)/.*\.ya?ml$', rel) or \
                (p.suffix == '.json' and re.search(r'deploymentTemplate\.json|deploymentParameters\.json', read(p)[:2000])):
            iac.append(rel)
        if '.github/workflows/' in '/' + rel and p.suffix in ('.yml', '.yaml'):
            wtext = read(p)
            workflows.append(workflow_info(rel, wtext))
            ci_entries += ci_trigger_entries(rel, wtext, workflows[-1]['triggers'])
            continue
        if p.suffix in ('.yml', '.yaml'):
            ytext = read(p)
            if is_ado_pipeline(rel, ytext):
                ado_files[rel] = ytext
            continue
        if p.suffix.lower() in HTML_EXT and not TEST_PATH.search(rel):
            for e in html_client_sources(rel, read(p)):
                key = (e['method'], e['snippet'])
                if key in html_seen:  # the same inline script repeated across generated pages
                    html_seen[key]['also_in'] = html_seen[key].get('also_in', 0) + 1
                else:
                    html_seen[key] = e
                    client += [e]
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
        entries += routes_in(rel, text, python_parsed=p.suffix == '.py')
        if p.suffix == '.py':
            py_files[rel] = text
        if p.suffix.lower() in CLIENT_EXT:
            js_files[rel] = text
        if p.suffix.lower() in CLIENT_EXT and not p.name.endswith('.min.js') and not CONFIG_FILE.search(rel) \
                and not nextjs_auth.server_file(rel, text):
            client += client_sources(rel, text)
        if 'gql`' in text or 'typeDefs' in text:
            entries += graphql_ops(rel, text)
        for i, line in enumerate(text.splitlines(), 1):
            for kind, rx in STORES:
                if CONFIG_FILE.search(rel):
                    break  # next.config.ts listing @prisma/client in serverExternalPackages is not a data store
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
    # Python: FastAPI, Flask and Azure Functions from the syntax tree, routers followed across files
    py_eps, py_failed = py_routes.entries(py_files, root, classify_decorators)
    entries += py_eps
    for rel in py_failed:  # Python 2 or broken syntax: fall back to the line patterns
        entries += [e for e in routes_in(rel, py_files[rel]) if e['framework'] != 'django']
    # Azure DevOps pipelines: templates are the files other pipelines include
    referenced = {Path(m).name for t in ado_files.values() for m in ADO_TEMPLATE_REF.findall(t)}
    for rel, t in sorted(ado_files.items()):
        template = Path(rel).name in referenced or not re.search(r'^(trigger|pr|schedules|pool|extends|name)\s*:', t, re.M)
        info, eps = ado_pipeline_info(rel, t, template)
        workflows.append(info)
        ci_entries += eps
    # Next.js: route handlers, server actions and middleware, with per-handler auth (helpers found across the repo)
    middleware = [nextjs_auth.parse_middleware(rel, t) for rel, t in js_files.items() if nextjs_auth.MIDDLEWARE_FILE.search(rel)]
    helpers = nextjs_auth.guard_helpers(js_files) if any(
        nextjs_auth.ROUTE_FILE.search(r) or nextjs_auth.PAGES_API.search(r) or "use server" in t for r, t in js_files.items()) else {}
    for rel, t in js_files.items():
        entries += nextjs_auth.route_entries(rel, t, helpers, middleware) + nextjs_auth.server_actions(rel, t, helpers)
    for mw in middleware:
        entries.append(nextjs_auth.middleware_entry(mw))
        if mw['auth_check']:
            global_hooks.append(f"{mw['file']}:{mw['line']}")
    entries += queues + clis + client + ci_entries
    for e in entries:  # Flask: no decorator and no app-wide hook means no auth check (FastAPI's is decided in py_routes)
        if e['framework'] == 'flask' and e['auth'] == 'unknown' and not global_hooks:
            e['auth'], e['auth_evidence'] = 'public', 'no auth decorator on the route and no app-wide auth hook found'
    for n, e in enumerate(entries, 1):
        e['id'] = f'EP-{n}'
    leads = []
    for e in entries:
        if e['kind'] in ('http_route', 'webhook', 'graphql') and ID_PARAM.search(e['path']):
            leads.append({'area': 'access_control', 'entry_point': e['id'], 'why': f"{e['method']} {e['path']} takes an object id: check ownership (IDOR/BOLA)"})
        if e['kind'] in ('http_route', 'webhook') and AUTH_FLOW.search(re.sub(r':\w+', '', e['path'])):
            leads.append({'area': 'authentication', 'entry_point': e['id'], 'why': f"{e['method']} {e['path']} is part of an auth flow: tokens, rate limits, enumeration"})
        if e['kind'] in ('http_route',) and re.search(r'(?i)admin', e['path']):
            leads.append({'area': 'access_control', 'entry_point': e['id'], 'why': f"{e['path']} is an admin surface: check function-level authorisation"})
        if e['kind'] == 'webhook':
            leads.append({'area': 'server_side', 'entry_point': e['id'], 'why': 'webhook: check signature verification, replay and any outbound fetch'})
        if e['framework'] == 'nextjs' and e['kind'] in ('http_route', 'webhook', 'server_action') and e['auth'] == 'public' \
                and e['method'] not in ('GET', 'HEAD', 'OPTIONS') and 'by design' not in (e['auth_evidence'] or ''):
            leads.append({'area': 'access_control', 'entry_point': e['id'],
                          'why': f"{e['method']} {e['path']} changes state with no auth call in the handler: confirm it is meant to be public"})
        if e['kind'] in ('deep_link', 'exported_component'):
            leads.append({'area': 'mobile', 'entry_point': e['id'], 'why': f"{e['path']} is reachable by other apps: validate every intent/URL parameter"})
    for kind in sorted({e['method'] for e in client if e['auth'] == 'public'}):
        eps = [e for e in client if e['method'] == kind]
        leads.append({'area': 'web', 'entry_point': eps[0]['id'],
                      'why': f"{kind} read in {len(eps)} file(s) ({', '.join(e['path'] for e in eps[:3])}): trace it to DOM "
                             "sinks (innerHTML, document.write, eval, location assignment) for DOM XSS and open redirects"})
    for w in workflows:
        if w['untrusted_input_triggers']:
            leads.append({'area': 'supply_chain_ci', 'entry_point': None,
                          'why': f"{w['file']} runs on {', '.join(w['untrusted_input_triggers'])}: check for untrusted checkout and expression injection"})
        if w.get('system') != 'azure_pipelines':
            continue
        privileged = w['service_connections'] or w['uses_secrets']
        what = ', '.join(w['service_connections'][:3]) or 'secrets'
        if w['all_branches'] and privileged:
            leads.append({'area': 'supply_chain_ci', 'entry_point': None,
                          'why': f"{w['file']} runs on pushes to every branch and uses {what}: anyone who can push a branch "
                                 "runs code with them; limit the trigger or move them behind an environment approval"})
        if w['runtime_parameters']:
            leads.append({'area': 'supply_chain_ci', 'entry_point': None,
                          'why': f"{w['file']} takes runtime parameters ({', '.join(w['runtime_parameters'][:5])}): check none is "
                                 "expanded into a script (${{ parameters.x }} is pasted in before the shell runs)"})
        if w['service_connections'] and not w['deployment_jobs'] and not w['template']:
            leads.append({'area': 'supply_chain_ci', 'entry_point': None,
                          'why': f"{w['file']} uses service connection(s) {what} outside a deployment job: no environment "
                                 "approval or check gates them"})
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
        # what a reviewer may read: source plus infrastructure and CI definitions (seed_report.py uses it)
        'files_in_scope': files_total + len({f for f in set(iac) | {w['file'] for w in workflows} if Path(f).suffix not in LANG}),
        'python_parse_failures': py_failed,
        'entry_points': entries,
        'entry_point_auth': dict(counts),
        'global_auth_hooks': global_hooks,
        'middleware': middleware,
        'auth_helpers': {name: {'kind': k, 'defined_at': w} for name, (k, w) in sorted(helpers.items())},
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
