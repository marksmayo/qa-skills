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
    allow_form_submission false (default): every non-GET/HEAD/OPTIONS request is aborted and logged
    third_party           block (default): every request to a host that isn't loopback (including *.test and
                          host_map hosts), a host the run navigated to with goto(), a host in targets, or a
                          host in allowed_hosts is aborted and logged in h.blocked with reason third_party,
                          GETs included, so a run never sends analytics hits or ad requests from a test page.
                          The page still attempts the request, so it shows in request logs. allow lets them out.
    targets               URLs whose hosts are part of the site under test (goto() adds each host it visits)
    allowed_hosts         further hosts to let through: ["cdn.example.com", "*.example-cdn.net"]
    service_workers       allow (default) or block, for every page unless new_page() says otherwise
    rate_limit_seconds    0.5 between page loads; 429/503 back off (Retry-After) up to twice
    auth                  {"storage_state": file} or {"login": {"url", "steps": [...], "wait_for"}};
                          login steps use {"fill": sel, "env": NAME}, so credentials never appear in output;
                          an identity provider the login redirects to must be listed in allowed_hosts
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
"""
from __future__ import annotations
import ipaddress, os, struct, time, zlib
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
        self.allow_post = bool(cfg.get('allow_form_submission', False))
        self.third_party = cfg.get('third_party', 'block')
        if self.third_party not in ('block', 'allow'):
            raise ValueError("third_party must be 'block' or 'allow'")
        self.allowed_hosts = [h.lower() for h in cfg.get('allowed_hosts', [])] + [h.lower() for h in (cfg.get('host_map') or {})]
        self.targets: set[str] = {(urlparse(u).hostname or '').lower() for u in cfg.get('targets', [])}
        self.service_workers = cfg.get('service_workers', 'allow')
        self.rate = float(cfg.get('rate_limit_seconds', 0.5))
        self.blocked: list[dict] = []
        self.events: list[dict] = []
        self.last_load = 0.0
        self.storage_state = None
        self._pw = self._browser = None
        self._logs: dict[int, dict] = {}
        self._contexts: dict[int, object] = {}
        self._cdp: dict[int, object] = {}

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.browser_name)
        args = []
        if self.cfg.get('host_map'):
            if self.browser_name != 'chromium':
                raise RuntimeError('host_map is Chromium only')
            args.append('--host-resolver-rules=' + ', '.join(f'MAP {h} {ip}' for h, ip in self.cfg['host_map'].items()))
        self._browser = launcher.launch(headless=self.cfg.get('headless', True), args=args)
        self.version = self._browser.version
        self.login()
        return self

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

    def guard(self, route, request):
        if self.third_party == 'block' and not self.host_allowed(request.url):
            self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': 'third_party', 'at': now()})
            return route.abort('blockedbyclient')
        if request.method in SAFE_METHODS or self.allow_post:
            return route.continue_()
        self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': 'write', 'at': now()})
        return route.abort()

    def new_page(self, viewport: str = '1280x800', timezone: str | None = None, locale: str | None = None,
                 clock: str | None = None, cpu_throttle: float | None = None, network: str | None = None,
                 reduced_motion: str = 'no-preference', trace: bool = False, user_agent: str | None = None,
                 service_workers: str | None = None, mobile: bool = False):
        service_workers = service_workers or self.service_workers
        w, h = parse_vp(viewport)
        kw = {'viewport': {'width': w, 'height': h}, 'reduced_motion': reduced_motion, 'storage_state': self.storage_state,
              'service_workers': service_workers}
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
        if trace:
            ctx.tracing.start(screenshots=True, snapshots=True)
        page = ctx.new_page()
        log = {'console': [], 'page_errors': [], 'failed_requests': [], 'applied': {'viewport': viewport, 'timezone': timezone,
                                                                                    'locale': locale, 'clock': clock,
                                                                                    'service_workers': service_workers},
               'not_applied': [], 'trace': trace}
        if mobile and self.browser_name != 'chromium':
            log['not_applied'].append('mobile: is_mobile/has_touch emulation is Chromium only')
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
        status = None
        host = (urlparse(url).hostname or '').lower()
        if host and not is_loopback(url):
            self.targets.add(host)  # a page the run navigates to is a target, not a third party
        for attempt in range(3):
            self.throttle()
            try:
                resp = page.goto(url, wait_until=wait_until, timeout=45000)
            except Exception as e:
                return {'ok': False, 'status': None, 'error': f'{type(e).__name__}: {str(e).splitlines()[0]}'}
            status = resp.status if resp else None
            if status in (429, 503) and attempt < 2:
                ra = resp.headers.get('retry-after', '')
                delay = min(int(ra) if ra.isdigit() else 10 * (attempt + 1), 60)
                self.events.append({'url': url, 'event': f'HTTP {status}; backing off {delay}s', 'at': now()})
                time.sleep(delay)
                continue
            self.settle(page)
            return {'ok': status is None or status < 400, 'status': status, 'final_url': page.url}
        return {'ok': False, 'status': status, 'error': 'rate limited after retries'}

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
            self.storage_state = auth['storage_state']
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
            self.close_page(page)
            return 'login_recipe'
        return None
