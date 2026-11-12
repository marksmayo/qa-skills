#!/usr/bin/env python3
"""Run the same pages and states in each engine, several times, and record what each run saw.

    python capture.py targets.json --out run/capture [--engines chromium,firefox,webkit] [--reruns 3]

targets.json:
    {"site": "http://127.0.0.1:8145/", "pages": [{"name": "home", "path": "index.html",
       "states": [{"name": "menu-open", "critical_path": true, "viewports": ["390x844"],
                   "actions": [{"click": "#menu"}, {"wait_for": "#nav.open"}]}]}],
     "viewports": ["1280x800", "390x844"], "engines": ["chromium", "firefox", "webkit"], "reruns": 3,
     "authorised_by": null, "auth": null, "allow_form_submission": false,
     "third_party": "block", "allowed_hosts": [], "service_workers": "allow", "scroll": true,
     "bypass_csp": true, "reduced_motion": "reduce", "freeze_animations": true,
     "rate_limit_seconds": 0.5, "retries": 2, "max_retry_after_seconds": 60, "request_budget": null}

bypass_csp: true (the default) creates every context with Playwright's bypass_csp, so the styles capture injects
(animation freeze, content-visibility) apply on sites whose Content-Security-Policy has a hash or nonce in
style-src. Each injected style is read back; one that didn't apply makes the run errored, never ran. runs.json
records the setting and whether the harness or capture applied it.

reduced_motion: "reduce" (the default) asks every page for reduced motion, and freeze_animations (default true)
stops CSS animations and transitions, for stable screenshots; engine differences in motion are then not
compared, and the report lists that as out of scope. "no-preference" and false turn them off.

Remote-site hygiene: rate_limit_seconds between page loads (default 0.1 on loopback, 0.5 elsewhere); a 429 or
503 is retried up to `retries` times after Retry-After (seconds or a date), never waiting more than
max_retry_after_seconds, and each wait is recorded in the run's `backoff`; still 429/503 after that, the run
is errored. Every run records requests_sent (requests the page made that the harness let out), runs.json totals
them in `requests`, and with request_budget set, runs after the total reaches it are recorded as skipped.

A run that couldn't load, inject or capture has run_status "errored" (or "skipped") with the error text; an
engine none of whose runs captured is "errored", never "ran".

A page or a state may carry "viewports", a subset of the top-level list, to run only there (a mobile
menu state at the mobile width only); a state inherits its page's. The planned checks (page or state
x viewport) are recorded in runs.json as "planned", and the validator counts from them.

third_party: block (the default) aborts every request to a host that isn't the site, loopback or in
allowed_hosts (analytics, ads, embeds, fonts from another host) and records it in blocked_requests;
the report then says third-party behaviour wasn't compared. allow lets them load. service_workers:
allow (the default) or block, for every run.

scroll: true (the default) scrolls through the page a viewport at a time before the full-page
screenshot, waits for lazy images to load (up to 5 s), and returns to the top. Firefox and WebKit
draw a full-page screenshot without moving the viewport, so without it loading=lazy images below
the fold come out blank there and look like content that isn't rendered. For the same reason
content-visibility is set to visible while the screenshot is taken (a user scrolling would see that
content rendered). Each run records what the scroll did.

Per page (and state) x viewport x engine x rerun, in a fresh context: a full-page screenshot; console
errors, page errors and failed requests; every visible element's signature, box and own text; images
with their natural size; whether the page overflows horizontally; the scripts loaded (to spot
user-agent sniffing); and for a state, the outcome of each step (ok, or the error). An engine that
isn't installed is recorded as not_run.

Output: <out>/runs.json and <out>/<engine>/<viewport>/<page>[~state].r<N>.png. Playwright's WebKit
is not Safari; the scope says so, and nothing here calls it Safari.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, is_loopback, NotAuthorised, now  # noqa: E402
from compare_engines import planned_checks  # noqa: E402

STABLE_CSS = ('*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important; '
              'transition: none !important; caret-color: transparent !important; } :root { --qa-capture-freeze: 1; }')
# Each injected style sets a marker custom property, read back to prove the style applied: a Content-Security-Policy
# that refuses inline styles fails silently in some engines.
APPLIED = "(name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() === '1'"
RETRY_STATUS = (429, 503)

# Firefox and WebKit draw a full-page screenshot without scrolling, so content-visibility:auto content outside the
# viewport would come out blank; a user scrolling to it would see it rendered.
# How an engine reports a request the harness aborted, without naming it: Chromium's console says ERR_BLOCKED_BY_CLIENT.
# Other console errors are dropped only when they name a blocked URL.
BLOCKED_NOISE = re.compile(r'ERR_BLOCKED_BY_CLIENT')
CAPTURE_CSS = '* { content-visibility: visible !important; } :root { --qa-capture-visible: 1; }'

SCROLL = r"""
async ([stepPx, pauseMs, maxSteps, waitMs]) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const height = () => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
  let steps = 0;
  for (let y = 0; y < height() && steps < maxSteps; y += stepPx, steps++) { window.scrollTo(0, y); await sleep(pauseMs); }
  window.scrollTo(0, height()); await sleep(pauseMs);
  const lazy = [...document.images].filter(i => !i.complete);
  const waited = lazy.length;
  await Promise.race([Promise.all(lazy.map(i => new Promise(r => { i.addEventListener('load', r, {once: true});
                                                                  i.addEventListener('error', r, {once: true}); }))),
                      sleep(waitMs)]);
  window.scrollTo(0, 0); await sleep(pauseMs);
  return {steps, height: height(), truncated: steps >= maxSteps, images_waited_for: waited,
          images_incomplete: [...document.images].filter(i => !i.complete).length};
}
"""

SNAP = r"""
() => {
  const sigOf = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (n.id) { parts.unshift('#' + n.id); break; }
      let s = n.tagName.toLowerCase();
      const cls = (typeof n.className === 'string' && n.className.trim()) ? n.className.trim().split(/\s+/)[0] : '';
      if (cls) s += '.' + cls;
      const p = n.parentElement;
      if (p) {
        const same = [...p.children].filter(c => c.tagName === n.tagName &&
              ((typeof c.className === 'string' && c.className.trim()) ? c.className.trim().split(/\s+/)[0] : '') === cls);
        if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      }
      parts.unshift(s);
    }
    return parts.join('>');
  };
  const els = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const own = [...el.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(' ').replace(/\s+/g, ' ').trim();
    els.push({sig: sigOf(el), tag: el.tagName.toLowerCase(), type: el.type || null, text: own.slice(0, 80),
              box: [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)]});
    if (els.length >= 2000) break;
  }
  const imgs = [...document.images].map(i => ({sig: sigOf(i), src: i.currentSrc || i.src, complete: i.complete, natural: [i.naturalWidth, i.naturalHeight]}));
  const scripts = [...document.scripts].map(s => s.src || ('inline:' + s.textContent.length));
  const de = document.documentElement;
  return {elements: els, images: imgs, scripts, overflow_x: de.scrollWidth > de.clientWidth + 1, size: [de.scrollWidth, de.scrollHeight]};
}
"""


def retry_delay(header: str | None, attempt: int, max_wait: float) -> tuple[float, str]:
    """Seconds to wait before retrying a 429 or 503: Retry-After (seconds or an HTTP date), else 10 s, 20 s, ...;
    never more than max_wait."""
    asked = None
    if header:
        h = header.strip()
        if h.isdigit():
            asked = float(h)
        else:
            try:
                asked = max(0.0, (parsedate_to_datetime(h) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                asked = None
    wait = asked if asked is not None else 10.0 * (attempt + 1)
    how = f'Retry-After {header!r}' if asked is not None else 'no Retry-After'
    if wait > max_wait:
        how += f', capped at {max_wait:g}s'
    return min(wait, max_wait), how


def load(h: Harness, page, url: str, net: dict, rec: dict):
    """page.goto with the run's rate limit, backing off on 429 and 503 (Retry-After, capped) up to net['retries'] times."""
    resp = None
    for attempt in range(net['retries'] + 1):
        h.throttle()
        resp = page.goto(url, wait_until='load', timeout=45000)
        status = resp.status if resp else None
        if status not in RETRY_STATUS:
            return resp
        if attempt == net['retries']:
            break
        delay, how = retry_delay(resp.headers.get('retry-after'), attempt, net['max_retry_after_seconds'])
        rec['backoff'].append({'status': status, 'waited_seconds': round(delay, 1), 'why': how, 'at': now()})
        time.sleep(delay)
    raise RuntimeError(f'HTTP {resp.status if resp else None} after {net["retries"]} retr{"y" if net["retries"] == 1 else "ies"}: '
                       'the site is rate limiting or unavailable')


