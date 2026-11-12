#!/usr/bin/env python3
"""Capture what a web app sends, sets and stores in each consent state, typing synthetic canaries.

    python capture_flows.py flows.json --canaries run/canaries.json --out run/capture.json [--server-log server.log]
        [--states before,reject,accept[,partial,dismiss]] [--screenshots run/screenshots]

For each consent state, a fresh browser context loads the start page and records every request, cookie and
storage entry before any interaction (phase "initial"); then, for reject and accept, it operates the banner;
then it runs each flow, typing canary values where a step says {"fill": sel, "canary": field}. Each request
(URL, method, referer, body), cookie and storage value is searched for every canary in every encoding and
written out redacted: values become <canary:FIELD:ENCODING> or <redacted:TYPE>. Raw values are never stored.

An app with no consent banner at all (an internal tool with no non-essential tracking) says so with
"consent": {"banner": null}: one state, no_banner, runs (no choice to make) and counts like "before" for findings;
the other states are not applicable. An app behind sign-in takes "storage_state": a Playwright storage-state file
a person saved after signing in by hand (a test account), kept outside the run folder; every state's context
starts from it, and the capture records only that a session was used, never the file or its contents. Capture the
file before making any consent choice, or the stored choice leaks into the "before" state. ("auth" with a login
recipe, as in browser_harness.py, works too.)

With --server-log (local runs only), the app's log is read after the run and the lines carrying a canary
are recorded, redacted, with their line numbers.

flows.json: {"site", "host_map"?, "authorised_by"?, "allow_form_submission"?, "third_party"?, "allowed_hosts"?, "storage_state"?,
"consent": {"accept": sel, "reject": sel, "partial"?: sel or [actions], "dismiss"?: sel or [actions], "banner"?: sel,
"wait_ms"?: 8000, "partial_refuses"?: [vendor categories]}, "flows": [{"id", "reach": 1-3, "steps": [{"name", "actions": [...]}]}]}.
States: before (no choice), reject, accept, and optionally partial (save preferences with some purposes refused:
a list of actions such as open settings, untick analytics, save) and dismiss (the banner closed without a choice: its
X or an overlay click). Before operating a control the capture waits up to wait_ms for the banner (or the first
control) to be visible. If the banner can't be operated, that state is recorded as untested with the reason. When
the choice itself triggers a write the harness blocks (the site's consent record), the state records
consent_writes_blocked: the server-side record of the choice was never made.

Request bodies are read as bytes and decompressed (gzip, deflate; brotli when the brotli module is installed) and
Base64 bodies decoded before searching; each request records body_read (text, gzip, deflate, br, base64+...,
binary_not_searched, br_not_searched). Content Security Policy violations in the console (the browser stops those
requests before any request event) are recorded per state under csp_violations. Screenshots mask every field a
canary was typed into (Playwright mask); text the page echoes back elsewhere can still show a canary.

Third parties are blocked by default (the browser harness's third_party: block): every request to a host that
isn't loopback (including *.test and host_map hosts), the site or allowed_hosts is aborted in the browser, so
no request reaches a real vendor. The page still attempts it, so it is recorded like any other request, with
"blocked": "third_party" (or "write" for a form submission that wasn't allowed). A blocked vendor never answers,
so its cookies, its follow-up hits and anything its script would have done are not in the capture: the report
must say so. "third_party": "allow" lets requests out to real vendors; use it only when the person running the
audit has agreed that the vendors receive the test's hits (record who in third_party_authorised_by).
"""
from __future__ import annotations
import argparse, base64, binascii, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from browser_harness import SAFE_METHODS, Harness, check_authorised, now  # noqa: E402
from match_canaries import Matcher, decode_body  # noqa: E402

HEX = re.compile(r'(?<![0-9a-f])([0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])', re.I)
CSP_CONSOLE = re.compile(r"Refused to [\w ]+? '(?P<url>https?://[^']+)'.*?Content Security Policy directive:? \"?(?P<directive>[\w-]+)", re.S)
STATES = ('before', 'reject', 'accept', 'partial', 'dismiss', 'no_banner')


def consent_actions(spec) -> list[dict]:
    """A consent control as actions: a selector is one click; a list is a sequence (open settings, untick, save)."""
    if not spec:
        return []
    return [{'click': spec}] if isinstance(spec, str) else list(spec)


