#!/usr/bin/env python3
"""Next.js attack surface for recon.py, from the shared static reader (next_app.py).

    from next_surface import surface
    s = surface(model, js_files, helpers, middleware, existing_entries)
    s['entries'], s['security_headers'], s['leads']

next_app.read_next_app() reads every Next.js project in the repository (App Router and pages/ router). This module
turns that model into what a security review needs:

  entries           pages (`page`), generated metadata routes (`metadata_route`: robots, sitemap, Open Graph and
                    icon images), the built-in image optimiser (`http_route` GET /_next/image, at the images config
                    line), service workers (`service_worker`, at the worker file) and middleware in a nested project.
                    Route handlers, server actions and root middleware still come from nextjs_auth.py (their auth is
                    read from the handler body); they are annotated here with the segment config.
                    Each page and route carries `static` (prerendered at build: dynamic = 'force-static' or 'error',
                    or output: 'export') and `params_bounded` (generateStaticParams with dynamicParams = false, or a
                    static export: only the build-time values are served, every other one is a 404), which bound
                    what an attacker can reach through a dynamic segment.
  security_headers  the response headers set in next.config headers(), vercel.json, netlify.toml and public/_headers:
                    each rule with its source, the CSP directives with their weaknesses ('unsafe-inline' or
                    'unsafe-eval' for script, a wildcard or bare-scheme script source, a tag-manager host, no
                    frame-ancestors, Report-Only), headers that are missing everywhere (Content-Security-Policy,
                    frame-ancestors or X-Frame-Options, Strict-Transport-Security) unless code sets them
                    (`set_in_code`), shared-cache Cache-Control on catch-all paths, the images config and the
                    third-party scripts the pages load.
  leads             review leads for all of the above, the service workers' caching and the third-party scripts.

Everything is read from source: headers a CDN, the hosting platform or a reverse proxy adds are not seen, so a
"missing" header is a lead to check on a deployed response, not a finding by itself.
"""
from __future__ import annotations
import re

import nextjs_auth

CATCH_ALL = re.compile(r'^/(?::\w+\*|:\w+\(\.\*\)|\(\.\*\)|\*|:\w+\+?)$')
TAG_MANAGERS = re.compile(r'(?i)googletagmanager\.com|tagmanager\.google\.com|tags\.tiqcdn\.com|tealiumiq\.com|'
                          r'assets\.adobedtm\.com|cdn\.segment\.com')
WILDCARD_SOURCES = ('*', 'https:', 'http:', 'data:', 'blob:')
SET_IN_CODE = re.compile(r'''["'`](Content-Security-Policy|Strict-Transport-Security|X-Frame-Options)["'`]''', re.I)
CAP = re.compile(r'\.slice\(\s*0\s*,\s*(\d+)\s*\)|\btake\s*:\s*(\d+)|\.limit\(\s*(\d+)\s*\)|\bLIMIT\s+(\d+)', re.I)

def url_path(url: str) -> str:
    """next_app's /{id} and /{rest*} as recon's /:id and /:rest*."""
    return re.sub(r'\{(\w+)(\*?)\}', r':\1\2', url or '/') or '/'

def _merged(route: dict) -> dict:
    return {**(route.get('inherited_config') or {}), **(route.get('segment_config') or {})}

def facts(route: dict, project: dict) -> dict:
    """static / params_bounded and a sentence saying why, for a page, route handler or metadata route."""
    sc = _merged(route)
    out, why = {}, []
    export = ((project.get('config') or {}).get('settings') or {}).get('output') == 'export'
    dyn = (sc.get('dynamic') or {}).get('value')
    if export:
        out['static'] = True
        why.append("prerendered at build (output: 'export')")
    elif dyn in ('force-static', 'error'):
        out['static'] = True
        why.append(f"prerendered at build (dynamic = '{dyn}')")
    elif dyn == 'force-dynamic':
        out['static'] = False
        why.append("rendered on every request (dynamic = 'force-dynamic')")
    if route.get('params'):
        dp = (sc.get('dynamic_params') or {}).get('value')
        if export or (sc.get('generate_static_params') and dp is False):
            out['params_bounded'] = True
            why.append('only the values generateStaticParams returns are served (dynamicParams = false); any other '
                       f"{', '.join(route['params'])} is a 404")
        elif sc.get('generate_static_params'):
            why.append('generateStaticParams lists build-time values, but dynamicParams is not false, so other values '
                       'are rendered on demand')
    rv = (sc.get('revalidate') or {}).get('value')
    if rv not in (None, False):
        why.append(f'revalidate = {rv}')
    if why:
        out['segment_note'] = '; '.join(why)
    return out

