# Repro for F8 (resilience-prober): chunk_failure in help / open help, fault asset_abort on **/chunk-help.js.
# Needs: pip install playwright; python -m playwright install chromium; and the app running at SITE.
# The fault is injected in the browser; the server is never sent anything broken. The guard aborts every
# request to a host other than the site's and every write the scenario doesn't target (writes are let through: flows.json allows form submission), so running
# this against a shared environment sends no analytics, telemetry or stray writes.
import json, re, time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

SITE = 'http://127.0.0.1:8147/'
HOSTS = ['127.0.0.1']
ALLOW_WRITES = True
ENDPOINT = '**/chunk-help.js'
METHOD = None
HEADERS = {}            # {name: value | true (present) | false (absent)}
FAULT = {'group': 'static_asset', 'action': 'abort', 'error': 'failed'}
BEFORE = [{'goto': '#orders'}]              # the steps before the faulty one
STEP = [{'click': 'a.help'}]          # the step the fault is on
INPUT = {}           # synthetic input only
EARLY = False                # install the fault before the page loads


def guard(route, request):
    if request.url.startswith(('data:', 'blob:')):
        return route.continue_()
    if (urlparse(request.url).hostname or '').lower() not in HOSTS:
        return route.abort('blockedbyclient')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not ALLOW_WRITES:
        return route.abort()
    return route.continue_()


seen = {'n': 0}


def fault(route, request):
    if METHOD and request.method != METHOD:
        return route.fallback()
    for k, v in HEADERS.items():
        have = request.headers.get(k.lower())
        if (v is False and have is not None) or (v is True and have is None) or (v not in (True, False) and have != str(v)):
            return route.fallback()
    seen['n'] += 1
    a = FAULT['action']
    if a == 'abort':
        return route.abort(FAULT.get('error', 'failed'))
    if a == 'hang':
        return None                              # never answered while the script watches
    if a in ('delay', 'delay_abort'):
        time.sleep(FAULT['ms'] / 1000)            # blocks the script's event loop: fine for a repro
        return route.abort(FAULT.get('error', 'timedout')) if a == 'delay_abort' else route.fallback()
    if a == 'flap' and seen['n'] > FAULT['failures']:
        return route.fallback()
    body = FAULT.get('body', '')
    if 'body_size' in FAULT:
        body = '{"items": [' + ','.join(['{"title": "x"}'] * (FAULT['body_size'] // 16)) + ']}'
    route.fulfill(status=FAULT.get('status', 200), body=body if isinstance(body, str) else json.dumps(body),
                  headers=FAULT.get('headers') or {}, content_type=FAULT.get('content_type', 'application/json'))


def run(page, actions):
    for a in actions:
        if 'goto' in a:
            if a['goto'].startswith('#'):
                page.evaluate('(h) => { location.hash = h; }', a['goto'][1:])
            else:
                page.goto(SITE.rstrip('/') + '/' + a['goto'].lstrip('/'))
        elif 'click' in a:
            page.click(a['click'], timeout=5000)
        elif 'fill' in a:
            page.fill(a['fill'], a.get('value', ''), timeout=5000)
        elif 'press' in a:
            page.keyboard.press(a['press'])
        elif 'select' in a:
            page.select_option(a['select'], a.get('value'))
        elif 'wait' in a:
            page.wait_for_timeout(int(a['wait']))
        elif 'wait_for' in a:
            page.wait_for_selector(a['wait_for'], timeout=10000)
        page.wait_for_timeout(300)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(service_workers='block')
    ctx.route('**/*', guard)
    page = ctx.new_page()
    if EARLY and FAULT['action'] != 'offline':
        page.route(ENDPOINT, fault)
    page.goto(SITE)
    run(page, BEFORE)
    if FAULT['action'] == 'offline':
        ctx.set_offline(True)
    elif not EARLY:
        page.route(ENDPOINT, fault)
    run(page, STEP)
    page.wait_for_timeout(10000)
    print('requests the fault answered:', seen['n'])
    page.screenshot(path='F8.png')
    browser.close()
