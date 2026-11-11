#!/usr/bin/env python3
"""Run the accessibility engines and browser probes against live pages (Playwright for Python).

    python run_audit.py --config audit_config.json --out <run-dir>
    python run_audit.py --config audit_config.json --plan-only --out <run-dir>   # explore and sample only

Needs `pip install -r requirements.txt` and `playwright install chromium`, plus the engines from
`fetch_engines.py`. Everything after this step (normalize, merge, validate, render) is stdlib.

Config (JSON):
    targets            list of URLs (required) or "sitemap": URL
    crawl              {"max_pages": 25, "same_origin": true}; omit to audit only the targets
    wcag_version       "2.2" (default), "2.1" or "2.0";  level "AA" (default), "A" or "AAA"
    viewports          ["1280x800", "320x640"] (default)
    engines            ["axe-core", "ibm-equal-access", "htmlcs"] (+ "vnu" if Java and the jar are available)
    probes             default: all of keyboard, focus_visible, reflow, text_spacing, resize_text,
                       target_size, hover_focus, motion, a11y_tree, language, media
    authorised_by      who confirmed the user may test the target (required unless every target is loopback)
    allow_form_submission  false (default): every non-GET request is aborted and logged
    third_party        "block" (default): every request to a host that is not loopback, a target's host or in
                       allowed_hosts is aborted and logged (GETs included), so an audit never sends analytics
                       hits or ad requests; third-party content is then absent and the report says so.
                       "allow" lets them through.
    allowed_hosts      further hosts to let through, e.g. a CDN the site needs: ["cdn.example.com", "*.cdn.net"]
    service_workers    "block" (default) or "allow"
    rate_limit_seconds 1.0 between page loads; 429/503 responses back off (Retry-After) up to twice
    consent            {"dismiss": "<selector>"}: engines run with the banner showing, then it is dismissed
    auth               {"storage_state": "<file>"} or {"login": {"url", "steps": [...], "wait_for"}};
                       login steps use {"fill": sel, "env": NAME} so credentials never appear in output
    states             [{"url", "name", "viewport"?, "actions": [...], "probes"?: [...]}]
                       actions: {"click": sel} {"hover": sel} {"press": key} {"fill": sel, "value"|"env"}
                                {"wait": ms} {"wait_for": sel}
    critical_paths     [{"name", "steps": [{"url", "state"?, "label"?}]}]
    random_sample      fraction of non-structured pages to add (default 0.1); "seed" for reproducibility

Writes <out>/run.json (metadata, sample, per-page checks), <out>/raw/*.json (engine and probe
results per page, viewport and state), <out>/screenshots/*.png and <out>/aria/*.txt.
"""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, os, random, re, shutil, struct, subprocess, sys, time, zlib
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_engines  # noqa: E402

ALL_PROBES = ['keyboard', 'focus_visible', 'reflow', 'text_spacing', 'resize_text', 'target_size',
              'hover_focus', 'motion', 'a11y_tree', 'language', 'media']
DESKTOP_ONLY = {'resize_text', 'hover_focus', 'motion', 'a11y_tree', 'language', 'media'}
NARROW_ONLY = {'reflow'}
ENGINE_NAMES = ['axe-core', 'ibm-equal-access', 'htmlcs', 'vnu']
AXE_TAGS = {'2.0': ['wcag2a', 'wcag2aa', 'wcag2aaa'], '2.1': ['wcag2a', 'wcag2aa', 'wcag2aaa', 'wcag21a', 'wcag21aa'],
            '2.2': ['wcag2a', 'wcag2aa', 'wcag2aaa', 'wcag21a', 'wcag21aa', 'wcag22aa']}