def _layouts(project: dict, file: str) -> list[str]:
    """Layout files in the page's own folder and every folder above it in the app dir."""
    folder = file.rsplit('/', 1)[0]
    return [s['files']['layout']['file'] for s in project.get('segments') or []
            if 'layout' in (s.get('files') or {}) and (folder == s['dir'] or folder.startswith(s['dir'].rstrip('/') + '/'))]

def _page_auth(project, route, path, js_files, helpers, middleware) -> tuple[str, str]:
    body = '\n'.join(js_files.get(f, '') for f in [route['file']] + _layouts(project, route['file']))
    cover = next((c for mw in middleware if (c := nextjs_auth.covers(mw, path))), None)
    auth, ev = nextjs_auth.classify(body, helpers, cover)
    if auth == 'public':
        ev = 'a page anyone can request: no session or guard call in the page or its layouts, and no middleware matcher covers it'
    return auth, ev

def _entry(kind, method, path, handler, auth, ev, extra=None) -> dict:
    e = {'kind': kind, 'framework': 'nextjs', 'method': method, 'path': path, 'handler': handler, 'function': None,
         'auth': auth, 'auth_evidence': ev}
    if extra:
        e.update(extra)
    return e

def _image_entry(project: dict, js_files: dict) -> dict | None:
    conf = project.get('config') or {}
    img = conf.get('images') or {}
    settings = conf.get('settings') or {}
    if img.get('unoptimized') is True or settings.get('output') == 'export' or img.get('loader') not in (None, 'default'):
        return None  # no built-in optimiser endpoint (static export, a custom loader, or optimisation off)
    if conf.get('file'):
        handler = f"{conf['file']}:{img.get('line') or 1}"
    else:
        pkg = (project['root'] + '/' if project['root'] else '') + 'package.json'
        line = next((i for i, l in enumerate((js_files.get(pkg) or '').splitlines(), 1) if '"next"' in l), 1)
        handler = f'{pkg}:{line}'
    remote = [p.get('hostname') or p.get('url') for p in img.get('remote_patterns') or []] + list(img.get('domains') or [])
    ev = ('the built-in image optimiser: anyone can request /_next/image?url=...&w=...&q=..., and the server fetches and '
          're-encodes the image; ' + (f"remote sources allowed by images config: {', '.join(map(str, remote))}" if remote
                                      else 'no remote sources are configured, so only local images are fetched'))
    base = settings.get('basePath') if isinstance(settings.get('basePath'), str) else ''
    return _entry('http_route', 'GET', f'{base}/_next/image', handler, 'public', ev)

def _sw_summary(sw: dict) -> str:
    strat = sorted({f"{s['strategy']} for {s['when']}" + (' (stores responses)' if s.get('caches_response') else '')
                    for s in sw.get('fetch_strategies') or []})
    bits = [', '.join(strat) or 'no fetch handler read']
    if sw.get('skip_waiting'):
        bits.append('skipWaiting')
    if sw.get('clients_claim'):
        bits.append('clients.claim')
    if sw.get('generated_by'):
        bits.append(f"generated by {sw['generated_by']}")
    return '; '.join(bits)

