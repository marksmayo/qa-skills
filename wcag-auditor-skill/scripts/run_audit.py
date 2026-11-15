#!/usr/bin/env python3
"""Run the accessibility engines and browser probes against live pages (Playwright for Python).

    python run_audit.py --config audit_config.json --out <run-dir>
    python run_audit.py --config audit_config.json --plan-only --out <run-dir>   # explore and sample only

Needs `pip install -r requirements.txt` and `playwright install chromium`, plus the engines from
`fetch_engines.py`. Everything after this step (normalize, merge, validate, render) is stdlib.

Config (JSON):
    targets            list of URLs (required) or "sitemap": URL. A target may be {"url": u, "expect_status": 404}:
                       an error template users reach (the not-found page) is then audited like any page; any
                       other 4xx/5xx answer makes a page unreachable. The sample records each page's status.
    crawl              {"max_pages": 25, "same_origin": true, "max_loads": 100}; omit to audit only the targets.
                       The crawl prints each page it loads, loads at most max_loads (default 4 x max_pages) and
                       stops sooner once max_pages templates are found or max_pages loads in a row add none.
                       Non-HTML responses (a linked .txt, XML or JSON file) are skipped, not sampled.
                       "per_pattern": 2 loads at most that many pages of one URL shape (/region/*) while
                       links of an unseen shape wait; run.json crawl.patterns counts loads per shape.
                       A path rule folds only the last segment of a nested path: give "routes" (the app's
                       route patterns, ["/", "/about", "/{slug}"]; [id] and :id also read) or "next_app" (a
                       Next.js project folder, read with next_app.py) for root-level dynamic routes; without
                       them root-level siblings fold into /* once more than "fold_root_after" (8) are seen.
    keyboard           {"max_presses": 2000}: hard cap on Tab presses per traversal (the budget grows while
                       presses keep reaching new elements); a traversal stopped by it is "partial".
    settle_seconds     10: most time spent settling each load (network quiet 500 ms, then a quiet DOM).
                       Event streams, websockets, long-poll and hot-reload requests, and any request open
                       longer than long_lived_seconds (4), don't hold it open; events names them.
    wcag_version       "2.2" (default), "2.1" or "2.0";  level "AA" (default), "A" or "AAA"
    viewports          ["1280x800", "320x640"] (default)
    mobile_viewports   viewports (from "viewports") audited as a phone: touch events, a coarse pointer, mobile
                       layout (Playwright is_mobile and has_touch), e.g. ["390x844"]; "mobile_user_agent" sets
                       its user agent. Without it every viewport is desktop Chromium, so touch-only controls
                       are not exercised. run.json records mobile_viewports.
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
    bypass_csp         true (default): pages run with their Content-Security-Policy bypassed (Playwright's
                       bypass_csp), so the engines, probes.js and the text-spacing style can be injected under a
                       hash- or nonce-based CSP. Each injection is checked; one that didn't take makes that engine
                       or probe errored, never "ran, no issues". false tests the CSP as served.
    consent            an overlay (cookie banner, sign-up modal) to dismiss: {"dismiss": "<selector>"} or
                       {"steps": [actions]}, plus "engines": "both" (default: engines run with the overlay showing
                       and again after dismissal, recorded as state consent_dismissed), "before" or "after", and
                       "probes_with_overlay": ["keyboard"] (default; run with the overlay open, recorded as state
                       consent_open). The other probes run after dismissal. run.json records which ran.
    auth               {"storage_state": "<file>"} or {"login": {"url", "steps": [...], "wait_for"}};
                       login steps use {"fill": sel, "env": NAME} so credentials never appear in output.
                       storage_state is a session a person captured by signing in by hand (SSO, MFA, MSAL):
                       `python browser_harness.py capture-session --url <app> --out <file outside the repo>`;
                       its sessionStorage is put back on its origin before the app's scripts run. run.json
                       records only that a session was used (auth.kind), never its contents or path
    states             [{"url", "name", "viewport"?, "actions": [...], "probes"?: [...], "keyboard_start"?}]
                       each state records focus_after_actions (where focus is once its actions are done) and
                       its final_url; "keyboard_start": "active_element" starts its keyboard probe from the
                       focused element ("top": always from the top of the document)
                       actions: {"click": sel} {"hover": sel} {"press": key} {"fill": sel, "value"|"env"}
                                {"wait": ms} {"wait_for": sel}
    critical_paths     [{"name", "steps": [{"url", "state"?, "label"?}]}]
    random_sample      fraction of the remaining pages to add at random (default 0.1; 0 adds none); "seed"
                       for reproducibility. Listed targets and critical-path steps are always audited;
                       template de-duplication only thins crawled and sitemap pages. Each sample entry's
                       reason says how it entered: critical_path, listed, structured, random or unreachable.

Probes: the keyboard probe starts sequential focus from an invisible point at the top of the body (no
click), so the first Tab reaches a skip link (in a named state with a modal open and focus inside it, it
starts from the focused element and expects only the modal's controls); target size is measured at the top of the page with
pointer targets only (never a tabindex-only container or a visually hidden link), and fixed or sticky
targets are not compared with in-flow ones; motion watches fresh loads in their own pages, so the
audited page is never reloaded under later probes.

Writes <out>/run.json (metadata, sample, per-page checks, mode, bypass_csp, consent, request counts),
<out>/raw/*.json (engine and probe results per page, viewport and state), <out>/screenshots/*.png and
<out>/aria/*.txt. With --plan-only no engine runs: they are recorded not_run with that reason.
"""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, os, random, re, shutil, struct, subprocess, sys, time, zlib
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse, urldefrag
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_engines  # noqa: E402
from browser_harness import load_session  # noqa: E402  (signed-in session files, shared with the other browser skills)

ALL_PROBES = ['keyboard', 'focus_visible', 'reflow', 'text_spacing', 'resize_text', 'target_size',
              'hover_focus', 'motion', 'a11y_tree', 'language', 'media']
DESKTOP_ONLY = {'resize_text', 'hover_focus', 'motion', 'a11y_tree', 'language', 'media'}
NARROW_ONLY = {'reflow'}
# The keyboard probe starts with a budget of 3 presses per visible focusable element (+20, at most 400) and gives
# itself 20 more presses each time a press reaches an element it has not seen (a menu that :focus-within reveals
# adds its links to the Tab order only once focus enters it), up to this hard cap ("keyboard": {"max_presses": N}).
KEYBOARD_MAX_PRESSES = 2000
# focused/unfocused crops kept per keyboard traversal for stops whose indicator changed fewer pixels than half the
# perimeter (no or a weak indicator), so the agent can look at them
FOCUS_SHOTS_MAX = 30
ENGINE_NAMES = ['axe-core', 'ibm-equal-access', 'htmlcs', 'vnu']
AXE_TAGS = {'2.0': ['wcag2a', 'wcag2aa', 'wcag2aaa'], '2.1': ['wcag2a', 'wcag2aa', 'wcag2aaa', 'wcag21a', 'wcag21aa'],
            '2.2': ['wcag2a', 'wcag2aa', 'wcag2aaa', 'wcag21a', 'wcag21aa', 'wcag22aa']}
IBM_RULESET = {'2.0': 'WCAG_2_0', '2.1': 'WCAG_2_1', '2.2': 'WCAG_2_2'}
HTMLCS_STD = {'A': 'WCAG2A', 'AA': 'WCAG2AA', 'AAA': 'WCAG2AAA'}
# connections a dev server or live page keeps open: they never finish, so waiting for them is waiting forever
LONG_LIVED_URL = re.compile(r'(?i)/socket\.io/|/sockjs(-node)?/|/_next/webpack-hmr|__webpack_hmr|/@vite/client|/livereload|'
                            r'/hot-update|/_hmr|/__hmr|[?&]transport=polling|/event-?stream|/sse(?:[/?]|$)|/long-?poll')
# landmarks that repeat with the content (code blocks, cards and panels as regions) rather than mark the template
CONTENT_LANDMARKS = {'region', 'section', 'dialog', 'form'}
HTML_TYPES = ('text/html', 'application/xhtml+xml')
NON_PAGE_LINK = re.compile(r'(?i)\.(pdf|zip|gz|png|jpe?g|gif|webp|avif|svg|ico|mp[34]|webm|docx?|xlsx?|pptx?|csv|txt|xml|json|rss|atom|md|js|css|woff2?)$')


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


