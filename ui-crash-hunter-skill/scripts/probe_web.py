#!/usr/bin/env python3
"""Force a crash trigger in a running web app with Playwright, and capture the evidence.

    python probe_web.py scenario.json --out-dir crash-evidence/C-1 [--base-url URL] [--emit-spec repro_C-1.py]
                        [--authorised-by "who approved a non-loopback target"]

The scenario mocks API responses (page.route), seeds web storage once, sets locale, time zone and
viewport, then runs steps (goto, click, fill, type, press, reload, back, offline, unroute, expect,
wait_for, wait_ms). It records uncaught page errors with stack traces, console errors (with their
arguments evaluated: an Error logged by an error boundary is kept as {name, message, stack}), every
request and its status, failed requests, whether the app root is empty (white screen) and whether a
fallback text is visible, plus a screenshot. Output is evidence.json in --out-dir. --emit-spec writes a
standalone script that replays the scenario (with the same browser, context options, init scripts
and guards) and exits 1 while it still reproduces; keep it outside the repository unless the user
asks for it there.

Environment options: "storage_blocked": true (or {"except": [stack substrings]}) makes the
localStorage/sessionStorage getters throw SecurityError, as when the browser blocks site data;
"init_scripts": [js string | {"file": path relative to the scenario}] run before the page's scripts;
"browser_prefs" (Firefox about:config, e.g. {"network.cookie.cookieBehavior": 2}) and "launch_args"
(Chromium switches); "bypass_csp".

Read-only mode ("read_only", the default for any target that is not loopback): every request that is
not GET, HEAD or OPTIONS is answered in the browser with 204 and never sent, to any host including
the target (the app's own POSTs, CSP reports, beacons); navigator.sendBeacon is stubbed, same-origin
analytics (/_vercel/*) is aborted and CSP is bypassed so no violation report is sent. Intercepted
writes are listed in writes_blocked. A non-loopback target is refused unless --authorised-by (or
the scenario's authorised_by) names who approved it.

A probe reproduces a crash (an error plus an empty root or a fallback text), or an effect:
scenario.expect (and expect steps) hold observable-effect assertions that describe the defect
(element or text visible or absent, a selector's count, a request made or not made with a status, a
computed style, an error message), and when every one holds the effect is observed. That is how a
silent failure (a swallowed fetch error leaving a feature dead) or a non-script failure (a 404 page
whose relative asset URLs break at nested paths) is reproduced. A scenario with "kind": "baseline"
asserts healthy behaviour instead and never counts as reproduced. A probe whose page ended off the app
(the browser's own error page after a navigation to a blocked host, a redirect to another origin such
as an identity provider, or the observe.root_selector missing) is inconclusive (evidence inconclusive
and inconclusive_reason): never a clean baseline and never a reproduction.

Local files: --base-url may be a local HTML file (a generated report) or a file:// URL. A local file is
loopback and probed read-only by default; goto steps resolve against the file ("#flow-3"). Recorded and
blocked requests keep their query string; a request glob without '?' also matches the URL without it.

Signed-in apps: --storage-state loads a Playwright storage-state file (cookies and localStorage) that a
person saved after signing in to a test account by hand. The file is read, never copied into the
evidence (which records only signed_in_session: true); keep it outside the repository and never commit
it. sessionStorage is not part of a storage state, so a session kept there (MSAL's default cache) needs
the app's own test configuration instead.

Third parties are blocked: every request to a host that is not loopback, the base URL's host or in
scenario.allowed_hosts is aborted and listed in blocked_requests (GETs included), so a probe never
sends analytics hits or ad requests. Console errors and failed requests caused by that blocking are
kept apart (blocked_console_errors) and never count towards a reproduction. scenario.third_party
"allow" lets them through; scenario.service_workers "block" stops service workers.

Needs the Playwright Python package and a browser (see requirements.txt). The rest of the skill
does not. Point it only at local or test environments the user has approved. Run it rather than
importing it; a wrapper that must import it should run with `python -B` (or set
sys.dont_write_bytecode before the import), so no __pycache__ is written into the skill folder.
"""
from __future__ import annotations
import argparse, inspect, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

def seed_script(storage: dict) -> str:
    """Init script that seeds storage on the first load only, so reloads see what the app wrote."""
    data = json.dumps(storage or {})
    return ("(() => { try { if (sessionStorage.getItem('__probe_seeded')) return;"
            f" const s = {data};"
            " for (const [k, v] of Object.entries(s.localStorage || {})) localStorage.setItem(k, v);"
            " for (const [k, v] of Object.entries(s.sessionStorage || {})) sessionStorage.setItem(k, v);"
            " sessionStorage.setItem('__probe_seeded', '1'); } catch (e) {} })();")

