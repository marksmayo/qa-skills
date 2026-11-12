#!/usr/bin/env python3
"""Repro spec: C-3: recipe with no ratings yet (rating null)

Replays the crash trigger with Playwright. Exits 1 while the crash reproduces and 0 once it is fixed.
    python repro_C-3.py [--base-url http://localhost:5173]
"""
import json, sys, time

SCENARIO = json.loads(r"""{
  "base_url": "http://localhost:4173",
  "browser": "chromium",
  "viewport": {
    "width": 1280,
    "height": 800
  },
  "locale": "en-AU",
  "timezone": "Australia/Sydney",
  "observe": {
    "root_selector": "#root",
    "fallback_text": [
      "Something went wrong",
      "Not available right now."
    ]
  },
  "settle_ms": 800,
  "name": "C-3: recipe with no ratings yet (rating null)",
  "routes": [
    {
      "url": "**/api/recipes/r-5",
      "status": 200,
      "json": {
        "id": "r-5",
        "title": "Miso greens",
        "author": {
          "id": "u-1",
          "displayName": "Mia Chen"
        },
        "rating": null,
        "ratingCount": 0,
        "tags": [
          "dinner"
        ],
        "ingredients": [
          {
            "name": "arborio rice",
            "quantity": 300,
            "unit": "g",
            "aisle": "Pantry"
          },
          {
            "name": "lemon",
            "quantity": 1,
            "aisle": "Produce"
          },
          {
            "name": "salt",
            "quantity": null
          }
        ],
        "steps": [
          "Toast the rice.",
          "Add stock a ladle at a time.",
          "Finish with lemon and butter."
        ],
        "publishedAt": "2026-09-14T08:00:00Z",
        "nutrition": {
          "calories": 1840,
          "protein": 44,
          "servings": 4
        }
      }
    }
  ],
  "steps": [
    {
      "goto": "/#/recipes/r-5"
    }
  ]
}""")

CONSOLE_ARG_JS = """(e) => { try {
    if (e instanceof Error || (e && typeof e === 'object' && 'message' in e && 'name' in e && 'stack' in e))
        return {name: String(e.name), message: String(e.message), stack: String(e.stack || '')};
    if (e && typeof e === 'object') return JSON.parse(JSON.stringify(e));
    return e === undefined ? null : e;
} catch (x) { return String(e); } }"""

def seed_script(storage: dict) -> str:
    """Init script that seeds storage on the first load only, so reloads see what the app wrote."""
    data = json.dumps(storage or {})
    return ("(() => { try { if (sessionStorage.getItem('__probe_seeded')) return;"
            f" const s = {data};"
            " for (const [k, v] of Object.entries(s.localStorage || {})) localStorage.setItem(k, v);"
            " for (const [k, v] of Object.entries(s.sessionStorage || {})) sessionStorage.setItem(k, v);"
            " sessionStorage.setItem('__probe_seeded', '1'); } catch (e) {} })();")


def is_loopback(url: str) -> bool:
    """True for localhost, *.localhost, *.test and loopback addresses."""
    from urllib.parse import urlparse
    import ipaddress
    host = (urlparse(url).hostname or '').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.test')):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def read_only(scenario: dict, base_url: str) -> bool:
    """Read-only mode is the default for any target that is not loopback."""
    return bool(scenario.get('read_only', not is_loopback(base_url)))


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


def context_options(scenario: dict, base_url: str) -> dict:
    """Browser context options. bypass_csp defaults to true in read-only mode, so a CSP violation never
    sends a report to the target."""
    return {'viewport': scenario.get('viewport') or {'width': 1280, 'height': 800},
            'locale': scenario.get('locale'), 'timezone_id': scenario.get('timezone'),
            'user_agent': scenario.get('user_agent'), 'service_workers': scenario.get('service_workers', 'allow'),
            'bypass_csp': bool(scenario.get('bypass_csp', read_only(scenario, base_url)))}


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
        blocked.append(request.url.split('?')[0])
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


def glob_rx(pattern: str):
    """Playwright-style URL glob: ** matches anything, * anything but '/'."""
    import re
    out = ''
    for part in re.split(r'(\*\*|\*)', pattern):
        out += '.*' if part == '**' else '[^/]*' if part == '*' else re.escape(part)
    return re.compile('^' + out + '$')


def log_requests(page, requests: list) -> None:
    """Record every request the page makes (URL without the query, method, status), for request assertions."""
    seen: dict = {}
    def on_response(resp):  # an HTTP error status still arrives here, even when the browser then drops the body
        if len(requests) < 2000:
            seen[id(resp.request)] = entry = {'url': resp.url.split('?')[0], 'method': resp.request.method,
                                              'status': resp.status, 'failed': False}
            requests.append(entry)
    def on_failed(req):
        if id(req) in seen:
            seen[id(req)]['failed'] = True
        elif len(requests) < 2000:
            requests.append({'url': req.url.split('?')[0], 'method': req.method, 'status': None, 'failed': True})
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
            rx = glob_rx(a['request'])
            hits = [r for r in ctx.get('requests', []) if rx.match(r['url'])
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


def run_steps(page, context, steps: list, base_url: str, log: list, checks=None) -> None:
    for n, step in enumerate(steps or [], 1):
        (action, arg), = step.items()
        log.append({'step': n, 'action': action, 'arg': arg, 'at': round(time.time(), 3)})
        if action == 'goto':
            page.goto(arg if '://' in arg else base_url.rstrip('/') + '/' + arg.lstrip('/'), wait_until='load')
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


def main():
    from playwright.sync_api import sync_playwright
    base_url = sys.argv[sys.argv.index('--base-url') + 1] if '--base-url' in sys.argv else SCENARIO['base_url']
    errors, page_errors, console, requests, results = [], [], [], [], []
    if not is_loopback(base_url) and not SCENARIO.get('authorised_by'):
        print('Refusing: ' + base_url + ' is not loopback and the scenario names no authorised_by.')
        return 2
    with sync_playwright() as pw:
        browser = getattr(pw, SCENARIO.get('browser', 'chromium')).launch(**launch_options(SCENARIO))
        context = browser.new_context(**context_options(SCENARIO, base_url))
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
        browser.close()
    crashed = bool(errors) and (state['root_empty'] or bool(state['fallback_visible']))
    # the expectations describe the defect's visible effect; in a baseline they describe healthy behaviour
    effect = SCENARIO.get('kind') != 'baseline' and bool(results) and all(r['passed'] for r in results)
    if writes:
        print(f'{len(writes)} write request(s) answered in the browser and never sent (read-only mode).')
    if crashed or effect:
        held = [json.dumps({k: v for k, v in r.items() if k != 'passed'}, ensure_ascii=False)[:200] for r in results]
        lines = list(dict.fromkeys(errors))[:5] + held[:5]
        print('REPRODUCED:\n  ' + '\n  '.join(lines))
        return 1
    if SCENARIO.get('kind') == 'baseline':
        ok = not errors and all(r['passed'] for r in results)
        print('Baseline ' + ('clean.' if ok else 'NOT clean: ' + '; '.join(errors + [json.dumps(r) for r in results if not r['passed']])))
        return 0 if ok else 1
    print('Not reproduced: no uncaught error replaced the screen' + (', and the expected effect did not appear.' if results else '.'))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