def normalise_targets(targets) -> tuple[list[str], dict[str, int]]:
    """Target URLs, and the HTTP status each target listed as {"url", "expect_status"} is expected to answer with.

    A not-found page (or another error template users really reach) answers 404 or 410: listed with
    "expect_status": 404 it is audited like any page, with its status recorded; without it, a 4xx/5xx answer
    makes the page unreachable."""
    urls, expect = [], {}
    for t in targets or []:
        if isinstance(t, dict):
            if not t.get('url'):
                raise ValueError(f'a target object needs "url": {t}')
            urls.append(t['url'])
            if t.get('expect_status') is not None:
                expect[urldefrag(t['url'])[0]] = int(t['expect_status'])
        else:
            urls.append(t)
    return urls, expect


def parse_vp(s: str) -> tuple[int, int]:
    w, h = s.lower().split('x')
    return int(w), int(h)


def url_pattern(u: str) -> str:
    path = urlparse(u).path or '/'
    path = re.sub(r'/\d+(?=/|$)', '/{n}', path)
    path = re.sub(r'/[0-9a-f]{8,}(?=/|$)', '/{id}', path)
    return path


# The crawl loads at most this many pages of one URL shape (crawl_pattern) while pages of an unseen shape wait in the
# queue: a menu of hundreds of /region/<name> links repeated on every page would otherwise take every load before the
# footer's /privacy or /contact is reached. "crawl": {"per_pattern": N} changes it.
CRAWL_PER_PATTERN = 2


def crawl_pattern(u: str) -> str:
    """The URL's shape for crawl ordering: numeric and id-like segments, the last segment of a path two or more
    segments deep, and query values become placeholders (/region/alder and /region/birch share /region/*)."""
    p = urlparse(u)
    segs = [s for s in (p.path or '/').split('/') if s]
    segs = ['{n}' if s.isdigit() else ('{id}' if re.fullmatch(r'[0-9a-f]{8,}', s) else s) for s in segs]
    if len(segs) >= 2:
        segs[-1] = '*'
    keys = '&'.join(sorted({k for k, _ in parse_qsl(p.query, keep_blank_values=True)}))
    return '/' + '/'.join(segs) + (('?' + keys) if keys else '')


# Root-level siblings (/heron-lake, /marsh-walk, ...) are one collection under a root dynamic segment (an App
# Router app/[slug]/page) once more than this many distinct ones are seen; until then each is its own shape, as a
# handful of root pages (/about, /contact) are different templates. File names (/index.html) never fold.
# "crawl": {"fold_root_after": N} changes it; route patterns ("routes" or "next_app") replace the guess.
FOLD_ROOT_AFTER = 8
DYNAMIC_SEG = re.compile(r'[{\[:]')


def route_regex(route: str):
    """A route pattern as the app declares it (/{slug}, /guides/{parts*}, /blog/[id] or /blog/:id) as a regex."""
    out = ''
    for seg in [x for x in route.strip('/').split('/') if x]:
        if re.fullmatch(r'\{[^}]+\*\}|\[\[?\.\.\.[^\]]+\]\]?|:[A-Za-z_]\w*[*+]', seg):
            out += r'(?:/.+)?' if seg.startswith('[[') else r'/.+'
        elif re.fullmatch(r'\{[^}]+\}|\[[^\]]+\]|:[A-Za-z_]\w*', seg):
            out += r'/[^/]+'
        else:
            out += '/' + re.escape(seg)
    return re.compile('^' + (out or '/') + '/?$')


class Shaper:
    """The URL shape the crawl orders by (crawl_pattern), with the app's route patterns when they are known
    (crawl.routes, or crawl.next_app read with next_app.py): a URL takes the static route it equals, else the
    dynamic route it matches, so /heron-lake and /marsh-walk share /{slug} while /about keeps its own.
    Without routes, root-level siblings fold into /* once more than fold_root_after of them are observed."""

    def __init__(self, routes=None, fold_root_after: int = FOLD_ROOT_AFTER):
        routes = list(dict.fromkeys(routes or []))
        dyn = [r for r in routes if DYNAMIC_SEG.search(r)]
        self.static = {'/' + r.strip('/') for r in routes if r not in dyn}
        # more literal segments first: /guides/{x} before /{slug}
        self.dynamic = sorted(((r, route_regex(r)) for r in dyn),
                              key=lambda x: (-len([q for q in x[0].split('/') if q and not DYNAMIC_SEG.search(q)]), x[0]))
        self.routes = routes
        self.fold_root_after = fold_root_after
        self.roots: set[str] = set()

    def observe(self, url: str):
        segs = [x for x in (urlparse(url).path or '/').split('/') if x]
        if len(segs) == 1 and '.' not in segs[0]:
            self.roots.add(segs[0])

    @property
    def folded(self) -> bool:
        return not self.routes and len(self.roots) > self.fold_root_after

    def shape(self, url: str) -> str:
        if self.routes:
            path = '/' + (urlparse(url).path or '/').strip('/')
            if path in self.static:
                return path
            for r, rx in self.dynamic:
                if rx.match(path):
                    return r
            return crawl_pattern(url)
        segs = [x for x in (urlparse(url).path or '/').split('/') if x]
        if self.folded and len(segs) == 1 and '.' not in segs[0] and not urlparse(url).query:
            return '/*'
        return crawl_pattern(url)


def read_routes(repo) -> list[str]:
    """Page route patterns of the Next.js project(s) in repo, as URLs ({x} and {x*}), via the shared next_app.py."""
    import next_app
    info = next_app.read_next_app(str(repo))
    out = []
    for proj in info.get('projects') or []:
        base = (((proj.get('config') or {}).get('settings') or {}).get('basePath'))
        base = base.get('value') if isinstance(base, dict) else base
        base = base.rstrip('/') if isinstance(base, str) and base.startswith('/') else ''
        for r in proj.get('routes') or []:
            if r.get('kind') in ('page', 'pages_page') and r.get('url'):
                out.append((base + r['url']).rstrip('/') or '/' if base else r['url'])
    return sorted(dict.fromkeys(out))


def crawl_pick(queue: list[str], loaded: dict[str, int], per_pattern: int = CRAWL_PER_PATTERN, must=(), shape=None) -> int:
    """Index of the next URL to load: a listed target first, then the first URL whose shape has been loaded fewer
    than per_pattern times, else the head of the queue (every shape is saturated: plain breadth-first order)."""
    shape = shape or crawl_pattern
    for i, u in enumerate(queue):
        if u in must:
            return i
    for i, u in enumerate(queue):
        if loaded.get(shape(u), 0) < per_pattern:
            return i
    return 0


def _distinct(parts) -> str:
    """In order of first appearance, each once: three cards or code blocks give the same key as one."""
    return '+'.join(dict.fromkeys(p for p in parts if p))


def template_key(f: dict) -> str:
    """The page's template: URL directory pattern, the page-level landmarks and the kinds of children of main.
    Landmarks that come with the content (regions, labelled sections, forms, dialogs) and repeat counts are left
    out, so a docs page with four code-block regions and one with none still share a template."""
    sig = f.get('template_sig') or {}
    lms = [x for x in (sig.get('landmarks') or '').split('+') if x and x not in CONTENT_LANDMARKS]
    shape = (sig.get('main_shape') or '').split(',')
    folder, name = (url_pattern(f['url']).rsplit('/', 1) + [''])[:2]
    if not lms and name:
        # no page-level landmarks: the folder says nothing about the template (one generated report.html per CI
        # run folder), so key on the file name (digits as {n}) and the DOM shape instead
        return f"*/{re.sub(r'[0-9]+', '{n}', name)}||{_distinct(shape)}"
    return f"{folder or '/'}|{_distinct(lms)}|{_distinct(shape)}"