def entries(model: dict, js_files: dict, helpers: dict, middleware: list[dict], existing: list[dict]) -> list[dict]:
    out = []
    by_file = {}
    for e in existing:
        by_file.setdefault(e['handler'].rsplit(':', 1)[0], []).append(e)
    mw_files = {m['file'] for m in middleware}
    for proj in model.get('projects') or []:
        for r in proj.get('routes') or []:
            path = url_path(r.get('url'))
            f = facts(r, proj)
            if r['kind'] in ('route_handler', 'pages_api'):
                for e in by_file.get(r['file'], []):  # nextjs_auth found it: add the segment config facts
                    e.update({k: v for k, v in f.items()})
                continue
            if r['kind'] in ('page', 'pages_page'):
                if r.get('slot') and r.get('intercepts'):
                    continue  # an intercepting route renders the same URL as the page it intercepts
                auth, ev = _page_auth(proj, r, path, js_files, helpers, middleware)
                out.append(_entry('page', 'GET', path, f"{r['file']}:{r.get('line') or 1}", auth, ev, f))
            elif r['kind'] == 'metadata' and r.get('form') == 'generated':
                ev = f"generated {r.get('metadata')} route: runs server code for anyone who requests it"
                out.append(_entry('metadata_route', 'GET', path, f"{r['file']}:{r.get('line') or 1}", 'public', ev, f))
        img = _image_entry(proj, js_files)
        if img:
            out.append(img)
        for sw in proj.get('service_workers') or []:
            reg = (sw.get('registrations') or [{}])[0]
            scope = reg.get('scope') or (sw['url'].rsplit('/', 1)[0] + '/' if sw.get('url') else '/')
            handler = f"{sw['worker_file']}:1" if sw.get('exists') and sw.get('worker_file') else \
                f"{reg.get('file')}:{reg.get('line')}" if reg.get('file') else None
            if not handler:
                continue
            ev = (f"runs in visitors' browsers and answers every request in scope {scope}: {_sw_summary(sw)}"
                  + ('' if sw.get('registrations') else '; no registration found (read whether anything registers it)'))
            out.append(_entry('service_worker', 'FETCH', f"{sw['url']} (scope {scope})", handler, 'public', ev,
                              {'registered_at': [f"{x['file']}:{x['line']}" for x in sw.get('registrations') or []]}))
        mw = proj.get('middleware')
        if mw and mw.get('file') not in mw_files and mw.get('file') in js_files:  # a nested project's middleware
            parsed = nextjs_auth.parse_middleware(mw['file'], js_files[mw['file']])
            middleware.append(parsed)
            out.append(nextjs_auth.middleware_entry(parsed))
    return out

def capped(text: str) -> str | None:
    """A literal cap on the result count in a handler file (slice(0, 20), take: 50, .limit(100), LIMIT 25)."""
    m = CAP.search(text or '')
    return next((g for g in m.groups() if g), None) if m else None

# ---------------------------------------------------------------- headers
def _rules(proj: dict) -> list[dict]:
    rules = []
    conf = proj.get('config') or {}
    for h in conf.get('headers') or []:
        rules.append({'file': conf['file'], 'line': h.get('line'), 'source': h.get('source'), 'headers': h.get('headers') or []})
    for name, plat in (proj.get('platform') or {}).items():
        for h in plat.get('headers') or []:
            rules.append({'file': plat['file'], 'line': h.get('line'), 'source': h.get('source'), 'headers': h.get('headers') or []})
    return rules

def _csps(proj: dict) -> list[dict]:
    out = list((proj.get('config') or {}).get('csp') or [])
    for plat in (proj.get('platform') or {}).values():
        out += plat.get('csp') or []
    return out

def csp_issues(directives: dict, header: str) -> list[dict]:
    script_dir = next((d for d in ('script-src-elem', 'script-src', 'default-src') if d in directives), None)
    script = directives.get(script_dir) or []
    issues = []
    nonce = any(s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) or s == "'strict-dynamic'" for s in script)
    if "'unsafe-inline'" in script and not nonce:
        issues.append({'issue': 'unsafe-inline', 'directive': script_dir, 'value': "'unsafe-inline'"})
    if "'unsafe-eval'" in script:
        issues.append({'issue': 'unsafe-eval', 'directive': script_dir, 'value': "'unsafe-eval'"})
    for d in ('script-src-elem', 'script-src', 'default-src', 'object-src'):
        for s in directives.get(d) or []:
            if s in WILDCARD_SOURCES:
                issues.append({'issue': 'wildcard_source', 'directive': d, 'value': s})
    for s in script:
        if TAG_MANAGERS.search(s):
            issues.append({'issue': 'tag_manager', 'directive': script_dir, 'value': s})
    if not script_dir:
        issues.append({'issue': 'no_script_policy', 'directive': None, 'value': 'no script-src or default-src'})
    if 'frame-ancestors' not in directives:
        issues.append({'issue': 'no_frame_ancestors', 'directive': None, 'value': None})
    if header.lower().endswith('report-only'):
        issues.append({'issue': 'report_only', 'directive': None, 'value': header})
    return issues