def inject(page, css: str, marker: str, what: str) -> None:
    """Add a style and prove it applied; a refused style (Content-Security-Policy) makes the run errored, never ran."""
    page.add_style_tag(content=css)
    if not page.evaluate(APPLIED, marker):
        raise RuntimeError(f'the {what} style did not apply (the page\'s Content-Security-Policy may refuse inline styles; '
                           'set "bypass_csp": true)')


def run_once(h: Harness, url: str, viewport: str, actions: list, shot: Path, scroll: bool = True, settings: dict | None = None) -> dict:
    s = settings or {}
    net = {'retries': s.get('retries', 2), 'max_retry_after_seconds': s.get('max_retry_after_seconds', 60)}
    n_blocked = len(h.blocked)
    page = h.new_page(viewport=viewport, reduced_motion=s.get('reduced_motion', 'reduce'))
    log = h.log(page)
    sent: list[str] = []
    page.on('request', lambda r: sent.append(r.url))
    rec = {'ok': False, 'run_status': 'errored', 'status': None, 'error': None, 'steps': [], 'backoff': []}
    try:
        resp = load(h, page, url, net, rec)
        rec['status'] = resp.status if resp else None
        if s.get('freeze_animations', True):
            inject(page, STABLE_CSS, '--qa-capture-freeze', 'freeze-animation')
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(200)
        for i, a in enumerate(actions):
            try:
                h.act(page, [a])
                rec['steps'].append({'index': i, 'action': a, 'outcome': 'ok', 'error': None})
            except Exception as e:  # noqa: BLE001
                rec['steps'].append({'index': i, 'action': a, 'outcome': 'failed', 'error': f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'})
                break
        page.wait_for_timeout(200)
        if scroll:
            rec['scroll'] = page.evaluate(SCROLL, [int(viewport.split('x')[1]), 100, 100, 5000])
            inject(page, CAPTURE_CSS, '--qa-capture-visible', 'content-visibility')
            page.wait_for_timeout(200)
        rec.update(page.evaluate(SNAP))
        h.screenshot(page, shot, full_page=True)
        rec['screenshot'] = shot.as_posix()
        rec['run_status'] = 'ran'
        rec['ok'] = bool(resp and resp.status < 400)
    except Exception as e:  # noqa: BLE001
        rec['error'] = f'{type(e).__name__}: {(str(e).splitlines() or [""])[0][:300]}'
    finally:
        # Requests the harness blocked (third parties, writes) fail in every engine with engine-specific wording; they are
        # recorded once in runs.json's blocked_requests, not as this page's failures or console errors.
        blocked = {b['url'] for b in h.blocked[n_blocked:]}
        rec['blocked_requests'] = len(h.blocked) - n_blocked
        rec['requests_sent'] = max(0, len(sent) - rec['blocked_requests'])
        rec['console_errors'] = [c['text'][:300] for c in log['console'] if c['type'] == 'error'
                                 and not (blocked and (BLOCKED_NOISE.search(c['text']) or any(u in c['text'] for u in blocked)))]
        rec['page_errors'] = [f'{e["name"]}: {e["message"]}'[:300] for e in log['page_errors']]
        rec['failed_requests'] = [f'{r["method"]} {r["url"]} ({r["failure"]})' for r in log['failed_requests']
                                  if r['url'].split('?')[0] not in blocked]
        if not rec['backoff']:
            del rec['backoff']
        h.close_page(page)
    return rec


def csp_bypass(h: Harness, on: bool) -> str:
    """Make every browser context the harness creates from here on bypass the page's Content-Security-Policy (Playwright's
    bypass_csp), so the freeze and capture styles apply on sites with a hashed or nonce style-src. A harness that applies
    the setting itself (it exposes `bypass_csp`) is left alone; otherwise its browser's new_context is wrapped."""
    if getattr(h, 'bypass_csp', None) is not None:
        return 'harness'
    browser = h._browser
    original = browser.new_context

    def new_context(*args, **kw):
        kw.setdefault('bypass_csp', on)
        return original(*args, **kw)
    browser.new_context = new_context
    return 'capture'


def settings_from(cfg: dict, urls: list[str]) -> dict:
    remote = any(not is_loopback(u) for u in urls)
    rm = cfg.get('reduced_motion', 'reduce')
    if rm not in ('reduce', 'no-preference'):
        raise ValueError('reduced_motion must be "reduce" or "no-preference"')
    budget = cfg.get('request_budget')
    if budget is not None and (not isinstance(budget, int) or budget < 1):
        raise ValueError('request_budget must be a positive integer or null')
    return {'bypass_csp': bool(cfg.get('bypass_csp', True)), 'reduced_motion': rm,
            'freeze_animations': bool(cfg.get('freeze_animations', True)),
            'rate_limit_seconds': float(cfg.get('rate_limit_seconds', 0.5 if remote else 0.1)),
            'retries': int(cfg.get('retries', 2)), 'max_retry_after_seconds': float(cfg.get('max_retry_after_seconds', 60)),
            'request_budget': budget}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('targets', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--engines')
    ap.add_argument('--reruns', type=int)
    a = ap.parse_args()
    cfg = json.loads(a.targets.read_text(encoding='utf-8'))
    engines = (a.engines or ','.join(cfg.get('engines') or ['chromium', 'firefox', 'webkit'])).split(',')
    reruns = a.reruns or cfg.get('reruns', 3)
    urls = [urljoin(cfg['site'], p['path']) for p in cfg['pages']]
    try:
        check_authorised(urls, cfg.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    try:
        plan = planned_checks(cfg)
        s = settings_from(cfg, urls)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    scroll = cfg.get('scroll', True)
    capture = {'third_party': cfg.get('third_party', 'block'), 'allowed_hosts': cfg.get('allowed_hosts') or [],
               'service_workers': cfg.get('service_workers', 'allow'), 'scroll': scroll,
               'content_visibility': 'visible during the screenshot' if scroll else 'as the page sets it',
               'bypass_csp': s['bypass_csp'], 'bypass_csp_applied_by': None, 'reduced_motion': s['reduced_motion'],
               'freeze_animations': s['freeze_animations'], 'rate_limit_seconds': s['rate_limit_seconds'],
               'retries': s['retries'], 'max_retry_after_seconds': s['max_retry_after_seconds'], 'request_budget': s['request_budget']}
    doc = {'site': cfg['site'], 'captured_at': now(), 'platform': sys.platform, 'reruns': reruns, 'capture': capture,
           'planned': plan, 'engines': [], 'runs': [], 'blocked_requests': [],
           'requests': {'sent': 0, 'by_engine': {}, 'budget': s['request_budget'], 'budget_reached': False, 'backoffs': 0}}
    pages = {p['name']: p for p in cfg['pages']}
    states = {p['name'] + (f'~{st["name"]}' if st else ''): st for p in cfg['pages'] for st in [None] + (p.get('states') or [])}
    req = doc['requests']
    for eng in engines:
        start = len(doc['runs'])
        try:
            with Harness({'browser': eng, 'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'),
                          'allow_form_submission': cfg.get('allow_form_submission', False),
                          'rate_limit_seconds': s['rate_limit_seconds'], 'bypass_csp': s['bypass_csp'],
                          'request_budget': s['request_budget'],
                          'third_party': capture['third_party'], 'allowed_hosts': capture['allowed_hosts'],
                          'service_workers': capture['service_workers'], 'targets': [cfg['site']] + urls}) as h:
                capture['bypass_csp_applied_by'] = csp_bypass(h, s['bypass_csp'])
                version = h.version
                for chk in plan:
                    name, vp = chk['page'], chk['viewport']
                    p, st = pages[name.split('~')[0]], states[name]
                    for r in range(1, reruns + 1):
                        base = {'engine': eng, 'page': name, 'viewport': vp, 'run': r, 'critical_path': bool((st or {}).get('critical_path'))}
                        if req['budget'] is not None and req['sent'] >= req['budget']:
                            req['budget_reached'] = True
                            doc['runs'].append({**base, 'ok': False, 'run_status': 'skipped', 'status': None,
                                                'error': f'request budget of {req["budget"]} reached before this run', 'steps': []})
                            continue
                        shot = a.out / eng / vp / f'{name}.r{r}.png'
                        rec = run_once(h, urljoin(cfg['site'], p['path']), vp, (st or {}).get('actions') or [], shot, scroll, s)
                        req['sent'] += rec['requests_sent']
                        req['by_engine'][eng] = req['by_engine'].get(eng, 0) + rec['requests_sent']
                        req['backoffs'] += len(rec.get('backoff') or [])
                        doc['runs'].append({**base, **rec})
                doc['blocked_requests'].extend(h.blocked)
            mine = doc['runs'][start:]
            captured = [r for r in mine if r.get('run_status') == 'ran']
            failed = [r for r in mine if r.get('run_status') != 'ran']
            if mine and not captured:
                doc['engines'].append({'name': eng, 'version': version, 'status': 'errored',
                                       'note': f'0 of {len(mine)} runs captured: {failed[0]["error"]}'[:400]})
            else:
                doc['engines'].append({'name': eng, 'version': version, 'status': 'ran',
                                       'note': f'{len(failed)} of {len(mine)} runs did not capture: {failed[0]["error"]}'[:400] if failed else None})
        except Exception as e:  # noqa: BLE001 - engine not installed or failed to launch
            doc['engines'].append({'name': eng, 'version': None, 'status': 'not_run', 'note': f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'})
    for r in doc['runs']:
        if r.get('screenshot'):
            r['screenshot'] = Path(r['screenshot']).relative_to(a.out).as_posix()
            r['screenshot_sha256'] = hashlib.sha256((a.out / r['screenshot']).read_bytes()).hexdigest()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'runs.json').write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    errored = sum(1 for r in doc['runs'] if r.get('run_status') != 'ran')
    print(f'{len(doc["runs"])} run(s), {errored} errored or skipped, {req["sent"]} request(s) sent, over '
          f'{", ".join(e["name"] + " " + e["status"] for e in doc["engines"])} -> {a.out}', file=sys.stderr)
    if errored:
        print(f'warning: {errored} run(s) did not capture; the comparison is inconclusive for them (see runs.json "error")',
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