IBM_RULESET = {'2.0': 'WCAG_2_0', '2.1': 'WCAG_2_1', '2.2': 'WCAG_2_2'}
HTMLCS_STD = {'A': 'WCAG2A', 'AA': 'WCAG2AA', 'AAA': 'WCAG2AAA'}


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def is_loopback(url: str) -> bool:
    host = urlparse(url).hostname or ''
    if host in ('localhost',) or host.endswith('.localhost') or host.endswith('.test'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_vp(s: str) -> tuple[int, int]:
    w, h = s.lower().split('x')
    return int(w), int(h)


# ---------------------------------------------------------------- PNG diff (stdlib)

def png_pixels(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG into rows of raw pixel bytes."""
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, idat, w = 8, b'', 0
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
        f = raw[i]; line = bytearray(raw[i + 1:i + 1 + stride]); i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line)); prev = line
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


# ---------------------------------------------------------------- page helpers

class Runner:
    def __init__(self, cfg: dict, out: Path, cache: Path | None):
        self.cfg = cfg
        self.out = out
        self.cache = cache
        self.version = cfg.get('wcag_version', '2.2')
        self.level = cfg.get('level', 'AA')
        self.viewports = cfg.get('viewports') or ['1280x800', '320x640']
        self.probes = cfg.get('probes') or ALL_PROBES
        self.rate = float(cfg.get('rate_limit_seconds', 1.0))
        self.allow_post = bool(cfg.get('allow_form_submission', False))
        self.third_party = cfg.get('third_party', 'block')
        targets = cfg.get('targets') if isinstance(cfg.get('targets'), list) else []
        self.site_hosts = {(urlparse(u).hostname or '').lower() for u in targets + [cfg.get('sitemap') or '']} - {''}
        self.site_hosts |= {(urlparse(s.get('url', '')).hostname or '').lower() for s in cfg.get('states') or []} - {''}
        self.site_hosts.add((urlparse(((cfg.get('auth') or {}).get('login') or {}).get('url', '')).hostname or '').lower())
        self.allowed_hosts = [h.lower() for h in cfg.get('allowed_hosts', [])]
        self.blocked: list[dict] = []
        self.events: list[dict] = []
        self.engine_status: list[dict] = []
        self.engine_files: dict[str, Path] = {}
        self.last_load = 0.0
        self.probes_js = (HERE / 'probes.js').read_text(encoding='utf-8')
        self.storage_state = None

    # engines -------------------------------------------------------------
    def prepare_engines(self):
        lock = {e['name']: e for e in fetch_engines.load_lock()['engines']}
        wanted = self.cfg.get('engines') or ['axe-core', 'ibm-equal-access', 'htmlcs']
        for name in ENGINE_NAMES:
            eng = lock[name]
            st = {'name': name, 'version': eng['version'], 'license': eng['license'], 'status': 'not_run', 'reason': '', 'sha256': None}
            if name not in wanted:
                st['reason'] = 'not requested' if eng['required'] else 'optional; not requested'
                self.engine_status.append(st); continue
            try:
                path = fetch_engines.engine_path(name, self.cache)
            except (FileNotFoundError, StopIteration) as e:
                st['reason'] = str(e)
                self.engine_status.append(st); continue
            if name == 'vnu' and not shutil.which('java'):
                st['reason'] = 'Java not found on PATH'
                self.engine_status.append(st); continue
            st['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            st['status'] = 'ready'
            self.engine_files[name] = path
            self.engine_status.append(st)

    # navigation ----------------------------------------------------------
    def throttle(self):
        wait = self.rate - (time.time() - self.last_load)
        if wait > 0:
            time.sleep(wait)
        self.last_load = time.time()

    def host_allowed(self, url: str) -> bool:
        if url.startswith(('data:', 'blob:', 'about:')) or is_loopback(url):
            return True
        host = (urlparse(url).hostname or '').lower()
        return host in self.site_hosts or any(host == p or (p.startswith('*.') and host.endswith(p[1:])) for p in self.allowed_hosts)

    def guard(self, route, request):
        if self.third_party == 'block' and not self.host_allowed(request.url):
            self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': 'third_party', 'at': now()})
            return route.abort('blockedbyclient')
        if request.method in ('GET', 'HEAD', 'OPTIONS') or self.allow_post:
            return route.continue_()
        self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': 'write', 'at': now()})
        return route.abort()

    def new_context(self, browser, vp: str, reduced_motion: str = 'no-preference', scale: float = 1):
        w, h = parse_vp(vp)
        ctx = browser.new_context(viewport={'width': w, 'height': h}, device_scale_factor=scale,
                                  reduced_motion=reduced_motion, storage_state=self.storage_state,
                                  user_agent=None, ignore_https_errors=False,
                                  service_workers=self.cfg.get('service_workers', 'block'))
        ctx.route('**/*', self.guard)
        return ctx

    def goto(self, page, url: str) -> dict:
        for attempt in range(3):
            self.throttle()
            try:
                resp = page.goto(url, wait_until='load', timeout=45000)
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

    def settle(self, page):
        try:
            page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            self.events.append({'url': page.url, 'event': 'network did not go idle within 8s', 'at': now()})
        page.evaluate("""() => new Promise(res => { let t = setTimeout(res, 500); const stop = setTimeout(res, 5000);
            new MutationObserver(() => { clearTimeout(t); t = setTimeout(() => { clearTimeout(stop); res(); }, 500); })
              .observe(document, {subtree: true, childList: true, attributes: true}); })""")
        self.inject_helpers(page)

    def inject_helpers(self, page):
        if not page.evaluate('() => !!window.__qa'):
            page.add_script_tag(content=self.probes_js)

    def act(self, page, actions: list[dict]):
        for a in actions:
            if 'click' in a:
                page.click(a['click'], timeout=5000)
            elif 'hover' in a:
                page.hover(a['hover'], timeout=5000)
            elif 'press' in a:
                page.keyboard.press(a['press'])
            elif 'fill' in a:
                value = os.environ.get(a['env'], '') if 'env' in a else a.get('value', '')
                page.fill(a['fill'], value, timeout=5000)
            elif 'wait' in a:
                page.wait_for_timeout(int(a['wait']))
            elif 'wait_for' in a:
                page.wait_for_selector(a['wait_for'], timeout=10000)
        page.wait_for_timeout(300)
        self.inject_helpers(page)

    def login(self, browser):
        auth = self.cfg.get('auth') or {}
        if auth.get('storage_state'):
            self.storage_state = auth['storage_state']
            return 'storage_state'
        if auth.get('login'):
            rec = auth['login']
            ctx = self.new_context(browser, self.viewports[0])
            page = ctx.new_page()
            self.goto(page, rec['url'])
            allow = self.allow_post
            self.allow_post = True  # the login form itself must post; only during this recipe
            try:
                self.act(page, rec.get('steps', []))
                if rec.get('wait_for'):
                    page.wait_for_selector(rec['wait_for'], timeout=15000)
            finally:
                self.allow_post = allow
            self.storage_state = ctx.storage_state()  # kept in memory only, never written
            ctx.close()
            return 'login_recipe'
        return None

    # engines in page -----------------------------------------------------
    def run_engines(self, page) -> dict:
        res = {}
        for name in ('axe-core', 'ibm-equal-access', 'htmlcs'):
            if name not in self.engine_files:
                continue
            t0 = time.time()
            try:
                page.add_script_tag(path=str(self.engine_files[name]))
                res[name] = getattr(self, 'eng_' + name.replace('-', '_'))(page)
                res[name]['status'] = 'ran'
            except Exception as e:
                res[name] = {'status': 'errored', 'reason': f'{type(e).__name__}: {str(e).splitlines()[0][:300]}'}
            res[name]['seconds'] = round(time.time() - t0, 2)
        if 'vnu' in self.engine_files:
            res['vnu'] = self.eng_vnu(page)
        return res

    def eng_axe_core(self, page) -> dict:
        tags = AXE_TAGS[self.version] + ['best-practice']
        return page.evaluate("""async (tags) => {
            const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations', 'incomplete']});
            const node = n => ({target: n.target, impact: n.impact, summary: n.failureSummary || null,
                                html: n.html.slice(0, 300), el: window.__qa.describeTarget(n.target)});
            const pack = list => list.map(v => ({id: v.id, impact: v.impact, tags: v.tags, help: v.help, helpUrl: v.helpUrl,
                                                 nodes: v.nodes.slice(0, 50).map(node), node_count: v.nodes.length}));
            return {version: r.testEngine.version, violations: pack(r.violations), incomplete: pack(r.incomplete),
                    passes: r.passes.map(p => ({id: p.id, nodes: p.nodes.length})), inapplicable: r.inapplicable.map(p => p.id)};
        }""", tags)

    def eng_ibm_equal_access(self, page) -> dict:
        return page.evaluate("""async (ruleset) => {
            const c = new ace.Checker();
            const r = await c.check(document, [ruleset]);
            const keep = r.results.filter(x => x.value[1] !== 'PASS');
            const passes = {};
            for (const x of r.results) if (x.value[1] === 'PASS') passes[x.ruleId] = (passes[x.ruleId] || 0) + 1;
            return {version: ace.Config ? (ace.Config.version || null) : null, ruleset,
                    results: keep.slice(0, 1500).map(x => ({ruleId: x.ruleId, value: x.value, reasonId: x.reasonId,
                        message: x.message, snippet: (x.snippet || '').slice(0, 300), xpath: x.path && x.path.dom,
                        el: window.__qa.describeXpath(x.path && x.path.dom)})),
                    truncated: keep.length > 1500, passes};
        }""", IBM_RULESET[self.version])

    def eng_htmlcs(self, page) -> dict:
        return page.evaluate("""(std) => new Promise(res => HTMLCS.process(std, document, () => {
            const msgs = HTMLCS.getMessages();
            const notices = {};
            const keep = [];
            for (const m of msgs) {
                if (m.type === 3) { notices[m.code] = (notices[m.code] || 0) + 1; continue; }
                if (m.element && m.element.closest && m.element.closest('script,style,#__qa_text_spacing')) continue;
                keep.push({type: m.type, code: m.code, msg: m.msg, el: window.__qa.describe(m.element)});
            }
            res({standard: std, messages: keep.slice(0, 1500), truncated: keep.length > 1500, notices});
        }))""", HTMLCS_STD[self.level])

    def eng_vnu(self, page) -> dict:
        html_file = self.out / 'raw' / f'_vnu_{int(time.time() * 1000)}.html'
        html_file.write_text(page.content(), encoding='utf-8')
        try:
            p = subprocess.run(['java', '-jar', str(self.engine_files['vnu']), '--format', 'json', '--stdout', str(html_file)],
                               capture_output=True, text=True, timeout=120)
            msgs = json.loads(p.stdout or '{"messages": []}').get('messages', [])
            return {'status': 'ran', 'messages': [{'type': m.get('type'), 'subType': m.get('subType'), 'message': m.get('message'),
                                                   'line': m.get('lastLine'), 'extract': (m.get('extract') or '')[:200]} for m in msgs[:500]]}
        except Exception as e:
            return {'status': 'errored', 'reason': f'{type(e).__name__}: {e}'}
        finally:
            html_file.unlink(missing_ok=True)

    # probes --------------------------------------------------------------
    def probe_keyboard(self, page, shots: Path, pid: str) -> dict:
        targets = page.evaluate('() => window.__qa.focusTargets()')
        focusable = targets['focusable']
        page.evaluate('() => { window.scrollTo(0, 0); document.activeElement && document.activeElement.blur && document.activeElement.blur(); }')
        page.mouse.click(1, 1)  # sequential focus starts from the top of the document
        page.mouse.move(1, 1)
        limit = min(400, 3 * len(focusable) + 20)
        stops, moves, trap = [], [], None
        seen: set[str] = set()
        last_new = 0
        measure = 'focus_visible' in self.probes
        pending = None  # previous stop waiting for its unfocused screenshot
        for i in range(limit):
            page.keyboard.press('Tab')
            page.wait_for_timeout(60)
            info = page.evaluate('() => window.__qa.activeInfo()')
            if info and stops and info['xpath'] == stops[-1]['xpath']:
                continue  # moving between a native control's internal parts (video controls)
            if measure and pending:
                self.finish_indicator(page, pending)
                pending = None
            if info is None:
                if stops:
                    break  # left the document: one full pass
                continue
            if measure:
                info['indicator'] = self.start_indicator(page, info)
                pending = info
            stops.append(info)
            moves.append(info['xpath'])
            if len(stops) > 1 and info['xpath'] == stops[0]['xpath']:
                break  # wrapped round to the first stop
            if info['xpath'] not in seen:
                seen.add(info['xpath'])
                last_new = len(moves)
            if len(moves) - last_new >= 15 and len(seen) < len(focusable):
                recent = moves[-15:]
                if measure and pending:
                    # still focused: no unfocused screenshot is possible, so this stop is not measured
                    pending['indicator'] = {'changed_px': None, 'perimeter': None, 'note': 'not measured: focus trapped here'}
                    pending = None
                escaped = self.try_escape(page, recent)
                container = page.evaluate('(x) => window.__qa.containerOf(x)', list(dict.fromkeys(recent)))
                trap = {'elements': list(dict.fromkeys(recent)), 'container': container, 'escaped_with': escaped,
                        'after_presses': len(moves)}
                if escaped is None:
                    shot = shots / f'{pid}-trap.png'
                    page.screenshot(path=str(shot))
                    trap['screenshot'] = shot.name
                elif escaped == 'Shift+Tab':
                    trap = None  # Shift+Tab leaves it: a cycle, not a trap
                break
        if measure and pending:
            self.finish_indicator(page, pending)
        for s in stops:
            s.pop('_shot', None)
        unreached = [f for f in focusable if f['xpath'] not in seen]
        pointer_only = targets['pointer_only'][:50]
        self.add_listeners(page, pointer_only)
        positive = [s for s in stops if s.get('tabindex') and s['tabindex'].lstrip('-').isdigit() and int(s['tabindex']) > 0]
        jumps = []
        for a, b in zip(stops, stops[1:]):
            if a['doc_y'] - b['doc_y'] > 600 and not b.get('in_dialog') and b['xpath'] != stops[0]['xpath']:
                jumps.append({'from': a['xpath'], 'to': b['xpath']})
        first = stops[0] if stops else None
        skip = bool(first and re.search(r'href="#', first.get('html') or '') and re.search(r'skip|main|content', first.get('html') or '', re.I))
        return {'presses': len(moves), 'stops': stops, 'focusable_count': len(focusable), 'unreached': unreached[:50],
                'pointer_only': pointer_only, 'trap': trap, 'positive_tabindex': positive, 'jumps': jumps[:20],
                'skip_link_first': skip}

    def add_listeners(self, page, elements):
        """Record click-type listeners on pointer-only elements (Chromium DevTools protocol)."""
        try:
            cdp = page.context.new_cdp_session(page)
        except Exception:
            for e in elements:
                e['listeners'] = None
            return
        for e in elements:
            try:
                obj = cdp.send('Runtime.evaluate', {'expression': f'window.__qa.byXpath({json.dumps(e["xpath"])})'})
                oid = obj['result'].get('objectId')
                ls = cdp.send('DOMDebugger.getEventListeners', {'objectId': oid})['listeners'] if oid else []
                e['listeners'] = sorted({l['type'] for l in ls if l['type'] in ('click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'touchstart', 'touchend')})
            except Exception:
                e['listeners'] = None
        cdp.detach()

    def try_escape(self, page, recent):
        for key in ('Shift+Tab', 'Escape'):
            for _ in range(8):
                page.keyboard.press(key)
                page.wait_for_timeout(60)
            info = page.evaluate('() => window.__qa.activeInfo()')
            if info is None or info['xpath'] not in recent:
                return key
        return None

    def _clip(self, page, r):
        vw, vh = page.viewport_size['width'], page.viewport_size['height']
        x, y = max(r['x'] - 4, 0), max(r['y'] - 4, 0)
        w, h = min(r['x'] + r['w'] + 4, vw) - x, min(r['y'] + r['h'] + 4, vh) - y
        return {'x': x, 'y': y, 'width': w, 'height': h} if w > 0 and h > 0 else None

    def start_indicator(self, page, info) -> dict:
        """Screenshot the focused element; finish_indicator() takes the same area once focus has moved on."""
        clip = self._clip(page, info['rect'])
        if not clip:
            return {'changed_px': None, 'perimeter': None, 'note': 'element outside the viewport'}
        try:
            info['_shot'] = (page.screenshot(clip=clip, animations='disabled', caret='hide'), clip, page.evaluate('() => scrollY'))
        except Exception as e:
            return {'changed_px': None, 'perimeter': None, 'note': type(e).__name__}
        return {'changed_px': None, 'perimeter': 2 * (info['rect']['w'] + info['rect']['h'])}

    def finish_indicator(self, page, info):
        shot = info.get('_shot')
        if not shot:
            return
        focused, clip, sy = shot
        try:
            now_y = page.evaluate('() => scrollY')
            if now_y != sy:
                page.evaluate('(y) => window.scrollTo(scrollX, y)', sy)  # put the element back where it was
            plain = page.screenshot(clip=clip, animations='disabled', caret='hide')
            if now_y != sy:
                page.evaluate('(y) => window.scrollTo(scrollX, y)', now_y)
            info['indicator']['changed_px'] = changed_pixels(focused, plain)
        except Exception as e:
            info['indicator']['note'] = type(e).__name__

    def probe_reflow(self, page) -> dict:
        return page.evaluate('() => window.__qa.reflow()')

    def probe_text_spacing(self, page) -> dict:
        before = {c['xpath']: c['overflow'] for c in page.evaluate('() => window.__qa.clipped()')}
        page.evaluate('() => window.__qa.textSpacing(true)')
        page.wait_for_timeout(200)
        after = page.evaluate('() => window.__qa.clipped()')
        page.evaluate('() => window.__qa.textSpacing(false)')
        # newly clipped, or clipped noticeably more than before the spacing overrides
        worse = [{**c, 'overflow_before': before.get(c['xpath'], 0)} for c in after if c['overflow'] > before.get(c['xpath'], 0) + 2]
        return {'clipped': worse, 'clipped_before': len(before)}

    def probe_resize_text(self, browser, url: str, shots: Path, pid: str) -> dict:
        w, h = parse_vp(self.viewports[0])
        ctx = self.new_context(browser, f'{w // 2}x{h // 2}', scale=2)  # same as 200% browser zoom
        page = ctx.new_page()
        nav = self.goto(page, url)
        if not nav['ok']:
            ctx.close()
            return {'status': 'errored', 'reason': nav.get('error') or f"HTTP {nav.get('status')}"}
        info = page.evaluate('() => ({clipped: window.__qa.clipped(), vm: (document.querySelector("meta[name=viewport]") || {}).content || null})')
        shot = shots / f'{pid}-zoom200.png'
        page.screenshot(path=str(shot))
        ctx.close()
        vm = info['vm'] or ''
        m = re.search(r'maximum-scale\s*=\s*([\d.]+)', vm)
        disabled = bool(re.search(r'user-scalable\s*=\s*(no|0)', vm)) or bool(m and float(m.group(1)) < 2)
        return {'emulation': f'{w // 2}x{h // 2} CSS px at device scale 2 (200% of {w}x{h})', 'clipped': info['clipped'],
                'viewport_meta': info['vm'], 'zoom_disabled': disabled, 'screenshot': shot.name}

    def probe_target_size(self, page) -> dict:
        return page.evaluate('() => window.__qa.targetSize()')

    def probe_hover_focus(self, page) -> dict:
        found = []
        cands = page.evaluate('() => window.__qa.hoverCandidates()')
        for c in cands:
            try:
                page.mouse.move(1, 1)
                page.wait_for_timeout(150)
                page.evaluate('() => window.__qa.visibleSet()')
                el = page.locator(f'xpath={c["xpath"]}') if '#shadow-root' not in (c['xpath'] or '') else None
                if el is None or el.count() == 0:
                    continue
                el.first.hover(timeout=2000)
                page.wait_for_timeout(400)
                popups = page.evaluate('() => window.__qa.newlyVisible()')
                if not popups:
                    continue
                pop = popups[0]
                page.keyboard.press('Escape')
                page.wait_for_timeout(200)
                dismissible = not page.evaluate('(x) => window.__qa.stillVisible(x)', pop['xpath'])
                el.first.hover(timeout=2000)
                page.wait_for_timeout(300)
                r = pop['rect']
                page.mouse.move(r['x'] + r['w'] / 2, r['y'] + r['h'] / 2, steps=4)
                page.wait_for_timeout(300)
                hoverable = page.evaluate('(x) => window.__qa.stillVisible(x)', pop['xpath'])
                el.first.hover(timeout=2000)
                page.wait_for_timeout(3000)
                persistent = page.evaluate('(x) => window.__qa.stillVisible(x)', pop['xpath'])
                found.append({'trigger': c, 'content': pop, 'dismissible': dismissible, 'hoverable': hoverable, 'persistent': persistent})
                if len(found) >= 10:
                    break
            except Exception as e:
                self.events.append({'url': page.url, 'event': f'hover probe skipped an element: {type(e).__name__}', 'at': now()})
        page.mouse.move(1, 1)
        return {'checked': len(cands), 'popups': found}

    def watch_motion(self, page) -> dict:
        # 2.2.2 concerns movement that lasts more than 5 seconds: compare 5.0 s and 7.5 s after load
        page.wait_for_timeout(5000)
        page.evaluate('() => window.__qa.motionSnapshot()')
        page.wait_for_timeout(2500)
        return page.evaluate('() => window.__qa.motionChanges()')

    def probe_motion(self, page, browser, url: str) -> dict:
        self.goto(page, url)
        normal = self.watch_motion(page)
        ctx = self.new_context(browser, self.viewports[0], reduced_motion='reduce')
        p2 = ctx.new_page()
        reduced = None
        if self.goto(p2, url)['ok']:
            reduced = self.watch_motion(p2)
        ctx.close()
        return {'window': 'changes between 5.0 s and 7.5 s after load', 'normal': normal, 'reduced_motion': reduced}

    def probe_a11y_tree(self, page, aria_dir: Path, pid: str) -> dict:
        try:
            snap = page.locator('body').aria_snapshot(timeout=15000)
        except Exception as e:
            return {'status': 'errored', 'reason': f'{type(e).__name__}'}
        f = aria_dir / f'{pid}.txt'
        f.write_text(snap[:300000], encoding='utf-8')
        info = page.evaluate('() => ({headings: window.__qa.pageInfo().headings, landmarks: window.__qa.pageInfo().landmarks})')
        return {'snapshot': f.name, 'bytes': len(snap), **info}

    def probe_language(self, page) -> dict:
        return page.evaluate('() => window.__qa.language()')

    def probe_media(self, page) -> dict:
        return page.evaluate('() => window.__qa.media()')

    # exploration ---------------------------------------------------------
    def explore(self, browser) -> list[dict]:
        targets = list(self.cfg.get('targets') or [])
        if self.cfg.get('sitemap'):
            try:
                xml = urllib.request.urlopen(self.cfg['sitemap'], timeout=30).read().decode('utf-8', 'replace')
                targets += re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', xml)
            except Exception as e:
                self.events.append({'url': self.cfg['sitemap'], 'event': f'sitemap unreadable: {e}', 'at': now()})
        crawl = self.cfg.get('crawl')
        max_pages = int((crawl or {}).get('max_pages', self.cfg.get('max_pages', 25)))
        cp_urls = [s['url'] for p in self.cfg.get('critical_paths', []) for s in p['steps']]
        queue = [urldefrag(u)[0] for u in targets + cp_urls]
        origin = {urlparse(u).netloc for u in targets}
        found, seen = [], set()
        ctx = self.new_context(browser, self.viewports[0])
        page = ctx.new_page()
        limit = max_pages * 4 if crawl else len(queue)
        while queue and len(found) < limit:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            nav = self.goto(page, url)
            if not nav['ok']:
                found.append({'url': url, 'ok': False, 'error': nav.get('error') or f"HTTP {nav.get('status')}"})
                continue
            info = page.evaluate('() => window.__qa.pageInfo()')
            found.append({'url': url, 'ok': True, 'final_url': nav.get('final_url'), 'title': info['title'],
                          'template_sig': info['template'], 'captcha': info['captcha']})
            if crawl:
                for link in info['links']:
                    link = urldefrag(link)[0]
                    if urlparse(link).netloc in origin and link not in seen and not re.search(r'\.(pdf|zip|png|jpe?g|gif|svg|mp4|docx?)$', link, re.I):
                        queue.append(link)
        ctx.close()
        return self.sample(found, set(cp_urls), max_pages)

    def sample(self, found: list[dict], cp_urls: set, max_pages: int) -> list[dict]:
        def pattern(u):
            path = urlparse(u).path or '/'
            path = re.sub(r'/\d+(?=/|$)', '/{n}', path)
            path = re.sub(r'/[0-9a-f]{8,}(?=/|$)', '/{id}', path)
            return path
        groups: dict[str, list[dict]] = {}
        for f in found:
            if not f['ok']:
                continue
            sig = f['template_sig']
            key = f"{pattern(f['url']).rsplit('/', 1)[0] or '/'}|{sig['landmarks']}|{sig['main_shape']}"
            f['template'] = 'T' + hashlib.sha1(key.encode()).hexdigest()[:6]
            f['template_key'] = key
            groups.setdefault(f['template'], []).append(f)
        sample, chosen = [], set()
        for f in found:
            if f['ok'] and f['url'] in cp_urls and f['url'] not in chosen:
                sample.append({**f, 'reason': 'critical_path'}); chosen.add(f['url'])
        for t, members in groups.items():
            if not any(m['url'] in chosen for m in members) and len(sample) < max_pages:
                sample.append({**members[0], 'reason': 'structured'}); chosen.add(members[0]['url'])
        rest = [f for f in found if f['ok'] and f['url'] not in chosen]
        rng = random.Random(self.cfg.get('seed', 1))
        k = min(len(rest), max(0, max_pages - len(sample)), max(1, round(len(rest) * float(self.cfg.get('random_sample', 0.1)))) if rest else 0)
        for f in rng.sample(rest, k):
            sample.append({**f, 'reason': 'random'})
        for f in found:
            if not f['ok']:
                sample.append({**f, 'reason': 'unreachable'})
        for i, s in enumerate(sample, 1):
            s['id'] = f'p{i:02d}'
        self.unsampled = [f['url'] for f in rest if f['url'] not in {s['url'] for s in sample}]
        return sample

    # main loop -----------------------------------------------------------
    def audit_page(self, browser, pg: dict) -> dict:
        url, pid = pg['url'], pg['id']
        raw, shots, aria = self.out / 'raw', self.out / 'screenshots', self.out / 'aria'
        checks = {'url': url, 'page': pid, 'viewports': {}}
        if pg.get('captcha'):
            checks['blocked'] = 'CAPTCHA detected; not bypassed'
            return checks
        states_cfg = [s for s in self.cfg.get('states', []) if urldefrag(s['url'])[0] == url]
        for vp in self.viewports:
            narrow = parse_vp(vp)[0] < 600
            vc = {'engines': {}, 'probes': {}, 'states': []}
            ctx = self.new_context(browser, vp)
            page = ctx.new_page()
            nav = self.goto(page, url)
            if not nav['ok']:
                vc['error'] = nav.get('error') or f"HTTP {nav.get('status')}"
                checks['viewports'][vp] = vc
                ctx.close(); continue
            tag = f'{pid}__{vp}'
            page.screenshot(path=str(shots / f'{tag}.png'), full_page=True)
            info = page.evaluate('() => window.__qa.pageInfo()')
            record = {'page': pid, 'url': url, 'final_url': page.url, 'viewport': vp, 'state': 'initial', 'page_info': {k: v for k, v in info.items() if k != 'links'},
                      'screenshot': f'{tag}.png', 'engines': self.run_engines(page), 'probes': {}}
            vc['engines'] = {k: v['status'] for k, v in record['engines'].items()}
            consent = (self.cfg.get('consent') or {}).get('dismiss')
            if consent:
                try:
                    page.click(consent, timeout=3000)
                    page.wait_for_timeout(300)
                    record['consent_dismissed'] = True
                except Exception:
                    record['consent_dismissed'] = False
            self.inject_helpers(page)
            for probe in self.probes:
                if (probe in DESKTOP_ONLY and narrow) or (probe in NARROW_ONLY and not narrow):
                    continue
                t0 = time.time()
                try:
                    if probe == 'keyboard':
                        result = self.probe_keyboard(page, shots, tag)
                    elif probe == 'focus_visible':
                        result = {'note': 'measured during the keyboard probe'} if 'keyboard' in self.probes else self.probe_keyboard(page, shots, tag)
                    elif probe == 'resize_text':
                        result = self.probe_resize_text(browser, url, shots, tag)
                    elif probe == 'motion':
                        result = self.probe_motion(page, browser, url)
                    elif probe == 'a11y_tree':
                        result = self.probe_a11y_tree(page, aria, tag)
                    else:
                        result = getattr(self, 'probe_' + probe)(page)
                    result.setdefault('status', 'ran')
                except Exception as e:
                    result = {'status': 'errored', 'reason': f'{type(e).__name__}: {str(e).splitlines()[0][:300]}'}
                result['seconds'] = round(time.time() - t0, 2)
                record['probes'][probe] = result
                vc['probes'][probe] = result['status']
            (raw / f'{tag}__initial.json').write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding='utf-8')
            ctx.close()
            for st in states_cfg:
                if st.get('viewport') and st['viewport'] != vp:
                    continue
                if not st.get('viewport') and vp != self.viewports[0]:
                    continue
                vc['states'].append(self.audit_state(browser, pg, vp, st))
            checks['viewports'][vp] = vc
        return checks

    def audit_state(self, browser, pg: dict, vp: str, st: dict) -> dict:
        tag = f"{pg['id']}__{vp}__{st['name']}"
        ctx = self.new_context(browser, vp)
        page = ctx.new_page()
        out = {'name': st['name'], 'engines': {}, 'probes': {}}
        nav = self.goto(page, pg['url'])
        if not nav['ok']:
            out['error'] = nav.get('error')
            ctx.close(); return out
        consent = (self.cfg.get('consent') or {}).get('dismiss')
        if consent:
            try:
                page.click(consent, timeout=3000)
            except Exception:
                pass
        try:
            self.act(page, st.get('actions', []))
        except Exception as e:
            out['error'] = f'state actions failed: {type(e).__name__}: {str(e).splitlines()[0][:200]}'
            ctx.close(); return out
        page.screenshot(path=str(self.out / 'screenshots' / f'{tag}.png'), full_page=True)
        record = {'page': pg['id'], 'url': pg['url'], 'final_url': page.url, 'viewport': vp, 'state': st['name'],
                  'screenshot': f'{tag}.png', 'engines': self.run_engines(page), 'probes': {}}
        out['engines'] = {k: v['status'] for k, v in record['engines'].items()}
        for probe in st.get('probes', []):
            try:
                if probe == 'keyboard':
                    result = self.probe_keyboard(page, self.out / 'screenshots', tag)
                else:
                    result = getattr(self, 'probe_' + probe)(page)
                result.setdefault('status', 'ran')
            except Exception as e:
                result = {'status': 'errored', 'reason': f'{type(e).__name__}: {str(e).splitlines()[0][:300]}'}
            record['probes'][probe] = result
            out['probes'][probe] = result['status']
        (self.out / 'raw' / f'{tag}.json').write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding='utf-8')
        ctx.close()
        return out


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--config', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--cache', help='engine cache (default as fetch_engines.py)')
    ap.add_argument('--plan-only', action='store_true', help='explore and sample, then stop')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
    targets = cfg.get('targets') or []
    if not targets and not cfg.get('sitemap'):
        ap.error('config needs targets or sitemap')
    if not all(is_loopback(u) for u in targets + ([cfg['sitemap']] if cfg.get('sitemap') else [])) and not cfg.get('authorised_by'):
        print('Refusing: a non-loopback target needs "authorised_by" in the config (who confirmed you may test it).', file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright
        import importlib.metadata as md
        pw_version = md.version('playwright')
    except Exception:
        print('Playwright for Python is not installed: pip install -r requirements.txt && playwright install chromium', file=sys.stderr)
        return 3
    out = Path(args.out)
    for d in ('raw', 'screenshots', 'aria'):
        (out / d).mkdir(parents=True, exist_ok=True)
    r = Runner(cfg, out, Path(args.cache) if args.cache else None)
    r.prepare_engines()
    started = now()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        auth_used = r.login(browser)
        sample = r.explore(browser)
        pages = []
        if not args.plan_only:
            for pg in sample:
                if pg['reason'] == 'unreachable':
                    pages.append({'url': pg['url'], 'page': pg['id'], 'error': pg.get('error')})
                    continue
                print(f"[{pg['id']}] {pg['url']}", file=sys.stderr)
                pages.append(r.audit_page(browser, pg))
        browser_version = browser.version
        browser.close()
    safe_cfg = {k: v for k, v in cfg.items() if k != 'auth'}
    safe_cfg['auth'] = {'kind': auth_used} if auth_used else None
    for e in r.engine_status:
        if e['status'] == 'ready':
            ran = [vp.get('engines', {}).get(e['name']) for pg in pages for vp in pg.get('viewports', {}).values()]
            e['status'] = 'ran' if 'ran' in ran else ('errored' if 'errored' in ran else 'not_run')
            if e['status'] != 'ran':
                e['reason'] = e['reason'] or 'no page completed'
    # one row per planned check (page x viewport x required engine or applicable probe), for evidence completeness
    lock = {e['name']: e for e in fetch_engines.load_lock()['engines']}
    required = [n for n in ENGINE_NAMES if lock[n]['required']]
    by_page = {p.get('page'): p for p in pages}
    checks = []
    for s in sample:
        pg = by_page.get(s['id']) or {}
        for vp in r.viewports:
            narrow = parse_vp(vp)[0] < 600
            vc = (pg.get('viewports') or {}).get(vp)
            blocked = 'blocked' if (pg.get('blocked') or pg.get('error') or s['reason'] == 'unreachable' or not vc or vc.get('error')) else None
            for name in required:
                checks.append({'page': s['id'], 'viewport': vp, 'kind': 'engine', 'name': name,
                               'status': blocked or vc['engines'].get(name, 'not_run')})
            for probe in r.probes:
                if (probe in DESKTOP_ONLY and narrow) or (probe in NARROW_ONLY and not narrow):
                    continue
                checks.append({'page': s['id'], 'viewport': vp, 'kind': 'probe', 'name': probe,
                               'status': blocked or vc['probes'].get(probe, 'not_run')})
    run = {'tool': 'run_audit.py', 'started_at': started, 'finished_at': now(), 'config': safe_cfg, 'checks': checks,
           'wcag_version': r.version, 'level': r.level, 'browser': {'name': 'chromium', 'version': browser_version},
           'playwright': pw_version, 'engines': r.engine_status, 'probes': r.probes, 'viewports': r.viewports,
           'sample': [{k: s.get(k) for k in ('id', 'url', 'final_url', 'title', 'template', 'template_key', 'reason', 'error', 'captcha')} for s in sample],
           'unsampled': getattr(r, 'unsampled', []), 'pages': pages, 'blocked_requests': r.blocked, 'events': r.events,
           'authorised_by': cfg.get('authorised_by') or ('loopback target' if all(is_loopback(u) for u in targets) else None),
           'auth_used': bool(auth_used)}
    (out / 'run.json').write_text(json.dumps(run, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'pages': len(sample), 'engines': {e['name']: e['status'] for e in r.engine_status},
                      'blocked_requests': len(r.blocked), 'out': str(out)}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