def security_headers(model: dict, js_files: dict) -> dict | None:
    projects = model.get('projects') or []
    if not projects:
        return None
    rules, csps, present = [], [], {}
    images, scripts = [], []
    for proj in projects:
        for r in _rules(proj):
            rules.append({'file': r['file'], 'line': r['line'], 'source': r['source'],
                          'catch_all': bool(CATCH_ALL.match(str(r['source'] or ''))),
                          'headers': [h.get('key') for h in r['headers']]})
            for h in r['headers']:
                key = str(h.get('key') or '')
                present.setdefault(key.lower(), {'header': key, 'at': f"{r['file']}:{h.get('line') or r['line'] or 1}",
                                                 'value': h.get('value')})
                if key.lower() == 'cache-control' and CATCH_ALL.match(str(r['source'] or '')):
                    v = str(h.get('value') or '')
                    if re.search(r'(?i)s-maxage\s*=\s*[1-9]', v) or (re.search(r'(?i)\bpublic\b', v) and re.search(r'(?i)max-age\s*=\s*[1-9]', v)):
                        rules[-1]['shared_cache'] = v
        for c in _csps(proj):
            csps.append({'file': c.get('file'), 'line': c.get('line'), 'source': c.get('source'), 'header': c.get('header'),
                         'directives': c.get('directives') or {}, 'unresolved': c.get('unresolved') or [],
                         'issues': csp_issues(c.get('directives') or {}, str(c.get('header') or ''))})
        conf = proj.get('config') or {}
        if conf.get('images'):
            img = dict(conf['images'])
            img['file'] = conf['file']
            hosts = [str(p.get('hostname') or '') for p in img.get('remote_patterns') or []]
            img['any_host'] = any(h in ('*', '**', '') or h.startswith('**') for h in hosts) if hosts else False
            images.append(img)
        for s in proj.get('scripts') or []:
            if s.get('host') and not s.get('first_party') and s.get('kind') in ('next/script', 'script', 'next/third-parties'):
                scripts.append({'file': s['file'], 'line': s['line'], 'kind': s['kind'], 'host': s['host'],
                                'src': s.get('src'), 'strategy': s.get('strategy'), 'integrity': bool(s.get('integrity'))})
    in_code = []
    for rel, text in js_files.items():
        if re.search(r'(^|/)next\.config\.\w+$|(^|/)__tests__/|\.(test|spec)\.\w+$', rel):
            continue  # the config is read above; tests only mention header names
        for i, line in enumerate(text.splitlines(), 1):
            m = SET_IN_CODE.search(line)
            if m:
                in_code.append({'header': m.group(1), 'at': f'{rel}:{i}'})
    coded = {x['header'].lower() for x in in_code}
    has_csp = bool(csps) or 'content-security-policy' in coded
    framing = any('frame-ancestors' in c['directives'] for c in csps) or 'x-frame-options' in present or \
        'x-frame-options' in coded or ('content-security-policy' in coded)
    missing = []
    if not has_csp:
        missing.append('Content-Security-Policy')
    if not framing:
        missing.append('frame-ancestors')
    if 'strict-transport-security' not in present and 'strict-transport-security' not in coded:
        missing.append('Strict-Transport-Security')
    return {'rules': rules, 'csp': csps, 'present': sorted(v['header'] for v in present.values()), 'missing': missing,
            'set_in_code': in_code, 'shared_cache_catch_all': [r for r in rules if r.get('shared_cache')],
            'images': images, 'third_party_scripts': scripts,
            'note': 'read from next.config headers(), vercel.json, netlify.toml and public/_headers; headers the CDN, '
                    'hosting platform or a proxy adds are not seen, so check a deployed response before reporting one '
                    'as missing'}

