#!/usr/bin/env python3
"""Measure pages in a real browser, repeatedly, under fixed network and CPU profiles (lab data).

    python measure.py plan.json --out run/measurements.json [--artifacts run/artifacts] [--runs 5]
        [--profiles mobile-mid,desktop] [--cache cold|warm|both] [--browser chromium|firefox|webkit]
        [--only URL_SUBSTRING] [--templates home,about] [--append] [--no-diagnostic]

plan.json:
    {"targets": [{"url", "template", "reason", "critical_path": false, "interaction": [actions]}],
     "profiles": ["mobile-mid", "desktop"], "runs": 5, "cache": "cold", "budget": "budget.json", "authorised_by": null,
     "allow_form_submission": false, "auth": null,
     "third_party": "block", "allowed_hosts": [], "service_workers": "cold"}

third_party: block (the default) aborts every request to a host that isn't loopback, a target or in
allowed_hosts, and records it in blocked_requests (reason third_party): analytics, ads and embeds
are then not measured, so their cost and behaviour are outside the numbers and the report says so.
allow lets them load and be measured (they are still marked third_party per resource); list a CDN
the site serves itself from in allowed_hosts. service_workers: cold (the default) blocks them for
cold runs and allows them for warm runs; block blocks them for every run; allow allows them.

Per page x profile x cache mode (x browser): one discarded warm-up, then N measured runs, each in a
fresh browser context (so a cold run has an empty cache; service workers are blocked for cold runs).
A warm run loads the page once in its context and measures the second load. When any budgeted
metric is unstable after N runs (CV above 0.25, unless its spread is within the floor or every run
is at or under half the budget: attribute.is_stable), more runs are added, one at a time, up to 10.

Each measured run records:
  * LCP, CLS, INP, FCP and TTFB from web-vitals 6.2.3 (attribution build, injected before the page
    loads; Chromium only for LCP, CLS and INP), with reportAllChanges;
  * TBT from Long Tasks after FCP (a lab metric), and long animation frames with their scripts;
  * bytes transferred per resource from DevTools Network events (Chromium) or Resource Timing;
  * render-blocking resources, the LCP element's attributes, layout-shift sources, and the
    navigation's Server-Timing entries, read passively from the response.
INP is measured only when the target has an interaction: the steps run after the page settles.

Then one diagnostic run (not counted in any statistic, because tracing and coverage slow the page)
records a DevTools trace with screenshots (gzipped), a filmstrip of frames 100 ms apart until load,
and JavaScript coverage.

The machine (CPU, cores, OS), the browser version, the throttling actually applied, and a CPU
calibration (a fixed workload timed three times) are recorded with the runs.
"""
from __future__ import annotations
import argparse, base64, gzip, json, os, platform, statistics, sys, time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now  # noqa: E402
from fetch_assets import asset_path, load_lock  # noqa: E402

VITALS = ('LCP', 'CLS', 'INP', 'FCP', 'TTFB')
CV_LIMIT = 0.25
MAX_RUNS = 10
TRACE_CATEGORIES = ['devtools.timeline', 'disabled-by-default-devtools.timeline', 'disabled-by-default-devtools.timeline.frame',
                    'disabled-by-default-devtools.screenshot', 'v8.execute', 'blink.user_timing', 'loading', 'latencyInfo']

