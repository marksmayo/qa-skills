#!/usr/bin/env python3
"""Run each flow's baseline, then each planned scenario with its fault injected in the browser, and record what the user sees.

    python run_resilience.py flows.json run/scenarios.json --out run/ [--runs 2] [--max-requests N] [--resume]
        [--run-timeout S] [--no-watchdog]

For every flow: the baseline runs twice with no faults (a flow whose two baselines disagree is `unstable`,
one that fails is `failed`; their scenarios are `untested`). A baseline run that fails is retried once
after a pause (`baseline_retries`, default 1; `baseline_retry_pause_s`, default 5): a machine under load
can miss a step's 10 s budget once. A flow that passed only on a retry is `passed` with `retried` set and
the failed attempt kept (`retried_runs`), and the report's confidence is medium at best. A failing
baseline step is screenshotted and observed like a scenario, so a journey that is broken without any fault
has evidence. For every scenario: the flow is replayed to the step, the fault is injected on the endpoint
pattern (Playwright routing, optionally narrowed by request headers or resource type, or the whole context
offline), the step's actions run, and the page is watched for the scenario's whole window: until an error
appears, or 10 s for a message, extended to 30 s while a spinner shows. A slow-but-successful (latency)
fault may end early once the step is done and nothing is pending. Then the fault is removed and recovery
is tried: the app's own retry control if it shows one (an accessible name matching retry, try again,
reload), otherwise the step's actions again (without re-typing input, and without reloading; a step that
only types, such as type-ahead, gets the field's value re-sent instead: `refill_input`). Faults on a flow's
first step, or with `"from": "load"`, are in place before the page loads (frameworks such as Next.js
prefetch route payloads at page load, so a fault installed later never sees them). Each scenario runs
--runs times (default 2).

A fault that matched no request in a run is recorded as `fault_triggered: false`; a scenario none of whose
runs triggered its fault is `not_triggered` (it tested nothing) rather than `ran`, with a reason from the
request log: matching requests sent before the fault was installed, only after it was removed, or none at
all. Offline counts only failed requests the scenario targets (its pattern, method, headers and resource
type), never every request that failed while the context was offline.

Never blocking: every read of the page has a deadline (page.evaluate has none in Playwright, and a page
whose navigation timed out never answers it), screenshots time out after 10 s, page loads after
`nav_timeout_ms` (default 30000), and a reload that did not finish is recorded without reading the page.
On top of that a watchdog runs the work in a child process: each baseline or scenario run has a time
limit (`run_timeout_s` in flows.json or --run-timeout; by default from the budgets, the navigation timeout
and the steps), and a run that exceeds it is stopped with its browser, written to `<out>/hung.json`, and
the work resumes after it: the scenario is `errored` (a baseline run `failed`) with the watchdog's reason,
never run again. run.json is written after every flow's baselines and every scenario (`run_status:
partial` until the end, then `complete`); `--resume` continues a run that was stopped, keeping what is
done and the request counts. --no-watchdog runs in this process (no time limit).

Recorded per run: requests matching the endpoint (method, URL with sensitive query values redacted, time,
resource type, whether it was a navigation (a fault on the page's own document), status, response time,
whether the injector answered or held it, and the run phase), the page's own document loads (`documents`:
URL, phase, status or failure, so a failed payload followed by a full page load is visible), page errors,
console errors (minus the harness's own blocked-request noise), the visible text and the app root's text,
spinners and loading words, error indicators, boundary fallback text and boundary selectors, visible
control names, visible images that failed to load (`broken_images`), the page URL and whether it is the
browser's own error page (`browser_error_page`: an offline or failed full page load shows the browser's
page, not the site), input field lengths and SHA-256 hashes (never the text) before the fault, after it
and after recovery, the retry controls found, whether the step completed after recovery, screenshots,
and the number of requests the browser let through to the site.

Safety: the site's host (and `targets`, `allowed_hosts`) are registered with the browser harness, so the
site is reachable and every other host is blocked. Writes (POST, PUT, ...) are blocked unless
`allow_form_submission` is true (or `read_only` false) in flows.json; the default is read-only. A write a
scenario targets is answered in the browser either way. The harness counts every request it lets out
(injected and blocked requests never reach it); `request_budget` in flows.json (or --max-requests) caps
them: no new run starts once it is reached (the remaining scenarios are `untested`), and the harness
aborts anything past it, which marks the run in progress as failed, never as evidence. Page loads go
through the harness's goto (paced, with its 429/503 back-off), except when a fault must be in place before
the load. The page's own Content-Security-Policy is enforced (bypass_csp false) unless flows.json says
otherwise. Keys under `harness` in flows.json are passed to the harness; the safety keys above win.
run.json records h.metadata() (settings and request counts) and the harness's events.

flows.json may also set: `indicators` (merged over data/indicators.json: lists are added to the defaults,
budgets replace them; a flow may carry its own), `setup` (actions run after the first page load, such as
dismissing a cookie banner; per flow too), `init_script` (JavaScript run before every page, such as seeding
localStorage), `prefetch` ("block" aborts Next.js-style prefetches: requests with a `Next-Router-Prefetch`
or `Sec-Purpose: prefetch` header; this changes a Next.js app, which then falls back to full page loads),
`service_workers` ("block", the default, or "allow"; per flow too: with "allow" the page's service worker
runs, the first load waits for it and reloads once so the worker controls the page, as for a returning
visitor, and run.json records whether it did; route faults may miss requests the worker answers from its
cache, while offline reaches the worker too), `step_timeout_ms` (default 10000: how long a step before
the target may take), `nav_timeout_ms`, `run_timeout_s`, `baseline_retries`, `baseline_retry_pause_s`,
and `unreachable` (data the browser cannot reach, recorded by the planner).
Steps may use {"back": true}, {"forward": true} and {"reload": true} (browser history; Playwright disables
the back/forward cache, so back is never a bfcache restore).
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, signal, subprocess, sys, time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now, is_loopback  # noqa: E402
from classify import recovery_actions, recovery_kind  # noqa: E402

READ_MS = 5000          # the longest any single read of the page may take
SHOT_MS = 10000         # the longest a screenshot may take
NAV_MS = 30000          # page loads and history steps (nav_timeout_ms)
STEP_MS = 10000         # a step before the target (step_timeout_ms)
NAV_ACTIONS = ('back', 'forward', 'reload')
ERROR_PAGE = ('chrome-error://', 'about:neterror', 'about:certerror', 'about:blocked')

TEXT = """(root) => { const r = root ? document.querySelector(root) : null;
  return {body: document.body ? document.body.innerText : '', root: r ? r.innerText : null, root_exists: !!r}; }"""
VISIBLE_ANY = """(sels) => sels.some(s => { try { return [...document.querySelectorAll(s)].some(e => { const b = e.getBoundingClientRect();
  const cs = getComputedStyle(e); return b.width > 0 && b.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; }); } catch (x) { return false; } })"""
VISIBLE_TEXT = """(sels) => sels.some(s => { try { return [...document.querySelectorAll(s)].some(e => e.getBoundingClientRect().width > 0 && e.innerText.trim()); }
  catch (x) { return false; } })"""
# a visible video, canvas or image of at least 200x200 px: a screen that shows media is not blank for want of text
MEDIA = """() => [...document.querySelectorAll('video, canvas, img, svg, picture, iframe')].some(e => { const b = e.getBoundingClientRect();
  const cs = getComputedStyle(e); return b.width >= 200 && b.height >= 200 && cs.visibility !== 'hidden' && cs.display !== 'none'
  && Number(cs.opacity) > 0.05; })"""
# visible images that finished loading with nothing to show (the broken-image icon or an empty box)
BROKEN_IMAGES = """() => [...document.images].filter(i => { const b = i.getBoundingClientRect();
  return b.width > 0 && b.height > 0 && i.complete && i.naturalWidth === 0 && !!(i.currentSrc || i.getAttribute('src')); }).length"""
# cut by code point (Array.from), never by UTF-16 unit: String.slice can split an emoji (a flag in a link's text)
# into a lone surrogate, which no UTF-8 writer accepts
CLIP_JS = "(s) => Array.from(s).slice(0, 40).join('')"
CONTROLS = """() => { const clip = """ + CLIP_JS + """;
  return [...new Set([...document.querySelectorAll('a[href], button, [role=button], input[type=submit]')]
  .filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; })
  .map(e => clip((e.innerText || e.getAttribute('aria-label') || e.value || '').trim().replace(/\\s+/g, ' '))).filter(Boolean))].slice(0, 60); }"""
# everything observe() reads, in one bounded call
OBSERVE = ("""([root, spin, errs, bnd]) => { const safe = (f, d) => { try { return f(); } catch (e) { return d; } };
  return {t: (""" + TEXT + """)(root), spin: spin.length ? safe(() => (""" + VISIBLE_ANY + """)(spin), false) : false,
    err: errs.length ? safe(() => (""" + VISIBLE_TEXT + """)(errs), false) : false,
    bnd: bnd.length ? safe(() => (""" + VISIBLE_ANY + """)(bnd), false) : false,
    controls: safe(""" + CONTROLS + """, []), media: safe(""" + MEDIA + """, false), broken: safe(""" + BROKEN_IMAGES + """, 0)}; }""")
RETRY_CONTROLS = """(names) => [...document.querySelectorAll('button, a, [role=button]')]
  .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(n => names.some(w => n.toLowerCase().includes(w)))"""
SW_STATE = """async (wait) => { if (!navigator.serviceWorker) return {supported: false, registered: false, controlled: false};
  await new Promise(r => setTimeout(r, wait));
  const regs = await navigator.serviceWorker.getRegistrations();
  if (regs.length) await Promise.race([navigator.serviceWorker.ready, new Promise(r => setTimeout(r, 3000))]);
  return {supported: true, registered: regs.length > 0, controlled: !!navigator.serviceWorker.controller,
          scripts: regs.map(r => ((r.active || r.waiting || r.installing) || {}).scriptURL).filter(Boolean).slice(0, 5)}; }"""
SENSITIVE = re.compile(r'(?i)token|key|secret|pass|auth|session|code|sig|email|phone|user|name|addr')
LIST_KEYS = ('spinner_selectors', 'loading_words', 'error_selectors', 'error_words', 'empty_state_words', 'boundary_fallback_words',
             'boundary_selectors', 'retry_control_names')
BLOCK_NOISE = re.compile(r'ERR_BLOCKED_BY_CLIENT')
PREFETCH_HEADERS = (('next-router-prefetch', None), ('sec-purpose', 'prefetch'), ('purpose', 'prefetch'))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def redact(url: str) -> str:
    """The URL with its query kept, but the values of sensitive-looking parameters replaced."""
    p = urlsplit(url)
    if not p.query:
        return url
    q = [(k, 'REDACTED' if SENSITIVE.search(k) else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
    return p._replace(query=urlencode(q)).geturl()


def merge_indicators(base: dict, over: dict | None) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if k in LIST_KEYS:
            out[k] = list(dict.fromkeys(list(out.get(k, [])) + [x.lower() if k.endswith('_words') or k == 'retry_control_names' else x for x in v]))
        elif k == 'budgets':
            out['budgets'] = {**out.get('budgets', {}), **v}
        elif k != 'note':
            out[k] = v
    for k in LIST_KEYS:
        out.setdefault(k, [])
    return out


def header_match(req_headers: dict, want: dict | None) -> bool:
    for name, v in (want or {}).items():
        have = req_headers.get(name.lower())
        if v is False or v is None:
            if have is not None:
                return False
        elif v is True or v == '*':
            if have is None:
                return False
        elif have != str(v):
            return False
    return True


def targeted(scen: dict, rx: re.Pattern | None, request) -> bool:
    """Whether a request is one the scenario's fault aims at: its pattern, and its method, headers and resource type
    when the scenario names them. Offline fails every request in the context; only these count as the fault's."""
    if rx is None or not rx.match(request.url):
        return False
    if scen.get('method') and request.method != scen['method']:
        return False
    if scen.get('resource_type') and request.resource_type != scen['resource_type']:
        return False
    return header_match(request.headers, scen.get('headers'))