def leads(sec: dict | None, eps: list[dict]) -> list[dict]:
    out = []
    if not sec:
        return out
    def lead(area, why, ep=None):
        out.append({'area': area, 'entry_point': ep, 'why': why})
    for c in sec['csp']:
        where = f"{c['file']}:{c['line'] or 1}"
        for i in c['issues']:
            src = f"the CSP for {c['source']} ({where})"
            if i['issue'] == 'unsafe-inline':
                lead('web', f"{src} allows 'unsafe-inline' in {i['directive']}: an injected <script> or event handler runs, "
                            'so the CSP does not limit XSS; move inline scripts to files or use nonces or hashes')
            elif i['issue'] == 'unsafe-eval':
                lead('web', f"{src} allows 'unsafe-eval' in {i['directive']}: eval and new Function run injected strings")
            elif i['issue'] == 'wildcard_source':
                lead('web', f"{src} allows {i['value']} in {i['directive']}: scripts (or objects) load from any host with "
                            'that scheme, so an injected tag can pull in script from anywhere')
            elif i['issue'] == 'tag_manager':
                lead('supply_chain_ci', f"{src} allows {i['value']} in {i['directive']}: a tag-manager container runs whatever "
                                        'custom HTML tags its editors publish, so everyone with publish access to the '
                                        'container can run script on the site; check who that is')
            elif i['issue'] == 'no_frame_ancestors':
                pass  # reported once below, across every CSP and X-Frame-Options
            elif i['issue'] == 'report_only':
                lead('web', f"{src} is sent as {i['value']}: browsers report violations but enforce nothing")
            elif i['issue'] == 'no_script_policy':
                lead('web', f"{src} has no script-src or default-src, so it does not restrict scripts at all")
        if c['unresolved']:
            lead('web', f"the CSP at {where} is built from values read at run time ({', '.join(map(str, c['unresolved'][:4]))}): "
                        'read them, or check the header on a deployed response')
    missing = sec['missing']
    if 'Content-Security-Policy' in missing:
        lead('web', 'no Content-Security-Policy found in next.config headers(), platform header files or code: an XSS has '
                    'nothing to stop it; check the deployed response and consider a nonce-based CSP')
    if 'frame-ancestors' in missing:
        lead('web', 'no frame-ancestors directive and no X-Frame-Options header found: any site can frame the pages '
                    '(clickjacking on forms and buttons); add frame-ancestors \'none\' or \'self\'')
    if 'Strict-Transport-Security' in missing:
        lead('configuration', 'no Strict-Transport-Security header found in the repository: check the deployed response '
                              '(some hosts add it) and otherwise add max-age of at least a year with includeSubDomains')
    for r in sec['shared_cache_catch_all']:
        lead('data_exposure', f"Cache-Control {r['shared_cache']!r} on the catch-all source {r['source']} ({r['file']}:"
                              f"{r['line'] or 1}): every route, including ones added later that read cookies or show "
                              'per-user data, is cacheable by a shared cache or CDN; scope it to static paths')
    for img in sec['images']:
        where = f"{img['file']}:{img.get('line') or 1}"
        if img.get('any_host'):
            lead('server_side', f"images.remotePatterns at {where} allows any host: the image optimiser (/_next/image) "
                                'fetches any https URL a visitor names, so the server is an open image proxy (SSRF to '
                                'internal hosts is limited to what the pattern allows; check it) and a cost amplifier')
        elif img.get('domains'):
            lead('server_side', f"images.domains at {where} ({', '.join(img['domains'][:4])}): deprecated; any path on "
                                'those hosts can be fetched through /_next/image; use remotePatterns with a pathname')
        if img.get('dangerouslyAllowSVG'):
            lead('web', f"images.dangerouslyAllowSVG at {where}: SVGs served from /_next/image can carry script; "
                        'needs contentSecurityPolicy and contentDispositionType attachment')
    hosts = {}
    for s in sec['third_party_scripts']:
        hosts.setdefault(s['host'], []).append(f"{s['file']}:{s['line']}")
    for host, at in sorted(hosts.items()):
        lead('supply_chain_ci', f"third-party script from {host} ({', '.join(at[:3])}): it runs with the page's privileges "
                                '(reads the DOM, cookies not marked HttpOnly, form input); a versionless URL cannot carry '
                                'Subresource Integrity, so the vendor (or whoever controls its container) can change it')
    for e in eps:
        if e['kind'] != 'service_worker':
            continue
        ev = e.get('auth_evidence') or ''
        risky = 'cache-first' in ev or 'stale-while-revalidate' in ev or 'cache-only' in ev
        lead('web', f"service worker {e['path']} ({e['handler']}): {ev}. "
                    + ('Check it never stores responses for signed-in or per-user pages, cross-origin or opaque responses, '
                       'or error pages, that cache names are versioned and old caches deleted on activate, and how a bad '
                       'worker is rolled back (it keeps serving cached pages until it is replaced)' if risky else
                       'Check what it caches and how it is updated or removed'), e['id'])
    return out