def is_loopback(url: str) -> bool:
    """True for localhost, *.localhost, *.test, loopback addresses and local files (file://)."""
    from urllib.parse import urlparse
    import ipaddress
    if urlparse(url).scheme == 'file':
        return True
    host = (urlparse(url).hostname or '').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.test')):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def read_only(scenario: dict, base_url: str) -> bool:
    """Read-only mode is the default for any target that is not loopback, and for a local file (a generated
    report opened as file://: any request it makes to a server is not the user's to send)."""
    return bool(scenario.get('read_only', not is_loopback(base_url) or base_url.startswith('file:')))

def target_url(base: str) -> str:
    """The probe target as a URL: a local file path (a generated report.html) becomes a file:// URL."""
    from pathlib import Path as _P
    if '://' in base or base.startswith(('about:', 'data:')):
        return base
    p = _P(base)
    return p.resolve().as_uri() if p.exists() or p.suffix.lower() in ('.html', '.htm') else base

def step_url(base_url: str, arg: str) -> str:
    """The URL a goto step opens: absolute as given; on a file:// target relative to the file ('#flow-3',
    'other.html'); on a server relative to its root."""
    from urllib.parse import urljoin
    if '://' in arg:
        return arg
    if base_url.startswith('file:'):
        return urljoin(base_url, arg) if arg else base_url
    return base_url.rstrip('/') + '/' + arg.lstrip('/')

def blocked_storage_script(spec) -> str:
    """Init script: window.localStorage / sessionStorage getters throw SecurityError, as when the browser
    blocks site data. spec is true, or {"except": [stack substrings]} to spare callers whose stack
    contains one of them (to isolate one of several throwing call sites)."""
    spare = json.dumps((spec or {}).get('except', []) if isinstance(spec, dict) else [])
    return ("(() => { const spare = " + spare + "; const real = {};"
            " for (const k of ['localStorage', 'sessionStorage']) { try { real[k] = window[k]; } catch (e) {} }"
            " const deny = (k) => { const ex = new DOMException(\"Failed to read the '\" + k + \"' property from 'Window': "
            "Access is denied for this document.\", 'SecurityError');"
            " try { Error.captureStackTrace(ex, deny); } catch (e) {} throw ex; };"
            " for (const k of ['localStorage', 'sessionStorage']) { try { Object.defineProperty(window, k, {"
            " configurable: true, get: () => { const st = String(new Error().stack);"
            " if (spare.some((x) => st.includes(x))) return real[k]; return deny(k); } }); } catch (e) {} } })();")

def init_scripts(scenario: dict, base_url: str) -> list:
    """Every init script, in order: the read-only beacon stub, blocked storage, the storage seed, then
    the scenario's own init_scripts (inline strings; files are read in when the scenario is loaded)."""
    out = []
    if read_only(scenario, base_url):
        out.append("(() => { try { navigator.sendBeacon = () => true; } catch (e) {} })();")
    if scenario.get('storage_blocked'):
        out.append(blocked_storage_script(scenario['storage_blocked']))
    else:
        out.append(seed_script(scenario.get('storage')))
    for s in scenario.get('init_scripts') or []:
        out.append(s['content'] if isinstance(s, dict) else s)
    return out

def launch_options(scenario: dict, headed: bool = False) -> dict:
    """Browser launch options: launch_args (Chromium/WebKit command-line switches) and browser_prefs
    (Firefox about:config preferences, e.g. network.cookie.cookieBehavior 2 to block site data)."""
    opts: dict = {'headless': not headed}
    if scenario.get('launch_args'):
        opts['args'] = list(scenario['launch_args'])
    if scenario.get('browser_prefs'):
        if scenario.get('browser', 'chromium') != 'firefox':
            raise ValueError('browser_prefs are Firefox preferences; use launch_args for Chromium')
        opts['firefox_user_prefs'] = dict(scenario['browser_prefs'])
    return opts

def context_options(scenario: dict, base_url: str, storage_state=None) -> dict:
    """Browser context options. bypass_csp defaults to true in read-only mode, so a CSP violation never
    sends a report to the target. storage_state is a Playwright storage-state file a person saved after
    signing in to a test account (cookies and localStorage); it is loaded, never copied into the evidence."""
    opts = {'viewport': scenario.get('viewport') or {'width': 1280, 'height': 800},
            'locale': scenario.get('locale'), 'timezone_id': scenario.get('timezone'),
            'user_agent': scenario.get('user_agent'), 'service_workers': scenario.get('service_workers', 'allow'),
            'bypass_csp': bool(scenario.get('bypass_csp', read_only(scenario, base_url)))}
    if storage_state:
        opts['storage_state'] = str(storage_state)
    return opts

def left_app(final_url: str, base_url: str) -> str | None:
    """Why the page is no longer the app under test, or None: the browser's own error page (a navigation
    to a blocked third party or a refused connection ends on chrome-error://, about:neterror, about:blank)
    or another origin (a sign-in redirect to an identity provider)."""
    from urllib.parse import urlparse
    u = (final_url or '').lower()
    if u.startswith(('chrome-error:', 'about:', 'edge-error:')) or 'neterror' in u:
        return f'the page ended on the browser\'s own page ({final_url.split("?")[0][:80]}), not the app'
    a, b = urlparse(final_url or ''), urlparse(base_url or '')
    if a.scheme in ('http', 'https') and (a.hostname, a.port) != (b.hostname, b.port):
        return f'the page navigated away from the app to {a.hostname} (a sign-in redirect or an external link)'
    return None