INNER_SCROLL_JS = """() => {
  const out = [];
  const path = (el) => { const p = []; for (let e = el; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
      let i = 1; for (let s = e.previousElementSibling; s; s = s.previousElementSibling) if (s.localName === e.localName) i++;
      p.unshift(e.localName + '[' + i + ']'); } return '/html/body/' + p.join('/'); };
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (!/(auto|scroll)/.test(cs.overflowY + ' ' + cs.overflowX)) continue;
    const hiddenY = el.scrollHeight - el.clientHeight, hiddenX = el.scrollWidth - el.clientWidth;
    if (el.clientHeight < 80 || (hiddenY < 50 && hiddenX < 50)) continue;
    const share = Math.round(100 * hiddenY / Math.max(1, el.scrollHeight));
    out.push({xpath: path(el), selector: el.localName + (el.id ? '#' + el.id : '') +
              [...el.classList].slice(0, 3).map(c => '.' + c).join(''),
              box_height: el.clientHeight, content_height: el.scrollHeight, hidden_px: Math.max(0, hiddenY),
              hidden_x_px: Math.max(0, hiddenX), hidden_share: share});
    if (out.length >= 10) break;
  }
  return out;
}"""


def inner_scroll_containers(page) -> list[dict]:
    """Elements that scroll their own content (overflow auto/scroll, at least 50 px hidden): a full-page screenshot
    shows only their first box, so content further down never appears in any screenshot."""
    try:
        return page.evaluate(INNER_SCROLL_JS)
    except Exception:
        return []


def scroll_limitation(pages: list[dict]) -> str | None:
    """The limitation to report when sampled pages scroll inside panels (an app shell of height 100vh)."""
    hit = [p for p in pages if p.get('inner_scroll')]
    if not hit:
        return None
    ex = '; '.join(f"{p['id']}: {', '.join(c['selector'] + ' (' + str(c['hidden_px']) + ' px hidden)' for c in p['inner_scroll'][:3])}"
                   for p in hit[:5])
    return (f'{len(hit)} sampled page(s) scroll inside panels ({ex}). Full-page screenshots show only the first box '
            'of each panel: findings on content further down have screenshots that do not show it, and the visual '
            'checks (contrast estimates, focus indicators, reflow) saw only what was in view. Scroll the panel and '
            'check that content by hand, or capture the element.')