COLLECTOR = r"""
(() => {
  const P = window.__perf = {vitals: {}, longtasks: [], loaf: [], shifts: [], events: []};
  const sel = (n) => {
    if (!n || n.nodeType !== 1) return null;
    if (n.id) return '#' + n.id;
    let s = n.tagName.toLowerCase();
    if (typeof n.className === 'string' && n.className.trim()) s += '.' + n.className.trim().split(/\s+/).join('.');
    return s;
  };
  const keep = (m) => {
    const a = m.attribution || {};
    const out = {value: m.value, rating: m.rating, attribution: {}};
    for (const [k, v] of Object.entries(a)) { if (v != null && typeof v !== 'object') out.attribution[k] = v; }
    if (m.name === 'INP' && a.longestScript) {
      const e = a.longestScript.entry;
      out.attribution.longestScript = {sourceURL: e.sourceURL, invoker: e.invoker, duration: e.duration, subpart: a.longestScript.subpart};
    }
    if (m.name === 'LCP' && a.lcpResourceEntry) {
      out.attribution.resourceTransferSize = a.lcpResourceEntry.transferSize;
      out.attribution.resourceRenderBlocking = a.lcpResourceEntry.renderBlockingStatus;
    }
    P.vitals[m.name] = out;
  };
  const all = {reportAllChanges: true};
  webVitals.onLCP(keep, all); webVitals.onCLS(keep, all); webVitals.onINP(keep, all);
  webVitals.onFCP(keep); webVitals.onTTFB(keep);
  const obs = (type, fn) => { try { new PerformanceObserver(l => l.getEntries().forEach(fn)).observe({type, buffered: true}); } catch (e) {} };
  obs('longtask', e => P.longtasks.push({start: e.startTime, duration: e.duration}));
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { if (e.interactionId) P.events.push({id: e.interactionId,
      name: e.name, target: sel(e.target), duration: e.duration, inputDelay: e.processingStart - e.startTime,
      processing: e.processingEnd - e.processingStart}); })).observe({type: 'event', buffered: true, durationThreshold: 16}); } catch (e) {}
  obs('long-animation-frame', e => P.loaf.push({start: e.startTime, duration: e.duration, blocking: e.blockingDuration,
      scripts: e.scripts.map(s => ({url: s.sourceURL, invoker: s.invoker, type: s.invokerType, duration: s.duration}))}));
  obs('layout-shift', e => { if (!e.hadRecentInput) P.shifts.push({start: e.startTime, value: e.value,
      sources: (e.sources || []).map(s => sel(s.node)).filter(Boolean)}); });
})();
"""

SNAPSHOT = r"""
() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paint = Object.fromEntries(performance.getEntriesByType('paint').map(p => [p.name, p.startTime]));
  return {
    vitals: (window.__perf || {}).vitals || {}, longtasks: (window.__perf || {}).longtasks || [],
    loaf: (window.__perf || {}).loaf || [], shifts: (window.__perf || {}).shifts || [], paint, now: performance.now(),
    nav: nav ? {responseStart: nav.responseStart, requestStart: nav.requestStart, dcl: nav.domContentLoadedEventEnd,
                load: nav.loadEventEnd, transfer: nav.transferSize,
                serverTiming: nav.serverTiming.map(s => ({name: s.name, dur: s.duration, desc: s.description}))} : null,
    resources: performance.getEntriesByType('resource').map(r => ({url: r.name, initiator: r.initiatorType,
                transfer: r.transferSize, encoded: r.encodedBodySize, blocking: r.renderBlockingStatus || null,
                start: r.startTime, duration: r.duration})),
  };
}
"""

LCP_ELEMENT = r"""
(target) => {
  let el = null;
  try { el = target ? document.querySelector(target) : null; } catch (e) {}
  if (!el) return null;
  return {tag: el.tagName.toLowerCase(), fetchpriority: el.getAttribute('fetchpriority'), loading: el.getAttribute('loading'),
          width_attr: el.getAttribute('width'), height_attr: el.getAttribute('height'),
          natural: el.naturalWidth ? [el.naturalWidth, el.naturalHeight] : null,
          rendered: [Math.round(el.getBoundingClientRect().width), Math.round(el.getBoundingClientRect().height)]};
}
"""

CALIBRATE = """() => { const t = performance.now(); let x = 0; for (let i = 0; i < 20000000; i++) { x += Math.sqrt(i) % 7; }
                     return [performance.now() - t, x > 0]; }"""


def machine() -> dict:
    cpu = os.environ.get('PROCESSOR_IDENTIFIER') or platform.processor() or platform.machine()
    try:
        if sys.platform.startswith('linux'):
            for line in Path('/proc/cpuinfo').read_text().splitlines():
                if line.startswith('model name'):
                    cpu = line.split(':', 1)[1].strip()
                    break
        elif sys.platform == 'darwin':
            import subprocess
            cpu = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True).stdout.strip() or cpu
    except OSError:
        pass
    return {'cpu': cpu, 'cores': os.cpu_count(), 'os': platform.platform(), 'python': platform.python_version()}


