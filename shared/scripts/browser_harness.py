#!/usr/bin/env python3
"""Shared browser plumbing for the skills that drive a real browser (Playwright for Python).

    from browser_harness import Harness, check_authorised
    check_authorised(urls, authorised_by)                 # raises unless every URL is loopback or someone authorised it
    with Harness(cfg) as h:
        page = h.new_page(viewport='1280x800', timezone='Australia/Sydney', locale='en-AU',
                          clock='2026-11-12T08:00:00+11:00', cpu_throttle=4, network='slow3g')
        h.goto(page, url); h.act(page, [{'click': '#add'}]); h.screenshot(page, path)
        h.close_page(page, trace_path)                    # console, page errors and failed requests are in h.log(page)

Needs `pip install playwright` and `playwright install chromium` (firefox and webkit for those
browsers). Extracted from the WCAG auditor's runner; that skill keeps its own copy until it is
migrated.

Config (dict):
    browser               chromium (default), firefox or webkit
    authorised_by         who confirmed testing the target is allowed; required unless every URL is loopback
    read_only             true (default): every request other than GET, HEAD and OPTIONS (form posts, fetch
                          writes, sendBeacon, CSP reports, telemetry) is aborted and logged in h.blocked with
                          reason write, its method, and site same_site (the target's own site) or third_party.
                          allow_form_submission is the older name: allow_form_submission true == read_only
                          false. Giving both with opposite meanings is an error. Only a login recipe posts.
    bypass_csp            true (default): pages run with Content-Security-Policy bypassed, so styles and scripts
                          the skills inject (freeze CSS, engines, probes) work under hash- or nonce-based CSPs.
                          h.metadata() records it, so a report can say the page's CSP was not enforced; set
                          false to test the site as its CSP really behaves.
    request_budget        none (default), or the most requests the run may send: once reached, every further
                          request is aborted (reason budget) and h.requests['budget_exhausted'] is true, so the
                          run must be reported as incomplete
    init_scripts          JavaScript run in every page before the site's own scripts: a list of source strings
                          or {"path": file}; new_page(init_scripts=[...]) adds more for one page (e.g. make
                          localStorage throw)
    launch_args           extra browser command-line arguments (e.g. Chromium flags), recorded in metadata
    firefox_user_prefs    Firefox preferences ({"dom.storage.enabled": false}); Firefox only
    max_retries           2 (default): how many times goto() retries a 429 or 503
    max_retry_after_seconds  60 (default): the longest Retry-After goto() will wait; a longer one stops retrying
    stop_on_429           false (default); true: the first 429 stops the run: no retry, and every later goto()
                          returns at once without sending (h.stopped says why)
    third_party           block (default): every request to a host that isn't loopback (including *.test and
                          host_map hosts), a host the run navigated to with goto(), a host in targets, or a
                          host in allowed_hosts is aborted and logged in h.blocked with reason third_party,
                          GETs included, so a run never sends analytics hits or ad requests from a test page.
                          The page still attempts the request, so it shows in request logs. allow lets them out.
    targets               URLs whose hosts are part of the site under test (goto() adds each host it visits)
    allowed_hosts         further hosts to let through: ["cdn.example.com", "*.example-cdn.net"]
    service_workers       allow (default) or block, for every page unless new_page() says otherwise
    rate_limit_seconds    0.5 between page loads; 429/503 back off (Retry-After) up to max_retries times
    auth                  {"storage_state": file} or {"login": {"url", "steps": [...], "wait_for"}};
                          login steps use {"fill": sel, "env": NAME}, so credentials never appear in output;
                          an identity provider the login redirects to must be listed in allowed_hosts.
                          storage_state is a session a person captured by signing in by hand, for apps behind
                          SSO, MFA or an OIDC library (MSAL, oidc-client-ts, Auth0): run
                          `python browser_harness.py capture-session --url <app> --out <file outside the repo>`.
                          It holds cookies, localStorage, IndexedDB and (unlike Playwright's own file)
                          sessionStorage, which is restored on its origin before the app's scripts run. The
                          run stays read-only and blocks third parties, so a token refresh that posts to the
                          identity provider fails: capture shortly before the run. metadata()["auth"] records
                          counts only (cookies, keys, expired cookies), never values or the file's path
    trace_with_auth       false (default): a page in a signed-in session (storage_state or login) is not
                          Playwright-traced, because a trace holds its cookies and tokens; the page's log says
                          so under not_applied. true records the trace anyway (keep it out of shared evidence)
    headless              true (default)
    host_map              {"*.shop.test": "127.0.0.1"}: resolve these hosts to loopback (Chromium only), so a
                          sample app can use made-up hostnames for itself and its stub vendors

Actions (act): {"click": sel} {"dblclick": sel} {"hover": sel} {"press": key} {"fill": sel, "value"|"env"}
{"select": sel, "value"} {"check": sel} {"uncheck": sel} {"wait": ms} {"wait_for": sel} {"goto": url}

Throttling uses the Chrome DevTools Protocol, so it is Chromium only; on another browser it is
recorded as not applied. Network presets: slow3g (400 kbit/s, 400 ms), fast3g (1.6 Mbit/s, 150 ms),
offline, or a dict {download_kbps, upload_kbps, rtt_ms}. new_page(service_workers='block') keeps
a cold run cold; mobile=True sets is_mobile and has_touch (Chromium). h.cdp(page) returns the
page's CDP session, and h.start_trace(page) / h.stop_trace() record a DevTools performance trace. The clock uses Playwright's page.clock (1.45+): the page sees that fixed time, and
time runs on from it.

Request accounting: h.requests counts every request the browser routed through the harness
({seen, sent, blocked: {write, third_party, budget}, by_method, budget, budget_exhausted}).
Requests the page's own CSP stopped never reach the harness (unless bypass_csp), and service
worker fetches may not be routed. h.metadata() returns the run settings a report should state
(browser, version, read_only, bypass_csp, third_party, allowed hosts, request budget, init
scripts, launch arguments, prefs, retry policy, stop) with the request counts.

retry_decision(status, retry_after, attempt, ...) is the 429/503 policy goto() uses, for skills
that send requests themselves (page.goto, fetch): it returns ("retry", seconds), ("stop", why)
or ("ok", None), honouring Retry-After in seconds or as an HTTP date.
"""
from __future__ import annotations
import ipaddress, json, os, struct, time, zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