def third_party_limitation(blocked: list[dict]) -> str | None:
    """The limitation to report when third-party requests were blocked (third_party "block"): that content (an
    embedded map, a chat widget, an ad slot, a consent manager loaded from a CDN) was absent from every check."""
    hosts: dict[str, int] = {}
    for b in blocked or []:
        if b.get('reason') == 'third_party':
            h = urlparse(b.get('url') or '').hostname or '?'
            hosts[h] = hosts.get(h, 0) + 1
    if not hosts:
        return None
    top = sorted(hosts.items(), key=lambda x: (-x[1], x[0]))
    n = sum(hosts.values())
    return (f'{n} third-party request(s) to {len(hosts)} host(s) were blocked ({", ".join(f"{h} ({c})" for h, c in top[:8])}'
            + (f', and {len(top) - 8} more' if len(top) > 8 else '') + '). Whatever they load (embedded players, maps, chat '
            'or consent widgets, ads, analytics) was absent from every check; audit it with third_party "allow" or '
            'allowed_hosts if it is part of the experience under test.')


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
        self.mobile_viewports = [v for v in (cfg.get('mobile_viewports') or []) if v in self.viewports]
        self.probes = cfg.get('probes') or ALL_PROBES
        self.rate = float(cfg.get('rate_limit_seconds', 1.0))
        self.allow_post = bool(cfg.get('allow_form_submission', False))
        self.third_party = cfg.get('third_party', 'block')
        self.bypass_csp = bool(cfg.get('bypass_csp', True))
        self.requests = {'seen': 0, 'sent': 0}   # every request the pages made, and those let out (the rest are in blocked)
        self.targets, self.expect_status = normalise_targets(cfg.get('targets') if isinstance(cfg.get('targets'), list) else [])
        targets = self.targets
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
        self._session_script = None   # restores a captured sessionStorage; holds token values, so never recorded
        self.settle_seconds = float(cfg.get('settle_seconds', 10))
        self.long_lived_seconds = float(cfg.get('long_lived_seconds', 4))
        self._inflight: dict[int, dict] = {}
        self.crawl_info: dict | None = None
        self.skipped_non_html: list[dict] = []
        self.route_source: str | None = None
        self._focus_shots: list | None = None
        self._shots_dir: Path = out / 'screenshots'
        self._focus_tag = ''

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
        self.requests['seen'] += 1
        if self.third_party == 'block' and not self.host_allowed(request.url):
            self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': 'third_party', 'at': now()})
            return route.abort('blockedbyclient')
        if request.method in ('GET', 'HEAD', 'OPTIONS') or self.allow_post:
            self.requests['sent'] += 1
            return route.continue_()
        self.blocked.append({'method': request.method, 'url': request.url.split('?')[0], 'reason': 'write', 'at': now()})
        return route.abort()

    def new_context(self, browser, vp: str, reduced_motion: str = 'no-preference', scale: float = 1):
        w, h = parse_vp(vp)
        # a viewport listed in mobile_viewports emulates a phone: touch events, a coarse pointer and mobile layout
        # (meta viewport honoured), so touch-only controls and (pointer: coarse) styles are what gets audited
        mobile = vp in self.mobile_viewports
        extra = {'is_mobile': True, 'has_touch': True} if mobile else {}
        if mobile and self.cfg.get('mobile_user_agent'):
            extra['user_agent'] = self.cfg['mobile_user_agent']
        ctx = browser.new_context(viewport={'width': w, 'height': h}, device_scale_factor=scale,
                                  reduced_motion=reduced_motion, storage_state=self.storage_state,
                                  ignore_https_errors=False, bypass_csp=self.bypass_csp,
                                  service_workers=self.cfg.get('service_workers', 'block'), **extra)
        if self._session_script:
            ctx.add_init_script(script=self._session_script)
        ctx.route('**/*', self.guard)
        return ctx

    def goto(self, page, url: str) -> dict:
        self.track(page)
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
            ctype = ((resp.headers.get('content-type') if resp else '') or '').split(';')[0].strip().lower()
            # a listed error template (expect_status: 404) is a page to audit; any other 4xx/5xx is unreachable
            expected = self.expect_status.get(urldefrag(url)[0])
            ok = status is None or status < 400 or status == expected
            extra = {'expected_status': expected} if expected is not None else {}
            if expected is not None and status != expected:
                self.events.append({'url': url, 'event': f'expected HTTP {expected}, got {status}', 'at': now()})
            if ctype and ctype not in HTML_TYPES:
                # a text, XML or JSON file linked from a page: nothing to audit, and no settling to wait for
                return {'ok': ok, 'status': status, 'final_url': page.url, 'content_type': ctype, **extra}
            self.settle(page)
            return {'ok': ok, 'status': status, 'final_url': page.url, 'content_type': ctype or None, **extra}
        return {'ok': False, 'status': status, 'error': 'rate limited after retries'}

    def track(self, page):
        """Count the page's requests in flight, so settle() can wait for the network without Playwright's
        networkidle, which never arrives while a dev server holds a long-poll or event stream open."""
        key = id(page)
        if key in self._inflight:
            return self._inflight[key]
        flights: dict = {}
        self._inflight[key] = flights
        page.on('request', lambda r: flights.__setitem__(r, (time.time(), r.url, r.resource_type)))
        page.on('requestfinished', lambda r: flights.pop(r, None))
        page.on('requestfailed', lambda r: flights.pop(r, None))
        return flights

    def settle(self, page):
        """Wait until no request is in flight for 500 ms, then for a quiet DOM; both bounded by settle_seconds
        (default 10) in all. Long-lived connections (event streams, websockets, long-poll and hot-reload
        endpoints, or any request open longer than long_lived_seconds, default 4) don't hold the wait open;
        each page that had one gets an event naming them (path only), so the report can say so."""
        t0 = time.time()
        budget = self.settle_seconds
        flights = self.track(page)
        quiet_since, long_lived = None, set()
        while time.time() - t0 < budget:
            t = time.time()
            busy = 0
            for r, (start, url, rtype) in list(flights.items()):
                if rtype in ('eventsource', 'websocket') or LONG_LIVED_URL.search(url) or t - start > self.long_lived_seconds:
                    long_lived.add(urlparse(url).path or url[:80])
                else:
                    busy += 1
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = t
            elif t - quiet_since >= 0.5:
                break
            page.wait_for_timeout(100)
        else:
            self.events.append({'url': page.url, 'event': f'network did not go idle within {budget:g}s (settle cap)', 'at': now()})
        if long_lived:
            self.events.append({'url': page.url, 'event': f'treated as idle despite {len(long_lived)} long-lived connection(s): '
                                                          + ', '.join(sorted(long_lived)[:3]), 'at': now()})
        dom_ms = int(max(0.5, min(5.0, budget - (time.time() - t0))) * 1000)
        quiet = page.evaluate("""(cap) => new Promise(res => { let t = setTimeout(() => res(true), 500);
            const stop = setTimeout(() => res(false), cap);
            new MutationObserver(() => { clearTimeout(t); t = setTimeout(() => { clearTimeout(stop); res(true); }, 500); })
              .observe(document, {subtree: true, childList: true, attributes: true}); })""", dom_ms)
        if quiet is False:
            self.events.append({'url': page.url, 'event': f'DOM still changing after {dom_ms / 1000:g}s; audited as it was then', 'at': now()})
        try:
            self.inject_helpers(page)
        except Exception as e:  # every probe on this page then errors, which is recorded; never a silent pass
            self.events.append({'url': page.url, 'event': f'probe helpers not injected: {type(e).__name__}: {str(e).splitlines()[0][:200]}',
                                'at': now()})

    def inject_helpers(self, page):
        """Add probes.js and check it took: a script CSP with a nonce or hash refuses injected scripts (set bypass_csp)."""
        if not page.evaluate('() => !!window.__qa'):
            page.add_script_tag(content=self.probes_js)
            if not page.evaluate('() => !!window.__qa'):
                raise RuntimeError('probes.js did not run (the page\'s Content-Security-Policy may refuse injected scripts; '
                                   'set "bypass_csp": true)')

    def page_info(self, page) -> dict:
        """window.__qa.pageInfo(), or a minimal stand-in (title from the page) when probes.js couldn't be injected, so
        the page is still sampled and its engines and probes are recorded as errored rather than the run crashing."""
        try:
            return page.evaluate('() => window.__qa.pageInfo()')
        except Exception as e:
            self.events.append({'url': page.url, 'event': f'page info unavailable (probes.js not injected): {type(e).__name__}', 'at': now()})
            return {'title': page.title(), 'lang': None, 'viewport_meta': None, 'template': {'landmarks': '', 'main_shape': ''},
                    'landmarks': [], 'headings': [], 'frames': [], 'captcha': False, 'links': [], 'probes_injected': False}

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
            self.storage_state, self._session_script, _ = load_session(auth['storage_state'])
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
            const contrastData = n => { const d = ((n.any || [])[0] || {}).data;
                return d && typeof d === 'object' ? {fgColor: d.fgColor || null, bgColor: d.bgColor || null,
                    contrastRatio: d.contrastRatio || null, messageKey: d.messageKey || null} : null; };
            const node = (n, id) => ({target: n.target, impact: n.impact, summary: n.failureSummary || null,
                                html: n.html.slice(0, 300), el: window.__qa.describeTarget(n.target),
                                ...(id.startsWith('color-contrast') ? {contrast: window.__qa.contrastTarget(n.target), check: contrastData(n)} : {})});
            const pack = list => list.map(v => ({id: v.id, impact: v.impact, tags: v.tags, help: v.help, helpUrl: v.helpUrl,
                                                 nodes: v.nodes.slice(0, 50).map(n => node(n, v.id)), node_count: v.nodes.length}));
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
                        el: window.__qa.describeXpath(x.path && x.path.dom),
                        ...(x.ruleId === 'text_contrast_sufficient' ? {contrast: window.__qa.contrastXpath(x.path && x.path.dom)} : {})})),
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
                keep.push({type: m.type, code: m.code, msg: m.msg, el: window.__qa.describe(m.element),
                           ...(/1_4_[36]\\./.test(m.code) ? {contrast: window.__qa.contrastContext(m.element)} : {})});
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
    def probe_keyboard(self, page, shots: Path, pid: str, from_active: bool = False, start_at: str | None = None) -> dict:
        self._focus_shots, self._shots_dir, self._focus_tag = [], shots, pid
        targets = page.evaluate('() => window.__qa.focusTargets()')
        focusable = targets['focusable']
        # Sequential focus starts from the top of the document: an invisible starting point is focused at the start
        # of the body (never a click: a click at fixed coordinates can land inside a fixed header, past the skip
        # link, or activate a control), so the first Tab reaches the first focusable element.
        # In a named state with a modal open and focus inside it (a search dialog opened with a shortcut), Tab
        # starts from the focused element instead: starting at the top of the body would tab through the page
        # behind the overlay, which a keyboard user never reaches.
        # A state with "keyboard_start": "active_element" starts from wherever its actions left focus (after a
        # client-side navigation, a focus manager may move it to <main> or a heading): the next Tab is what a
        # keyboard user reaches next. With nothing focused (the body), it starts from the top and says so.
        modal = page.evaluate('() => window.__qa.openModal()') if from_active else None
        start_note = None
        if start_at == 'active_element' and not (modal and modal.get('focus_inside')):
            modal = None
            if page.evaluate('() => window.__qa.activeInfo()'):
                start = 'active_element'
            else:
                start, start_note = 'top_of_document', 'keyboard_start active_element: nothing was focused after the actions (body)'
        else:
            start = 'active_element' if modal and modal.get('focus_inside') else 'top_of_document'
        if start == 'top_of_document' and not page.evaluate('() => window.__qa.focusStart()'):
            raise RuntimeError('could not set the focus starting point at the top of the document')
        if start == 'active_element' and modal:
            # only the modal's own controls are expected to be reachable while it is open
            focusable = [f for f in focusable if (f.get('xpath') or '').startswith(modal['xpath'] + '/')]
        hard_cap = max(1, int((self.cfg.get('keyboard') or {}).get('max_presses', KEYBOARD_MAX_PRESSES)))
        limit = min(hard_cap, 400, 3 * len(focusable) + 20)
        presses, stopped = 0, None
        started = start == 'active_element'
        stops, moves, trap = [], [], None
        seen: set[str] = set()
        last_new = 0
        measure = 'focus_visible' in self.probes
        pending = None  # previous stop waiting for its unfocused screenshot
        if start == 'active_element':
            # the focused control the traversal starts from is its first stop
            info = page.evaluate('() => window.__qa.activeInfo()')
            if info:
                if measure:
                    info['indicator'] = self.start_indicator(page, info)
                    pending = info
                stops.append(info); moves.append(info['xpath']); seen.add(info['xpath'])
                last_new = 1
        while True:
            if presses >= limit:
                stopped = 'press_limit'
                break
            presses += 1
            if presses % 100 == 0:
                print(f'  [keyboard {pid}] {presses} presses, {len(seen)} elements reached', file=sys.stderr, flush=True)
            page.keyboard.press('Tab')
            page.wait_for_timeout(60)
            if not started:
                page.evaluate('() => window.__qa.focusStartDone()')
                started = True
            info = page.evaluate('() => window.__qa.activeInfo()')
            if info and stops and info['xpath'] == stops[-1]['xpath']:
                continue  # moving between a native control's internal parts (video controls)
            if measure and pending:
                self.finish_indicator(page, pending)
                pending = None
            if info is None:
                if stops:
                    stopped = 'left_document'
                    break  # left the document: one full pass
                continue
            if measure:
                info['indicator'] = self.start_indicator(page, info)
                pending = info
            stops.append(info)
            moves.append(info['xpath'])
            if len(stops) > 1 and info['xpath'] == stops[0]['xpath']:
                stopped = 'wrapped'
                break  # wrapped round to the first stop
            if info['xpath'] not in seen:
                seen.add(info['xpath'])
                last_new = len(moves)
                # still finding new elements (content that focus reveals): keep going, up to the hard cap
                limit = max(limit, min(hard_cap, presses + 20))
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
                stopped = 'trap' if trap else 'cycle'
                break
        if measure and pending:
            self.finish_indicator(page, pending)
        for s in stops:
            s.pop('_shot', None)
        unreached = [f for f in focusable if f['xpath'] not in seen]
        # A traversal cut short by the press limit while it still had elements to reach measured only the start of
        # the Tab order: what it did not reach is not evidence of anything, so the probe is partial, not ran.
        # A page with no Tab stop at all (nothing focusable, and Tab never focused anything) was fully traversed: the
        # absence of stops is the evidence (2.1.1), not a probe cut short.
        if stopped == 'press_limit' and not moves and not focusable:
            stopped = 'no_tab_stops'
        partial = stopped == 'press_limit' and (bool(unreached) or len(moves) - last_new < 15)
        hover_only = [] if trap or start == 'active_element' else self.hover_only_menus(page, seen)
        if start == 'active_element' and not modal:
            # started part-way down the page: what lies before the start point was not traversed, so nothing
            # is "unreached" and the skip link is not measured
            unreached = []
        pointer_only = targets['pointer_only'][:50]
        self.add_listeners(page, pointer_only)
        positive = [s for s in stops if s.get('tabindex') and s['tabindex'].lstrip('-').isdigit() and int(s['tabindex']) > 0]
        jumps = []
        for a, b in zip(stops, stops[1:]):
            if a['doc_y'] - b['doc_y'] > 600 and not b.get('in_dialog') and b['xpath'] != stops[0]['xpath']:
                jumps.append({'from': a['xpath'], 'to': b['xpath']})
        first = stops[0] if stops else None
        skip = bool(first and re.search(r'href="#', first.get('html') or '') and re.search(r'skip|main|content', first.get('html') or '', re.I))
        if start == 'active_element':
            skip = None  # not measured: the traversal started inside the modal or from the focused element
            if modal:
                pointer_only = [p for p in pointer_only if (p.get('xpath') or '').startswith(modal['xpath'] + '/')]
        status = {}
        if partial:
            status = {'status': 'partial',
                      'reason': (f'Tab traversal stopped at the press limit ({presses} presses; max_presses {hard_cap}) before a '
                                 f'full pass: {len(unreached)} of {len(focusable)} focusable elements not reached, so Tab order, '
                                 'focus visibility and focus obscuring past that point were not measured')}
        return {**status, 'presses': len(moves), 'tab_presses': presses, 'press_limit': limit, 'stopped': stopped,
                'stops': stops, 'focusable_count': len(focusable), 'unreached': unreached[:50],
                'pointer_only': pointer_only, 'trap': trap, 'positive_tabindex': positive, 'jumps': jumps[:20],
                'skip_link_first': skip, 'hover_only': hover_only, 'start': start,
                **({'start_note': start_note} if start_note else {}),
                **({'focus_screenshots': len(self._focus_shots)} if self._focus_shots else {}),
                **({'modal': {k: modal.get(k) for k in ('xpath', 'selector', 'html')}} if modal else {})}

    def hover_only_menus(self, page, seen: set) -> list[dict]:
        """Links and buttons that only appear on :hover and that no key opens.

        Hidden menu items are invisible to the Tab traversal, so they can never count as unreached.
        For every group of hidden items, hover the nearest visible ancestor (the menu item); if that
        reveals them, try the keyboard on its control: focus it (covers :focus-within), then
        ArrowDown, and Enter and Space where they don't follow a link. Items that no key reveals
        and that Tab never reached are reported. The page is restored after each attempt."""
        out = []
        start = page.url
        try:
            groups = page.evaluate('() => window.__qa.hiddenMenuItems()')
        except Exception:
            return out

        def reset():
            page.keyboard.press('Escape')
            page.evaluate('() => window.__qa.blurActive()')
            page.mouse.move(1, 1)
            page.wait_for_timeout(200)

        def shown(items):
            return {i['xpath'] for i in items if page.evaluate('(x) => window.__qa.stillVisible(x)', i['xpath'])}

        for g in groups:
            items = [i for i in g['items'] if i['xpath'] not in seen]
            if not items or '#shadow-root' in (g['trigger']['xpath'] or ''):
                continue
            try:
                reset()
                page.locator(f"xpath={g['trigger']['xpath']}").first.hover(timeout=2000)
                page.wait_for_timeout(300)
                revealed = [i for i in items if i['xpath'] in shown(items)]
                reset()
                if not revealed:
                    continue  # not a hover menu (a closed drawer or toggle panel is another probe's business)
                ctl, tried, reached = g.get('control'), [], set()
                if ctl:
                    link = ctl['tag'] == 'a' and (ctl.get('href') or '').strip() not in ('', '#') and not (ctl.get('href') or '').startswith(('#', 'javascript:'))
                    keys = ['focus', 'ArrowDown'] + ([] if link or ctl.get('submits') else ['Enter', 'Space'])
                    for key in keys:
                        page.evaluate('(x) => window.__qa.refocus(x)', ctl['xpath'])
                        if key != 'focus':
                            page.keyboard.press(key)
                        page.wait_for_timeout(250)
                        tried.append(key)
                        if page.url != start:
                            self.events.append({'url': start, 'event': f'hover-menu check: {key} navigated away; page reloaded', 'at': now()})
                            self.goto(page, start)
                            break
                        reached |= shown(revealed)
                        act = page.evaluate('() => window.__qa.activeXpath()')
                        if act in {i['xpath'] for i in revealed}:
                            reached.add(act)
                        reset()
                    if link or ctl.get('submits'):
                        tried.append('Enter/Space not tried: the control ' + ('follows a link' if link else 'submits a form'))
                stuck = [i for i in revealed if i['xpath'] not in reached]
                if stuck:
                    out.append({'trigger': g['trigger'], 'control': ctl, 'items': stuck, 'revealed_on_hover': len(revealed),
                                'keys_tried': tried})
            except Exception as e:
                self.events.append({'url': start, 'event': f'hover-menu check skipped a menu: {type(e).__name__}', 'at': now()})
            if len(out) >= 10:
                break
        try:
            reset()
        except Exception:
            pass
        return out

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
        """Screenshot the focused element; finish_indicator() takes the same area once focus has moved on.
        An inline element that wraps (a link over two lines) is measured per line box: its perimeter is the sum of
        its fragments' perimeters, not that of the box round both lines, which also spans the text between them."""
        clip = self._clip(page, info['rect'])
        if not clip:
            return {'changed_px': None, 'perimeter': None, 'note': 'element outside the viewport'}
        try:
            info['_shot'] = (page.screenshot(clip=clip, animations='disabled', caret='hide'), clip, page.evaluate('() => scrollY'))
        except Exception as e:
            return {'changed_px': None, 'perimeter': None, 'note': type(e).__name__}
        lines = [r for r in (info.get('rects') or []) if r.get('w', 0) > 0 and r.get('h', 0) > 0]
        if len(lines) > 1:
            return {'changed_px': None, 'perimeter': sum(2 * (r['w'] + r['h']) for r in lines), 'line_boxes': len(lines)}
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
            ind = info['indicator']
            ind['changed_px'] = changed_pixels(focused, plain)
            # keep the focused and unfocused crops of a stop with no or a weak indicator, so the result can be
            # checked by eye (capped per traversal; the clip is the element plus 4 px)
            if ind['changed_px'] < (ind.get('perimeter') or 1) / 2 and self._focus_shots is not None and \
                    len(self._focus_shots) < FOCUS_SHOTS_MAX:
                k = len(self._focus_shots) + 1
                base = f'{self._focus_tag}-focus{k:02d}'
                (self._shots_dir / f'{base}.png').write_bytes(focused)
                (self._shots_dir / f'{base}-unfocused.png').write_bytes(plain)
                ind['screenshot'], ind['screenshot_unfocused'] = f'{base}.png', f'{base}-unfocused.png'
                self._focus_shots.append(base)
        except Exception as e:
            info['indicator']['note'] = type(e).__name__

    def probe_reflow(self, page) -> dict:
        return page.evaluate('() => window.__qa.reflow()')

    def probe_text_spacing(self, page) -> dict:
        before = {c['xpath']: c['overflow'] for c in page.evaluate('() => window.__qa.clipped()')}
        if not page.evaluate('() => window.__qa.textSpacing(true)'):
            page.evaluate('() => window.__qa.textSpacing(false)')
            # the override never applied, so nothing was measured: errored, never "ran, nothing clipped"
            return {'status': 'errored', 'reason': 'the text-spacing style did not apply (the page\'s Content-Security-Policy '
                                                   'refuses inline styles; set "bypass_csp": true)'}
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
        # earlier probes (keyboard) leave the page scrolled; measure every target at the top of the page, in one
        # coordinate system (fixed and in-flow targets are never compared with each other, see probes.js)
        page.evaluate('() => window.scrollTo(0, 0)')
        page.wait_for_timeout(150)
        return page.evaluate('() => window.__qa.targetSize()')

    def probe_hover_focus(self, page) -> dict:
        found, own_box, skipped = [], 0, []
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
                popups = page.evaluate('(x) => window.__qa.newlyVisible(x)', c['xpath'])
                own_box += page.evaluate('() => window.__qa.ownBoxChanges()')
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
                # named, and the probe is partial: what this element shows on hover was not measured
                skipped.append({'xpath': c.get('xpath'), 'selector': c.get('selector'), 'html': (c.get('html') or '')[:120],
                                'error': f'{type(e).__name__}: {str(e).splitlines()[0][:160]}'})
                self.events.append({'url': page.url, 'event': f"hover probe skipped {c.get('selector') or c.get('xpath')}: "
                                                              f'{type(e).__name__}', 'at': now()})
        page.mouse.move(1, 1)
        # own_box_changes: elements that appeared only inside the hovered element's own box (not content on hover)
        out = {'checked': len(cands) - len(skipped), 'candidates': len(cands), 'popups': found, 'own_box_changes': own_box,
               'skipped': skipped}
        if skipped:
            out.update(status='partial', reason=(f'{len(skipped)} of {len(cands)} hover candidate(s) could not be hovered '
                                                 f"({', '.join(x['selector'] or x['xpath'] or '?' for x in skipped[:3])}): "
                                                 'content they show on hover or focus was not checked'))
        return out

    def watch_motion(self, page) -> dict:
        # 2.2.2 concerns movement that lasts more than 5 seconds: compare 5.0 s and 7.5 s after load
        page.wait_for_timeout(5000)
        page.evaluate('() => window.__qa.motionSnapshot()')
        page.wait_for_timeout(2500)
        return page.evaluate('() => window.__qa.motionChanges()')

    def probe_motion(self, browser, url: str, vp: str) -> dict:
        """Watch a fresh load in its own pages (normal, then reduced motion): the probed page is never reloaded, so
        later probes keep its state (a dismissed consent banner stays dismissed)."""
        out = {'window': 'changes between 5.0 s and 7.5 s after load', 'normal': None, 'reduced_motion': None}
        for key, motion in (('normal', 'no-preference'), ('reduced_motion', 'reduce')):
            ctx = self.new_context(browser, vp, reduced_motion=motion)
            try:
                p = ctx.new_page()
                nav = self.goto(p, url)
                if not nav['ok']:
                    if key == 'normal':
                        return {'status': 'errored', 'reason': nav.get('error') or f"HTTP {nav.get('status')}"}
                    break
                self.inject_helpers(p)
                self.consent_dismiss(p)
                out[key] = self.watch_motion(p)
            finally:
                ctx.close()
        return out

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
    def shaper(self, crawl: dict) -> Shaper:
        """crawl.routes (route patterns) or crawl.next_app (a Next.js project folder, read by next_app.py)."""
        routes, self.route_source = list(crawl.get('routes') or []), None
        if routes:
            self.route_source = 'crawl.routes'
        elif crawl.get('next_app'):
            try:
                routes = read_routes(crawl['next_app'])
                self.route_source = 'next_app' if routes else None
            except Exception as e:
                self.events.append({'url': str(crawl['next_app']), 'event': f'route patterns unreadable: {type(e).__name__}: {e}', 'at': now()})
            if not routes:
                self.events.append({'url': str(crawl['next_app']), 'event': 'no page routes found; crawl shapes come from URL paths', 'at': now()})
        return Shaper(routes, int(crawl.get('fold_root_after', FOLD_ROOT_AFTER)))

    def explore(self, browser) -> list[dict]:
        targets = list(self.targets)
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
        # max_loads bounds the crawl (default 4 x max_pages); it also stops early once the structured sample is
        # full (max_pages templates) or once max_pages loads in a row found no new template (saturated)
        limit = int((crawl or {}).get('max_loads', max_pages * 4)) if crawl else len(queue)
        must = {urldefrag(u)[0] for u in targets + cp_urls}
        templates: set[str] = set()
        since_new, stopped = 0, 'queue empty'
        per_pattern = int((crawl or {}).get('per_pattern', CRAWL_PER_PATTERN))
        shapes: dict[str, int] = {}
        shaper = self.shaper(crawl or {})
        for u in queue:
            shaper.observe(u)
        while queue:
            if len(found) >= limit:
                stopped = f'load limit ({limit}) reached'
                break
            if crawl and not must - seen:
                if len(templates) >= max_pages:
                    stopped = f'template sample full ({len(templates)} templates for max_pages {max_pages})'
                    break
                if since_new >= max_pages:
                    stopped = f'no new template in the last {since_new} pages'
                    break
            url = queue.pop(crawl_pick(queue, shapes, per_pattern, must - seen, shape=shaper.shape) if crawl else 0)
            if url in seen:
                continue
            seen.add(url)
            shapes[shaper.shape(url)] = shapes.get(shaper.shape(url), 0) + 1
            print(f'[crawl {len(found) + 1}/{limit}] {url}', file=sys.stderr, flush=True)
            nav = self.goto(page, url)
            if not nav['ok']:
                found.append({'url': url, 'ok': False, 'status': nav.get('status'),
                              'error': nav.get('error') or f"HTTP {nav.get('status')}"})
                continue
            if nav.get('content_type') and nav['content_type'] not in HTML_TYPES:
                self.skipped_non_html.append({'url': url, 'content_type': nav['content_type']})
                continue
            info = self.page_info(page)
            f = {'url': url, 'ok': True, 'final_url': nav.get('final_url'), 'title': info['title'],
                 'template_sig': info['template'], 'captcha': info['captcha'], 'status': nav.get('status'),
                 **({'expected_status': nav['expected_status']} if 'expected_status' in nav else {})}
            found.append(f)
            key = template_key(f)
            since_new = 0 if key not in templates else since_new + 1
            templates.add(key)
            if crawl:
                for link in info['links']:
                    link = urldefrag(link)[0]
                    if urlparse(link).netloc in origin and link not in seen and not NON_PAGE_LINK.search(urlparse(link).path):
                        queue.append(link)
                        shaper.observe(link)
        ctx.close()
        self.crawl_info = {'loaded': len(found) + len(self.skipped_non_html), 'limit': limit, 'templates': len(templates),
                           'stopped': stopped if crawl else 'no crawl: listed targets only',
                           **({'per_pattern': per_pattern,
                               'patterns': dict(sorted(shapes.items(), key=lambda x: (-x[1], x[0]))[:30]),
                               'shapes_from': self.route_source or ('URL path; root-level siblings folded into /* (more than '
                                                                    f'{shaper.fold_root_after} seen)' if shaper.folded else 'URL path'),
                               **({'routes': shaper.routes[:100]} if shaper.routes else {})} if crawl else {}),
                           'skipped_non_html': self.skipped_non_html[:50]}
        print(f'[crawl] {self.crawl_info["loaded"]} loaded, {len(templates)} template(s); stopped: {self.crawl_info["stopped"]}',
              file=sys.stderr, flush=True)
        return self.sample(found, set(cp_urls), max_pages)

    def sample(self, found: list[dict], cp_urls: set, max_pages: int) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for f in found:
            if not f['ok']:
                continue
            key = template_key(f)
            f['template'] = 'T' + hashlib.sha1(key.encode()).hexdigest()[:6]
            f['template_key'] = key
            groups.setdefault(f['template'], []).append(f)
        # Every page enters the sample once, labelled by how it entered: critical-path steps and the
        # explicitly listed targets always (template de-duplication never drops them), then one page
        # per template not yet covered (structured), then a seeded random share of what is left.
        listed = {urldefrag(u)[0] for u in self.targets}
        sample, chosen = [], set()
        for f in found:
            if f['ok'] and f['url'] in cp_urls and f['url'] not in chosen:
                sample.append({**f, 'reason': 'critical_path'}); chosen.add(f['url'])
        for f in found:
            if f['ok'] and f['url'] in listed and f['url'] not in chosen:
                sample.append({**f, 'reason': 'listed'}); chosen.add(f['url'])
        for t, members in groups.items():
            if not any(m['url'] in chosen for m in members) and len(sample) < max_pages:
                sample.append({**members[0], 'reason': 'structured'}); chosen.add(members[0]['url'])
        rest = [f for f in found if f['ok'] and f['url'] not in chosen]
        rng = random.Random(self.cfg.get('seed', 1))
        share = float(self.cfg.get('random_sample', 0.1))
        want = 0 if share <= 0 else max(1, round(len(rest) * share))  # 0 means no random pages
        k = min(len(rest), max(0, max_pages - len(sample)), want)
        for f in rng.sample(rest, k):
            sample.append({**f, 'reason': 'random'})
        for f in found:
            if not f['ok']:
                sample.append({**f, 'reason': 'unreachable'})
        for i, s in enumerate(sample, 1):
            s['id'] = f'p{i:02d}'
        self.unsampled = [f['url'] for f in rest if f['url'] not in {s['url'] for s in sample}]
        return sample

    # consent / overlays -------------------------------------------------
    def consent_cfg(self) -> dict | None:
        """The configured overlay dismissal: {"dismiss": selector} or {"steps": [actions]}; engines run "before" it
        (as before 1.2), "after" it or "both" (the default); probes_with_overlay (default ["keyboard"]) run with it open."""
        c = self.cfg.get('consent') or {}
        if not (c.get('dismiss') or c.get('steps')):
            return None
        engines = c.get('engines', 'both')
        if engines not in ('before', 'after', 'both'):
            raise ValueError('consent.engines must be "before", "after" or "both"')
        return {'engines': engines, 'probes_with_overlay': [p for p in c.get('probes_with_overlay', ['keyboard']) if p in ALL_PROBES]}

    def consent_dismiss(self, page) -> bool:
        c = self.cfg.get('consent') or {}
        if not (c.get('dismiss') or c.get('steps')):
            return False
        try:
            if c.get('steps'):
                self.act(page, c['steps'])
            else:
                page.click(c['dismiss'], timeout=3000)
                page.wait_for_timeout(300)
            return True
        except Exception as e:
            self.events.append({'url': page.url, 'event': f'overlay dismissal failed: {type(e).__name__}', 'at': now()})
            return False

    def run_probe(self, probe, page, browser, url, vp, shots, aria, tag, from_active: bool = False, start_at: str | None = None) -> dict:
        t0 = time.time()
        try:
            if probe in ('keyboard', 'focus_visible'):
                result = self.probe_keyboard(page, shots, tag, from_active=from_active, start_at=start_at)
            elif probe == 'resize_text':
                result = self.probe_resize_text(browser, url, shots, tag)
            elif probe == 'motion':
                result = self.probe_motion(browser, url, vp)
            elif probe == 'a11y_tree':
                result = self.probe_a11y_tree(page, aria, tag)
            else:
                result = getattr(self, 'probe_' + probe)(page)
            result.setdefault('status', 'ran')
        except Exception as e:
            result = {'status': 'errored', 'reason': f'{type(e).__name__}: {str(e).splitlines()[0][:300]}'}
        result['seconds'] = round(time.time() - t0, 2)
        # one line per probe: a page whose probes take a minute and a half no longer looks hung
        print(f"  [{tag}] {probe}: {result['status']} in {result['seconds']:g}s"
              + (f" ({result.get('reason', '')[:120]})" if result['status'] != 'ran' and result.get('reason') else ''),
              file=sys.stderr, flush=True)
        return result

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
            scrollers = inner_scroll_containers(page)
            if scrollers:  # the full-page screenshot shows only the first box of each panel
                vc['inner_scroll'] = scrollers
                checks.setdefault('inner_scroll', scrollers)
            info = self.page_info(page)
            consent = self.consent_cfg()
            record = {'page': pid, 'url': url, 'final_url': page.url, 'viewport': vp, 'state': 'initial', 'page_info': {k: v for k, v in info.items() if k != 'links'},
                      'screenshot': f'{tag}.png', 'engines': {}, 'probes': {}}
            if not consent or consent['engines'] in ('before', 'both'):
                record['engines'] = self.run_engines(page)
            if consent:
                # probes with the overlay open (does focus enter it? is focus hidden behind it?), on the same page
                for probe in consent['probes_with_overlay']:
                    if (probe in DESKTOP_ONLY and narrow) or (probe in NARROW_ONLY and not narrow):
                        continue
                    open_rec = {'page': pid, 'url': url, 'final_url': page.url, 'viewport': vp, 'state': 'consent_open',
                                'screenshot': f'{tag}.png', 'engines': {}, 'probes': {probe: self.run_probe(probe, page, browser, url, vp, shots, aria, f'{tag}__consent_open')}}
                    (raw / f'{tag}__consent_open.json').write_text(json.dumps(open_rec, indent=1, ensure_ascii=False), encoding='utf-8')
                    vc.setdefault('consent', {})['probes_with_overlay'] = {probe: open_rec['probes'][probe]['status']}
                record['consent_dismissed'] = self.consent_dismiss(page)
                vc.setdefault('consent', {})['dismissed'] = record['consent_dismissed']
                if consent['engines'] == 'after' or (consent['engines'] == 'both' and record['consent_dismissed']):
                    after = self.run_engines(page) if record['consent_dismissed'] else {}
                    if consent['engines'] == 'after':
                        record['engines'] = after
                    else:
                        shot = f'{tag}__consent_dismissed.png'
                        page.screenshot(path=str(shots / shot), full_page=True)
                        (raw / f'{tag}__consent_dismissed.json').write_text(json.dumps(
                            {'page': pid, 'url': url, 'final_url': page.url, 'viewport': vp, 'state': 'consent_dismissed',
                             'screenshot': shot, 'engines': after, 'probes': {}}, indent=1, ensure_ascii=False), encoding='utf-8')
                        vc['consent']['engines_after_dismissal'] = {k: v['status'] for k, v in after.items()}
                record['consent'] = {'engines': consent['engines'], 'probes_with_overlay': consent['probes_with_overlay'],
                                     'dismissed': record['consent_dismissed']}
            vc['engines'] = {k: v['status'] for k, v in record['engines'].items()}
            try:
                self.inject_helpers(page)
            except Exception as e:
                self.events.append({'url': url, 'event': f'probe helpers not injected: {type(e).__name__}', 'at': now()})
            # motion loads the page afresh in its own context, so its order doesn't matter; it runs last anyway
            for probe in sorted(self.probes, key=lambda p: (p == 'motion', p != 'keyboard')):
                if (probe in DESKTOP_ONLY and narrow) or (probe in NARROW_ONLY and not narrow):
                    continue
                if probe == 'focus_visible' and 'keyboard' in self.probes:
                    result = {'note': 'measured during the keyboard probe', 'status': record['probes'].get('keyboard', {}).get('status', 'ran')}
                else:
                    result = self.run_probe(probe, page, browser, url, vp, shots, aria, tag)
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

    def focus_after(self, page, tag: str) -> dict | None:
        """document.activeElement after a state's actions (None: the body), its indicator area and a crop of it."""
        try:
            info = page.evaluate('() => window.__qa.activeInfo()')
        except Exception as e:
            return {'error': type(e).__name__}
        if not info:
            return None
        info = {k: info.get(k) for k in ('xpath', 'selector', 'sig', 'html', 'rect', 'tabindex', 'in_dialog', 'obscured')}
        clip = self._clip(page, info['rect']) if info.get('rect') else None
        if clip:
            try:
                name = f'{tag}-focus.png'
                page.screenshot(path=str(self.out / 'screenshots' / name), clip=clip, animations='disabled', caret='hide')
                info['screenshot'] = name
            except Exception:
                pass
        return info

    def audit_state(self, browser, pg: dict, vp: str, st: dict) -> dict:
        tag = f"{pg['id']}__{vp}__{st['name']}"
        ctx = self.new_context(browser, vp)
        page = ctx.new_page()
        out = {'name': st['name'], 'engines': {}, 'probes': {}}
        nav = self.goto(page, pg['url'])
        if not nav['ok']:
            out['error'] = nav.get('error')
            ctx.close(); return out
        if self.consent_cfg():
            out['consent_dismissed'] = self.consent_dismiss(page)
        try:
            self.act(page, st.get('actions', []))
        except Exception as e:
            out['error'] = f'state actions failed: {type(e).__name__}: {str(e).splitlines()[0][:200]}'
            ctx.close(); return out
        # where focus is once the actions are done (after a client-side navigation: <main>, the new heading, the
        # body?), with a crop of it, before anything else moves it
        focus = self.focus_after(page, tag)
        page.screenshot(path=str(self.out / 'screenshots' / f'{tag}.png'), full_page=True)
        record = {'page': pg['id'], 'url': pg['url'], 'final_url': page.url, 'viewport': vp, 'state': st['name'],
                  'screenshot': f'{tag}.png', 'focus_after_actions': focus, 'engines': self.run_engines(page), 'probes': {}}
        out['final_url'] = page.url
        out['focus_after_actions'] = (focus or {}).get('selector') or ('body (nothing focused)' if focus is None else None)
        out['engines'] = {k: v['status'] for k, v in record['engines'].items()}
        start_at = st.get('keyboard_start')
        for probe in sorted(st.get('probes', []), key=lambda p: p == 'motion'):
            result = self.run_probe(probe, page, browser, pg['url'], vp, self.out / 'screenshots', self.out / 'aria', tag,
                                    from_active=start_at != 'top', start_at=start_at)
            record['probes'][probe] = result
            out['probes'][probe] = result['status']
        (self.out / 'raw' / f'{tag}.json').write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding='utf-8')
        ctx.close()
        return out


