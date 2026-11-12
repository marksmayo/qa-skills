#!/usr/bin/env python3
"""Run each flow's baseline, then each planned scenario with its fault injected in the browser, and record what the user sees.

    python run_resilience.py flows.json run/scenarios.json --out run/ [--runs 2] [--max-requests N]

For every flow: the baseline runs twice with no faults (a flow whose two baselines disagree is `unstable`,
one that fails is `failed`; their scenarios are `untested`). A failing baseline step is screenshotted and
observed like a scenario, so a journey that is broken without any fault has evidence. For every scenario:
the flow is replayed to the step, the fault is injected on the endpoint pattern (Playwright routing,
optionally narrowed by request headers or resource type, or the whole context offline), the step's actions
run, and the page is watched for the scenario's whole window: until an error appears, or 10 s for a
message, extended to 30 s while a spinner shows. A slow-but-successful (latency) fault may end early once
the step is done and nothing is pending. Then the fault is removed and recovery is tried: the app's own
retry control if it shows one (an accessible name matching retry, try again, reload), otherwise the step's
actions again (without re-typing input, and without reloading). Faults on a flow's first step, or with
`"from": "load"`, are in place before the page loads (frameworks such as Next.js prefetch route payloads at
page load, so a fault installed later never sees them). Each scenario runs --runs times (default 2).

A fault that matched no request in a run is recorded as `fault_triggered: false`; a scenario none of whose
runs triggered its fault is `not_triggered` (it tested nothing) rather than `ran`.

Recorded per run: requests matching the endpoint (method, URL with sensitive query values redacted, time,
status, response time, whether the injector answered or held it), page errors, console errors (minus the
harness's own blocked-by-client noise), the visible text and the app root's text, spinners and loading
words, error indicators, boundary fallback text and boundary selectors, visible control names, input field
lengths and SHA-256 hashes (never the text) before the fault, after it and after recovery, the retry
controls found, whether the step completed after recovery, screenshots, and the number of requests the
browser let through to the site.

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
or `Sec-Purpose: prefetch` header), and `unreachable` (data the browser cannot reach, recorded by the planner).
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now, is_loopback  # noqa: E402

TEXT = """(root) => { const r = root ? document.querySelector(root) : null;
  return {body: document.body ? document.body.innerText : '', root: r ? r.innerText : null, root_exists: !!r}; }"""
VISIBLE_ANY = """(sels) => sels.some(s => { try { return [...document.querySelectorAll(s)].some(e => { const b = e.getBoundingClientRect();
  const cs = getComputedStyle(e); return b.width > 0 && b.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; }); } catch (x) { return false; } })"""
VISIBLE_TEXT = """(sels) => sels.some(s => { try { return [...document.querySelectorAll(s)].some(e => e.getBoundingClientRect().width > 0 && e.innerText.trim()); }
  catch (x) { return false; } })"""
CONTROLS = """() => [...new Set([...document.querySelectorAll('a[href], button, [role=button], input[type=submit]')]
  .filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; })
  .map(e => (e.innerText || e.getAttribute('aria-label') || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 40)).filter(Boolean))].slice(0, 60)"""
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


def act(h: Harness, page, site: str, actions: list):
    for a in actions:
        if 'goto' in a:
            target = urljoin(site, a['goto']) if not a['goto'].startswith('#') else site.rstrip('/') + '/' + a['goto']
            if page.url.split('#')[0] == target.split('#')[0] and '#' in target:
                page.evaluate('(h) => { location.hash = h; }', target.split('#', 1)[1])
            else:
                h.throttle()
                page.goto(target, wait_until='load', timeout=30000)
            page.wait_for_timeout(300)
        else:
            h.act(page, [a])


def done(page, selector: str, timeout_ms: int = 0) -> bool:
    try:
        if timeout_ms <= 1:
            return page.locator(selector).first.is_visible()
        page.wait_for_selector(selector, timeout=timeout_ms, state='visible')
        return True
    except Exception:  # noqa: BLE001
        return False


def observe(page, ind: dict, root: str | None) -> dict:
    t = page.evaluate(TEXT, root)
    lower = t['body'].lower()
    spin_el = page.evaluate(VISIBLE_ANY, ind['spinner_selectors']) if ind['spinner_selectors'] else False
    loading = sorted({w for w in ind['loading_words'] if w in lower})
    err_el = page.evaluate(VISIBLE_TEXT, ind['error_selectors']) if ind['error_selectors'] else False
    boundary_el = page.evaluate(VISIBLE_ANY, ind['boundary_selectors']) if ind['boundary_selectors'] else False
    try:
        controls = page.evaluate(CONTROLS)
    except Exception:  # noqa: BLE001
        controls = []
    return {'body_len': len(t['body'].strip()), 'root_text': (t['root'] or '')[:400], 'root_len': len((t['root'] or '').strip()),
            'spinner': bool(spin_el or loading), 'loading_words': loading, 'error_element': err_el,
            'error_words': sorted({w for w in ind['error_words'] if w in lower}),
            'empty_words': sorted({w for w in ind['empty_state_words'] if w in lower}),
            'fallback_words': sorted({w for w in ind['boundary_fallback_words'] if w in lower}), 'boundary_element': boundary_el,
            'controls': controls, 'text': t['body'][:600]}


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


def run_flow(c: Ctx, flow, upto: int | None, scen: dict | None, ind: dict, tag: str) -> dict:
    h, cfg, faults, shots = c.h, c.cfg, c.faults, c.shots
    site = cfg['site']
    page = h.new_page(viewport=cfg.get('viewport', '1280x800'), service_workers='block')
    sent_before = h.requests['sent']    # the harness counts what it let out; injected and blocked requests never reach it
    exhausted_before = h.requests['budget_exhausted']
    log = h.log(page)
    reqs: list[dict] = []
    t0 = time.monotonic()
    pattern = scen['endpoint'] if scen else None
    rx = glob_rx(pattern) if pattern else None
    entries: dict[int, dict] = {}
    started: dict[int, float] = {}
    inj: Injector | None = None
    prefetch_ids: set[int] = set()
    offline = {'on': False, 'failed': 0}

    def on_req(r):
        started[id(r)] = time.monotonic()
        if rx and rx.match(r.url):
            e = {'method': r.method, 't_ms': round((time.monotonic() - t0) * 1000), 'url': redact(r.url)}
            entries[id(r)] = e
            reqs.append(e)

    def finish(r, status, failure=None):
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

    def on_fail(r):
        finish(r, None, r.failure)
        if offline['on']:
            offline['failed'] += 1

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
    try:
        if early:   # requests that start with the page (the first step's, or prefetches) need the fault in place first
            t_fault = time.monotonic()
            inj = Injector(fault, scen, t0)
            page.route(pattern, inj.handler)
        if early:
            # with the fault already in place the page may never go idle (a hang, a fast retry loop), so the
            # harness's settle would eat the scenario's window: load without it, still paced
            h.throttle()
            page.goto(site, wait_until='load', timeout=30000)
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
                act(h, page, site, setup)
            except Exception as e:  # noqa: BLE001
                rec['setup_error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:160]}'
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
            before = {}
            try:
                for ai, action in enumerate(step['actions']):
                    act(h, page, site, [action])
                    if target and fields and 'fill' in action and not any('fill' in x for x in step['actions'][ai + 1:]):
                        before = inputs(page, fields)    # what the user typed, before the action that sends it
                step_error = None
            except Exception as e:  # noqa: BLE001
                step_error = f'{type(e).__name__}: {str(e).splitlines()[0][:160]}'
            if not target:
                ok = done(page, step['done'], 10000)
                entry = {'name': step['name'], 'completed': ok, 'error': step_error, 'observed': observe(page, ind, cfg.get('app_root'))}
                if not ok:
                    shot = shots / f'{(scen or {}).get("id") or "baseline-" + re.sub(r"[^A-Za-z0-9]+", "-", flow["name"])}.{tag}.failed-step.png'
                    h.screenshot(page, shot, full_page=False)
                    entry['screenshot'] = shot.name
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
            completed_under_fault = done(page, step['done'], 1)
            shot1 = shots / f'{scen["id"]}.{tag}.fault.png'
            h.screenshot(page, shot1, full_page=False)
            mid_inputs = inputs(page, fields) if fields else {}
            elapsed = round((time.monotonic() - t_fault) * 1000)
            faulted = (inj.seen if inj else 0) if fault['action'] != 'offline' else offline['failed']
            # recovery
            if inj:
                inj.release()
            if fault['action'] == 'offline':
                page.context.set_offline(False)
                offline['on'] = False
            controls = page.evaluate("""(names) => [...document.querySelectorAll('button, a, [role=button]')]
                .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(n => names.some(w => n.toLowerCase().includes(w)))""",
                                     ind['retry_control_names'])
            recovery = 'retry_control' if controls else 'repeat_step'
            try:
                if controls:
                    page.get_by_role('button', name=re.compile('|'.join(ind['retry_control_names']), re.I)).first.click(timeout=3000)
                else:
                    act(h, page, site, [a for a in step['actions'] if 'fill' not in a])   # typed input is not re-entered
            except Exception:  # noqa: BLE001
                pass
            recovered = done(page, step['done'], 5000)
            shot2 = shots / f'{scen["id"]}.{tag}.recovery.png'
            h.screenshot(page, shot2, full_page=False)
            rec['fault_triggered'] = faulted > 0
            rec['faulted_requests'] = faulted
            rec['steps'].append({'name': step['name'], 'target': True, 'error': step_error, 'completed_under_fault': completed_under_fault,
                                 'first_error_ms': first_error_ms, 'spinner_seen': spinner_seen, 'watched_ms': elapsed, 'observed': after,
                                 'inputs': {'before': before, 'after_fault': mid_inputs}, 'retry_controls': controls, 'recovery': recovery,
                                 'recovered': recovered, 'screenshots': [shot1.name, shot2.name]})
    except Exception as e:  # noqa: BLE001
        rec['ok'] = False
        rec['error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'
    finally:
        if inj:
            inj.release()
        rec['requests'] = reqs
        rec['page_errors'] = [f'{e["name"]}: {e["message"]}'[:200] for e in log['page_errors']]
        errs = [m['text'][:200] for m in log['console'] if m['type'] == 'error']
        rec['console_errors'] = [m for m in errs if not BLOCK_NOISE.search(m)]
        rec['console_filtered'] = len(errs) - len(rec['console_errors'])
        rec['finished_at'] = now()
        h.close_page(page)
        rec['requests_sent'] = h.requests['sent'] - sent_before
        c.sent = h.requests['sent']
        if h.requests['budget_exhausted'] and not exhausted_before:
            # the harness aborted requests mid-run: what the page showed is the budget, not the fault
            rec['ok'] = False
            rec['budget_exhausted'] = True
            rec['error'] = rec.get('error') or 'the request budget ran out during this run; the harness aborted the rest'
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('flows', type=Path)
    ap.add_argument('scenarios', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--runs', type=int, default=2)
    ap.add_argument('--max-requests', type=int, help='stop starting new runs once this many requests reached the site (overrides request_budget)')
    a = ap.parse_args()
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
    ind = {n: merge_indicators(base_ind, f.get('indicators')) for n, f in flows.items()}
    doc = {'site': cfg['site'], 'started_at': now(), 'runs_per_scenario': a.runs,
           'safety': {'allow_form_submission': allow_post, 'targets': targets, 'allowed_hosts': cfg.get('allowed_hosts', []),
                      'third_party': cfg.get('third_party', 'block'), 'request_budget': budget, 'prefetch': cfg.get('prefetch', 'allow')},
           'indicators': ind, 'baselines': {}, 'scenarios': []}
    hcfg = {'bypass_csp': False}    # test the site under its own CSP unless flows.json says otherwise; this skill injects nothing
    hcfg.update(cfg.get('harness') or {})
    hcfg.pop('allow_form_submission', None)
    hcfg.update({'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'), 'read_only': not allow_post,
                 'targets': targets, 'allowed_hosts': cfg.get('allowed_hosts', []), 'third_party': cfg.get('third_party', 'block'),
                 # a hard cap in the harness too: a run that hits it mid-way is marked failed, never classified
                 'request_budget': budget,
                 'rate_limit_seconds': cfg.get('rate_limit_seconds', 0.1 if all(is_loopback(u) for u in targets) else 0.5)})
    if cfg.get('init_script'):
        hcfg['init_scripts'] = list(hcfg.get('init_scripts') or []) + [cfg['init_script']]
    if cfg.get('host_map'):
        hcfg['host_map'] = cfg['host_map']
    over = False
    with Harness(hcfg) as h:
        c = Ctx(h, cfg, faults, shots, allow_post)
        doc['browser'] = {'name': 'chromium', 'version': h.version}
        for name, flow in flows.items():
            runs = []
            for i in (1, 2):
                if budget is not None and c.sent >= budget:
                    over = True
                    break
                runs.append(run_flow(c, flow, None, None, ind[name], f'baseline{i}'))
            ok = [r['ok'] for r in runs]
            status = ('not_run' if len(runs) < 2 or any(r.get('budget_exhausted') for r in runs) else
                      'passed' if all(ok) else 'failed' if not any(ok) else 'unstable')
            over = over or status == 'not_run'
            doc['baselines'][name] = {'status': status, 'critical_path': bool(flow.get('critical_path')), 'runs': runs}
            per = round(sum(r['requests_sent'] for r in runs) / max(1, len(runs)))
            print(f'baseline {name}: {status} (about {per} requests to the site per run)', file=sys.stderr)
        for s in plan['scenarios']:
            base = doc['baselines'][s['flow']]['status']
            entry = {'id': s['id'], 'status': 'untested', 'runs': []}
            if base != 'passed':
                entry['reason'] = f'baseline {base}' + (' (request budget reached)' if base == 'not_run' else '')
            elif over or (budget is not None and c.sent >= budget):
                over = True
                entry['reason'] = f'request budget reached ({c.sent} of {budget} requests sent)'
            else:
                step = next(i for i, st in enumerate(flows[s['flow']]['steps']) if st['name'] == s['step'])
                for i in range(1, a.runs + 1):
                    entry['runs'].append(run_flow(c, flows[s['flow']], step, s, ind[s['flow']], f'run{i}'))
                if any(r.get('budget_exhausted') for r in entry['runs']):
                    over = True
                    entry['status'] = 'errored'
                    entry['reason'] = 'the request budget ran out during a run, so what it showed is not evidence'
                elif any(not r['steps'] or not r['steps'][-1].get('target') for r in entry['runs']):
                    entry['status'] = 'errored'
                    entry['reason'] = 'a run did not reach the target step'
                elif not any(r.get('fault_triggered') for r in entry['runs']):
                    entry['status'] = 'not_triggered'
                    entry['reason'] = ('the fault matched no request in any run: the step made no matching request after the fault was installed '
                                       '(prefetched or cached earlier, or the pattern, method or headers do not match)')
                else:
                    entry['status'] = 'ran'
            doc['scenarios'].append(entry)
            print(f'{s["id"]} {s["flow"]}/{s["step"]} {s["fault"]} on {s["endpoint"]}: {entry["status"]}', file=sys.stderr)
        doc['blocked_requests'] = h.blocked
        doc['requests_sent'] = h.requests['sent']
        doc['harness'] = h.metadata()
        doc['harness_events'] = h.events
        doc['prefetch_blocked'] = c.prefetch_blocked
        doc['budget_reached'] = over
    doc['finished_at'] = now()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'run.json').write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'{c.sent} request(s) reached the site; {len(doc["blocked_requests"])} blocked by the harness', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
