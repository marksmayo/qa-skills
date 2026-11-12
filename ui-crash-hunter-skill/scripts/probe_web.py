#!/usr/bin/env python3
"""Force a crash trigger in a running web app with Playwright, and capture the evidence.

    python probe_web.py scenario.json --out-dir crash-evidence/C-1 [--base-url URL] [--emit-spec repro_C-1.py]

The scenario mocks API responses (page.route), seeds web storage once, sets locale, time zone and
viewport, then runs steps (goto, click, fill, type, press, reload, back, offline, unroute, expect,
wait_for, wait_ms). It records uncaught page errors with stack traces, console errors, every request
and its status, failed requests, whether the app root is empty (white screen) and whether a fallback
text is visible, plus a screenshot. Output is evidence.json in --out-dir. --emit-spec writes a
standalone script that replays the scenario and exits 1 while it still reproduces; keep it outside
the repository unless the user asks for it there.

A probe reproduces a crash (an error plus an empty root or a fallback text), or an effect:
scenario.expect (and expect steps) hold observable-effect assertions that describe the defect
(element or text visible or absent, a selector's count, a request made or not made with a status, a
computed style, an error message), and when every one holds the effect is observed. That is how a
silent failure (a swallowed fetch error leaving a feature dead) or a non-script failure (a 404 page
whose relative asset URLs break at nested paths) is reproduced. A scenario with "kind": "baseline"
asserts healthy behaviour instead and never counts as reproduced.

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
            actual = [c['text'] for c in ctx.get('console_errors', []) if a['console_error'] in c['text']]
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

def probe(scenario: dict, base_url: str, out_dir: Path, headed: bool = False) -> dict:
    from playwright.sync_api import sync_playwright, Error as PwError
    out_dir.mkdir(parents=True, exist_ok=True)
    errors, console, failed, steps_log = [], [], [], []
    current = lambda: steps_log[-1]['step'] if steps_log else 0
    with sync_playwright() as pw:
        bt = getattr(pw, scenario.get('browser', 'chromium'))
        browser = bt.launch(headless=not headed)
        context = browser.new_context(viewport=scenario.get('viewport') or {'width': 1280, 'height': 800},
                                      locale=scenario.get('locale'), timezone_id=scenario.get('timezone'),
                                      user_agent=scenario.get('user_agent'),
                                      service_workers=scenario.get('service_workers', 'allow'))
        blocked: list = []
        guard_third_party(context, base_url, scenario, blocked)
        context.add_init_script(seed_script(scenario.get('storage')))
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append({'after_step': current(), 'name': e.name, 'message': e.message,
                                                      'stack': e.stack}))
        page.on('console', lambda m: console.append({'after_step': current(), 'type': m.type, 'text': m.text[:1000]})
                if m.type in ('error', 'warning') else None)
        page.on('requestfailed', lambda r: None if r.url.split('?')[0] in blocked else
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
        try:
            run_steps(page, context, scenario.get('steps'), base_url, steps_log, checks)
            page.wait_for_timeout(int(scenario.get('settle_ms', 800)))
            if scenario.get('expect'):
                checks('final', scenario['expect'])
        except (PwError, ValueError) as exc:
            step_error = str(exc).splitlines()[0]
        state = observe(page, scenario.get('observe') or {})
        shot = out_dir / 'screenshot.png'
        page.screenshot(path=str(shot), full_page=True)
        own = own_errors()
        baseline = scenario.get('kind') == 'baseline'
        crash = bool(errors or own) and (state['root_empty'] or bool(state['fallback_visible']))
        effect = bool(expectations) and all(e['passed'] for e in expectations) and step_error is None
        evidence = {
            'scenario': scenario.get('name'), 'base_url': base_url, 'final_url': page.url,
            'browser': f"{scenario.get('browser', 'chromium')} {browser.version}",
            'viewport': scenario.get('viewport') or {'width': 1280, 'height': 800},
            'locale': scenario.get('locale'), 'timezone': scenario.get('timezone'),
            'captured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'steps': [{k: v for k, v in s.items() if k != 'at'} for s in steps_log], 'step_error': step_error,
            'page_errors': errors, 'console_errors': own,
            'blocked_console_errors': [c for c in console if c['type'] == 'error' and 'ERR_BLOCKED_BY_CLIENT' in c['text']],
            'blocked_requests': sorted(set(blocked)), 'failed_requests': failed, **state, 'screenshot': shot.name,
            'kind': 'baseline' if baseline else 'defect', 'requests': requests, 'expectations': expectations,
            # defect probe: every assertion describes the bad effect and held; baseline: every assertion held
            'effect_observed': effect,
            # an error boundary catches the throw and React logs it with console.error, so both count
            'reproduced': not baseline and (crash or effect),
            'reproduced_by': [] if baseline else [k for k, v in (('crash', crash), ('effect', effect)) if v],
        }
        if baseline:
            evidence['baseline_ok'] = not errors and not own and step_error is None and all(e['passed'] for e in expectations)
        browser.close()
    (out_dir / 'evidence.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return evidence

SPEC_MAIN = '''
def main():
    from playwright.sync_api import sync_playwright
    base_url = sys.argv[sys.argv.index('--base-url') + 1] if '--base-url' in sys.argv else SCENARIO['base_url']
    errors, page_errors, console, requests, results = [], [], [], [], []
    with sync_playwright() as pw:
        browser = getattr(pw, SCENARIO.get('browser', 'chromium')).launch()
        context = browser.new_context(viewport=SCENARIO.get('viewport') or {'width': 1280, 'height': 800},
                                      locale=SCENARIO.get('locale'), timezone_id=SCENARIO.get('timezone'),
                                      service_workers=SCENARIO.get('service_workers', 'allow'))
        guard_third_party(context, base_url, SCENARIO, [])
        context.add_init_script(seed_script(SCENARIO.get('storage')))
        page = context.new_page()
        def on_error(e):
            errors.append(e.message)
            page_errors.append({'name': e.name, 'message': e.message})
        def on_console(m):
            if m.type == 'error' and 'ERR_BLOCKED_BY_CLIENT' not in m.text:
                errors.append(m.text.splitlines()[0])
                console.append({'text': m.text})
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
    if crashed or effect:
        held = [json.dumps({k: v for k, v in r.items() if k != 'passed'}, ensure_ascii=False) for r in results]
        print('REPRODUCED: ' + '; '.join(errors + held))
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
    funcs = '\n\n'.join(inspect.getsource(f) for f in (seed_script, guard_third_party, apply_routes, glob_rx, log_requests,
                                                         check_expectations, run_steps, observe))
    sc = dict(scenario, base_url=scenario.get('base_url') or base_url)
    body = (f'#!/usr/bin/env python3\n"""Repro spec: {title}\n\nReplays the crash trigger with Playwright. Exits 1 while the crash '
            f'reproduces and 0 once it is fixed.\n    python {path.name} [--base-url http://localhost:5173]\n"""\n'
            f'import json, sys, time\n\nSCENARIO = json.loads(r"""{json.dumps(sc, indent=2, ensure_ascii=False)}""")\n\n'
            f'{funcs}\n{SPEC_MAIN}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('scenario', type=Path)
    ap.add_argument('--out-dir', type=Path, required=True)
    ap.add_argument('--base-url', help='overrides scenario.base_url')
    ap.add_argument('--emit-spec', type=Path, help='write a standalone repro script here')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args()
    try:
        scenario = json.loads(args.scenario.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print(f'Unable to read scenario: {exc}', file=sys.stderr)
        return 2
    base_url = args.base_url or scenario.get('base_url')
    if not base_url:
        print('No base URL: set scenario.base_url or pass --base-url', file=sys.stderr)
        return 2
    try:
        import playwright  # noqa: F401
    except ImportError:
        print('Playwright for Python is not installed. Ask the user before installing: '
              'pip install -r requirements.txt && python -m playwright install chromium', file=sys.stderr)
        return 3
    ev = probe(scenario, base_url, args.out_dir, args.headed)
    if args.emit_spec:
        emit_spec(scenario, base_url, args.emit_spec, scenario.get('name', 'crash repro'))
    verdict = ('baseline ' + ('ok' if ev['baseline_ok'] else 'NOT CLEAN')) if ev['kind'] == 'baseline' else \
        (f"REPRODUCED ({', '.join(ev['reproduced_by'])})" if ev['reproduced'] else 'not reproduced')
    held = sum(1 for e in ev['expectations'] if e['passed'])
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