def install_guards(context, base_url: str, scenario: dict, blocked: list, writes: list) -> None:
    """The third-party guard, then (registered last, so it runs first) the read-only guard: every
    request that is not GET or HEAD is answered in the browser with 204 and never sent, to any host,
    the target included (the app's own POSTs, CSP reports, beacons), and same-origin analytics
    (/_vercel/*) is aborted."""
    guard_third_party(context, base_url, scenario, blocked)
    if not read_only(scenario, base_url):
        return
    def handler(route, request):
        url = request.url.split('?')[0]
        if '/_vercel/' in url:
            blocked.append(url)
            return route.abort('blockedbyclient')
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return route.fallback()
        writes.append({'method': request.method, 'url': url})
        return route.fulfill(status=204, body='')
    context.route('**/*', handler)

CONSOLE_ARG_JS = """(e) => { try {
    if (e instanceof Error || (e && typeof e === 'object' && 'message' in e && 'name' in e && 'stack' in e))
        return {name: String(e.name), message: String(e.message), stack: String(e.stack || '')};
    if (e && typeof e === 'object') return JSON.parse(JSON.stringify(e));
    return e === undefined ? null : e;
} catch (x) { return String(e); } }"""

def console_entry(m, step: int) -> dict:
    """A console message with its arguments evaluated: Error objects as {name, message, stack} (React
    logs an error caught by a boundary with console.error(error), and Firefox's text for it is just
    'JSHandle@object'), other objects as JSON."""
    args, err = [], None
    for a in m.args[:4]:
        try:
            v = a.evaluate(CONSOLE_ARG_JS)
        except Exception:  # the page navigated away; keep the text
            v = None
        if isinstance(v, dict) and {'name', 'message', 'stack'} <= set(v) and err is None:
            err = {'name': v['name'], 'message': v['message'], 'stack': v['stack'][:4000]}
        args.append(v if not isinstance(v, str) else v[:1000])
    text = m.text[:1000]
    if err and err['message'] not in text:
        text = f"{err['name']}: {err['message']} ({text[:200]})"
    entry = {'after_step': step, 'type': m.type, 'text': text}
    if err:
        entry['error'] = err
    if args and any(a is not None for a in args):
        entry['args'] = json.loads(json.dumps(args, default=str))[:4]
    return entry

def console_haystack(c: dict) -> str:
    e = c.get('error') or {}
    return ' '.join([c.get('text', ''), f"{e.get('name', '')}: {e.get('message', '')}", e.get('stack', ''),
                     json.dumps(c.get('args') or [], default=str)])

def guard_third_party(context, base_url: str, scenario: dict, blocked: list) -> None:
    """Abort every request to a host that is not loopback, the base URL's host or an allowed host."""
    from urllib.parse import urlparse
    import ipaddress
    if scenario.get('third_party', 'block') == 'allow':
        return
    site = (urlparse(base_url).hostname or '').lower()
    allowed = [h.lower() for h in scenario.get('allowed_hosts', [])]

    def ok(url: str) -> bool:
        if url.startswith(('data:', 'blob:', 'about:')):
            return True
        host = (urlparse(url).hostname or '').lower()
        if host in (site, 'localhost') or host.endswith(('.localhost', '.test')):
            return True
        try:
            if ipaddress.ip_address(host).is_loopback:
                return True
        except ValueError:
            pass
        return any(host == p or (p.startswith('*.') and host.endswith(p[1:])) for p in allowed)

    def handler(route, request):
        if ok(request.url):
            return route.fallback()
        blocked.append(request.url)
        return route.abort('blockedbyclient')
    context.route('**/*', handler)

def apply_routes(page, routes: list) -> None:
    def make(r):
        def handler(route):
            if r.get('delay_ms'):
                time.sleep(r['delay_ms'] / 1000)
            if r.get('abort'):
                return route.abort(r['abort'])
            body = json.dumps(r['json']) if 'json' in r else r.get('body', '')
            headers = {'content-type': 'application/json' if 'json' in r else r.get('content_type', 'text/plain')}
            route.fulfill(status=r.get('status', 200), body=body, headers=headers)
        return handler
    for r in routes or []:
        page.route(r['url'], make(r))

def url_matches(pattern: str, url: str) -> bool:
    """A request glob against a recorded URL (query string kept): a glob without '?' also matches the URL
    without its query, so '**/report.json' matches '/report.json?t=1' and '**/report.json?t=*' matches too."""
    rx = glob_rx(pattern)
    return bool(rx.match(url) or ('?' not in pattern and rx.match(url.split('?')[0])))

