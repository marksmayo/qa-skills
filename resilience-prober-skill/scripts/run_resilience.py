#!/usr/bin/env python3
"""Run each flow's baseline, then each planned scenario with its fault injected in the browser, and record what the user sees.

    python run_resilience.py flows.json run/scenarios.json --out run/ [--runs 2]

For every flow: the baseline runs twice with no faults (a flow whose two baselines disagree is `unstable`,
one that fails is `failed`; their scenarios are `untested`). For every scenario: the flow is replayed to
the step, the fault is injected on the endpoint pattern (Playwright routing, or the whole context offline),
the step's actions run, and the page is watched until an error appears or the budget runs out (10 s for a
message, 30 s for a spinner when one is showing). Then the fault is removed and recovery is tried: the
app's own retry control if it shows one (an accessible name matching retry, try again, reload), otherwise
the step's actions again (without re-typing input, and without reloading). Faults on a flow's first step
are in place before the page loads, because that step's requests start with it. Each scenario runs --runs times (default 2: reproduced or not).

Recorded per run: every request to the endpoint (method, time, status or injected fault), page errors,
console errors, the visible text and the app root's text, spinners, error indicators, boundary fallback
text, input field lengths and SHA-256 hashes (never the text) before the fault, after it and after
recovery, the retry controls found, whether the step completed after recovery, and screenshots after the
fault and after recovery.

Writes are never forwarded during a scenario that targets them: the fault answers them in the browser.
Other writes follow the harness rule (blocked unless allow_form_submission).
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now  # noqa: E402

TEXT = """(root) => { const r = root ? document.querySelector(root) : null;
  const vis = (sel) => [...document.querySelectorAll(sel)].some(e => { const b = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                                                                       return b.width > 0 && b.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; });
  return {body: document.body ? document.body.innerText : '', root: r ? r.innerText : null, root_exists: !!r}; }"""


def sha(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


class Injector:
    """Applies one fault to requests matching a pattern. Delays are queued and released by pump()."""
    def __init__(self, fault: dict, method: str | None):
        self.fault, self.method = fault, method
        self.pending: list[tuple[float, object, str]] = []
        self.seen = 0
        self.active = True

    def handler(self, route, request):
        if not self.active or (self.method and request.method != self.method):
            return route.fallback()
        self.seen += 1
        f = self.fault
        act = f['action']
        if act == 'delay':
            self.pending.append((time.monotonic() + f['ms'] / 1000, route, 'continue'))
        elif act == 'delay_abort':
            self.pending.append((time.monotonic() + f['ms'] / 1000, route, 'abort:' + f.get('error', 'timedout')))
        elif act == 'hang':
            self.pending.append((float('inf'), route, 'continue'))
        elif act == 'abort':
            route.abort(f.get('error', 'failed'))
        elif act == 'flap':
            if self.seen <= f['failures']:
                route.fulfill(status=f['status'], body=json.dumps({'error': 'injected'}), content_type='application/json')
            else:
                route.fallback()
        elif act == 'fulfill':
            body = f.get('body', '')
            if 'body_size' in f:
                body = '{"items": [' + ','.join(['{"title": "x"}'] * (f['body_size'] // 16)) + ']}'
            if not isinstance(body, str):
                body = json.dumps(body)
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
    spin = page.evaluate('(sels) => sels.some(s => [...document.querySelectorAll(s)].some(e => e.getBoundingClientRect().width > 0))',
                         ind['spinner_selectors'])
    err_el = page.evaluate('(sels) => sels.some(s => [...document.querySelectorAll(s)].some(e => e.getBoundingClientRect().width > 0 && e.innerText.trim()))',
                           ind['error_selectors'])
    return {'body_len': len(t['body'].strip()), 'root_text': (t['root'] or '')[:400], 'root_len': len((t['root'] or '').strip()),
            'spinner': spin, 'error_element': err_el, 'error_words': sorted({w for w in ind['error_words'] if w in lower}),
            'empty_words': sorted({w for w in ind['empty_state_words'] if w in lower}),
            'fallback_words': sorted({w for w in ind['boundary_fallback_words'] if w in lower}), 'text': t['body'][:600]}


def inputs(page, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        try:
            v = page.input_value(f, timeout=1000)
            out[f] = {'length': len(v), 'sha256': sha(v)}
        except Exception:  # noqa: BLE001
            out[f] = {'length': None, 'sha256': None, 'note': 'field not present'}
    return out


def run_flow(h, cfg, flow, upto: int | None, scen: dict | None, faults: dict, ind: dict, shots: Path, tag: str) -> dict:
    site = cfg['site']
    page = h.new_page(viewport=cfg.get('viewport', '1280x800'), service_workers='block')
    log = h.log(page)
    reqs: list[dict] = []
    t0 = time.monotonic()
    pattern = scen['endpoint'] if scen else None
    rx = re.compile('^' + re.escape(pattern).replace(r'\*\*', '.*').replace(r'\*', '[^/]*') + '$') if pattern else None
    responses: dict[int, dict] = {}

    def on_req(r):
        if rx and rx.match(r.url):
            reqs.append({'method': r.method, 't_ms': round((time.monotonic() - t0) * 1000), 'url': r.url.split('?')[0]})

    def on_resp(r):
        if rx and rx.match(r.url):
            for q in reversed(reqs):
                if q['url'] == r.url.split('?')[0] and 'status' not in q:
                    q['status'] = r.status
                    q['retry_after'] = r.headers.get('retry-after')
                    break

    def on_fail(r):
        if rx and rx.match(r.url):
            for q in reversed(reqs):
                if q['url'] == r.url.split('?')[0] and 'status' not in q:
                    q['status'] = None
                    q['failure'] = r.failure
                    break
    page.on('request', on_req)
    page.on('response', on_resp)
    page.on('requestfailed', on_fail)
    rec = {'tag': tag, 'started_at': now(), 'steps': [], 'ok': True}
    inj = None
    early = scen is not None and upto == 0 and faults[scen['fault']]['action'] != 'offline'
    try:
        if early:   # the first step's requests start at page load, so the fault must be in place before it
            inj = Injector(faults[scen['fault']], scen.get('method'))
            page.route(pattern, inj.handler)
            t_fault = time.monotonic()
        page.goto(site, wait_until='load', timeout=30000)
        page.wait_for_timeout(500)
        for i, step in enumerate(flow['steps']):
            if upto is not None and i > upto:
                break
            target = scen is not None and i == upto
            fields = list((step.get('input') or {}).keys())
            if target:
                fault = faults[scen['fault']]
                if fault['action'] == 'offline':
                    page.context.set_offline(True)
                    t_fault = time.monotonic()
                elif not early:
                    inj = Injector(fault, scen.get('method'))
                    page.route(pattern, inj.handler)
                    t_fault = time.monotonic()
            try:
                act(h, page, site, step['actions'])
                step_error = None
            except Exception as e:  # noqa: BLE001
                step_error = f'{type(e).__name__}: {str(e).splitlines()[0][:160]}'
            if not target:
                ok = done(page, step['done'], 10000)
                rec['steps'].append({'name': step['name'], 'completed': ok, 'error': step_error,
                                     'observed': observe(page, ind, cfg.get('app_root'))})
                if not ok:
                    rec['ok'] = False
                    break
                continue
            before = inputs(page, fields) if fields else {}
            # watch until an error shows, or the budget runs out
            budget = ind['budgets']['message_ms']
            first_error_ms, spinner_seen = None, False
            while (time.monotonic() - t_fault) * 1000 < budget:
                if inj:
                    inj.pump()
                page.wait_for_timeout(250)
                ob = observe(page, ind, cfg.get('app_root'))
                spinner_seen = spinner_seen or ob['spinner']
                if (ob['error_element'] or ob['fallback_words']) and first_error_ms is None:
                    first_error_ms = round((time.monotonic() - t_fault) * 1000)
                    break
                if ob['spinner'] and budget < ind['budgets']['spinner_ms']:
                    budget = ind['budgets']['spinner_ms']
                if not ob['spinner'] and done(page, step['done'], 1) and (time.monotonic() - t_fault) * 1000 > 1500:
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
            # recovery
            if inj:
                inj.release()
            if fault['action'] == 'offline':
                page.context.set_offline(False)
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
        rec['console_errors'] = [c['text'][:200] for c in log['console'] if c['type'] == 'error']
        rec['finished_at'] = now()
        h.close_page(page)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('flows', type=Path)
    ap.add_argument('scenarios', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--runs', type=int, default=2)
    a = ap.parse_args()
    cfg = json.loads(a.flows.read_text(encoding='utf-8'))
    plan = json.loads(a.scenarios.read_text(encoding='utf-8'))
    faults = json.loads((SKILL / 'data' / 'faults.json').read_text(encoding='utf-8'))['faults']
    ind = json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8'))
    try:
        check_authorised([cfg['site']], cfg.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    shots = a.out / 'screenshots'
    flows = {f['name']: f for f in cfg['flows']}
    doc = {'site': cfg['site'], 'started_at': now(), 'baselines': {}, 'scenarios': []}
    with Harness({'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'), 'rate_limit_seconds': 0.1,
                  'allow_form_submission': cfg.get('allow_form_submission', True)}) as h:
        doc['browser'] = {'name': 'chromium', 'version': h.version}
        for name, flow in flows.items():
            runs = [run_flow(h, cfg, flow, None, None, faults, ind, shots, f'baseline{i}') for i in (1, 2)]
            ok = [r['ok'] for r in runs]
            status = 'passed' if all(ok) else 'failed' if not any(ok) else 'unstable'
            doc['baselines'][name] = {'status': status, 'runs': runs}
            print(f'baseline {name}: {status}', file=sys.stderr)
        for s in plan['scenarios']:
            base = doc['baselines'][s['flow']]['status']
            entry = {'id': s['id'], 'status': 'untested' if base != 'passed' else 'ran', 'runs': []}
            if base == 'passed':
                step = next(i for i, st in enumerate(flows[s['flow']]['steps']) if st['name'] == s['step'])
                for i in range(1, a.runs + 1):
                    entry['runs'].append(run_flow(h, cfg, flows[s['flow']], step, s, faults, ind, shots, f'run{i}'))
                if any(not r['steps'] or not r['steps'][-1].get('target') for r in entry['runs']):
                    entry['status'] = 'errored'
            else:
                entry['reason'] = f'baseline {base}'
            doc['scenarios'].append(entry)
            print(f'{s["id"]} {s["flow"]}/{s["step"]} {s["fault"]} on {s["endpoint"]}: {entry["status"]}', file=sys.stderr)
        doc['blocked_requests'] = h.blocked
    doc['finished_at'] = now()
    (a.out / 'run.json').write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