def masked_screenshot(page, path: Path, selectors: list[str]) -> Path:
    """A viewport screenshot with every field a canary was typed into masked (Playwright mask), so the image never
    shows a canary value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    masks = []
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                masks.append(loc)
        except Exception:  # noqa: BLE001 - a selector this page doesn't have
            continue
    page.screenshot(path=str(path), full_page=False, mask=masks, mask_color='#000000')
    return path


class Recorder:
    def __init__(self, m: Matcher, h: Harness | None = None):
        self.m, self.h, self.requests, self.phase, self.csp = m, h, [], None, []

    def blocked(self, req) -> str | None:
        """Why the harness will abort this request (it routes after the request event), or None."""
        h = self.h
        if h is None:
            return None
        if getattr(h, 'third_party', 'allow') == 'block' and not h.host_allowed(req.url):
            return 'third_party'
        if req.method not in SAFE_METHODS and not h.allow_post:
            return 'write'
        return None

    def on_request(self, req):
        url = req.url
        if url.startswith('data:'):
            return
        try:
            buf = req.post_data_buffer
        except Exception:  # noqa: BLE001
            buf = None
        body, body_read = decode_body(buf, req.headers.get('content-encoding'))
        if body is not None and len(body) < 20000 and re.fullmatch(r'[A-Za-z0-9+/=_-]{24,}', body.strip()):
            try:  # a Base64 body (some beacons): search the decoded text too
                inner, how = decode_body(base64.b64decode(body.strip() + '=' * (-len(body.strip()) % 4), altchars=b'-_'
                                                          if '-' in body or '_' in body else None))
                if inner is not None:
                    body, body_read = inner, f'base64+{how}'
            except (ValueError, binascii.Error):
                pass
        parts = {'url': url, 'body': body, 'referer': req.headers.get('referer')}
        matches = [dict(h, where=k) for k, v in parts.items() for h in self.m.find(v)]
        red_url = self.m.redact(url)
        hexes = sorted({h for h in HEX.findall(self.m.redact((url or '') + ' ' + (body or ''))) if h})
        red_body = self.m.redact(body) if body else None
        self.requests.append({'n': len(self.requests) + 1, 'phase': self.phase, 'method': req.method, 'resource_type': req.resource_type,
                              'host': urlparse(url).hostname, 'url': red_url, 'path': urlparse(red_url).path,
                              'referer': self.m.redact(req.headers.get('referer')), 'body': red_body[:2000] if red_body else None,
                              'body_length': len(red_body) if red_body else 0, 'body_read': body_read if buf else None,
                              'matches': matches, 'unmatched_hex': hexes, 'blocked': self.blocked(req)})

    def on_console(self, msg):
        """A Content Security Policy violation in the console: the browser stopped the request before it was sent, so
        no request event fires. Recorded as an attempt with its URL (redacted) and directive."""
        try:
            text = msg.text
        except Exception:  # noqa: BLE001
            return
        m = CSP_CONSOLE.search(text or '')
        if m:
            u = m.group('url')
            self.csp.append({'phase': self.phase, 'source': 'console', 'url': self.m.redact(u)[:300], 'host': urlparse(u).hostname,
                             'directive': m.group('directive')})


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
    hcfg = {k: cfg[k] for k in ('host_map', 'authorised_by', 'allow_form_submission', 'third_party', 'allowed_hosts', 'auth') if k in cfg}
    hcfg['targets'] = [site]
    # a signed-in session for an app behind sign-in: a storage-state file a person captured by signing in by hand
    # (Playwright storage_state). It holds session cookies and tokens: it stays outside the run folder and is never recorded.
    if cfg.get('storage_state'):
        st_path = Path(cfg['storage_state']).resolve()
        if not st_path.is_file():
            ap.error(f'storage_state {cfg["storage_state"]} does not exist (sign in by hand and save it with context.storage_state(path=...))')
        if a.out.resolve().parent in st_path.parents or (a.screenshots and a.screenshots.resolve() in st_path.parents):
            ap.error('keep the storage_state file outside the run folder: it holds session cookies and tokens and must not become evidence')
        hcfg['auth'] = {'storage_state': str(st_path)}
    consent_cfg = cfg.get('consent') or {}
    no_banner = 'banner' in consent_cfg and consent_cfg['banner'] is None
    states = a.states.split(',')
    if no_banner:
        if a.states != 'before,reject,accept' and states != ['no_banner']:
            ap.error('consent.banner is null (the app has no consent banner): only the no_banner state runs')
        states = ['no_banner']
    # the site's own Content Security Policy stays in force (the harness bypasses it by default): a vendor call the CSP
    # forbids is then stopped by the browser and seen as a CSP violation, as it would be for a real visitor
    hcfg['bypass_csp'] = bool(cfg.get('bypass_csp', False))
    if cfg.get('third_party') == 'allow' and not (cfg.get('third_party_authorised_by') or '').strip():
        ap.error('third_party: allow sends the test\'s requests to real vendors; set third_party_authorised_by in flows.json')
    hcfg['rate_limit_seconds'] = cfg.get('rate_limit_seconds', 0.3)
    fills = sorted({act['fill'] for f in cfg['flows'] for s in f['steps'] for act in s['actions'] if 'canary' in act})
    if a.screenshots:
        out['screenshots'] = {'masked_fields': fills, 'note': 'fields a canary was typed into are masked; a canary the page '
                              'echoes elsewhere (a greeting, a summary) can still show in an image'}
    with Harness(hcfg) as h:
        out['browser'] = f'{h.browser_name} {h.version}'
        if hcfg.get('auth'):
            out['auth'] = {'method': h.login(), 'note': 'signed-in session from a file a person captured by signing in (or a login recipe '
                           'with credentials from the environment); the session itself is not recorded'}
        if no_banner:
            out['consent_banner'] = False
        out['third_party'] = getattr(h, 'third_party', 'allow')
        if cfg.get('third_party_authorised_by'):
            out['third_party_authorised_by'] = cfg['third_party_authorised_by']
        consent = cfg.get('consent') or {}
        wait_ms = int(consent.get('wait_ms', 8000))
        for state in states:
            if state not in STATES:
                ap.error(f'unknown consent state {state!r}; use {", ".join(STATES)}')
            page = h.new_page(service_workers='block')
            ctx = page.context
            rec = Recorder(m, h)
            page.on('request', rec.on_request)
            page.on('console', rec.on_console)
            st = {'state': state, 'status': 'tested', 'snapshots': []}
            rec.phase = 'initial'
            res = h.goto(page, site)
            st['start'] = {'ok': res.get('ok'), 'status': res.get('status')}
            page.wait_for_timeout(800)
            st['snapshots'].append(dict(phase='initial', **cookies_and_storage(ctx, page, m)))
            if state not in ('before', 'no_banner'):
                acts = consent_actions(consent.get(state))
                writes_before = sum(1 for b in h.blocked if b.get('reason', 'write') == 'write')
                try:
                    if not acts:
                        raise LookupError(f'flows.json has no consent.{state} control')
                    rec.phase = f'after_{state}'
                    first = consent.get('banner') or next((x.get('click') for x in acts if x.get('click')), None)
                    if first:  # banners often appear after hydration or a timer: wait for it before clicking
                        page.wait_for_selector(first, state='visible', timeout=wait_ms)
                    h.act(page, acts)
                    h.settle(page)
                    page.wait_for_timeout(800)
                    st['snapshots'].append(dict(phase=f'after_{state}', **cookies_and_storage(ctx, page, m)))
                    # the site's own record of the choice (a POST) is blocked on a read-only run
                    st['consent_writes_blocked'] = sum(1 for b in h.blocked if b.get('reason', 'write') == 'write') - writes_before
                except Exception as e:
                    what = consent.get(state)
                    st.update(status='untested', reason=f'consent control {what!r} could not be operated: {type(e).__name__}')
            if a.screenshots:
                masked_screenshot(page, a.screenshots / f'{state}-initial.png', fills)
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
                        masked_screenshot(page, a.screenshots / f'{state}-{flow["id"]}.png', fills)
            st['requests'] = rec.requests
            st['csp_violations'] = rec.csp
            st['blocked_writes'] = sum(1 for b in h.blocked if b.get('reason', 'write') == 'write')
            st['blocked_third_party'] = sum(1 for b in h.blocked if b.get('reason') == 'third_party')
            st['blocked_hosts'] = sorted({urlparse(b['url']).hostname or '' for b in h.blocked if b.get('reason') == 'third_party'})
            # writes the harness blocked on the site itself (telemetry, CSP reports, consent records), by the harness's own site field
            st['blocked_first_party_writes'] = sum(1 for b in h.blocked if b.get('reason', 'write') == 'write' and b.get('site') == 'same_site')
            h.blocked.clear()
            h.close_page(page)
            out['states'].append(st)
        out['harness'] = h.metadata()
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
        b = sum(1 for r in st['requests'] if r.get('blocked') == 'third_party')
        print(f'{st["state"]:7} {st["status"]:9} {len(st["requests"]):3} requests, {n} carrying a canary, {b} to third parties blocked')
    print(f'-> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