def glob_rx(pattern: str):
    """Playwright-style URL glob: ** matches anything, * anything but '/'."""
    import re
    out = ''
    for part in re.split(r'(\*\*|\*)', pattern):
        out += '.*' if part == '**' else '[^/]*' if part == '*' else re.escape(part)
    return re.compile('^' + out + '$')

def log_requests(page, requests: list) -> None:
    """Record every request the page makes (URL with its query string, method, status), for request assertions."""
    seen: dict = {}
    def on_response(resp):  # an HTTP error status still arrives here, even when the browser then drops the body
        if len(requests) < 2000:
            seen[id(resp.request)] = entry = {'url': resp.url, 'method': resp.request.method,
                                              'status': resp.status, 'failed': False}
            requests.append(entry)
    def on_failed(req):
        if id(req) in seen:
            seen[id(req)]['failed'] = True
        elif len(requests) < 2000:
            requests.append({'url': req.url, 'method': req.method, 'status': None, 'failed': True})
    page.on('response', on_response)
    page.on('requestfailed', on_failed)

def check_expectations(page, assertions, ctx: dict) -> list:
    """Evaluate observable-effect assertions. Each holds when the page shows that effect:
    {"visible": sel} {"hidden": sel} {"text_visible": t} {"text_absent": t}
    {"count": sel, "equals"|"min"|"max": n} {"style": sel, "property": p, "equals"|"contains"|"not_equals"|"not_contains": v}
    {"request": glob, "made": true|false, "status": n} {"page_error": substring} {"console_error": substring}"""
    out = []
    for a in ([assertions] if isinstance(assertions, dict) else assertions or []):
        actual, ok = None, False
        if 'visible' in a or 'hidden' in a:
            loc = page.locator(a.get('visible') or a.get('hidden'))
            actual = loc.count() > 0 and loc.first.is_visible()
            ok = actual if 'visible' in a else not actual
        elif 'text_visible' in a or 'text_absent' in a:
            loc = page.get_by_text(a.get('text_visible') or a.get('text_absent'))
            actual = loc.count() > 0 and loc.first.is_visible()
            ok = actual if 'text_visible' in a else not actual
        elif 'count' in a:
            actual = page.locator(a['count']).count()
            ok = (('equals' not in a or actual == a['equals']) and ('min' not in a or actual >= a['min'])
                  and ('max' not in a or actual <= a['max']))
        elif 'style' in a:
            actual = page.evaluate('([s, p]) => { const e = document.querySelector(s);'
                                   ' return e ? getComputedStyle(e).getPropertyValue(p) : null; }', [a['style'], a['property']])
            v = str(a.get('equals', a.get('contains', a.get('not_equals', a.get('not_contains', '')))))
            ok = actual is not None and (actual == v if 'equals' in a else v in actual if 'contains' in a
                                         else actual != v if 'not_equals' in a else v not in actual)
        elif 'request' in a:
            hits = [r for r in ctx.get('requests', []) if url_matches(a['request'], r['url'])
                    and ('status' not in a or r['status'] == a['status'])]
            actual = len(hits)
            ok = bool(hits) == bool(a.get('made', True))
        elif 'page_error' in a:
            actual = [e['message'] for e in ctx.get('page_errors', []) if a['page_error'] in f"{e['name']}: {e['message']}"]
            ok = bool(actual)
        elif 'console_error' in a:
            actual = [c['text'] for c in ctx.get('console_errors', []) if a['console_error'] in console_haystack(c)]
            ok = bool(actual)
        else:
            raise ValueError(f'unknown expectation: {a}')
        out.append({**a, 'actual': actual, 'passed': bool(ok)})
    return out

STEP_ACTIONS = {'goto': 'str', 'click': 'str', 'fill': 'pair', 'type': 'pair', 'press': 'str', 'reload': 'any',
                'back': 'any', 'offline': 'any', 'unroute': 'str', 'expect': 'assert', 'wait_for': 'str', 'wait_ms': 'int'}
EXPECT_KEYS = ('visible', 'hidden', 'text_visible', 'text_absent', 'count', 'style', 'request', 'page_error', 'console_error')


