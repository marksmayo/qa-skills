#!/usr/bin/env python3
"""Capture what a web app sends, sets and stores in three consent states, typing synthetic canaries.

    python capture_flows.py flows.json --canaries run/canaries.json --out run/capture.json [--server-log server.log]
        [--states before,reject,accept] [--screenshots run/screenshots]

For each consent state, a fresh browser context loads the start page and records every request, cookie and
storage entry before any interaction (phase "initial"); then, for reject and accept, it operates the banner;
then it runs each flow, typing canary values where a step says {"fill": sel, "canary": field}. Each request
(URL, method, referer, body), cookie and storage value is searched for every canary in every encoding and
written out redacted: values become <canary:FIELD:ENCODING> or <redacted:TYPE>. Raw values are never stored.

With --server-log (local runs only), the app's log is read after the run and the lines carrying a canary
are recorded, redacted, with their line numbers.

flows.json: {"site", "host_map"?, "authorised_by"?, "allow_form_submission"?, "consent": {"accept": sel,
"reject": sel}, "flows": [{"id", "reach": 1-3, "steps": [{"name", "actions": [...]}]}]}. If the banner
can't be operated, that state is recorded as untested with the reason.
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, now  # noqa: E402
from match_canaries import Matcher  # noqa: E402

HEX = re.compile(r'(?<![0-9a-f])([0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])', re.I)


class Recorder:
    def __init__(self, m: Matcher):
        self.m, self.requests, self.phase = m, [], None

    def on_request(self, req):
        try:
            body = req.post_data
        except Exception:  # binary bodies
            body = None
        url = req.url
        if url.startswith('data:'):
            return
        parts = {'url': url, 'body': body, 'referer': req.headers.get('referer')}
        matches = [dict(h, where=k) for k, v in parts.items() for h in self.m.find(v)]
        red_url = self.m.redact(url)
        hexes = sorted({h for h in HEX.findall(self.m.redact((url or '') + ' ' + (body or ''))) if h})
        self.requests.append({'n': len(self.requests) + 1, 'phase': self.phase, 'method': req.method, 'resource_type': req.resource_type,
                              'host': urlparse(url).hostname, 'url': red_url, 'path': urlparse(red_url).path,
                              'referer': self.m.redact(req.headers.get('referer')), 'body': self.m.redact(body)[:2000] if body else None,
                              'matches': matches, 'unmatched_hex': hexes})


def cookies_and_storage(ctx, page, m: Matcher) -> dict:
    cookies = []
    for c in ctx.cookies():
        cookies.append({'name': c['name'], 'domain': c['domain'], 'path': c['path'], 'http_only': c['httpOnly'], 'secure': c['secure'],
                        'same_site': c.get('sameSite'), 'persistent': c.get('expires', -1) > 0,
                        'expires_days': round((c['expires'] - time.time()) / 86400) if c.get('expires', -1) > 0 else None,
                        'value_length': len(c['value']), 'matches': m.find(c['value']), 'value': m.redact(c['value']) if m.find(c['value']) else None})
    storage = []
    try:
        for kind in ('localStorage', 'sessionStorage'):
            items = page.evaluate(f'() => Object.entries(window.{kind})')
            for k, v in items:
                storage.append({'kind': kind, 'origin': urlparse(page.url).hostname, 'key': k, 'value_length': len(v),
                                'matches': m.find(v), 'value': m.redact(v)})
    except Exception as e:
        storage.append({'error': type(e).__name__})
    return {'cookies': cookies, 'storage': storage}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('flows', type=Path)
    ap.add_argument('--canaries', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--server-log', type=Path)
    ap.add_argument('--states', default='before,reject,accept')
    ap.add_argument('--screenshots', type=Path)
    a = ap.parse_args()
    cfg = json.loads(a.flows.read_text(encoding='utf-8'))
    canaries = json.loads(a.canaries.read_text(encoding='utf-8'))
    m = Matcher(canaries)
    site = cfg['site']
    check_authorised([site], cfg.get('authorised_by'))
    log_start = a.server_log.read_text(encoding='utf-8').count('\n') if a.server_log and a.server_log.exists() else 0
    out = {'site': site, 'canary_set': canaries['id'], 'started': now(), 'states': [], 'browser': None,
           'allow_form_submission': bool(cfg.get('allow_form_submission')), 'flows': [{'id': f['id'], 'reach': f.get('reach', 2),
                                                                                       'steps': [s['name'] for s in f['steps']]} for f in cfg['flows']]}
    hcfg = {k: cfg[k] for k in ('host_map', 'authorised_by', 'allow_form_submission') if k in cfg}
    hcfg['rate_limit_seconds'] = cfg.get('rate_limit_seconds', 0.3)
    with Harness(hcfg) as h:
        out['browser'] = f'{h.browser_name} {h.version}'
        for state in a.states.split(','):
            page = h.new_page(service_workers='block')
            ctx = page.context
            rec = Recorder(m)
            page.on('request', rec.on_request)
            st = {'state': state, 'status': 'tested', 'snapshots': []}
            rec.phase = 'initial'
            res = h.goto(page, site)
            st['start'] = {'ok': res.get('ok'), 'status': res.get('status')}
            page.wait_for_timeout(800)
            st['snapshots'].append(dict(phase='initial', **cookies_and_storage(ctx, page, m)))
            if state in ('reject', 'accept'):
                sel = (cfg.get('consent') or {}).get(state)
                try:
                    rec.phase = f'after_{state}'
                    page.click(sel, timeout=5000)
                    h.settle(page)
                    page.wait_for_timeout(800)
                    st['snapshots'].append(dict(phase=f'after_{state}', **cookies_and_storage(ctx, page, m)))
                except Exception as e:
                    st.update(status='untested', reason=f'consent control {sel!r} could not be operated: {type(e).__name__}')
            if a.screenshots:
                h.screenshot(page, a.screenshots / f'{state}-initial.png', full_page=False)
            if st['status'] == 'tested':
                for flow in cfg['flows']:
                    for step in flow['steps']:
                        rec.phase = f'{flow["id"]}/{step["name"]}'
                        actions = []
                        for act in step['actions']:
                            if 'canary' in act:
                                actions.append({'fill': act['fill'], 'value': canaries['fields'][act['canary']]['value']})
                            elif 'goto' in act:
                                actions.append({'goto': urljoin(site, act['goto'])})
                            else:
                                actions.append(act)
                        try:
                            h.act(page, actions)
                            h.settle(page)
                            page.wait_for_timeout(700)
                            step_ok = True
                        except Exception as e:
                            step_ok = False
                            st.setdefault('errors', []).append({'phase': rec.phase, 'error': f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'})
                        st['snapshots'].append(dict(phase=rec.phase, ok=step_ok, url=m.redact(page.url), **cookies_and_storage(ctx, page, m)))
                    if a.screenshots:
                        h.screenshot(page, a.screenshots / f'{state}-{flow["id"]}.png', full_page=False)
            st['requests'] = rec.requests
            st['blocked_writes'] = len(h.blocked)
            h.blocked.clear()
            h.close_page(page)
            out['states'].append(st)
    if a.server_log and a.server_log.exists():
        lines = a.server_log.read_text(encoding='utf-8').splitlines()[log_start:]
        out['server_log'] = {'file': a.server_log.name, 'lines_read': len(lines),
                             'hits': [{'line': log_start + i + 1, 'matches': m.find(l), 'text': m.redact(l)[:300]}
                                      for i, l in enumerate(lines) if m.find(l)]}
    out['finished'] = now()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    for st in out['states']:
        n = sum(1 for r in st['requests'] if r['matches'])
        print(f'{st["state"]:7} {st["status"]:9} {len(st["requests"]):3} requests, {n} carrying a canary')
    print(f'-> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