class Injector:
    """Applies one fault to requests matching a pattern (and method, headers, resource type). Delays are queued and released by pump()."""
    def __init__(self, fault: dict, scen: dict, t0: float):
        self.fault, self.method = fault, scen.get('method')
        self.headers, self.rtype = scen.get('headers'), scen.get('resource_type')
        self.pending: list[tuple[float, object, str]] = []
        self.seen = 0
        self.answered: set[int] = set()     # requests fulfilled or aborted in the browser (never reached the site)
        self.hits: dict[int, dict] = {}
        self.keep: list = []
        self.active = True
        self.t0 = t0

    def matches(self, request) -> bool:
        if self.method and request.method != self.method:
            return False
        if self.rtype and request.resource_type != self.rtype:
            return False
        return header_match(request.headers, self.headers)

    def handler(self, route, request):
        if not self.active or not self.matches(request):
            return route.fallback()
        self.seen += 1
        self.keep.append(request)
        f = self.fault
        act = f['action']
        self.hits[id(request)] = {'action': act, 'at_ms': round((time.monotonic() - self.t0) * 1000)}
        if act == 'delay':
            self.pending.append((time.monotonic() + f['ms'] / 1000, route, 'continue'))
        elif act == 'delay_abort':
            self.pending.append((time.monotonic() + f['ms'] / 1000, route, 'abort:' + f.get('error', 'timedout')))
            self.answered.add(id(request))
        elif act == 'hang':
            self.pending.append((float('inf'), route, 'continue'))
        elif act == 'abort':
            self.answered.add(id(request))
            route.abort(f.get('error', 'failed'))
        elif act == 'flap':
            if self.seen <= f['failures']:
                self.answered.add(id(request))
                route.fulfill(status=f['status'], body=json.dumps({'error': 'injected'}), content_type='application/json')
            else:
                self.hits[id(request)]['action'] = 'passed_through'
                route.fallback()
        elif act == 'fulfill':
            body = f.get('body', '')
            if 'body_size' in f:
                body = '{"items": [' + ','.join(['{"title": "x"}'] * (f['body_size'] // 16)) + ']}'
            if not isinstance(body, str):
                body = json.dumps(body)
            self.answered.add(id(request))
            route.fulfill(status=f['status'], body=body, headers=f.get('headers') or {},
                          content_type=f.get('content_type', 'application/json'))
        else:
            route.fallback()

    def pump(self):
        t = time.monotonic()
        keep = []
        for due, route, what in self.pending:
            if due <= t:
                try:
                    route.abort(what.split(':', 1)[1]) if what.startswith('abort:') else route.fallback()
                except Exception:  # noqa: BLE001 - the page may have moved on
                    pass
            else:
                keep.append((due, route, what))
        self.pending = keep

    def release(self):
        self.active = False
        for _, route, _ in self.pending:
            try:
                route.fallback()
            except Exception:  # noqa: BLE001
                pass
        self.pending = []


# ---------------------------------------------------------------- bounded page calls
def ev(page, js: str, arg=None, timeout_ms: int = READ_MS):
    """page.evaluate with a deadline. Playwright's evaluate has no timeout, and on a page whose navigation timed out it
    never returns (no execution context arrives); wait_for_function honours its timeout, and the wrapped function
    returns an object, so the first poll is truthy and returns at once."""
    h = page.wait_for_function('async (a) => ({v: await (' + js + ')(a)})', arg=arg, timeout=timeout_ms, polling=100)
    try:
        return h.json_value()['v']
    finally:
        try:
            h.dispose()
        except Exception:  # noqa: BLE001
            pass


def short(e: Exception) -> str:
    return f'{type(e).__name__}: {(str(e).splitlines() or [""])[0][:160]}'


def is_error_page(url: str) -> bool:
    """The browser's own error page (Chromium's chrome-error://, Firefox's about:neterror): a full page load failed."""
    return (url or '').startswith(ERROR_PAGE)


def page_url(page) -> str:
    try:
        return page.url or ''
    except Exception:  # noqa: BLE001
        return ''


def observe(page, ind: dict, root: str | None, timeout_ms: int = READ_MS) -> dict:
    """What the page shows now, read in one bounded call (retried once: a navigation can destroy the context mid-read).
    A page that can't be read gives an observation marked `unavailable` (never a blank screen)."""
    url = page_url(page)
    last = None
    for attempt in (1, 2):
        try:
            r = ev(page, OBSERVE, [root, ind['spinner_selectors'], ind['error_selectors'], ind['boundary_selectors']], timeout_ms)
            t = r['t']
            lower = t['body'].lower()
            loading = sorted({w for w in ind['loading_words'] if w in lower})
            url = page_url(page) or url
            return {'body_len': len(t['body'].strip()), 'root_text': (t['root'] or '')[:400], 'root_len': len((t['root'] or '').strip()),
                    'spinner': bool(r['spin'] or loading), 'loading_words': loading, 'error_element': bool(r['err']),
                    'error_words': sorted({w for w in ind['error_words'] if w in lower}),
                    'empty_words': sorted({w for w in ind['empty_state_words'] if w in lower}),
                    'fallback_words': sorted({w for w in ind['boundary_fallback_words'] if w in lower}), 'boundary_element': bool(r['bnd']),
                    'controls': r['controls'] or [], 'media_visible': bool(r['media']), 'broken_images': int(r['broken'] or 0),
                    'text': t['body'][:600], 'page_url': redact(url), 'browser_error_page': is_error_page(url)}
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 1:
                try:
                    page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    pass
    url = page_url(page) or url
    return {'unavailable': f'the page could not be read: {short(last)}', 'body_len': 0, 'root_text': '', 'root_len': 0, 'spinner': False,
            'loading_words': [], 'error_element': False, 'error_words': [], 'empty_words': [], 'fallback_words': [], 'boundary_element': False,
            'controls': [], 'media_visible': False, 'broken_images': 0, 'text': '', 'page_url': redact(url),
            'browser_error_page': is_error_page(url)}


def shoot(page, path: Path, timeout_ms: int = SHOT_MS) -> str | None:
    """A viewport screenshot with a deadline; None when the page can't be captured (the run goes on)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=False, timeout=timeout_ms)
        return path.name
    except Exception:  # noqa: BLE001
        return None


def unstick(page) -> None:
    """Leave a page whose navigation never finished (every later call would wait on it)."""
    try:
        page.goto('about:blank', timeout=5000)
    except Exception:  # noqa: BLE001
        pass


def act(h: Harness, page, site: str, actions: list, nav_ms: int = NAV_MS):
    for a in actions:
        if 'goto' in a:
            target = urljoin(site, a['goto']) if not a['goto'].startswith('#') else site.rstrip('/') + '/' + a['goto']
            if page.url.split('#')[0] == target.split('#')[0] and '#' in target:
                ev(page, '(h) => { location.hash = h; return true; }', target.split('#', 1)[1])
            else:
                h.throttle()
                page.goto(target, wait_until='load', timeout=nav_ms)
            page.wait_for_timeout(300)
        elif any(a.get(k) for k in NAV_ACTIONS):
            # browser history, with the run's navigation timeout (the harness's own would also wait for the page to settle)
            fn = page.go_back if a.get('back') else page.go_forward if a.get('forward') else page.reload
            fn(timeout=nav_ms, wait_until='load')
            page.wait_for_timeout(300)
        else:
            h.act(page, [a])


def done(page, selector: str, timeout_ms: int = 0) -> bool:
    """The selector is visible within timeout_ms (at least 100 ms: a bounded "visible now", where is_visible has no
    deadline on a stuck page)."""
    try:
        page.wait_for_selector(selector, timeout=max(int(timeout_ms), 100), state='visible')
        return True
    except Exception:  # noqa: BLE001
        return False


def step_done(page, step: dict, timeout_ms: int = 0) -> bool:
    """The step's `done` selector is visible and none of its `done_not` selectors is: a step can say "done only if the
    data really arrived" (no error text, no fallback) when the app shows its `done` on failure too."""
    if not done(page, step['done'], timeout_ms):
        return False
    nots = step.get('done_not') or []
    return not any(done(page, s, 1) for s in ([nots] if isinstance(nots, str) else nots))


def reload_check(cfg: dict, scen: dict) -> bool:
    """Whether to reload after recovery and run the flow to the step again (a failure the app stored, in Cache Storage,
    IndexedDB or localStorage, outlives the fault): on by default for reads under a failing fault or a static-asset
    fault (a write is never sent again); `reload_after_recovery: false` in flows.json turns it off."""
    return cfg.get('reload_after_recovery', True) is not False and scen.get('group') != 'latency' and not scen.get('write')


def sw_mode(cfg: dict, flow: dict) -> str:
    """Service workers for a flow's pages: `block` (the default: faults reach every request, as on a first visit) or
    `allow` (the worker runs, for returning visitors and offline navigation), from the flow or flows.json."""
    m = (flow or {}).get('service_workers') or (cfg or {}).get('service_workers') or 'block'
    if m not in ('block', 'allow'):
        raise ValueError(f'service_workers must be block or allow, not {m!r}')
    return m


CACHE_STORAGE = """async () => { if (!self.caches) return null; const out = [];
  try { for (const name of (await caches.keys()).slice(0, 10)) { const c = await caches.open(name); const reqs = await c.keys();
    const urls = [], statuses = [], sizes = [];
    for (const r of reqs.slice(0, 10)) { const m = await c.match(r); urls.push(r.url); statuses.push(m ? m.status : null);
      sizes.push(m ? Number(m.headers.get('content-length')) || null : null); }
    out.push({cache: name, entries: reqs.length, urls, statuses, sizes}); } } catch (e) { return [{error: String(e).slice(0, 120)}]; }
  return out; }"""


def inputs(page, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        try:
            v = page.input_value(f, timeout=1000)
            out[f] = {'length': len(v), 'sha256': sha(v)}
        except Exception:  # noqa: BLE001
            out[f] = {'length': None, 'sha256': None, 'note': 'field not present'}
    return out


def glob_rx(pattern: str) -> re.Pattern:
    return re.compile('^' + re.escape(pattern).replace(r'\*\*', '.*').replace(r'\*', '[^/]*') + '$')


class Ctx:
    """What one invocation of the runner shares between runs: the harness, config, request accounting."""
    def __init__(self, h, cfg, faults, shots, allow_post):
        self.h, self.cfg, self.faults, self.shots, self.allow_post = h, cfg, faults, shots, allow_post
        self.sent = 0               # mirrors h.requests['sent'] after each run
        self.prefetch_blocked = 0
        self.nav_ms = int(cfg.get('nav_timeout_ms', NAV_MS))
        self.step_ms = int(cfg.get('step_timeout_ms', STEP_MS))


def warm_service_worker(c: Ctx, page) -> dict:
    """With service workers allowed: wait for the page's worker and reload once if it does not control the page yet, so
    the run sees what a returning visitor does (a worker answers only pages it controls)."""
    st: dict = {'mode': 'allow'}
    try:
        st.update(ev(page, SW_STATE, 1500, 8000))
        if st.get('registered') and not st.get('controlled'):
            c.h.throttle()
            page.reload(timeout=c.nav_ms, wait_until='load')
            page.wait_for_timeout(500)
            st['reloaded'] = True
            st.update({k: v for k, v in ev(page, SW_STATE, 300, 8000).items() if k in ('controlled', 'registered')})
    except Exception as e:  # noqa: BLE001
        st['error'] = short(e)
    st['scripts'] = [redact(u) for u in st.get('scripts') or []]
    return st


def run_flow(c: Ctx, flow, upto: int | None, scen: dict | None, ind: dict, tag: str) -> dict:
    h, cfg, faults, shots = c.h, c.cfg, c.faults, c.shots
    site = cfg['site']
    sw = sw_mode(cfg, flow)
    page = h.new_page(viewport=cfg.get('viewport', '1280x800'), service_workers=sw)
    sent_before = h.requests['sent']    # the harness counts what it let out; injected and blocked requests never reach it
    exhausted_before = h.requests['budget_exhausted']
    log = h.log(page)
    reqs: list[dict] = []
    docs: list[dict] = []
    t0 = time.monotonic()
    pattern = scen['endpoint'] if scen else None
    rx = glob_rx(pattern) if pattern else None
    entries: dict[int, dict] = {}
    doc_entries: dict[int, dict] = {}
    started: dict[int, float] = {}
    inj: Injector | None = None
    prefetch_ids: set[int] = set()
    offline = {'on': False, 'failed': 0}
    phase = {"now": "before"}    # before the fault, fault, recovery, reload: which part of the run a request belongs to
    nav_ms, step_ms = c.nav_ms, c.step_ms

    def on_req(r):
        started[id(r)] = time.monotonic()
        try:
            nav = bool(r.is_navigation_request())
        except Exception:  # noqa: BLE001
            nav = False
        if nav:
            try:
                main = r.frame.parent_frame is None
            except Exception:  # noqa: BLE001
                main = True
            if main:    # the page's own document: a full page load (a framework's fallback, a link, a reload)
                d = {'url': redact(r.url), 't_ms': round((time.monotonic() - t0) * 1000), 'phase': phase['now']}
                doc_entries[id(r)] = d
                docs.append(d)
        if rx and rx.match(r.url):
            e = {'method': r.method, 't_ms': round((time.monotonic() - t0) * 1000), 'url': redact(r.url), 'resource_type': r.resource_type,
                 'phase': phase["now"], 'navigation': nav}     # a fault on a navigation answers the page itself, not the app
            entries[id(r)] = e
            reqs.append(e)

    def finish(r, status, failure=None):
        d = doc_entries.get(id(r))
        if d is not None:
            d['status'] = status
            if failure:
                d['failure'] = failure
        e = entries.get(id(r))
        if e is not None:
            hit = inj.hits.get(id(r)) if inj else None
            e['injected'] = bool(hit) and hit['action'] != 'passed_through'
            if hit:
                e['fault_action'] = hit['action']
            e['status'] = status
            e['ms'] = round((time.monotonic() - started.get(id(r), t0)) * 1000)
            if hit and hit['action'] in ('hang', 'delay', 'delay_abort'):
                e['held'] = True
            if failure:
                e['failure'] = failure

    def on_resp(r):
        finish(r.request, r.status)
        e = entries.get(id(r.request))
        if e is not None:
            e['retry_after'] = r.headers.get('retry-after')
            try:
                sw_answer = bool(r.from_service_worker)
            except Exception:  # noqa: BLE001
                sw_answer = False
            if sw_answer:
                e['from_service_worker'] = True
                if offline['on'] and targeted(scen or {}, rx, r.request):
                    offline['failed'] += 1      # the fault reached the request, and the page's service worker answered it
                    e['offline'] = True

    def on_fail(r):
        finish(r, None, r.failure)
        if offline['on'] and targeted(scen or {}, rx, r):
            offline['failed'] += 1          # only requests the scenario targets, never the whole context's failures
            e = entries.get(id(r))
            if e is not None:
                e['offline'] = True

    def prefetch_guard(route, request):
        hd = request.headers
        if any(hd.get(n) is not None and (v is None or hd.get(n) == v) for n, v in PREFETCH_HEADERS):
            prefetch_ids.add(id(request))
            c.prefetch_blocked += 1
            return route.abort('blockedbyclient')
        return route.fallback()

    page.on('request', on_req)
    page.on('response', on_resp)
    page.on('requestfailed', on_fail)
    if cfg.get('prefetch') == 'block':
        page.route('**/*', prefetch_guard)
    rec = {'tag': tag, 'started_at': now(), 'steps': [], 'ok': True}
    fault = faults[scen['fault']] if scen else None
    early = scen is not None and (upto == 0 or scen.get('from') == 'load') and fault['action'] != 'offline'
    t_fault = None
    label = (scen or {}).get('id') or 'baseline-' + re.sub(r'[^A-Za-z0-9]+', '-', flow['name'])
    try:
        if early:   # requests that start with the page (the first step's, or prefetches) need the fault in place first
            t_fault = time.monotonic()
            phase["now"] = "fault"
            inj = Injector(fault, scen, t0)
            page.route(pattern, inj.handler)
        if early:
            # with the fault already in place the page may never go idle (a hang, a fast retry loop), so the
            # harness's settle would eat the scenario's window: load without it, still paced
            h.throttle()
            page.goto(site, wait_until='load', timeout=nav_ms)
        else:
            nav = h.goto(page, site)        # paced, 429/503 back-off, settled
            if not nav['ok'] and nav.get('status') is None:
                raise RuntimeError(nav.get('error') or 'the site did not load')
            if nav.get('status') and nav['status'] >= 400:
                rec['load_status'] = nav['status']
        page.wait_for_timeout(500)
        setup = (cfg.get('setup') or []) + (flow.get('setup') or [])
        if setup:
            try:
                act(h, page, site, setup, nav_ms)
            except Exception as e:  # noqa: BLE001
                rec['setup_error'] = short(e)
        if sw == 'allow':
            rec['service_worker'] = (warm_service_worker(c, page) if not early else
                                     {'mode': 'allow', 'note': 'the fault was in place before the first load, so the worker was not waited for'})
        for i, step in enumerate(flow['steps']):
            if upto is not None and i > upto:
                break
            target = scen is not None and i == upto
            fields = list((step.get('input') or {}).keys())
            if target:
                if fault['action'] == 'offline':
                    page.context.set_offline(True)
                    offline['on'] = True
                    t_fault = time.monotonic()
                elif not early:
                    inj = Injector(fault, scen, t0)
                    page.route(pattern, inj.handler)
                    t_fault = time.monotonic()
                phase['now'] = 'fault'
            before = {}
            t_step = time.monotonic()
            try:
                for ai, action in enumerate(step['actions']):
                    act(h, page, site, [action], nav_ms)
                    if target and fields and 'fill' in action and not any('fill' in x for x in step['actions'][ai + 1:]):
                        before = inputs(page, fields)    # what the user typed, before the action that sends it
                step_error = None
            except Exception as e:  # noqa: BLE001
                step_error = short(e)
            if not target:
                ok = step_done(page, step, step_ms)
                entry = {'name': step['name'], 'completed': ok, 'error': step_error, 'observed': observe(page, ind, cfg.get('app_root'))}
                if ok:
                    entry['done_ms'] = round((time.monotonic() - t_step) * 1000)   # how long the step took (a loaded machine shows here)
                else:
                    shot = shoot(page, shots / f'{label}.{tag}.failed-step.png')
                    if shot:
                        entry['screenshot'] = shot
                rec['steps'].append(entry)
                if not ok:
                    rec['ok'] = False
                    break
                continue
            if fields and not before:
                before = inputs(page, fields)
            # watch for the scenario's whole window: until an error shows, or the budget runs out
            failing = scen['group'] != 'latency'
            budget = ind['budgets']['message_ms']
            first_error_ms, spinner_seen = None, False
            while (time.monotonic() - t_fault) * 1000 < budget:
                if inj:
                    inj.pump()
                page.wait_for_timeout(250)
                ob = observe(page, ind, cfg.get('app_root'))
                if ob.get('unavailable') or ob.get('browser_error_page'):
                    break           # nothing more to watch: the page is the browser's own, or can't be read
                spinner_seen = spinner_seen or ob['spinner']
                if (ob['error_element'] or ob['fallback_words'] or ob['boundary_element']) and first_error_ms is None:
                    first_error_ms = round((time.monotonic() - t_fault) * 1000)
                    break
                if ob['spinner'] and budget < ind['budgets']['spinner_ms']:
                    budget = ind['budgets']['spinner_ms']
                if (not failing and not ob['spinner'] and not (inj and inj.pending) and done(page, step['done'], 1)
                        and (time.monotonic() - t_fault) * 1000 > 1500):
                    break
            page.wait_for_timeout(500)
            if inj:
                inj.pump()
            after = observe(page, ind, cfg.get('app_root'))
            completed_under_fault = step_done(page, step, 1)
            shot1 = shoot(page, shots / f'{scen["id"]}.{tag}.fault.png')
            mid_inputs = inputs(page, fields) if fields else {}
            elapsed = round((time.monotonic() - t_fault) * 1000)
            faulted = (inj.seen if inj else 0) if fault['action'] != 'offline' else offline['failed']
            # recovery
            if inj:
                inj.release()
            if fault['action'] == 'offline':
                page.context.set_offline(False)
                offline['on'] = False
            phase['now'] = 'recovery'
            try:
                controls = ev(page, RETRY_CONTROLS, ind['retry_control_names'])
            except Exception:  # noqa: BLE001
                controls = []
            again = recovery_actions(step)
            # repeating a step whose only action is a navigation reloads the page: that is not recovery, so it is not tried
            recovery = ('retry_control' if controls else 'refill_input' if any('refill' in x for x in again)
                        else 'reload' if recovery_kind(step) == 'reload' else 'repeat_step')
            try:
                if recovery == 'reload':
                    pass
                elif controls:
                    page.get_by_role('button', name=re.compile('|'.join(ind['retry_control_names']), re.I)).first.click(timeout=3000)
                elif recovery == 'refill_input':
                    # a step that only types (type-ahead): re-send what is in the field, as the user would by typing again
                    for x in again:
                        page.fill(x['refill'], page.input_value(x['refill'], timeout=2000), timeout=3000)
                else:
                    act(h, page, site, again, nav_ms)   # typed input is not re-entered
            except Exception:  # noqa: BLE001
                pass
            recovered = False if recovery == 'reload' else step_done(page, step, 5000)
            shot2 = shoot(page, shots / f'{scen["id"]}.{tag}.recovery.png')
            rec['fault_triggered'] = faulted > 0
            rec['faulted_requests'] = faulted
            target_rec = {'name': step['name'], 'target': True, 'error': step_error, 'completed_under_fault': completed_under_fault,
                          'first_error_ms': first_error_ms, 'spinner_seen': spinner_seen, 'watched_ms': elapsed, 'observed': after,
                          'inputs': {'before': before, 'after_fault': mid_inputs}, 'retry_controls': controls, 'recovery': recovery,
                          'recovered': recovered, 'screenshots': [x for x in (shot1, shot2) if x]}
            if not shot1 or not shot2:
                target_rec['screenshot_errors'] = [n for n, x in (('fault', shot1), ('recovery', shot2)) if not x]
            rec['steps'].append(target_rec)
            if reload_check(cfg, scen):
                # the fault is gone: reload and run the flow to the step again. A failure the app stored (Cache Storage,
                # IndexedDB, localStorage) shows again with no request to the endpoint
                phase['now'] = 'reload'
                rl: dict = {}
                try:
                    h.throttle()
                    page.goto(site, wait_until='load', timeout=nav_ms)
                    page.wait_for_timeout(500)
                    if setup:
                        try:
                            act(h, page, site, setup, nav_ms)
                        except Exception:  # noqa: BLE001
                            pass
                    for j, st2 in enumerate(flow['steps'][:upto + 1]):
                        act(h, page, site, st2['actions'], nav_ms)
                        if j < upto and not step_done(page, st2, step_ms):
                            rl['stopped_at'] = st2['name']
                            break
                    rl['completed'] = 'stopped_at' not in rl and step_done(page, step, step_ms)
                except Exception as e:  # noqa: BLE001
                    rl['error'] = short(e)
                    rl['completed'] = False
                after_reload = [q for q in reqs if q.get('phase') == 'reload']
                rl['endpoint_requests'] = len(after_reload)
                rl['ok_responses'] = sum(1 for q in after_reload if isinstance(q.get('status'), int) and 200 <= q['status'] < 300)
                if rl.get('error'):
                    # a page whose reload did not finish answers no read (it would wait forever): record that and leave it
                    rl['cache_storage'] = None
                    rl['note'] = 'the reload did not finish, so the page, its Cache Storage and a screenshot were not read'
                    unstick(page)
                else:
                    try:
                        rl['cache_storage'] = [dict(x, urls=[redact(u) for u in x.get('urls', [])]) for x in (ev(page, CACHE_STORAGE) or [])]
                    except Exception:  # noqa: BLE001
                        rl['cache_storage'] = None
                    shot3 = shoot(page, shots / f'{scen["id"]}.{tag}.reload.png')
                    if shot3:
                        target_rec['screenshots'].append(shot3)
                        rl['screenshot'] = shot3
                target_rec['reload'] = rl
    except Exception as e:  # noqa: BLE001
        rec['ok'] = False
        rec['error'] = f'{type(e).__name__}: {(str(e).splitlines() or [""])[0][:200]}'
    finally:
        if inj:
            inj.release()
        rec['requests'] = reqs
        rec['documents'] = docs
        rec['page_errors'] = [f'{e["name"]}: {e["message"]}'[:200] for e in log['page_errors']]
        errs = [m['text'][:200] for m in log['console'] if m['type'] == 'error']
        rec['console_errors'] = [m for m in errs if not (BLOCK_NOISE.search(m) or h.blocked_by_harness(m))]
        rec['console_filtered'] = len(errs) - len(rec['console_errors'])
        rec['finished_at'] = now()
        try:
            h.close_page(page)
        except Exception as e:  # noqa: BLE001
            rec['close_error'] = short(e)
        rec['requests_sent'] = h.requests['sent'] - sent_before
        c.sent = h.requests['sent']
        if h.requests['budget_exhausted'] and not exhausted_before:
            # the harness aborted requests mid-run: what the page showed is the budget, not the fault
            rec['ok'] = False
            rec['budget_exhausted'] = True
            rec['error'] = rec.get('error') or 'the request budget ran out during this run; the harness aborted the rest'
    return rec


# ---------------------------------------------------------------- the run: incremental, resumable, watched
def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write_json(p: Path, data) -> None:
    """Write through a temporary file and replace, so a run killed mid-write never leaves half a file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')
    os.replace(tmp, p)


def save(doc: dict, out: Path, status: str, extra=None) -> None:
    if extra:
        doc.update(extra())
    doc['run_status'] = status
    write_json(out / 'run.json', doc)


def resume_doc(out: Path) -> dict | None:
    """run.json as a stopped run left it (baselines and scenarios done so far), or None."""
    d = read_json(out / 'run.json')
    return d if isinstance(d, dict) and 'scenarios' in d else None


def progress(out: Path, unit: str | None, limit_s: float = 0, sent: int | None = None) -> None:
    write_json(out / 'progress.json', {'unit': unit, 'started': time.time(), 'limit_s': limit_s, 'sent': sent})


def hung_rec(tag: str, hg: dict) -> dict:
    return {'tag': tag, 'started_at': hg.get('at'), 'steps': [], 'ok': False, 'hung': True,
            'error': f'the run did not finish within {hg.get("limit_s")} s (a browser call never returned) and was stopped by the watchdog',
            'requests': [], 'documents': [], 'page_errors': [], 'console_errors': [], 'console_filtered': 0,
            'finished_at': hg.get('at'), 'requests_sent': 0}


def untriggered_reason(runs: list[dict]) -> str:
    """Why a fault matched nothing after it was installed, from the request log's phases."""
    ph = Counter(q.get('phase') for r in runs for q in r.get('requests') or [])
    before, later, during = ph.get('before', 0), ph.get('recovery', 0) + ph.get('reload', 0), ph.get('fault', 0)
    if during:
        return (f'{during} matching request(s) were made while the fault was in place, but none was failed by it: answered '
                'before reaching the network (the HTTP cache, a service worker) or narrowed out by the method, headers or resource type')
    if before:
        return (f'the fault matched no request after it was installed: {before} matching request(s) were sent before the fault '
                '(prefetched at page load or loaded on an earlier step); install it earlier ("from": "load") or aim at the request the step makes'
                + (f'; {later} more only after the fault was removed' if later else ''))
    if later:
        return (f'the fault matched no request while it was in place: {later} matching request(s) came only after the fault was removed '
                '(recovery or the reload), so the step itself reused data the page already had (a client-side or router cache)')
    return ('no request matched the endpoint in any phase of any run: the step was answered from a cache (the framework\'s client '
            'cache or a service worker), or the pattern, method, headers or resource type match no request the step makes')


def run_limit(cfg: dict, flow: dict, ind: dict, limit_s: float | None) -> float:
    """The watchdog's time limit for one run of a flow."""
    if limit_s:
        return float(limit_s)
    if cfg.get('run_timeout_s'):
        return float(cfg['run_timeout_s'])
    nav = int(cfg.get('nav_timeout_ms', NAV_MS)) / 1000
    step = int(cfg.get('step_timeout_ms', STEP_MS)) / 1000
    spin = ind.get('budgets', {}).get('spinner_ms', 30000) / 1000
    n = len(flow['steps'])
    return max(120.0, 3 * nav + spin + 2 * n * step + 60)


def execute(doc: dict, cfg: dict, flows: dict, plan: dict, ind: dict, runner, sent, out: Path, runs: int, budget, limit_s=None,
            extra=None) -> dict:
    """Run every baseline and scenario not already in doc (a resumed run keeps what is done), writing run.json after each
    flow's baselines and each scenario. runner(flow, upto, scen, ind, tag) does one run; sent() counts the requests that
    reached the site so far. A run listed in hung.json (stopped by the watchdog) is recorded, never run again."""
    hung = {x['unit']: x for x in (read_json(out / 'hung.json') or [])}
    doc.setdefault('baselines', {})
    doc.setdefault('scenarios', [])
    doc['hung'] = list(hung.values())
    over = bool(doc.get('budget_reached'))
    retries = int(cfg.get('baseline_retries', 1))
    pause = float(cfg.get('baseline_retry_pause_s', 5))

    def guarded(unit, flow, fn):
        progress(out, unit, run_limit(cfg, flow, ind.get(flow['name']) or {}, limit_s), sent())
        try:
            return fn()
        finally:
            progress(out, None)

    for name, flow in flows.items():
        if name in doc['baselines']:
            continue
        brs, retried = [], []
        for i in (1, 2):
            unit = f'baseline:{name}:{i}'
            if unit in hung:
                brs.append(hung_rec(f'baseline{i}', hung[unit]))
                continue
            if budget is not None and sent() >= budget:
                over = True
                break
            r = guarded(unit, flow, lambda: runner(flow, None, None, ind[name], f'baseline{i}'))
            for k in range(retries):
                if r['ok'] or r.get('budget_exhausted') or (budget is not None and sent() >= budget):
                    break
                # a step can miss its budget once on a loaded machine: keep the failed attempt as evidence, try again
                retried.append(r)
                time.sleep(pause)
                r = guarded(unit, flow, lambda: runner(flow, None, None, ind[name], f'baseline{i}'))
                r['retry'] = k + 1
            brs.append(r)
        ok = [r['ok'] for r in brs]
        status = ('not_run' if len(brs) < 2 or any(r.get('budget_exhausted') for r in brs) else
                  'passed' if all(ok) else 'failed' if not any(ok) else 'unstable')
        over = over or status == 'not_run'
        b = {'status': status, 'critical_path': bool(flow.get('critical_path')), 'runs': brs}
        if retried:
            passed_on_retry = sum(1 for r in brs if r.get('retry') and r['ok'])
            if passed_on_retry:
                b['retried'] = passed_on_retry      # runs that failed and passed when tried again
            b['retried_runs'] = retried             # every failed attempt, kept as evidence
        doc['baselines'][name] = b
        doc['budget_reached'] = over
        per = round(sum(r['requests_sent'] for r in brs) / max(1, len(brs)))
        print(f'baseline {name}: {status}' + (f' ({len(retried)} failed run(s) tried again)' if retried else '')
              + f' (about {per} requests to the site per run)', file=sys.stderr)
        save(doc, out, 'partial', extra)
    done_ids = {s['id'] for s in doc['scenarios']}
    for s in plan['scenarios']:
        if s['id'] in done_ids:
            continue
        base = doc['baselines'][s['flow']]['status']
        entry = {'id': s['id'], 'status': 'untested', 'runs': []}
        if base != 'passed':
            entry['reason'] = f'baseline {base}' + (' (request budget reached)' if base == 'not_run' else '')
        elif over or (budget is not None and sent() >= budget):
            over = True
            entry['reason'] = f'request budget reached ({sent()} of {budget} requests sent)'
        else:
            flow = flows[s['flow']]
            step = next(i for i, st in enumerate(flow['steps']) if st['name'] == s['step'])
            # a scenario the watchdog stopped is never run again (its runs before the stop were lost with the worker)
            stopped = next(((i, hung[f'scenario:{s["id"]}:run{i}']) for i in range(1, runs + 1) if f'scenario:{s["id"]}:run{i}' in hung), None)
            for i in range(1, runs + 1) if not stopped else ():
                entry['runs'].append(guarded(f'scenario:{s["id"]}:run{i}', flow, lambda: runner(flow, step, s, ind[s['flow']], f'run{i}')))
            if stopped:
                entry['status'] = 'errored'
                entry['reason'] = (f'run{stopped[0]} did not finish within {stopped[1].get("limit_s")} s (a browser call never returned) and '
                                   'was stopped by the watchdog; the browser was restarted and the run went on without this scenario')
            elif any(r.get('budget_exhausted') for r in entry['runs']):
                over = True
                entry['status'] = 'errored'
                entry['reason'] = 'the request budget ran out during a run, so what it showed is not evidence'
            elif any(not r['steps'] or not r['steps'][-1].get('target') for r in entry['runs']):
                bad = next(r for r in entry['runs'] if not r['steps'] or not r['steps'][-1].get('target'))
                stop = next((x for x in bad['steps'] if not x.get('completed', True)), None)
                entry['status'] = 'errored'
                entry['reason'] = ('a run did not reach the target step: ' + (f'step "{stop["name"]}" did not complete' if stop else '')
                                   + (f' ({bad["error"]})' if bad.get('error') else '')).rstrip(': ')
            elif not any(r.get('fault_triggered') for r in entry['runs']):
                entry['status'] = 'not_triggered'
                entry['reason'] = untriggered_reason(entry['runs'])
            else:
                entry['status'] = 'ran'
        doc['scenarios'].append(entry)
        doc['budget_reached'] = over
        print(f'{s["id"]} {s["flow"]}/{s["step"]} {s["fault"]} on {s["endpoint"]}: {entry["status"]}', file=sys.stderr)
        save(doc, out, 'partial', extra)
    doc['budget_reached'] = over
    doc['finished_at'] = now()
    save(doc, out, 'complete', extra)
    return doc


def _spawn(cmd: list[str]) -> subprocess.Popen:
    if os.name == 'nt':
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(cmd, start_new_session=True)


def _kill_tree(p: subprocess.Popen) -> None:
    """Stop the worker and everything it started (Playwright's driver and the browser)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        p.kill()
    try:
        p.wait(timeout=30)
    except Exception:  # noqa: BLE001
        pass


def supervise(cmd: list[str], out: Path, poll_s: float = 1.0, max_restarts: int = 100) -> int:
    """Run the worker; when a unit (one baseline or scenario run) passes its time limit, stop the worker with its browser,
    record the unit in hung.json, and start it again with --resume. Returns the worker's exit code."""
    out.mkdir(parents=True, exist_ok=True)
    prog, hung_p = out / 'progress.json', out / 'hung.json'
    resume = '--resume' in cmd
    for _ in range(max_restarts + 1):
        prog.unlink(missing_ok=True)
        p = _spawn(cmd + (['--resume'] if resume and '--resume' not in cmd else []))
        killed = None
        try:
            while p.poll() is None:
                time.sleep(poll_s)
                pr = read_json(prog)
                if pr and pr.get('unit') and time.time() - float(pr.get('started', 0)) > float(pr.get('limit_s') or 0) > 0:
                    killed = pr
                    _kill_tree(p)
                    break
        except KeyboardInterrupt:
            _kill_tree(p)
            raise
        if not killed:
            prog.unlink(missing_ok=True)
            return p.returncode
        hung = read_json(hung_p) or []
        hung.append({'unit': killed['unit'], 'limit_s': killed['limit_s'], 'at': now(), 'requests_sent_before': killed.get('sent')})
        write_json(hung_p, hung)
        print(f'watchdog: {killed["unit"]} did not finish within {killed["limit_s"]:.0f} s; stopped the browser, resuming after it',
              file=sys.stderr)
        resume = True
    print('watchdog: too many hung runs; stopping (run.json is partial; rerun with --resume)', file=sys.stderr)
    return 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('flows', type=Path)
    ap.add_argument('scenarios', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--runs', type=int, default=2)
    ap.add_argument('--max-requests', type=int, help='stop starting new runs once this many requests reached the site (overrides request_budget)')
    ap.add_argument('--resume', action='store_true', help='continue a stopped run: keep the baselines and scenarios run.json already has')
    ap.add_argument('--run-timeout', type=float, help='the watchdog\'s limit for one run, in seconds (default: run_timeout_s or from the budgets)')
    ap.add_argument('--no-watchdog', action='store_true', help='run in this process, with no time limit per run')
    ap.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    a = ap.parse_args()
    if not a.worker and not a.no_watchdog:
        argv = [x for x in sys.argv[1:]]
        return supervise([sys.executable, str(Path(__file__).resolve()), *argv, '--worker'], a.out)
    cfg = json.loads(a.flows.read_text(encoding='utf-8'))
    plan = json.loads(a.scenarios.read_text(encoding='utf-8'))
    faults = json.loads((SKILL / 'data' / 'faults.json').read_text(encoding='utf-8'))['faults']
    base_ind = merge_indicators(json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8')), cfg.get('indicators'))
    base_ind['budgets'] = {**base_ind['budgets'], **plan.get('budgets', {})}   # the budgets written before the run
    targets = [cfg['site']] + list(cfg.get('targets') or [])
    try:
        check_authorised(targets, cfg.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    budget = a.max_requests if a.max_requests is not None else cfg.get('request_budget')
    ro, afs = cfg.get('read_only'), cfg.get('allow_form_submission')
    if ro is not None and afs is not None and bool(ro) == bool(afs):
        print('error: read_only and allow_form_submission disagree in flows.json; give one of them', file=sys.stderr)
        return 2
    allow_post = (not bool(ro)) if ro is not None else bool(afs)     # default: writes blocked
    shots = a.out / 'screenshots'
    flows = {f['name']: f for f in cfg['flows']}
    try:
        modes = {n: sw_mode(cfg, f) for n, f in flows.items()}
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    ind = {n: merge_indicators(base_ind, f.get('indicators')) for n, f in flows.items()}
    sw = 'allow' if 'allow' in modes.values() else 'block'
    fresh = {'site': cfg['site'], 'started_at': now(), 'runs_per_scenario': a.runs,
             'safety': {'allow_form_submission': allow_post, 'targets': targets, 'allowed_hosts': cfg.get('allowed_hosts', []),
                        'third_party': cfg.get('third_party', 'block'), 'request_budget': budget, 'prefetch': cfg.get('prefetch', 'allow'),
                        'service_workers': sw},
             'indicators': ind, 'baselines': {}, 'scenarios': []}
    if sw == 'allow':
        fresh['safety']['service_workers_by_flow'] = modes
    doc = resume_doc(a.out) if a.resume else None
    if doc and doc.get('runs_per_scenario') != a.runs:
        print(f'error: run.json was made with --runs {doc.get("runs_per_scenario")}; resume with the same --runs', file=sys.stderr)
        return 2
    if doc is None:
        doc = fresh
        if (a.out / 'hung.json').is_file() and not a.resume:
            (a.out / 'hung.json').unlink()      # a fresh run starts with no hung runs
    prior_sent = int(doc.get('requests_sent') or 0)
    prior_blocked = list(doc.get('blocked_requests') or [])
    prior_events = list(doc.get('harness_events') or [])
    prior_prefetch = int(doc.get('prefetch_blocked') or 0)
    sessions = list(doc.get('sessions') or [])
    hcfg = {'bypass_csp': False}    # test the site under its own CSP unless flows.json says otherwise; this skill injects nothing
    hcfg.update(cfg.get('harness') or {})
    hcfg.pop('allow_form_submission', None)
    hcfg.update({'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'), 'read_only': not allow_post,
                 'targets': targets, 'allowed_hosts': cfg.get('allowed_hosts', []), 'third_party': cfg.get('third_party', 'block'),
                 # a hard cap in the harness too: a run that hits it mid-way is marked failed, never classified
                 'request_budget': None if budget is None else max(0, budget - prior_sent), 'service_workers': 'block',
                 'rate_limit_seconds': cfg.get('rate_limit_seconds', 0.1 if all(is_loopback(u) for u in targets) else 0.5)})
    if cfg.get('init_script'):
        hcfg['init_scripts'] = list(hcfg.get('init_scripts') or []) + [cfg['init_script']]
    if cfg.get('host_map'):
        hcfg['host_map'] = cfg['host_map']
    with Harness(hcfg) as h:
        c = Ctx(h, cfg, faults, shots, allow_post)
        c.prefetch_blocked = prior_prefetch
        doc['browser'] = {'name': 'chromium', 'version': h.version}
        sessions.append({'started_at': now(), 'resumed': bool(a.resume and (doc.get('baselines') or doc.get('scenarios')))})

        def extra():
            sessions[-1]['requests_sent'] = h.requests['sent']
            return {'blocked_requests': prior_blocked + h.blocked, 'requests_sent': prior_sent + h.requests['sent'],
                    'harness': h.metadata(), 'harness_events': prior_events + h.events, 'prefetch_blocked': c.prefetch_blocked,
                    'sessions': sessions}
        execute(doc, cfg, flows, plan, ind, lambda f, u, s, i, t: run_flow(c, f, u, s, i, t), lambda: prior_sent + h.requests['sent'],
                a.out, a.runs, budget, a.run_timeout, extra)
    print(f'{doc["requests_sent"]} request(s) reached the site; {len(doc["blocked_requests"])} blocked by the harness'
          + (f'; {len(doc["hung"])} run(s) stopped by the watchdog' if doc.get('hung') else ''), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