def normalise_step(step) -> tuple:
    """(action, arg) from a step. One action key per step; fill and type take a selector and a text, written
    either {"fill": ["#email", "a@b.test"]} or {"fill": "#email", "value": "a@b.test"} ("text" works too)."""
    if not isinstance(step, dict) or not step:
        raise ValueError(f'a step must be an object with one action, e.g. {{"click": "#save"}}; got {step!r}')
    keys = [k for k in step if k in STEP_ACTIONS]
    rest = sorted(set(step) - set(keys) - {'value', 'text'})
    if len(keys) != 1 or rest:
        raise ValueError(f'step {json.dumps(step)[:120]}: one action key from {", ".join(STEP_ACTIONS)} '
                         f'(plus "value" for fill/type) expected' + (f'; unknown key(s) {", ".join(rest)}' if rest else ''))
    action = keys[0]
    arg = step[action]
    if STEP_ACTIONS[action] == 'pair':
        companion = step.get('value', step.get('text'))
        if isinstance(arg, str) and companion is not None:
            arg = [arg, str(companion)]
        if not (isinstance(arg, list) and len(arg) == 2 and all(isinstance(x, str) for x in arg)):
            raise ValueError(f'step {json.dumps(step)[:120]}: {action} takes a selector and a text, written '
                             f'{{"{action}": ["#sel", "text"]}} or {{"{action}": "#sel", "value": "text"}}')
    elif 'value' in step or 'text' in step:
        raise ValueError(f'step {json.dumps(step)[:120]}: "value" belongs only with fill or type')
    elif STEP_ACTIONS[action] == 'str' and not isinstance(arg, str):
        raise ValueError(f'step {json.dumps(step)[:120]}: {action} takes a string (a selector, URL path, key or glob)')
    elif STEP_ACTIONS[action] == 'int' and (isinstance(arg, bool) or not isinstance(arg, (int, float))):
        raise ValueError(f'step {json.dumps(step)[:120]}: wait_ms takes a number of milliseconds')
    return action, arg


def validate_scenario(scenario: dict) -> list:
    """Problems with a scenario's steps and assertions, found before any browser starts."""
    problems = []
    if not isinstance(scenario, dict):
        return ['the scenario must be a JSON object']
    for n, step in enumerate(scenario.get('steps') or [], 1):
        try:
            action, arg = normalise_step(step)
        except ValueError as exc:
            problems.append(f'step {n}: {exc}')
            continue
        if action == 'expect':
            problems += [f'step {n}: {p}' for p in validate_assertions(arg)]
    problems += [f'expect: {p}' for p in validate_assertions(scenario.get('expect'))]
    if scenario.get('kind') not in (None, 'baseline', 'defect'):
        problems.append(f'kind must be "baseline" or "defect", not {scenario.get("kind")!r}')
    return problems


def validate_assertions(assertions) -> list:
    out = []
    for a in ([assertions] if isinstance(assertions, dict) else assertions or []):
        if not isinstance(a, dict) or not any(k in a for k in EXPECT_KEYS):
            out.append(f'unknown assertion {json.dumps(a)[:120]}: use one of {", ".join(EXPECT_KEYS)}')
        elif 'style' in a and 'property' not in a:
            out.append(f'style assertion {json.dumps(a)[:120]} needs "property"')
    return out


def run_steps(page, context, steps: list, base_url: str, log: list, checks=None) -> None:
    for n, step in enumerate(steps or [], 1):
        action, arg = normalise_step(step)
        log.append({'step': n, 'action': action, 'arg': arg, 'at': round(time.time(), 3)})
        if action == 'goto':
            page.goto(step_url(base_url, arg), wait_until='load')
        elif action == 'click':
            page.click(arg, timeout=5000)
        elif action == 'fill':
            page.fill(arg[0], arg[1], timeout=5000)
        elif action == 'type':  # key by key, so input listeners fire as they do for a person typing
            page.locator(arg[0]).first.press_sequentially(arg[1], delay=20, timeout=5000)
        elif action == 'unroute':  # stop mocking: the next request goes to the real server ("the server recovered")
            page.unroute(arg)
        elif action == 'expect':  # assertions at this point; the final scenario.expect runs after settle_ms
            if checks is not None:
                checks(n, arg)
        elif action == 'press':
            page.keyboard.press(arg)
        elif action == 'reload':
            page.reload(wait_until='load')
        elif action == 'back':
            page.go_back(wait_until='load')
        elif action == 'offline':
            context.set_offline(bool(arg))
        elif action == 'wait_for':
            page.wait_for_selector(arg, timeout=5000)
        elif action == 'wait_ms':
            page.wait_for_timeout(int(arg))
        else:
            raise ValueError(f'unknown step action: {action}')

def observe(page, obs: dict) -> dict:
    root = obs.get('root_selector', 'body')
    js = """([root, texts]) => {
        const el = document.querySelector(root);
        const text = (document.body && document.body.innerText) || '';
        return {root_found: !!el, root_empty: !!el && el.children.length === 0 && !el.textContent.trim(),
                fallback_visible: texts.filter(t => text.includes(t)), body_text: text.slice(0, 400)};
    }"""
    return page.evaluate(js, [root, obs.get('fallback_text') or []])

