#!/usr/bin/env python3
"""Force a crash trigger in a running web app with Playwright, and capture the evidence.

    python probe_web.py scenario.json --out-dir crash-evidence/C-1 [--base-url URL] [--emit-spec repro_C-1.py]

The scenario mocks API responses (page.route), seeds web storage once, sets locale, time zone and
viewport, then runs steps (goto, click, fill, reload, back, offline, wait). It records uncaught page
errors with stack traces, console errors, failed requests, whether the app root is empty (white
screen) and whether a fallback text is visible, plus a screenshot. Output is evidence.json in
--out-dir. --emit-spec writes a standalone script that replays the scenario and exits 1 while the
crash still reproduces; keep it outside the repository unless the user asks for it there.

Third parties are blocked: every request to a host that is not loopback, the base URL's host or in
scenario.allowed_hosts is aborted and listed in blocked_requests (GETs included), so a probe never
sends analytics hits or ad requests. Console errors and failed requests caused by that blocking are
kept apart (blocked_console_errors) and never count towards a reproduction. scenario.third_party
"allow" lets them through; scenario.service_workers "block" stops service workers.

Needs the Playwright Python package and a browser (see requirements.txt). The rest of the skill
does not. Point it only at local or test environments the user has approved.
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

def run_steps(page, context, steps: list, base_url: str, log: list) -> None:
    for n, step in enumerate(steps or [], 1):
        (action, arg), = step.items()
        log.append({'step': n, 'action': action, 'arg': arg, 'at': round(time.time(), 3)})
        if action == 'goto':
            page.goto(arg if '://' in arg else base_url.rstrip('/') + '/' + arg.lstrip('/'), wait_until='load')
        elif action == 'click':
            page.click(arg, timeout=5000)
        elif action == 'fill':
            page.fill(arg[0], arg[1], timeout=5000)
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
        step_error = None
        try:
            run_steps(page, context, scenario.get('steps'), base_url, steps_log)
            page.wait_for_timeout(int(scenario.get('settle_ms', 800)))
        except (PwError, ValueError) as exc:
            step_error = str(exc).splitlines()[0]
        state = observe(page, scenario.get('observe') or {})
        shot = out_dir / 'screenshot.png'
        page.screenshot(path=str(shot), full_page=True)
        own = [c for c in console if c['type'] == 'error' and 'ERR_BLOCKED_BY_CLIENT' not in c['text']]
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
            # an error boundary catches the throw and React logs it with console.error, so both count
            'reproduced': bool(errors or own)
                          and (state['root_empty'] or bool(state['fallback_visible'])),
        }
        browser.close()
    (out_dir / 'evidence.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return evidence

SPEC_MAIN = '''
def main():
    from playwright.sync_api import sync_playwright
    base_url = sys.argv[sys.argv.index('--base-url') + 1] if '--base-url' in sys.argv else SCENARIO['base_url']
    errors = []
    with sync_playwright() as pw:
        browser = getattr(pw, SCENARIO.get('browser', 'chromium')).launch()
        context = browser.new_context(viewport=SCENARIO.get('viewport') or {'width': 1280, 'height': 800},
                                      locale=SCENARIO.get('locale'), timezone_id=SCENARIO.get('timezone'),
                                      service_workers=SCENARIO.get('service_workers', 'allow'))
        guard_third_party(context, base_url, SCENARIO, [])
        context.add_init_script(seed_script(SCENARIO.get('storage')))
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(e.message))
        page.on('console', lambda m: errors.append(m.text.splitlines()[0]) if m.type == 'error' and 'ERR_BLOCKED_BY_CLIENT' not in m.text else None)
        apply_routes(page, SCENARIO.get('routes'))
        run_steps(page, context, SCENARIO.get('steps'), base_url, [])
        page.wait_for_timeout(int(SCENARIO.get('settle_ms', 800)))
        state = observe(page, SCENARIO.get('observe') or {})
        browser.close()
    crashed = bool(errors) and (state['root_empty'] or bool(state['fallback_visible']))
    print(('REPRODUCED: ' + '; '.join(errors)) if crashed else 'Not reproduced: no uncaught error replaced the screen.')
    return 1 if crashed else 0

if __name__ == '__main__':
    raise SystemExit(main())
'''

def emit_spec(scenario: dict, base_url: str, path: Path, title: str) -> None:
    funcs = '\n\n'.join(inspect.getsource(f) for f in (seed_script, guard_third_party, apply_routes, run_steps, observe))
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
    print(f"{'REPRODUCED' if ev['reproduced'] else 'not reproduced'}: {len(ev['page_errors'])} page error(s), "
          f"root empty: {ev['root_empty']}, fallback: {', '.join(ev['fallback_visible']) or 'none'} -> {args.out_dir / 'evidence.json'}")
    for e in ev['page_errors'][:3]:
        print(f"  {e['name']}: {e['message']}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