def classify(rtype: str, mime: str, url: str) -> str:
    rtype = (rtype or '').lower()
    if rtype in ('document',):
        return 'document'
    if rtype == 'script' or 'javascript' in mime:
        return 'script'
    if rtype == 'stylesheet' or mime == 'text/css':
        return 'stylesheet'
    if rtype == 'image' or mime.startswith('image/'):
        return 'image'
    if rtype == 'font' or mime.startswith('font/') or url.split('?')[0].endswith(('.woff2', '.woff', '.ttf', '.otf')):
        return 'font'
    if rtype in ('xhr', 'fetch'):
        return 'fetch'
    return 'other'


def tbt(longtasks: list[dict], fcp: float | None, end: float) -> float:
    if fcp is None:
        return 0.0
    total = 0.0
    for t in longtasks:
        s, e = max(t['start'], fcp), min(t['start'] + t['duration'], end)
        total += max(0.0, (e - s) - 50.0)
    return round(total, 1)


def disjoint_used(functions: list[dict]) -> tuple[int, int]:
    """(used bytes, total bytes) from V8 block coverage, as Puppeteer's convertToDisjointRanges does."""
    points, total = [], 0
    for f in functions:
        for r in f['ranges']:
            points.append((r['startOffset'], 0, r))
            points.append((r['endOffset'], 1, r))
            total = max(total, r['endOffset'])

    def key(p):
        off, typ, r = p
        length = r['endOffset'] - r['startOffset']
        return (off, -typ, -length if typ == 0 else length)
    points.sort(key=key)
    stack, used, last = [], [], 0
    for off, typ, r in points:
        if stack and last < off and stack[-1] > 0:
            if used and used[-1][1] == last:
                used[-1][1] = off
            else:
                used.append([last, off])
        last = off
        if typ == 0:
            stack.append(r['count'])
        else:
            stack.pop()
    return sum(e - s for s, e in used if e - s > 1), total