def probe(scenario: dict, base_url: str, out_dir: Path, headed: bool = False, storage_state=None) -> dict:
    from playwright.sync_api import sync_playwright, Error as PwError
    out_dir.mkdir(parents=True, exist_ok=True)
    errors, console, failed, steps_log = [], [], [], []
    current = lambda: steps_log[-1]['step'] if steps_log else 0
    t0 = time.monotonic()
    timing: dict = {}
    with sync_playwright() as pw:
        bt = getattr(pw, scenario.get('browser', 'chromium'))
        browser = bt.launch(**launch_options(scenario, headed))
        timing['launch_s'] = round(time.monotonic() - t0, 2)
        context = browser.new_context(**context_options(scenario, base_url, storage_state))
        blocked: list = []
        writes: list = []
        install_guards(context, base_url, scenario, blocked, writes)
        for script in init_scripts(scenario, base_url):
            context.add_init_script(script)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append({'after_step': current(), 'name': e.name, 'message': e.message,
                                                      'stack': e.stack}))
        page.on('console', lambda m: console.append(console_entry(m, current())) if m.type in ('error', 'warning') else None)
        page.on('requestfailed', lambda r: None if r.url in blocked else
                failed.append({'after_step': current(), 'url': r.url, 'failure': r.failure}))
        apply_routes(page, scenario.get('routes'))
        requests: list = []
        log_requests(page, requests)
        own_errors = lambda: [c for c in console if c['type'] == 'error' and 'ERR_BLOCKED_BY_CLIENT' not in c['text']]
        expectations: list = []
        def checks(step_no, assertions):
            ctx = {'requests': requests, 'page_errors': errors, 'console_errors': own_errors()}
            expectations.extend({**r, 'after_step': step_no} for r in check_expectations(page, assertions, ctx))
        step_error = None
        final_checked = False
        t_steps = time.time()
        try:
            run_steps(page, context, scenario.get('steps'), base_url, steps_log, checks)
            t_end = time.time()
            page.wait_for_timeout(int(scenario.get('settle_ms', 800)))
            if scenario.get('expect'):
                checks('final', scenario['expect'])
                final_checked = True
        except (PwError, ValueError) as exc:
            t_end = time.time()
            step_error = str(exc).splitlines()[0]
        # how long each step took (to the next step's start, the last to the end of the steps): slow loads show here
        for i, st in enumerate(steps_log):
            nxt = steps_log[i + 1]['at'] if i + 1 < len(steps_log) else t_end
            st['ms'] = int(round((nxt - st['at']) * 1000))
        timing['steps_s'] = round(t_end - t_steps, 2)
        state = observe(page, scenario.get('observe') or {})
        shot = out_dir / 'screenshot.png'
        page.screenshot(path=str(shot), full_page=True)
        own = own_errors()
        baseline = scenario.get('kind') == 'baseline'
        # a probe that ended off the app (the browser's error page, a sign-in redirect), or without the app's root
        # when the scenario names one, checked nothing about the app: inconclusive, never clean or reproduced
        away = left_app(page.url, base_url)
        if not away and (scenario.get('observe') or {}).get('root_selector') and not state['root_found']:
            away = f"the app root ({scenario['observe']['root_selector']}) was not on the page"
        crash = not away and bool(errors or own) and (state['root_empty'] or bool(state['fallback_visible']))
        effect = not away and bool(expectations) and all(e['passed'] for e in expectations) and step_error is None
        evidence = {
            'scenario': scenario.get('name'), 'base_url': base_url, 'final_url': page.url,
            'browser': f"{scenario.get('browser', 'chromium')} {browser.version}",
            'viewport': scenario.get('viewport') or {'width': 1280, 'height': 800},
            'locale': scenario.get('locale'), 'timezone': scenario.get('timezone'),
            'captured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'steps': [{k: v for k, v in s.items() if k != 'at'} for s in steps_log], 'step_error': step_error,
            # a step error stops the scenario: its final expectations were never evaluated (not "held" or "failed")
            'expectations_skipped': bool(scenario.get('expect')) and not final_checked,
            'page_errors': errors, 'console_errors': own,
            'blocked_console_errors': [c for c in console if c['type'] == 'error' and 'ERR_BLOCKED_BY_CLIENT' in c['text']],
            'blocked_requests': sorted(set(blocked)), 'failed_requests': failed, **state, 'screenshot': shot.name,
            'kind': 'baseline' if baseline else 'defect', 'requests': requests, 'expectations': expectations,
            'read_only': read_only(scenario, base_url), 'writes_blocked': writes,
            'bypass_csp': context_options(scenario, base_url)['bypass_csp'],
            'context': {k: scenario[k] for k in ('storage_blocked', 'launch_args', 'browser_prefs', 'authorised_by')
                        if scenario.get(k)} | ({'init_scripts': len(scenario['init_scripts'])} if scenario.get('init_scripts') else {})
                       | ({'signed_in_session': True} if storage_state else {}),
            'inconclusive': bool(away), 'inconclusive_reason': away,
            # defect probe: every assertion describes the bad effect and held; baseline: every assertion held
            'effect_observed': effect,
            # an error boundary catches the throw and React logs it with console.error, so both count
            'reproduced': not baseline and (crash or effect),
            'reproduced_by': [] if baseline else [k for k, v in (('crash', crash), ('effect', effect)) if v],
        }
        if baseline:
            evidence['baseline_ok'] = not away and not errors and not own and step_error is None and \
                all(e['passed'] for e in expectations)
        browser.close()
    timing['total_s'] = round(time.monotonic() - t0, 2)
    evidence['timing'] = timing
    (out_dir / 'evidence.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return evidence

SPEC_MAIN = '''
def main():
    from playwright.sync_api import sync_playwright
    base_url = target_url(sys.argv[sys.argv.index('--base-url') + 1] if '--base-url' in sys.argv else SCENARIO['base_url'])
    errors, page_errors, console, requests, results = [], [], [], [], []
    if not is_loopback(base_url) and not SCENARIO.get('authorised_by'):
        print('Refusing: ' + base_url + ' is not loopback and the scenario names no authorised_by.')
        return 2
    storage_state = sys.argv[sys.argv.index('--storage-state') + 1] if '--storage-state' in sys.argv else None
    with sync_playwright() as pw:
        browser = getattr(pw, SCENARIO.get('browser', 'chromium')).launch(**launch_options(SCENARIO))
        context = browser.new_context(**context_options(SCENARIO, base_url, storage_state))
        writes = []
        install_guards(context, base_url, SCENARIO, [], writes)
        for script in init_scripts(SCENARIO, base_url):
            context.add_init_script(script)
        page = context.new_page()
        def on_error(e):
            errors.append(e.name + ': ' + e.message)
            page_errors.append({'name': e.name, 'message': e.message})
        def on_console(m):
            if m.type == 'error' and 'ERR_BLOCKED_BY_CLIENT' not in m.text:
                c = console_entry(m, 0)
                errors.append(c['text'].splitlines()[0][:300])
                console.append(c)
        page.on('pageerror', on_error)
        page.on('console', on_console)
        apply_routes(page, SCENARIO.get('routes'))
        log_requests(page, requests)
        ctx = {'requests': requests, 'page_errors': page_errors, 'console_errors': console}
        checks = lambda n, a: results.extend(check_expectations(page, a, ctx))
        run_steps(page, context, SCENARIO.get('steps'), base_url, [], checks)
        page.wait_for_timeout(int(SCENARIO.get('settle_ms', 800)))
        if SCENARIO.get('expect'):
            checks('final', SCENARIO['expect'])
        state = observe(page, SCENARIO.get('observe') or {})
        away = left_app(page.url, base_url)
        browser.close()
    if away:
        print('Inconclusive: ' + away + '; nothing about the app was checked.')
        return 2
    crashed = bool(errors) and (state['root_empty'] or bool(state['fallback_visible']))
    # the expectations describe the defect's visible effect; in a baseline they describe healthy behaviour
    effect = SCENARIO.get('kind') != 'baseline' and bool(results) and all(r['passed'] for r in results)
    if writes:
        print(f'{len(writes)} write request(s) answered in the browser and never sent (read-only mode).')
    if crashed or effect:
        held = [json.dumps({k: v for k, v in r.items() if k != 'passed'}, ensure_ascii=False)[:200] for r in results]
        lines = list(dict.fromkeys(errors))[:5] + held[:5]
        print('REPRODUCED:\\n  ' + '\\n  '.join(lines))
        return 1
    if SCENARIO.get('kind') == 'baseline':
        ok = not errors and all(r['passed'] for r in results)
        print('Baseline ' + ('clean.' if ok else 'NOT clean: ' + '; '.join(errors + [json.dumps(r) for r in results if not r['passed']])))
        return 0 if ok else 1
    print('Not reproduced: no uncaught error replaced the screen' + (', and the expected effect did not appear.' if results else '.'))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
'''

def emit_spec(scenario: dict, base_url: str, path: Path, title: str) -> None:
    funcs = '\n\n'.join(inspect.getsource(f) for f in (
        normalise_step, seed_script, is_loopback, read_only, target_url, step_url, blocked_storage_script, init_scripts, launch_options, context_options,
        install_guards, console_entry, console_haystack, guard_third_party, apply_routes, url_matches, glob_rx, log_requests,
        check_expectations, run_steps, observe, left_app))
    funcs = f'CONSOLE_ARG_JS = """{CONSOLE_ARG_JS}"""\n\nSTEP_ACTIONS = {STEP_ACTIONS!r}\n\n{funcs}'
    sc = dict(scenario, base_url=scenario.get('base_url') or base_url)
    body = (f'#!/usr/bin/env python3\n"""Repro spec: {title}\n\nReplays the crash trigger with Playwright. Exits 1 while the crash '
            f'reproduces, 0 once it is fixed and 2 when the page left the app (inconclusive).\n'
            f'    python {path.name} [--base-url http://localhost:5173] [--storage-state <signed-in state file>]\n"""\n'
            f'import json, sys, time\n\nSCENARIO = json.loads(r"""{json.dumps(sc, indent=2, ensure_ascii=False)}""")\n\n'
            f'{funcs}\n{SPEC_MAIN}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('scenario', type=Path)
    ap.add_argument('--out-dir', type=Path)
    ap.add_argument('--check', action='store_true', help='validate the scenario (steps and assertions) and exit')
    ap.add_argument('--base-url', help='overrides scenario.base_url')
    ap.add_argument('--emit-spec', type=Path, help='write a standalone repro script here')
    ap.add_argument('--headed', action='store_true')
    ap.add_argument('--authorised-by', help='who approved probing a non-loopback target (required for one)')
    ap.add_argument('--storage-state', type=Path,
                    help='a Playwright storage-state file a person saved after signing in to a test account; loaded '
                         'into the browser context, never copied into the evidence. Keep it outside the repository')
    args = ap.parse_args()
    if args.storage_state and not args.storage_state.is_file():
        print(f'--storage-state {args.storage_state} not found', file=sys.stderr)
        return 2
    try:
        scenario = json.loads(args.scenario.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print(f'Unable to read scenario: {exc}', file=sys.stderr)
        return 2
    if not args.check and args.out_dir is None:
        ap.error('--out-dir is required (except with --check)')
    problems = validate_scenario(scenario)
    if problems:  # before any browser starts: a malformed step would otherwise fail mid-run and skip the assertions
        print(f'Invalid scenario {args.scenario}:\n  ' + '\n  '.join(problems), file=sys.stderr)
        return 2
    if args.check:
        print(f'{args.scenario}: valid ({len(scenario.get("steps") or [])} step(s))')
        return 0
    base_url = args.base_url or scenario.get('base_url')
    if not base_url:
        print('No base URL: set scenario.base_url or pass --base-url', file=sys.stderr)
        return 2
    base_url = target_url(base_url)
    if args.authorised_by:
        scenario['authorised_by'] = args.authorised_by
    if not is_loopback(base_url) and not scenario.get('authorised_by'):
        print(f'Refusing to probe {base_url}: it is not a loopback address. Probe only environments the user approved: '
              'pass --authorised-by "<who approved it>" (or set authorised_by in the scenario). The probe then runs '
              'read-only unless the scenario sets "read_only": false.', file=sys.stderr)
        return 2
    try:  # init scripts given as files are read in now, so the evidence and the repro spec carry their content
        scenario['init_scripts'] = [
            {'file': s['file'], 'content': (args.scenario.parent / s['file']).read_text(encoding='utf-8')}
            if isinstance(s, dict) and 'file' in s and 'content' not in s else s
            for s in scenario.get('init_scripts') or []]
    except OSError as exc:
        print(f'Unable to read an init script: {exc}', file=sys.stderr)
        return 2
    try:
        import playwright  # noqa: F401
    except ImportError:
        print('Playwright for Python is not installed. Ask the user before installing: '
              'pip install -r requirements.txt && python -m playwright install chromium', file=sys.stderr)
        return 3
    ev = probe(scenario, base_url, args.out_dir, args.headed, args.storage_state)
    if args.emit_spec and ev['kind'] == 'baseline':
        print(f'--emit-spec skipped: a baseline probe describes healthy behaviour and never reproduces, so it has no '
              f'repro spec ({args.emit_spec} not written)')
    elif args.emit_spec:
        emit_spec(scenario, base_url, args.emit_spec, scenario.get('name', 'crash repro'))
    verdict = f"INCONCLUSIVE ({ev['inconclusive_reason']})" if ev['inconclusive'] else \
        ('baseline ' + ('ok' if ev['baseline_ok'] else 'NOT CLEAN')) if ev['kind'] == 'baseline' else \
        (f"REPRODUCED ({', '.join(ev['reproduced_by'])})" if ev['reproduced'] else 'not reproduced')
    if ev['writes_blocked']:
        print(f"read-only: {len(ev['writes_blocked'])} write request(s) answered in the browser and never sent")
    held = sum(1 for e in ev['expectations'] if e['passed'])
    if ev.get('expectations_skipped'):
        print('  the final expectations were not evaluated: a step failed first (see step error)')
    slow = sorted(ev['steps'], key=lambda st: -st.get('ms', 0))[:1]
    print(f"  timing: launch {ev['timing'].get('launch_s')}s, steps {ev['timing'].get('steps_s')}s, total "
          f"{ev['timing'].get('total_s')}s" + (f"; slowest step {slow[0]['step']} ({slow[0]['action']}) {slow[0]['ms']} ms"
                                                if slow and 'ms' in slow[0] else ''))
    print(f"{verdict}: {len(ev['page_errors'])} page error(s), root empty: {ev['root_empty']}, "
          f"fallback: {', '.join(ev['fallback_visible']) or 'none'}, expectations held: {held}/{len(ev['expectations'])}"
          f"{', step error: ' + ev['step_error'] if ev['step_error'] else ''} -> {args.out_dir / 'evidence.json'}")
    for e in ev['expectations']:
        if not e['passed']:
            print('  did not hold: ' + json.dumps({k: v for k, v in e.items() if k != 'passed'}, ensure_ascii=False)[:200])
    for e in ev['page_errors'][:3]:
        print(f"  {e['name']}: {e['message']}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