def check_matrix(sample: list[dict], pages: list[dict], viewports: list[str], probes: list[str], required: list[str],
                 states_cfg: list[dict] | None = None) -> list[dict]:
    """One row per planned check, for evidence completeness: page x viewport x required engine or applicable probe,
    plus every named state's engines and its own probes (rows with a `state`). A state's probe that was cut short or
    errored counts against completeness and confidence like one on the initial page."""
    by_page = {p.get('page'): p for p in pages}
    by_url = {}
    for st in states_cfg or []:
        by_url.setdefault(urldefrag(st.get('url', ''))[0], []).append(st)
    checks = []
    for s in sample:
        pg = by_page.get(s['id']) or {}
        for vp in viewports:
            narrow = parse_vp(vp)[0] < 600
            vc = (pg.get('viewports') or {}).get(vp)
            blocked = 'blocked' if (pg.get('blocked') or pg.get('error') or s['reason'] == 'unreachable' or not vc or vc.get('error')) else None
            for name in required:
                checks.append({'page': s['id'], 'viewport': vp, 'kind': 'engine', 'name': name,
                               'status': blocked or vc['engines'].get(name, 'not_run')})
            for probe in probes:
                if (probe in DESKTOP_ONLY and narrow) or (probe in NARROW_ONLY and not narrow):
                    continue
                checks.append({'page': s['id'], 'viewport': vp, 'kind': 'probe', 'name': probe,
                               'status': blocked or vc['probes'].get(probe, 'not_run')})
            # named states: those that ran are in vc['states']; planned ones that never did are blocked
            done = {x.get('name'): x for x in ((vc or {}).get('states') or [])}
            planned = [st for st in by_url.get(urldefrag(s.get('url') or '')[0], [])
                       if (st.get('viewport') == vp) or (not st.get('viewport') and vp == viewports[0])]
            names = list(dict.fromkeys([st['name'] for st in planned] + list(done)))
            for name in names:
                x = done.get(name) or {}
                st_cfg = next((st for st in planned if st['name'] == name), {})
                failed = 'blocked' if (blocked or x.get('error') or name not in done) else None
                for eng in required:
                    checks.append({'page': s['id'], 'viewport': vp, 'state': name, 'kind': 'engine', 'name': eng,
                                   'status': failed or (x.get('engines') or {}).get(eng, 'not_run')})
                for probe in list(dict.fromkeys(list(st_cfg.get('probes') or []) + list((x.get('probes') or {})))):
                    checks.append({'page': s['id'], 'viewport': vp, 'state': name, 'kind': 'probe', 'name': probe,
                                   'status': failed or x['probes'].get(probe, 'not_run')})
    return checks


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--cache', help='engine cache (default as fetch_engines.py)')
    ap.add_argument('--plan-only', action='store_true', help='explore and sample, then stop')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
    try:
        targets, _ = normalise_targets(cfg.get('targets') or [])
    except (ValueError, TypeError) as exc:
        ap.error(str(exc))
    if not targets and not cfg.get('sitemap'):
        ap.error('config needs targets or sitemap')
    stray = [v for v in cfg.get('mobile_viewports') or [] if v not in (cfg.get('viewports') or ['1280x800', '320x640'])]
    if stray:
        ap.error(f'mobile_viewports {stray} are not in viewports; list them in both')
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
    try:
        r.consent_cfg()
    except ValueError as exc:
        ap.error(str(exc))
    for st in cfg.get('states') or []:
        if st.get('keyboard_start') not in (None, 'active_element', 'top'):
            ap.error(f'state {st.get("name")}: keyboard_start must be "active_element" or "top"')
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
        if e['status'] == 'ready' and args.plan_only:
            e['status'], e['reason'] = 'not_run', 'plan only: ready, but engines do not run with --plan-only'
        elif e['status'] == 'ready':
            ran = [vp.get('engines', {}).get(e['name']) for pg in pages for vp in pg.get('viewports', {}).values()]
            e['status'] = 'ran' if 'ran' in ran else ('errored' if 'errored' in ran else 'not_run')
            if e['status'] != 'ran':
                e['reason'] = e['reason'] or 'no page completed'
    # one row per planned check (page x viewport x required engine or applicable probe), for evidence completeness
    lock = {e['name']: e for e in fetch_engines.load_lock()['engines']}
    required = [n for n in ENGINE_NAMES if lock[n]['required']]
    checks = check_matrix(sample, pages, r.viewports, r.probes, required, cfg.get('states') or [])
    run = {'tool': 'run_audit.py', 'started_at': started, 'finished_at': now(), 'config': safe_cfg, 'checks': checks,
           'wcag_version': r.version, 'level': r.level, 'browser': {'name': 'chromium', 'version': browser_version},
           'playwright': pw_version, 'engines': r.engine_status, 'probes': r.probes, 'viewports': r.viewports,
           'mobile_viewports': r.mobile_viewports,
           'sample': [{k: s.get(k) for k in ('id', 'url', 'final_url', 'title', 'template', 'template_key', 'reason', 'error', 'captcha', 'status')}
                      | ({'expected_status': s['expected_status']} if s.get('expected_status') is not None else {}) for s in sample],
           'unsampled': getattr(r, 'unsampled', []), 'pages': pages, 'blocked_requests': r.blocked, 'events': r.events,
           'authorised_by': cfg.get('authorised_by') or ('loopback target' if all(is_loopback(u) for u in targets) else None),
           'auth_used': bool(auth_used), 'mode': 'plan_only' if args.plan_only else 'audit',
           'bypass_csp': r.bypass_csp, 'consent': r.consent_cfg(), 'requests': r.requests, 'crawl': r.crawl_info,
           'settle': {'cap_seconds': r.settle_seconds, 'long_lived_seconds': r.long_lived_seconds}}
    scroll_note = scroll_limitation([{'id': p.get('page'), 'url': p.get('url'), 'inner_scroll': p.get('inner_scroll')}
                                     for p in pages])
    tp_note = third_party_limitation(r.blocked)
    run['limitations'] = [x for x in (scroll_note, tp_note) if x]
    for x in run['limitations']:
        print(f'WARN: {x}', file=sys.stderr)
    (out / 'run.json').write_text(json.dumps(run, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'mode': run['mode'], 'pages': len(sample),
                      'reachable': sum(1 for s in sample if s['reason'] != 'unreachable'),
                      'engines': {e['name']: e['status'] for e in r.engine_status},
                      'requests_sent': r.requests['sent'], 'blocked_requests': len(r.blocked), 'out': str(out)}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