NETWORK = {'slow3g': {'download': 400_000 / 8, 'upload': 400_000 / 8, 'latency': 400},
           'fast3g': {'download': 1_600_000 / 8, 'upload': 750_000 / 8, 'latency': 150},
           'offline': None}
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


class NotAuthorised(Exception):
    pass


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def is_loopback(url: str) -> bool:
    host = urlparse(url).hostname or ''
    if host == 'localhost' or host.endswith('.localhost') or host.endswith('.test'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_authorised(urls: list[str], authorised_by: str | None) -> None:
    remote = [u for u in urls if not is_loopback(u)]
    if remote and not (authorised_by or '').strip():
        raise NotAuthorised(f'{remote[0]} is not on loopback: say who authorised testing it (authorised_by)')


def retry_after_seconds(value: str | None) -> float | None:
    """Retry-After as seconds: a number of seconds or an HTTP date; None when absent or unreadable."""
    value = (value or '').strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def retry_decision(status: int | None, retry_after: str | None, attempt: int, max_retries: int = 2,
                   max_wait: float = 60, stop_on_429: bool = False) -> tuple[str, float | str | None]:
    """The 429/503 policy: ("ok", None) for any other status; ("retry", seconds) to wait and try again;
    ("stop", reason) when retrying would hammer the site or the caller asked to stop at the first 429.
    attempt counts from 0 (the first response)."""
    if status not in (429, 503):
        return 'ok', None
    if status == 429 and stop_on_429:
        return 'stop', 'HTTP 429: stopping at the first rate limit (stop_on_429)'
    if attempt >= max_retries:
        return 'stop', f'HTTP {status} after {max_retries} retr{"y" if max_retries == 1 else "ies"}'
    wait = retry_after_seconds(retry_after)
    if wait is None:
        wait = 10.0 * (attempt + 1)
    if wait > max_wait:
        return 'stop', f'HTTP {status} with Retry-After {wait:.0f}s, longer than the {max_wait:.0f}s maximum'
    return 'retry', wait


_SECOND_LEVEL = {'co', 'com', 'net', 'org', 'gov', 'edu', 'ac', 'govt', 'nhs', 'ltd', 'plc', 'or', 'ne', 'go'}


def site_of(host: str) -> str:
    """An approximate registrable domain ("site"): the last two labels, three under a two-letter country code with
    a common second level (shop.example.co.uk -> example.co.uk). IP addresses and single labels are themselves."""
    host = (host or '').lower().rstrip('.')
    try:
        ipaddress.ip_address(host.strip('[]'))
        return host
    except ValueError:
        pass
    parts = host.split('.')
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def parse_vp(s: str) -> tuple[int, int]:
    w, h = s.lower().split('x')
    return int(w), int(h)


# ---------------------------------------------------------------- PNG (stdlib)
def png_pixels(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG into rows of raw pixel bytes."""
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, idat, w, h, bpp = 8, b'', 0, 0, 3
    while pos < len(data):
        n, typ = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b'IHDR':
            w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', body)
            if depth != 8 or ctype not in (2, 6) or interlace:
                raise ValueError('unsupported PNG')
            bpp = 3 if ctype == 2 else 4
        elif typ == b'IDAT':
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, bpp, rows


def changed_pixels(a: bytes, b: bytes, threshold: int = 40) -> int:
    wa, ha, bpa, ra = png_pixels(a)
    wb, hb, bpb, rb = png_pixels(b)
    n = 0
    for y in range(min(ha, hb)):
        la, lb = ra[y], rb[y]
        for x in range(min(wa, wb)):
            ia, ib = x * bpa, x * bpb
            if abs(la[ia] - lb[ib]) + abs(la[ia + 1] - lb[ib + 1]) + abs(la[ia + 2] - lb[ib + 2]) > threshold:
                n += 1
    return n


def png_encode(width: int, height: int, rows: list[bytes], bpp: int = 3) -> bytes:
    """Encode raw RGB (bpp 3) or RGBA (bpp 4) rows as a PNG (no filtering)."""
    def chunk(typ: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + typ + body + struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff)
    raw = b''.join(b'\x00' + r for r in rows)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2 if bpp == 3 else 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')


# ---------------------------------------------------------------- harness
class Harness:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.browser_name = cfg.get('browser', 'chromium')
        ro, afs = cfg.get('read_only'), cfg.get('allow_form_submission')
        if ro is not None and afs is not None and bool(ro) == bool(afs):
            raise ValueError('read_only and allow_form_submission disagree; give one of them')
        self.read_only = bool(ro) if ro is not None else not bool(afs)
        self.allow_post = not self.read_only        # older name, kept for callers that read it
        self.bypass_csp = bool(cfg.get('bypass_csp', True))
        self.third_party = cfg.get('third_party', 'block')
        if self.third_party not in ('block', 'allow'):
            raise ValueError("third_party must be 'block' or 'allow'")
        self.allowed_hosts = [h.lower() for h in cfg.get('allowed_hosts', [])] + [h.lower() for h in (cfg.get('host_map') or {})]
        self.targets: set[str] = {(urlparse(u).hostname or '').lower() for u in cfg.get('targets', [])}
        self.visited: set[str] = set(self.targets)   # every host goto() opened, loopback included: the "same site"
        self.service_workers = cfg.get('service_workers', 'allow')
        self.rate = float(cfg.get('rate_limit_seconds', 0.5))
        self.max_retries = int(cfg.get('max_retries', 2))
        self.max_retry_after = float(cfg.get('max_retry_after_seconds', 60))
        self.stop_on_429 = bool(cfg.get('stop_on_429', False))
        self.stopped: dict | None = None
        budget = cfg.get('request_budget')
        self.requests = {'seen': 0, 'sent': 0, 'blocked': {'write': 0, 'third_party': 0, 'budget': 0}, 'by_method': {},
                         'budget': int(budget) if budget is not None else None, 'budget_exhausted': False}
        self.init_scripts = [self._script_source(s) for s in cfg.get('init_scripts') or []]
        self.launch_args = [str(a) for a in cfg.get('launch_args') or []]
        self.firefox_user_prefs = dict(cfg.get('firefox_user_prefs') or {})
        if self.firefox_user_prefs and self.browser_name != 'firefox':
            raise ValueError('firefox_user_prefs is for browser firefox only')
        self.version = None
        self.blocked: list[dict] = []
        self.events: list[dict] = []
        self.last_load = 0.0
        self.storage_state = None
        self._session_script = None   # restores a captured sessionStorage; holds token values, so never recorded
        self.auth_info: dict | None = None
        self.trace_with_auth = bool(cfg.get('trace_with_auth', False))
        self._pw = self._browser = None
        self._logs: dict[int, dict] = {}
        self._contexts: dict[int, object] = {}
        self._cdp: dict[int, object] = {}

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.browser_name)
        args = list(self.launch_args)
        if self.cfg.get('host_map'):
            if self.browser_name != 'chromium':
                raise RuntimeError('host_map is Chromium only')
            args.append('--host-resolver-rules=' + ', '.join(f'MAP {h} {ip}' for h, ip in self.cfg['host_map'].items()))
        kw = {'headless': self.cfg.get('headless', True), 'args': args}
        if self.firefox_user_prefs:
            kw['firefox_user_prefs'] = self.firefox_user_prefs
        self._browser = launcher.launch(**kw)
        self.version = self._browser.version
        self.login()
        return self

    @staticmethod
    def _script_source(s) -> str:
        if isinstance(s, dict) and 'path' in s:
            return Path(s['path']).read_text(encoding='utf-8')
        if isinstance(s, str):
            return s
        raise ValueError('init_scripts entries are JavaScript source strings or {"path": file}')

    def metadata(self) -> dict:
        """The settings and counts a report should state about how the browser ran."""
        return {'browser': self.browser_name, 'version': self.version, 'read_only': self.read_only,
                'bypass_csp': self.bypass_csp, 'third_party': self.third_party, 'allowed_hosts': list(self.allowed_hosts),
                'request_budget': self.requests['budget'], 'init_scripts': len(self.init_scripts),
                'launch_args': list(self.launch_args), 'firefox_user_prefs': dict(self.firefox_user_prefs),
                'retry': {'max_retries': self.max_retries, 'max_retry_after_seconds': self.max_retry_after,
                          'stop_on_429': self.stop_on_429},
                'stopped': self.stopped, 'requests': {**self.requests, 'blocked': dict(self.requests['blocked']),
                                                      'by_method': dict(self.requests['by_method'])},
                'auth': dict(self.auth_info) if self.auth_info else None, 'trace_with_auth': self.trace_with_auth}

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -------------------------------------------------------- pages
    def host_allowed(self, url: str) -> bool:
        """Loopback, the hosts this run navigated to, host_map hosts and allowed_hosts; nothing else."""
        if url.startswith(('data:', 'blob:', 'about:')):
            return True
        host = (urlparse(url).hostname or '').lower()
        if is_loopback(url) or host in self.targets:
            return True
        return any(host == p or (p.startswith('*.') and host.endswith(p[1:])) for p in self.allowed_hosts)

    def site(self, url: str) -> str:
        """same_site when the URL is on the site under test (a host goto() opened or a target, or the same
        registrable domain), else third_party. Loopback hosts count as the site only when the run opened them."""
        host = (urlparse(url).hostname or '').lower()
        if not host or host in self.visited:
            return 'same_site'
        s = site_of(host)
        return 'same_site' if any(site_of(v) == s for v in self.visited if v) else 'third_party'

    def _block(self, request, reason: str):
        self.requests['blocked'][reason] = self.requests['blocked'].get(reason, 0) + 1
        self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': reason,
                             'site': self.site(request.url), 'at': now()})

    def guard(self, route, request):
        r = self.requests
        r['seen'] += 1
        r['by_method'][request.method] = r['by_method'].get(request.method, 0) + 1
        if self.third_party == 'block' and not self.host_allowed(request.url):
            self._block(request, 'third_party')
            return route.abort('blockedbyclient')
        if request.method not in SAFE_METHODS and not self.allow_post:
            self._block(request, 'write')
            return route.abort()
        if r['budget'] is not None and r['sent'] >= r['budget']:
            if not r['budget_exhausted']:
                r['budget_exhausted'] = True
                self.events.append({'url': request.url.split('?')[0], 'event': f'request budget of {r["budget"]} used up; '
                                    'every further request is aborted', 'at': now()})
            self._block(request, 'budget')
            return route.abort('blockedbyclient')
        r['sent'] += 1
        return route.continue_()

    def new_page(self, viewport: str = '1280x800', timezone: str | None = None, locale: str | None = None,
                 clock: str | None = None, cpu_throttle: float | None = None, network: str | None = None,
                 reduced_motion: str = 'no-preference', trace: bool = False, user_agent: str | None = None,
                 service_workers: str | None = None, mobile: bool = False, init_scripts: list | None = None):
        service_workers = service_workers or self.service_workers
        w, h = parse_vp(viewport)
        kw = {'viewport': {'width': w, 'height': h}, 'reduced_motion': reduced_motion, 'storage_state': self.storage_state,
              'service_workers': service_workers, 'bypass_csp': self.bypass_csp}
        if mobile and self.browser_name == 'chromium':
            kw.update(is_mobile=True, has_touch=True)
        if timezone:
            kw['timezone_id'] = timezone
        if locale:
            kw['locale'] = locale
        if user_agent:
            kw['user_agent'] = user_agent
        ctx = self._browser.new_context(**kw)
        ctx.route('**/*', self.guard)
        if self._session_script:
            ctx.add_init_script(script=self._session_script)
        page_scripts = self.init_scripts + [self._script_source(s) for s in init_scripts or []]
        for src in page_scripts:
            ctx.add_init_script(script=src)
        trace_refused = bool(trace and self.storage_state and not self.trace_with_auth)
        if trace_refused:
            trace = False   # a trace of a signed-in page holds its cookies and tokens
        if trace:
            ctx.tracing.start(screenshots=True, snapshots=True)
        page = ctx.new_page()
        log = {'console': [], 'page_errors': [], 'failed_requests': [], 'applied': {'viewport': viewport, 'timezone': timezone,
                                                                                    'locale': locale, 'clock': clock,
                                                                                    'service_workers': service_workers,
                                                                                    'bypass_csp': self.bypass_csp,
                                                                                    'init_scripts': len(page_scripts)},
               'not_applied': [], 'trace': trace}
        if mobile and self.browser_name != 'chromium':
            log['not_applied'].append('mobile: is_mobile/has_touch emulation is Chromium only')
        if trace_refused:
            log['not_applied'].append('trace: not recorded because the page runs in a signed-in session, and a '
                                      'Playwright trace would hold its cookies and tokens (set trace_with_auth to '
                                      'record it, and keep that trace out of shared evidence)')
        page.on('console', lambda m: log['console'].append({'type': m.type, 'text': m.text}))
        page.on('pageerror', lambda e: log['page_errors'].append({'name': getattr(e, 'name', 'Error'), 'message': str(e)}))
        page.on('requestfailed', lambda r: log['failed_requests'].append({'method': r.method, 'url': r.url.split('?')[0],
                                                                          'failure': r.failure}))
        if clock:
            page.clock.install(time=clock)
        if cpu_throttle or network:
            if self.browser_name != 'chromium':
                log['not_applied'].append('cpu_throttle/network: CDP throttling is Chromium only')
            else:
                cdp = ctx.new_cdp_session(page)
                self._cdp[id(page)] = cdp
                if cpu_throttle:
                    cdp.send('Emulation.setCPUThrottlingRate', {'rate': float(cpu_throttle)})
                    log['applied']['cpu_throttle'] = cpu_throttle
                if network:
                    # a preset name, or {"download_kbps", "upload_kbps", "rtt_ms"}
                    preset = NETWORK[network] if isinstance(network, str) else {
                        'download': network['download_kbps'] * 1000 / 8, 'upload': network['upload_kbps'] * 1000 / 8,
                        'latency': network['rtt_ms']}
                    cdp.send('Network.enable')
                    cdp.send('Network.emulateNetworkConditions', {
                        'offline': preset is None, 'latency': (preset or {}).get('latency', 0),
                        'downloadThroughput': (preset or {}).get('download', -1), 'uploadThroughput': (preset or {}).get('upload', -1)})
                    log['applied']['network'] = network
        self._logs[id(page)] = log
        self._contexts[id(page)] = ctx
        return page

    def cdp(self, page):
        """The page's Chrome DevTools Protocol session (Chromium only; created on first use)."""
        if self.browser_name != 'chromium':
            raise RuntimeError('CDP is Chromium only')
        if id(page) not in self._cdp:
            self._cdp[id(page)] = self._contexts[id(page)].new_cdp_session(page)
        return self._cdp[id(page)]

    def start_trace(self, page, screenshots: bool = True, categories: list[str] | None = None):
        """Start a Chromium performance trace (the DevTools timeline) for this page."""
        self._browser.start_tracing(page=page, screenshots=screenshots, categories=categories)

    def stop_trace(self) -> bytes:
        return self._browser.stop_tracing()

    def log(self, page) -> dict:
        return self._logs[id(page)]

    def close_page(self, page, trace_path: Path | None = None) -> None:
        ctx = self._contexts.pop(id(page))
        self._cdp.pop(id(page), None)
        if self._logs[id(page)]['trace']:
            if trace_path:
                ctx.tracing.stop(path=str(trace_path))
            else:
                ctx.tracing.stop()
        ctx.close()

    def throttle(self):
        wait = self.rate - (time.time() - self.last_load)
        if wait > 0:
            time.sleep(wait)
        self.last_load = time.time()

    def goto(self, page, url: str, wait_until: str = 'load') -> dict:
        if self.stopped:
            return {'ok': False, 'status': None, 'error': f'not sent: the run stopped ({self.stopped["reason"]})',
                    'stopped': True}
        status = None
        host = (urlparse(url).hostname or '').lower()
        if host and not is_loopback(url):
            self.targets.add(host)  # a page the run navigates to is a target, not a third party
        if host:
            self.visited.add(host)
        attempt = 0
        while True:
            self.throttle()
            try:
                resp = page.goto(url, wait_until=wait_until, timeout=45000)
            except Exception as e:
                return {'ok': False, 'status': None, 'error': f'{type(e).__name__}: {str(e).splitlines()[0]}'}
            status = resp.status if resp else None
            action, detail = retry_decision(status, resp.headers.get('retry-after') if resp else None, attempt,
                                            self.max_retries, self.max_retry_after, self.stop_on_429)
            if action == 'retry':
                self.events.append({'url': url, 'event': f'HTTP {status}; backing off {detail:.0f}s', 'at': now()})
                time.sleep(detail)
                attempt += 1
                continue
            if action == 'stop':
                self.events.append({'url': url, 'event': detail, 'at': now()})
                if status == 429 and self.stop_on_429:
                    self.stopped = {'url': url, 'status': status, 'reason': detail, 'at': now()}
                return {'ok': False, 'status': status, 'error': detail, 'stopped': bool(self.stopped)}
            self.settle(page)
            return {'ok': status is None or status < 400, 'status': status, 'final_url': page.url}

    def settle(self, page, idle_ms: int = 300, max_ms: int = 5000):
        try:
            page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            self.events.append({'url': page.url, 'event': 'network did not go idle within 8s', 'at': now()})
        page.evaluate("""([idle, max]) => new Promise(res => { let t = setTimeout(res, idle); const stop = setTimeout(res, max);
            new MutationObserver(() => { clearTimeout(t); t = setTimeout(() => { clearTimeout(stop); res(); }, idle); })
              .observe(document, {subtree: true, childList: true, attributes: true}); })""", [idle_ms, max_ms])

    def act(self, page, actions: list[dict]):
        for a in actions:
            if 'goto' in a:
                self.goto(page, a['goto'])
            elif 'click' in a:
                page.click(a['click'], timeout=5000)
            elif 'dblclick' in a:
                page.dblclick(a['dblclick'], timeout=5000)
            elif 'hover' in a:
                page.hover(a['hover'], timeout=5000)
            elif 'press' in a:
                page.keyboard.press(a['press'])
            elif 'fill' in a:
                value = os.environ.get(a['env'], '') if 'env' in a else a.get('value', '')
                page.fill(a['fill'], value, timeout=5000)
            elif 'select' in a:
                page.select_option(a['select'], a.get('value'), timeout=5000)
            elif 'check' in a:
                page.check(a['check'], timeout=5000)
            elif 'uncheck' in a:
                page.uncheck(a['uncheck'], timeout=5000)
            elif 'wait' in a:
                page.wait_for_timeout(int(a['wait']))
            elif 'wait_for' in a:
                page.wait_for_selector(a['wait_for'], timeout=10000)
            else:
                raise ValueError(f'unknown action {a}')

    def screenshot(self, page, path: Path, full_page: bool = True) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=full_page)
        return path

    def login(self):
        auth = self.cfg.get('auth') or {}
        if auth.get('storage_state'):
            self.storage_state, self._session_script, self.auth_info = load_session(auth['storage_state'])
            return 'storage_state'
        if auth.get('login'):
            rec = auth['login']
            page = self.new_page()
            self.goto(page, rec['url'])
            allow = self.allow_post
            self.allow_post = True          # the login form itself must post; only during this recipe
            try:
                self.act(page, rec.get('steps', []))
                if rec.get('wait_for'):
                    page.wait_for_selector(rec['wait_for'], timeout=15000)
            finally:
                self.allow_post = allow
            ctx = self._contexts[id(page)]
            self.storage_state = ctx.storage_state()   # kept in memory only, never written
            self.auth_info = {'method': 'login_recipe', 'cookies': len(self.storage_state.get('cookies', [])),
                              'origins': len(self.storage_state.get('origins', []))}
            self.close_page(page)
            return 'login_recipe'
        return None


# ---------------------------------------------------------------- signed-in sessions (storage_state)
def load_session(path) -> tuple[dict, str | None, dict]:
    """Read a session file: Playwright's storage_state JSON ({cookies, origins: [{origin, localStorage}]}), which
    capture-session extends with a sessionStorage list per origin (Playwright's own storage_state has none, and
    MSAL and other OIDC libraries often keep their token cache there). Returns (the storage_state Playwright gets,
    an init script that puts the sessionStorage back on its own origin, a summary with counts only). The values
    stay in memory: neither the file's contents nor its location go into metadata or output."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    cookies = list(data.get('cookies') or [])
    origins, session = [], {}
    for o in data.get('origins') or []:
        origins.append({k: v for k, v in o.items() if k != 'sessionStorage'})
        items = o.get('sessionStorage') or []
        if items:
            session[o['origin']] = {i['name']: i['value'] for i in items}
    nowts = time.time()
    expired = sum(1 for c in cookies if isinstance(c.get('expires'), (int, float)) and 0 < c['expires'] < nowts)
    script = None
    if session:
        script = ('(() => { const all = ' + json.dumps(session) + '; const mine = all[location.origin];'
                  ' if (!mine) return; try { for (const [k, v] of Object.entries(mine))'
                  ' if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v); } catch (e) {} })();')
    info = {'method': 'storage_state', 'cookies': len(cookies), 'expired_cookies': expired, 'origins': len(origins),
            'local_storage_keys': sum(len(o.get('localStorage') or []) for o in origins),
            'session_storage_origins': len(session), 'session_storage_keys': sum(len(v) for v in session.values()),
            'indexed_db': any(o.get('indexedDB') for o in origins), 'captured_at': data.get('captured_at')}
    return {'cookies': cookies, 'origins': origins}, script, info


def capture_session(url: str, out: Path, browser: str = 'chromium', wait_for: str | None = None,
                    wait_url: str | None = None, timeout_s: int = 600, authorised_by: str | None = None) -> dict:
    """Open a visible browser at url for a person to sign in by hand (MSAL, OIDC, SSO, MFA), then save the session
    (cookies, localStorage, sessionStorage, IndexedDB where Playwright supports it) to out. Nothing is clicked or
    typed by this function; the person signs in. Returns the counts only."""
    import re
    check_authorised([url], authorised_by)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = getattr(pw, browser).launch(headless=False)
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto(url)
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout_s * 1000)
        elif wait_url:
            page.wait_for_url(re.compile(wait_url), timeout=timeout_s * 1000)
        else:
            input('Sign in in the browser window, wait until the app shows a signed-in page, then press Enter here... ')
        try:
            state = ctx.storage_state(indexed_db=True)   # Playwright 1.51+
        except TypeError:
            state = ctx.storage_state()
        by_origin = {o['origin']: o for o in state.get('origins', [])}
        for p in ctx.pages:
            try:
                origin = p.evaluate('location.origin')
                items = p.evaluate('Object.entries(sessionStorage).map(([name, value]) => ({name, value}))')
            except Exception:   # a closed page, or one that can't be read
                continue
            if items and origin and origin != 'null':
                by_origin.setdefault(origin, {'origin': origin, 'localStorage': []})['sessionStorage'] = items
        state['origins'] = list(by_origin.values())
        state['captured_at'] = now()
        b.close()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state), encoding='utf-8')
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return load_session(out)[2]


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Browser harness tools. capture-session: sign in by hand once and save '
                                             'the session for read-only runs (auth: {"storage_state": file}).')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture-session', help='open a visible browser, let a person sign in, save the session')
    c.add_argument('--url', required=True)
    c.add_argument('--out', type=Path, required=True, help='session file to write; keep it outside the repository '
                                                           'and the evidence, and delete it after the run')
    c.add_argument('--browser', default='chromium', choices=['chromium', 'firefox', 'webkit'])
    c.add_argument('--wait-for', help='CSS selector that appears once signed in (instead of pressing Enter)')
    c.add_argument('--wait-url', help='regular expression the URL matches once signed in (instead of pressing Enter)')
    c.add_argument('--timeout', type=int, default=600, help='seconds to wait for --wait-for/--wait-url (default 600)')
    c.add_argument('--authorised-by', help='who confirmed testing this target is allowed (needed unless loopback)')
    args = ap.parse_args(argv)
    try:
        info = capture_session(args.url, args.out, args.browser, args.wait_for, args.wait_url, args.timeout,
                               args.authorised_by)
    except NotAuthorised as e:
        print(f'refused: {e}')
        return 2
    print(f"session saved: {info['cookies']} cookie(s), {info['local_storage_keys']} localStorage and "
          f"{info['session_storage_keys']} sessionStorage key(s) over {info['origins']} origin(s). The file holds live "
          'credentials: keep it out of the repository and the evidence, and delete it when the run is done.')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