class Runner:
    def __init__(self, h: Harness, plan: dict, profiles: dict, wv_source: str, art: Path | None, browser: str):
        self.h, self.plan, self.profiles, self.art, self.browser = h, plan, profiles, art, browser
        self.init = wv_source + '\n' + COLLECTOR

    def page(self, profile: str, cache: str):
        p = self.profiles[profile]
        chromium = self.browser == 'chromium'
        page = self.h.new_page(viewport=p['viewport'], cpu_throttle=p['cpu_slowdown'] if chromium and p['cpu_slowdown'] > 1 else None,
                               network={'download_kbps': p['download_kbps'], 'upload_kbps': p['upload_kbps'], 'rtt_ms': p['rtt_ms']},
                               service_workers=sw_mode(self.plan, cache), mobile=p.get('mobile', False))
        page.add_init_script(self.init)
        return page

    def one(self, target: dict, profile: str, cache: str, run_id: str, kind: str, diag: bool = False) -> dict:
        page = self.page(profile, cache)
        log = self.h.log(page)
        reqs: dict[str, dict] = {}
        rec = {'id': run_id, 'url': target['url'], 'template': target['template'], 'profile': profile, 'cache': cache,
               'browser': self.browser, 'kind': kind, 'ok': False, 'status': None, 'error': None, 'started_at': now()}
        cdp = None
        try:
            if self.browser == 'chromium':
                cdp = self.h.cdp(page)
                cdp.send('Network.enable')
                cdp.on('Network.responseReceived', lambda e: reqs.setdefault(e['requestId'], {}).update(
                    url=e['response']['url'], type=e.get('type', ''), mime=e['response'].get('mimeType', ''),
                    from_cache=e['response'].get('fromDiskCache', False) or e['response'].get('fromServiceWorker', False)))
                cdp.on('Network.loadingFinished', lambda e: reqs.setdefault(e['requestId'], {}).update(bytes=e['encodedDataLength']))
                if diag:
                    cdp.send('Profiler.enable')
                    cdp.send('Profiler.startPreciseCoverage', {'callCount': True, 'detailed': True})
            if cache == 'warm':
                self.h.throttle()
                page.goto(target['url'], wait_until='load', timeout=60000)
                page.wait_for_timeout(500)
                reqs.clear()
            if diag:
                self.h.start_trace(page, screenshots=True, categories=TRACE_CATEGORIES)
            self.h.throttle()
            t0 = time.monotonic()
            resp = page.goto(target['url'], wait_until='load', timeout=90000)
            rec['status'] = resp.status if resp else None
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:  # noqa: BLE001
                self.h.events.append({'url': target['url'], 'event': f'{run_id}: network not idle after 15 s', 'at': now()})
            page.wait_for_timeout(1500)  # late layout shifts and long tasks after load
            snap = page.evaluate(SNAPSHOT)
            rec['wall_ms'] = round((time.monotonic() - t0) * 1000)
            interaction = target.get('interaction')
            inp = None
            inp_source = None
            if interaction:
                self.h.act(page, interaction)
                for _ in range(30):   # web-vitals reports INP once the browser is idle; give it up to 3 s
                    page.wait_for_timeout(100)
                    inp = page.evaluate('() => (window.__perf || {}).vitals.INP || null')
                    if inp:
                        inp_source = 'web-vitals onINP'
                        break
                if not inp:
                    ev = page.evaluate('() => (window.__perf || {}).events || []')
                    if ev:   # Event Timing directly: the longest interaction (with few interactions INP is the maximum)
                        worst = max(ev, key=lambda e: e['duration'])
                        inp = {'value': worst['duration'], 'attribution': {'interactionTarget': worst['target'], 'interactionType': worst['name'],
                                                                           'inputDelay': worst['inputDelay'], 'processingDuration': worst['processing']}}
                        inp_source = 'Event Timing (web-vitals had not reported)'
            trace_bytes = self.h.stop_trace() if diag else None
            coverage = cdp.send('Profiler.takePreciseCoverage')['result'] if diag and cdp else None
            v = snap['vitals']
            chromium = self.browser == 'chromium'
            fcp = (v.get('FCP') or {}).get('value') if chromium else snap['paint'].get('first-contentful-paint')
            ttfb = (v.get('TTFB') or {}).get('value') if chromium else (snap['nav'] or {}).get('responseStart')
            sources = {'FCP': 'web-vitals onFCP' if chromium else 'Paint Timing', 'TTFB': 'web-vitals onTTFB' if chromium else 'Navigation Timing',
                       'LCP': 'web-vitals onLCP' if chromium else 'not_supported', 'CLS': 'web-vitals onCLS' if chromium else 'not_supported',
                       'INP': inp_source if chromium and interaction else ('untested: no interaction' if chromium else 'not_supported'),
                       'TBT': 'Long Tasks API after FCP' if chromium else 'not_supported',
                       'transfer_total': 'DevTools Network encodedDataLength' if chromium else 'Resource Timing transferSize',
                       'transfer_script': 'DevTools Network encodedDataLength' if chromium else 'Resource Timing transferSize'}
            metrics = {'FCP': fcp, 'TTFB': ttfb,
                       'LCP': (v.get('LCP') or {}).get('value') if chromium else None,
                       'CLS': (v.get('CLS') or {}).get('value', 0.0) if chromium else None,
                       'INP': (inp or {}).get('value') if chromium and interaction else None,
                       'TBT': tbt(snap['longtasks'], fcp, snap['now']) if chromium else None}
            if chromium:
                resources = [{'url': r['url'], 'type': classify(r.get('type', ''), r.get('mime', ''), r['url']),
                              'bytes': r.get('bytes', 0), 'from_cache': r.get('from_cache', False)}
                             for r in reqs.values() if r.get('url') and not r['url'].startswith('data:')]
            else:
                resources = [{'url': r['url'], 'type': classify(r['initiator'] if r['initiator'] in ('script', 'img', 'css', 'link') else '', '',
                                                                r['url']), 'bytes': r['transfer'], 'from_cache': r['transfer'] == 0}
                             for r in snap['resources']]
                resources.append({'url': target['url'], 'type': 'document', 'bytes': (snap['nav'] or {}).get('transfer', 0), 'from_cache': False})
            by_type: dict[str, int] = {}
            for r in resources:
                by_type[r['type']] = by_type.get(r['type'], 0) + int(r['bytes'] or 0)
            metrics['transfer_total'] = sum(by_type.values())
            metrics['transfer_script'] = by_type.get('script', 0)
            metrics = {k: (None if v is None else round(v, 4) if k == 'CLS' else round(v, 1) if isinstance(v, float) else v)
                       for k, v in metrics.items()}
            origin = '{0.scheme}://{0.netloc}'.format(urlparse(target['url']))
            lcp_attr = (v.get('LCP') or {}).get('attribution', {})
            rec.update(ok=bool(resp and resp.status < 400), metrics=metrics, metric_sources=sources, transfer_by_type=by_type,
                       attribution={
                           'LCP': dict(lcp_attr, element=page.evaluate(LCP_ELEMENT, lcp_attr.get('target')) if lcp_attr.get('target') else None),
                           'CLS': dict((v.get('CLS') or {}).get('attribution', {}),
                                       shifts=[s for s in snap['shifts'] if s['value'] >= 0.001]),
                           'INP': (inp or {}).get('attribution') if inp else None,
                           'long_tasks': [{'start': round(t['start']), 'duration': round(t['duration'])} for t in snap['longtasks']],
                           'loaf_scripts': [dict(s, frame_start=round(f['start']), frame_blocking=round(f['blocking'] or 0))
                                            for f in snap['loaf'] for s in f['scripts'] if s['duration'] >= 30],
                           'render_blocking': [{'url': r['url'], 'duration': round(r['duration'])} for r in snap['resources']
                                               if r.get('blocking') == 'blocking'],
                       },
                       resources=sorted(({**r, 'third_party': not r['url'].startswith(origin)} for r in resources),
                                        key=lambda r: -int(r['bytes'] or 0))[:12],
                       server_timing=(snap['nav'] or {}).get('serverTiming', []),
                       throttling={'applied': {k: v for k, v in log['applied'].items() if v is not None},
                                   'not_applied': log['not_applied']},
                       console_errors=[c['text'][:200] for c in log['console'] if c['type'] == 'error'][:5],
                       page_errors=[e['message'][:200] for e in log['page_errors']][:5])
            if diag and self.art:
                d = self.art / slug(target['url']) / f'{profile}-{cache}-{self.browser}'
                d.mkdir(parents=True, exist_ok=True)
                rec['artifacts'] = {}
                if trace_bytes:
                    frames = filmstrip(trace_bytes, d / 'filmstrip', (snap['nav'] or {}).get('load', 0))
                    rec['artifacts']['filmstrip'] = [f.as_posix() for f in frames]
                    (d / 'trace.json.gz').write_bytes(gzip.compress(slim_trace(trace_bytes), 6))
                    rec['artifacts']['trace'] = (d / 'trace.json.gz').as_posix()
                if coverage is not None:
                    cov = []
                    for s in coverage:
                        if not s['url'] or not s['url'].startswith('http'):
                            continue
                        used, total = disjoint_used(s['functions'])
                        cov.append({'url': s['url'], 'total_bytes': total, 'used_bytes': used, 'unused_bytes': total - used})
                    (d / 'coverage.json').write_text(json.dumps(cov, indent=1) + '\n', encoding='utf-8')
                    rec['artifacts']['coverage'] = (d / 'coverage.json').as_posix()
                    rec['coverage'] = cov
        except Exception as e:  # noqa: BLE001
            rec['error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:300]}'
            try:
                if diag:
                    self.h.stop_trace()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.h.close_page(page)
        return rounded(rec)


def rounded(o):
    if isinstance(o, float):
        return round(o, 4) if abs(o) < 1 else round(o, 1)
    if isinstance(o, dict):
        return {k: rounded(v) for k, v in o.items()}
    if isinstance(o, list):
        return [rounded(v) for v in o]
    return o


def slug(url: str) -> str:
    u = urlparse(url)
    s = (u.path.strip('/') or 'home').replace('/', '-')
    return s + (('-' + u.query.replace('=', '-').replace('&', '-')) if u.query else '')


def slim_trace(trace: bytes) -> bytes:
    """Drop the screenshots (kept as the filmstrip) and tasks under 1 ms; the trace still opens in DevTools."""
    try:
        doc = json.loads(trace.decode('utf-8'))
    except ValueError:
        return trace
    events = doc.get('traceEvents', doc) if isinstance(doc, dict) else doc
    kept = [e for e in events if e.get('name') != 'Screenshot' and not (e.get('name') == 'RunTask' and e.get('dur', 1000) < 1000)]
    if isinstance(doc, dict):
        doc['traceEvents'] = kept
        return json.dumps(doc).encode()
    return json.dumps(kept).encode()


def filmstrip(trace: bytes, out: Path, load_ms: float, step_ms: int = 100) -> list[Path]:
    """Frames from the trace's screenshots, one per 100 ms until load (plus one after)."""
    try:
        events = json.loads(trace.decode('utf-8'))
        events = events.get('traceEvents', events) if isinstance(events, dict) else events
    except ValueError:
        return []
    nav = next((e['ts'] for e in events if e.get('name') in ('navigationStart', 'NavigationStart') or
                (e.get('name') == 'TracingStartedInBrowser')), None)
    shots = sorted((e['ts'], e['args']['snapshot']) for e in events if e.get('name') == 'Screenshot' and 'snapshot' in e.get('args', {}))
    if not shots:
        return []
    t0 = nav if nav is not None else shots[0][0]
    out.mkdir(parents=True, exist_ok=True)
    frames, next_ms = [], 0
    limit = (load_ms or 0) + step_ms
    for ts, data in shots:
        ms = (ts - t0) / 1000
        if ms < next_ms:
            continue
        if ms > limit and frames:
            break
        p = out / f'{int(ms):05d}ms.jpg'
        p.write_bytes(base64.b64decode(data))
        frames.append(p)
        next_ms = ms + step_ms
    return frames


def cv(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    m = statistics.mean(vals)
    return round(statistics.stdev(vals) / m, 4) if m else 0.0


def sw_mode(plan: dict, cache: str) -> str:
    mode = plan.get('service_workers', 'cold')
    if mode == 'cold':
        return 'block' if cache == 'cold' else 'allow'
    return mode


def budgeted_metrics(budget, profile: str, target: dict) -> list[str]:
    return [m for m in budget.for_(target['url'], profile) if m != 'INP' or target.get('interaction')]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plan', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--artifacts', type=Path)
    ap.add_argument('--runs', type=int)
    ap.add_argument('--profiles')
    ap.add_argument('--cache', choices=['cold', 'warm', 'both'])
    ap.add_argument('--browser', default='chromium', choices=['chromium', 'firefox', 'webkit'])
    ap.add_argument('--only', help='measure only targets whose URL contains this')
    ap.add_argument('--templates', help='measure only these templates (comma-separated)')
    ap.add_argument('--append', action='store_true', help='add to an existing measurements file')
    ap.add_argument('--no-diagnostic', action='store_true')
    a = ap.parse_args()
    plan = json.loads(a.plan.read_text(encoding='utf-8'))
    all_profiles = json.loads((SKILL / 'data' / 'profiles.json').read_text(encoding='utf-8'))
    profile_names = (a.profiles or ','.join(plan.get('profiles') or ['mobile-mid', 'desktop'])).split(',')
    runs = a.runs or plan.get('runs', 5)
    cache_modes = {'both': ['cold', 'warm']}.get(a.cache or plan.get('cache', 'cold'), [a.cache or plan.get('cache', 'cold')])
    targets = [t for t in plan['targets'] if (not a.only or a.only in t['url']) and
               (not a.templates or t['template'] in a.templates.split(','))]
    try:
        check_authorised([t['url'] for t in targets], plan.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    from attribute import Budget, UNITS, is_stable, stats
    if plan.get('third_party', 'block') not in ('block', 'allow') or plan.get('service_workers', 'cold') not in ('cold', 'block', 'allow'):
        print('error: third_party must be block or allow; service_workers cold, block or allow', file=sys.stderr)
        return 2
    budget = Budget((a.plan.parent / plan['budget']) if plan.get('budget') else None)
    lock = load_lock()['assets'][0]
    wv_path = asset_path('web-vitals')
    wv = wv_path.read_text(encoding='utf-8')
    doc = json.loads(a.out.read_text(encoding='utf-8')) if a.append and a.out.exists() else None
    cfg = {'browser': a.browser, 'authorised_by': plan.get('authorised_by'), 'allow_form_submission': plan.get('allow_form_submission', False),
           'auth': plan.get('auth'), 'rate_limit_seconds': 0.2, 'third_party': plan.get('third_party', 'block'),
           'allowed_hosts': plan.get('allowed_hosts') or [], 'targets': [t['url'] for t in plan['targets']]}
    with Harness(cfg) as h:
        if doc is None:
            cal_page = h.new_page()
            cal = [round(cal_page.evaluate(CALIBRATE)[0], 1) for _ in range(3)]
            h.close_page(cal_page)
            doc = {'schema': 1, 'generated_at': now(), 'machine': machine(),
                   'calibration': {'workload': '2e7 iterations of Math.sqrt, no throttling', 'runs_ms': cal,
                                   'median_ms': statistics.median(cal), 'cv': cv(cal), 'busy': cv(cal) > 0.1},
                   'tools': [], 'config': {'runs': runs, 'warmup': 1, 'cv_limit': CV_LIMIT, 'max_runs': MAX_RUNS,
                                           'profiles': {p: all_profiles[p] for p in profile_names}, 'cache': cache_modes,
                                           'third_party': {'mode': cfg['third_party'], 'allowed_hosts': cfg['allowed_hosts']},
                                           'service_workers': plan.get('service_workers', 'cold')},
                   'sample': [{k: t.get(k) for k in ('url', 'template', 'reason', 'critical_path')} | {'interaction': bool(t.get('interaction'))}
                              for t in plan['targets']],
                   'runs': [], 'events': [], 'blocked_requests': []}
            if doc['calibration']['busy']:
                print(f'warning: calibration CV {doc["calibration"]["cv"]} > 0.1; the machine looks busy', file=sys.stderr)
        doc['tools'] = [t for t in doc['tools'] if t['name'] not in (f'playwright-{a.browser}', 'web-vitals')]
        doc['tools'] += [{'name': f'playwright-{a.browser}', 'version': h.version, 'sha256': None, 'status': 'ran', 'note': None},
                         {'name': 'web-vitals', 'version': lock['version'], 'sha256': lock['sha256'], 'status': 'ran',
                          'note': 'attribution IIFE build, injected before page scripts'}]
        for p in profile_names:
            doc['config']['profiles'][p] = all_profiles[p]
        r = Runner(h, plan, all_profiles, wv, a.artifacts, a.browser)
        for t in targets:
            for prof in profile_names:
                for cache in cache_modes:
                    base = f'{slug(t["url"])}/{prof}/{cache}/{a.browser}'
                    doc['runs'] = [x for x in doc['runs'] if not x['id'].startswith(base + '/')]
                    doc['runs'].append(r.one(t, prof, cache, f'{base}/w', 'warmup'))
                    measured = []
                    for i in range(1, runs + 1):
                        rec = r.one(t, prof, cache, f'{base}/r{i}', 'measured')
                        doc['runs'].append(rec)
                        measured.append(rec)
                    names = budgeted_metrics(budget, prof, t)
                    while len(measured) < MAX_RUNS:
                        limits = budget.for_(t['url'], prof)
                        unstable = [m for m in names if not is_stable(stats([x.get('metrics', {}).get(m) for x in measured if x['ok']]),
                                                                      UNITS[m], limits.get(m))]
                        if not unstable:
                            break
                        rec = r.one(t, prof, cache, f'{base}/r{len(measured) + 1}', 'extra')
                        rec['extra_reason'] = f'unstable (CV above {CV_LIMIT}, spread above the floor) for {", ".join(unstable)}'
                        doc['runs'].append(rec)
                        measured.append(rec)
                    if not a.no_diagnostic:
                        doc['runs'].append(r.one(t, prof, cache, f'{base}/diag', 'diagnostic', diag=True))
                    oks = [x for x in measured if x['ok']]
                    print(f'{base}: {len(oks)}/{len(measured)} ok; LCP ' +
                          ', '.join(str(round(x['metrics']['LCP'])) if x['metrics'].get('LCP') is not None else '-' for x in oks),
                          file=sys.stderr)
        doc['events'] += h.events
        doc['blocked_requests'] += h.blocked
    doc['generated_at'] = now()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.artifacts:
        rel = lambda s: os.path.relpath(s, a.out.parent).replace(os.sep, '/')  # noqa: E731
        for x in doc['runs']:
            if 'artifacts' in x:
                arts = x['artifacts']
                for k in ('trace', 'coverage'):
                    if arts.get(k) and not arts[k].startswith('artifacts/'):
                        arts[k] = rel(arts[k])
                arts['filmstrip'] = [f if f.startswith('artifacts/') else rel(f) for f in arts.get('filmstrip', [])]
    a.out.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'{len(doc["runs"])} run(s) -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
