#!/usr/bin/env python3
"""Turn measured runs into statistics, budget statuses, Server-Timing summaries and rated candidates (stdlib).

    python attribute.py run/measurements.json --out run/candidates.json [--budget budget.json]

Statistics per (url, profile, cache, browser, metric), over the measured and extra runs that loaded
(warm-up and diagnostic runs never count): n, median, min, max and coefficient of variation
(sample standard deviation / mean). Status:

  within          median <= budget, stable
  over            median > budget, stable
  unstable        not stable within the run cap (max_runs, 10 unless the plan sets it): can't pass or
                  fail the budget on its own; "unstable at the cap of N runs" in its note when the
                  cap was reached
  measured        no budget for this metric
  untested        INP on a page without a scripted interaction
  not_supported   LCP, CLS, INP or TBT in Firefox or WebKit

Stable means CV <= 0.25, or a spread too small to matter for the budget: max - min within a floor
of 10 ms, 0.01 CLS or 1 KB (the floor compare.py uses), or every run at or under half the budget.
Near-zero metrics (TBT of 17 ms, CLS of 0.003) have large CVs that say nothing about the budget.
Last, with five or more runs and a budget, a measurement is stable `robust` when at most a quarter
of its runs (at least one) are isolated outliers (more than 3 robust standard deviations from the
median), the rest have a CV within 0.25, and their median is on the same side of the budget as the
median of all runs: two slow network round trips on a remote target can't then block a verdict the
median already settles. The outliers' run ids are listed (`outliers`), and `stable_by` names the rule.
Each measurement records `runs_used` and `run_cap`; a group stopped early (an HTTP error page) says
why in its note, and failed runs are listed with their HTTP status or error.

`bimodal` is set when the sorted values split into two groups of at least two runs with a gap more
than three times the larger group's own range: report the groups instead of trusting the median.

Candidates: one per (metric, cause) for every measurement that is over, or unstable with its median
over the budget. A specific cause (a resource URL, a Server-Timing step, an element with an id or
class) shared by several templates is one site-wide candidate (`site_wide`, `templates`, `template`
null) whose reach counts every page; a generic cause (an unattributed long task, weight spread over
many resources) stays per template. The cause comes from the runs: the LCP element and its resource with
the web-vitals breakdown; the largest layout shift's target and sources; the scripts in long
animation frames; the interaction target and its handler; the largest downloads and, from the
diagnostic run, unused JavaScript bytes; Server-Timing for TTFB. Ratings follow RUBRIC.md.

The budget is data/default-budget.json unless --budget is given: either this skill's format
({"profiles": {name: {metric: value}}}) or a Lighthouse budget.json (a list of {path, timings,
resourceSizes}; sizes in KB), whose entries apply to every profile. The budget's recorded source
is the file's own "source" field when it has one (so a copy of the defaults says what it is), else
its file name; the output also records the budget file's path, which validate_report.py reads when
it isn't given --budget.

With the measurements' config recording third_party (measure.py does), the output summarises the
requests the harness blocked to third-party hosts: their cost and behaviour were not measured.
It also summarises, when the runs recorded them: first-party requests the harness blocked
(`blocked_first_party`: non-GET beacons, CSP reports, telemetry), the CDN cache headers of each
measured document (`cdn_cache`), and request classes the top-12 resource list hides
(`request_summary`: Next.js `?_rsc=` prefetches, fetch/XHR counts, fetch paths repeated in a load).
"""
from __future__ import annotations
import argparse, fnmatch, json, os, statistics, sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

SKILL = Path(__file__).resolve().parent.parent
METRICS = ['LCP', 'CLS', 'INP', 'TBT', 'FCP', 'TTFB', 'transfer_total', 'transfer_script']
UNITS = {'LCP': 'ms', 'CLS': 'score', 'INP': 'ms', 'TBT': 'ms', 'FCP': 'ms', 'TTFB': 'ms', 'transfer_total': 'bytes',
         'transfer_script': 'bytes'}
CHROMIUM_ONLY = {'LCP', 'CLS', 'INP', 'TBT'}
CV_LIMIT = 0.25
SPREAD_FLOOR = {'ms': 10.0, 'score': 0.01, 'bytes': 1024.0}  # as compare.py
FAR_UNDER = 0.5  # every run at or under half the budget: noise can't change the verdict
ROBUST_MIN_RUNS = 5
DEFAULT_MAX_RUNS = 10
CDN_HEADERS = ('x-vercel-cache', 'cf-cache-status', 'x-cache', 'x-cache-status', 'cdn-cache-control', 'age', 'x-served-by',
               'x-vercel-id', 'cf-ray', 'x-amz-cf-pop', 'via')
CACHE_STATUS_HEADERS = ('x-vercel-cache', 'cf-cache-status', 'x-cache', 'x-cache-status')
GENERIC_KEYS = {'unknown', 'weight-spread', 'fcp-unattributed', 'unattributed-long-tasks'}
WEIGHT = {'Critical': 4, 'High': 3, 'Medium': 2, 'Low': 1}
LEVELS = ['Low', 'Medium', 'High', 'Critical']
LH_TIMINGS = {'largest-contentful-paint': 'LCP', 'cumulative-layout-shift': 'CLS', 'total-blocking-time': 'TBT',
              'first-contentful-paint': 'FCP', 'interaction-to-next-paint': 'INP', 'server-response-time': 'TTFB'}
LH_SIZES = {'total': 'transfer_total', 'script': 'transfer_script'}


# ------------------------------------------------------------------ budget
class Budget:
    def __init__(self, path: Path | None):
        if path is None:
            path = SKILL / 'data' / 'default-budget.json'
        self.path = path
        self.builtin = builtin = path.resolve() == (SKILL / 'data' / 'default-budget.json').resolve()
        raw = json.loads(path.read_text(encoding='utf-8'))
        self.profiles = raw.get('profiles') if isinstance(raw, dict) else None
        self.lighthouse = raw if isinstance(raw, list) else (raw.get('lighthouse') if isinstance(raw, dict) else None)
        own = raw.get('source') if isinstance(raw, dict) else None
        label = 'data/default-budget.json (built-in defaults)' if builtin else path.name
        self.source = own or label
        self.note = label if own else ('Lighthouse budget.json' if isinstance(raw, list) else None)

    def for_(self, url: str, profile: str) -> dict:
        out = dict((self.profiles or {}).get(profile, {}))
        for entry in self.lighthouse or []:
            if fnmatch.fnmatch(urlparse(url).path or '/', entry.get('path', '/*')):
                for t in entry.get('timings') or []:
                    if t.get('metric') in LH_TIMINGS:
                        out[LH_TIMINGS[t['metric']]] = t['budget']
                for r in entry.get('resourceSizes') or []:
                    if r.get('resourceType') in LH_SIZES:
                        out[LH_SIZES[r['resourceType']]] = r['budget'] * 1024
        return out


# ------------------------------------------------------------------ statistics
def stats(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {'n': 0, 'median': None, 'min': None, 'max': None, 'cv': None}
    m = statistics.mean(vals)
    cv = round(statistics.stdev(vals) / m, 4) if len(vals) > 1 and m else 0.0
    med = statistics.median(vals)
    return {'n': len(vals), 'median': round(med, 4) if isinstance(med, float) else med, 'min': min(vals), 'max': max(vals), 'cv': cv}


def is_stable(st: dict, unit: str, limit: float | None) -> bool:
    """CV within the limit, or a spread that can't matter for the budget (see the module docstring)."""
    return stability(st, None, unit, limit)[0]


def robust_outliers(values: list[float], unit: str, limit: float | None) -> list[int] | None:
    """Indexes of isolated outliers when, without them, the runs are stable and the verdict is the same; else None.

    Needs at least ROBUST_MIN_RUNS runs and a budget. An outlier is further than 3 robust standard
    deviations (1.4826 x the median absolute deviation, at least the spread floor) from the median;
    at most a quarter of the runs (at least one) may be outliers. The remaining runs must have a CV
    within the limit, and their median must fall on the same side of the budget as the median of
    all runs: two network blips can't then hide or invent an overrun.
    """
    vals = [(i, v) for i, v in enumerate(values) if v is not None]
    if limit is None or len(vals) < ROBUST_MIN_RUNS:
        return None
    xs = [v for _, v in vals]
    med = statistics.median(xs)
    scale = max(1.4826 * statistics.median(abs(x - med) for x in xs), SPREAD_FLOOR[unit])
    out = [i for i, v in vals if abs(v - med) > 3 * scale]
    if not out or len(out) > max(1, len(vals) // 4):
        return None
    rest = [v for i, v in vals if i not in out]
    m = statistics.mean(rest)
    if len(rest) < 2 or (m and statistics.stdev(rest) / m > CV_LIMIT):
        return None
    if (statistics.median(rest) > limit) != (med > limit):
        return None
    return out


def stability(st: dict, values: list[float] | None, unit: str, limit: float | None) -> tuple[bool, str | None, list[int]]:
    """(stable, by which rule, outlier indexes into values). Rules in order: cv, spread_floor, far_under, robust."""
    if st['n'] == 0 or st['cv'] is None:
        return True, None, []
    if st['cv'] <= CV_LIMIT:
        return True, 'cv', []
    if st['max'] - st['min'] <= SPREAD_FLOOR[unit]:
        return True, 'spread_floor', []
    if limit is not None and st['max'] <= FAR_UNDER * limit:
        return True, 'far_under', []
    out = robust_outliers(values, unit, limit) if values is not None else None
    if out:
        return True, 'robust', out
    return False, None, []


def bimodal(values: list[float]) -> bool:
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 6:
        return False
    gaps = [(vals[i + 1] - vals[i], i) for i in range(len(vals) - 1)]
    gap, i = max(gaps)
    lo, hi = vals[:i + 1], vals[i + 1:]
    if len(lo) < 2 or len(hi) < 2:
        return False
    return gap > 3 * max(lo[-1] - lo[0], hi[-1] - hi[0], 1e-9)


def counted(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r['kind'] in ('measured', 'extra') and r['ok']]


def group_key(r: dict) -> tuple:
    return (r['url'], r['profile'], r['cache'], r['browser'], r.get('variant'))


def groups(doc: dict) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for r in doc['runs']:
        out.setdefault(group_key(r), []).append(r)
    return out


def run_limits(doc: dict) -> tuple[int, int, int]:
    """(runs, min_runs, max_runs) from the measurements' config; older runs recorded only runs (cap 10)."""
    cfg = doc['config']
    runs = cfg['runs']
    return runs, cfg.get('min_runs', runs), cfg.get('max_runs', DEFAULT_MAX_RUNS)


def failure_label(r: dict) -> str:
    if r.get('status') and r['status'] >= 400:
        return f'{r["id"]} (HTTP {r["status"]})'
    return f'{r["id"]} ({r["error"]})' if r.get('error') else r['id']


def measurements(doc: dict, budget: Budget) -> list[dict]:
    sample = {s['url']: s for s in doc['sample']}
    _, min_runs, cap = run_limits(doc)
    out = []
    for (url, profile, cache, browser, variant), runs in groups(doc).items():
        ok = counted(runs)
        planned = [r for r in runs if r['kind'] in ('measured', 'extra')]
        b = budget.for_(url, profile)
        notes = []
        stops = [r['stop_reason'] for r in runs if r.get('stop_reason')]
        if stops:
            notes.append('stopped early: ' + '; '.join(stops))
        elif len(planned) < min_runs:
            notes.append(f'{len(planned)} run(s) recorded, {min_runs} configured')
        failed = [failure_label(r) for r in planned if not r['ok']]
        if failed:
            notes.append(f'{len(failed)} run(s) failed: {", ".join(failed)}')
        group_note = '; '.join(notes) or None
        for metric in METRICS:
            vals = [r['metrics'].get(metric) if r.get('metrics') else None for r in ok]
            st = stats(vals)
            limit = b.get(metric)
            stable, by, outl = stability(st, vals, UNITS[metric], limit)
            note_parts = [group_note] if group_note else []
            if browser != 'chromium' and metric in CHROMIUM_ONLY:
                status = 'not_supported'
            elif metric == 'INP' and not sample.get(url, {}).get('interaction'):
                status = 'untested'
            elif st['n'] == 0:
                status = 'untested'
            elif limit is None:
                status = 'measured'
            elif not stable:
                status = 'unstable'
                if len(planned) >= cap:
                    note_parts.append(f'unstable at the cap of {cap} runs')
            elif st['median'] > limit:
                status = 'over'
            else:
                status = 'within'
            if metric == 'INP' and status not in ('not_supported',):
                broke = [f'{r["id"]} ({r["interaction_error"]})' for r in planned if r.get('interaction_error')]
                if broke:
                    note_parts.append(f'interaction failed in {len(broke)} run(s), load metrics kept: {", ".join(broke)}')
            measured_status = status not in ('not_supported', 'untested')
            outliers = [ok[i]['id'] for i in outl] if measured_status and by == 'robust' else []
            out.append({'url': url, 'template': sample.get(url, {}).get('template'), 'variant': variant, 'profile': profile,
                        'cache': cache, 'browser': browser, 'metric': metric, 'unit': UNITS[metric],
                        'runs': [{'run_id': r['id'], 'value': r['metrics'].get(metric)} for r in ok] if status not in ('not_supported',) else [],
                        **(st if measured_status else {'n': 0, 'median': None, 'min': None, 'max': None, 'cv': None}),
                        'budget': limit, 'status': status,
                        'stable_by': by if measured_status and status != 'measured' and stable else None,
                        'outliers': outliers, 'runs_used': len(planned), 'run_cap': cap,
                        'bimodal': bimodal(vals) if measured_status else False,
                        'source': (ok[0].get('metric_sources') or {}).get(metric) if ok else None,
                        'note': '; '.join(note_parts) or None})
    return out


def third_party(doc: dict) -> dict | None:
    """What the harness did with third-party requests, when the run recorded it: mode, allowed hosts, blocked hosts."""
    cfg = doc['config'].get('third_party')
    if cfg is None:
        return None
    hosts = Counter(urlparse(b['url']).hostname or b['url'] for b in doc.get('blocked_requests') or [] if b.get('reason') == 'third_party')
    return {'mode': cfg['mode'], 'allowed_hosts': cfg.get('allowed_hosts') or [],
            'blocked': [{'host': h, 'requests': n} for h, n in sorted(hosts.items(), key=lambda x: (-x[1], x[0]))]}


def blocked_first_party(doc: dict) -> list[dict]:
    """Requests to the site itself that the harness blocked (non-GET writes: beacons, CSP reports, telemetry; or a spent
    request budget). The harness marks each blocked entry same_site or third_party (`site`); older runs, without it,
    count every block whose reason isn't third_party."""
    def first_party(b: dict) -> bool:
        return b['site'] == 'same_site' if b.get('site') else b.get('reason') != 'third_party'
    by = Counter((b.get('method') or '?', b.get('reason') or '?', urlparse(b['url']).hostname or '', urlparse(b['url']).path or '/')
                 for b in doc.get('blocked_requests') or [] if first_party(b))
    return [{'method': m, 'reason': reason, 'host': h, 'path': p, 'requests': n}
            for (m, reason, h, p), n in sorted(by.items(), key=lambda x: (-x[1], x[0]))]


def cdn_cache(doc: dict) -> list[dict]:
    """Per page, profile and variant: the CDN cache headers the measured documents came back with (when any did)."""
    out = []
    for (url, profile, cache, browser, variant), runs in groups(doc).items():
        ok = counted(runs)
        heads = [r.get('document_headers') or {} for r in ok]
        if not any(heads):
            continue
        status = Counter(' '.join(f'{h}: {d[h]}' for h in CACHE_STATUS_HEADERS if h in d) or 'no cache-status header' for d in heads)
        ages = [int(d['age']) for d in heads if str(d.get('age', '')).isdigit()]
        out.append({'url': url, 'profile': profile, 'cache': cache, 'browser': browser, 'variant': variant, 'runs': len(ok),
                    'cache_status': [{'value': v, 'runs': n} for v, n in sorted(status.items(), key=lambda x: (-x[1], x[0]))],
                    'age_min': min(ages) if ages else None, 'age_max': max(ages) if ages else None})
    return out


def request_summary(doc: dict) -> list[dict]:
    """Per page, profile and variant: median counts and bytes of request classes the top-12 resource list hides
    (framework prefetches such as Next.js `?_rsc=`, fetch/XHR) and the fetch paths requested more than once per load."""
    out = []
    for (url, profile, cache, browser, variant), runs in groups(doc).items():
        ok = [r for r in counted(runs) if r.get('request_summary')]
        if not ok:
            continue
        classes: dict[str, dict] = {}
        for r in ok:
            for cls, v in r['request_summary']['classes'].items():
                c = classes.setdefault(cls, {'requests': [], 'bytes': []})
                c['requests'].append(v['requests'])
                c['bytes'].append(v['bytes'])
        repeated = Counter()
        for r in ok:
            for f in r['request_summary'].get('repeated') or []:
                repeated[f['path']] += 1
        out.append({'url': url, 'profile': profile, 'cache': cache, 'browser': browser, 'variant': variant, 'runs': len(ok),
                    'classes': {k: {'requests_median': med(v['requests'] + [0] * (len(ok) - len(v['requests']))),
                                    'bytes_median': med(v['bytes'] + [0] * (len(ok) - len(v['bytes'])))} for k, v in sorted(classes.items())},
                    'repeated_fetches': [{'path': p, 'runs': n} for p, n in sorted(repeated.items(), key=lambda x: (-x[1], x[0]))][:10]})
    return out


def server_timing(doc: dict) -> list[dict]:
    by: dict[str, dict] = {}
    for r in doc['runs']:
        if r['kind'] not in ('measured', 'extra') or not r['ok'] or not r.get('server_timing'):
            continue
        route = urlparse(r['url']).path or '/'
        g = by.setdefault(route, {'ttfb': [], 'entries': {}})
        g['ttfb'].append(r['metrics'].get('TTFB'))
        for e in r['server_timing']:
            g['entries'].setdefault(e['name'], {'durs': [], 'desc': e.get('desc') or None})['durs'].append(e['dur'])
    return [{'route': route, 'ttfb_median': stats(g['ttfb'])['median'], 'runs': len(g['ttfb']),
             'entries': [{'name': n, 'dur_median': stats(v['durs'])['median'], 'desc': v['desc']} for n, v in g['entries'].items()]}
            for route, g in sorted(by.items())]


# ------------------------------------------------------------------ attribution
def path_of(url: str) -> str:
    u = urlparse(url or '')
    return (u.path or '/') + (('?' + u.query) if u.query else '')


def med(xs) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 1) if xs else None


def overlapping_long_tasks(runs: list[dict]) -> list[dict]:
    """Long tasks running while the interaction waited for the main thread (from its time to the end of its input
    delay), from the first run that has any; the input delay's likely cause."""
    for r in runs:
        a = r.get('attribution') or {}
        x = a.get('INP') or {}
        t0, delay = x.get('interactionTime'), x.get('inputDelay')
        if t0 is None or delay is None:
            continue
        hits = [{'start': t['start'], 'duration': t['duration']} for t in a.get('long_tasks') or []
                if t['start'] < t0 + max(delay, 1) and t['start'] + t['duration'] > t0]
        if hits:
            return sorted(hits, key=lambda t: -t['duration'])[:3]
    return []


def attribute(metric: str, runs: list[dict], diag: dict | None) -> tuple[str, str, dict]:
    """(cause, cause_key, attribution) for one over-budget measurement."""
    a = [r['attribution'] for r in runs]
    if metric == 'LCP':
        lcp = [x['LCP'] for x in a if x.get('LCP')]
        target = Counter(x.get('target') for x in lcp).most_common(1)[0][0] if lcp else None
        url = Counter(x.get('url') for x in lcp if x.get('url')).most_common(1)[0][0] if any(x.get('url') for x in lcp) else None
        el = next((x.get('element') for x in lcp if x.get('element')), None) or {}
        attr = {'element': target, 'resource': url, 'resource_bytes': next((x.get('resourceTransferSize') for x in lcp if x.get('resourceTransferSize')), None),
                'breakdown_ms': {k: med(x.get(k) for x in lcp) for k in ('timeToFirstByte', 'resourceLoadDelay', 'resourceLoadDuration', 'elementRenderDelay')},
                'fetchpriority': el.get('fetchpriority'), 'has_size_attributes': bool(el.get('width_attr') and el.get('height_attr')),
                'loading': el.get('loading'), 'natural_size': el.get('natural'), 'rendered_size': el.get('rendered'),
                'render_blocking': a[0].get('render_blocking', [])}
        if url:
            kb = round((attr['resource_bytes'] or 0) / 1024)
            if el.get('background_image'):
                # a CSS background is found only once the stylesheet that names it has loaded and applied: unless a
                # preload names it, its request starts late (the resource load delay shows how late)
                attr['css_background'] = True
                attr['preloaded'] = el.get('preloaded')
                delay = attr['breakdown_ms']['resourceLoadDelay']
                how = 'preloaded' if el.get('preloaded') else 'not preloaded, so it is found only once the stylesheet applies'
                return (f'LCP image {path_of(url)} ({kb} KB), a CSS background {how}' +
                        (f' ({delay} ms resource load delay)' if delay is not None else ''), url, attr)
            return f'LCP image {path_of(url)} ({kb} KB)', url, attr
        hints = []
        els = [x.get('element') for x in lcp if x.get('element')]
        if els and sum(bool(e.get('in_fixed_overlay')) for e in els) * 2 > len(els):
            attr['in_fixed_overlay'] = True
            hints.append('inside a position: fixed overlay (a consent banner or modal?)')
        late = [x.get('after_load') for x in lcp if x.get('after_load') is not None]
        if late and sum(late) * 2 > len(late):
            attr['after_load'] = True
            hints.append('painted more than 500 ms after the load event with no resource (rendered by script, after hydration or a timer)')
        return (f'LCP element {target} (render delay {attr["breakdown_ms"]["elementRenderDelay"]} ms)' +
                (': ' + '; '.join(hints) if hints else ''), target or 'unknown', attr)
    if metric == 'CLS':
        cls = [x['CLS'] for x in a if x.get('CLS')]
        target = Counter(x.get('largestShiftTarget') for x in cls).most_common(1)[0][0] if cls else None
        shifts = max((x.get('shifts') or [] for x in cls), key=lambda s: sum(i['value'] for i in s), default=[])
        attr = {'largest_shift_target': target, 'largest_shift_value': med(x.get('largestShiftValue') for x in cls),
                'largest_shift_time_ms': med(x.get('largestShiftTime') for x in cls), 'shifts': shifts}
        return f'layout shift moving {target}', target or 'unknown', attr
    if metric == 'TBT':
        per: dict[str, list[float]] = {}
        for x in a:
            seen: dict[str, float] = {}
            for s in x.get('loaf_scripts') or []:
                seen[s['url']] = seen.get(s['url'], 0) + s['duration']
            for u, d in seen.items():
                per.setdefault(u, []).append(d)
        scripts = sorted(({'url': u, 'script_ms_median': med(d), 'runs': len(d)} for u, d in per.items()), key=lambda s: -s['script_ms_median'])
        attr = {'scripts': scripts, 'long_tasks_ms': [t['duration'] for t in a[0].get('long_tasks') or []]}
        if scripts:
            return f'long tasks in {path_of(scripts[0]["url"])} ({scripts[0]["script_ms_median"]} ms of script)', scripts[0]['url'], attr
        return 'long tasks without script attribution', 'unattributed-long-tasks', attr
    if metric == 'INP':
        inp = [x['INP'] for x in a if x.get('INP')]
        target = Counter(x.get('interactionTarget') for x in inp).most_common(1)[0][0] if inp else None
        ls = next((x.get('longestScript') for x in inp if x.get('longestScript')), None) or {}
        attr = {'interaction_target': target, 'interaction_type': next((x.get('interactionType') for x in inp), None),
                'input_delay_ms': med(x.get('inputDelay') for x in inp), 'processing_ms': med(x.get('processingDuration') for x in inp),
                'presentation_delay_ms': med(x.get('presentationDelay') for x in inp),
                'longest_script': {'source': ls.get('sourceURL'), 'invoker': ls.get('invoker'), 'subpart': ls.get('subpart')} if ls else None,
                'style_layout_ms': med(x.get('totalStyleAndLayoutDuration') for x in inp)}
        # name the largest of the three INP subparts: an interaction that waits 350 ms for a busy main thread and then
        # runs a 10 ms handler is an input-delay problem, and the handler is not where the time went
        parts = {k: attr[k + '_ms'] for k in ('input_delay', 'processing', 'presentation_delay') if attr[k + '_ms'] is not None}
        attr['dominant_subpart'] = max(parts, key=parts.get) if parts else None
        attr['overlapping_long_tasks'] = overlapping_long_tasks(runs)
        what = f'{attr["interaction_type"] or "interaction"} on {target}'
        if attr['dominant_subpart'] == 'input_delay':
            lt = attr['overlapping_long_tasks']
            blame = []
            if lt:
                blame.append(f'a {lt[0]["duration"]} ms long task from {lt[0]["start"]} ms overlaps it')
            if ls.get('subpart') == 'input-delay' and ls.get('invoker'):
                blame.append(f'longest script {ls["invoker"]}')
            if attr['style_layout_ms']:
                blame.append(f'{attr["style_layout_ms"]} ms of style and layout in the frame')
            return (f'{attr["input_delay_ms"]} ms input delay before {what}: main thread busy' +
                    (f' ({"; ".join(blame)})' if blame else ''), f'interaction:{target}', attr)
        if attr['dominant_subpart'] == 'presentation_delay':
            return (f'{attr["presentation_delay_ms"]} ms presentation delay after {what}: rendering the next frame' +
                    (f' ({attr["style_layout_ms"]} ms of style and layout)' if attr['style_layout_ms'] else ''),
                    f'interaction:{target}', attr)
        return f'{what}: {attr["processing_ms"]} ms processing' + \
            (f' in {ls.get("invoker")}' if ls.get('invoker') else ''), f'interaction:{target}', attr
    if metric in ('transfer_total', 'transfer_script'):
        res = runs[0].get('resources') or []
        if metric == 'transfer_script':
            res = [r for r in res if r['type'] == 'script']
        cov = {c['url']: c for c in (diag or {}).get('coverage') or []}
        top = [{'url': r['url'], 'type': r['type'], 'bytes': r['bytes'], 'third_party': r.get('third_party', False),
                'unused_bytes': (cov.get(r['url']) or {}).get('unused_bytes')} for r in res[:5]]
        attr = {'largest': top, 'by_type_bytes': runs[0].get('transfer_by_type')}
        if runs[0].get('request_summary'):
            attr['request_classes'] = runs[0]['request_summary']['classes']
        if top:
            t = top[0]
            extra = f', {round(t["unused_bytes"] / 1024)} KB unused' if t.get('unused_bytes') else ''
            return f'largest download {path_of(t["url"])} ({round(t["bytes"] / 1024)} KB {t["type"]}{extra})', t['url'], attr
        return 'no single large resource', 'weight-spread', attr
    if metric == 'TTFB':
        st = [e for r in runs for e in r.get('server_timing') or []]
        by: dict[str, list[float]] = {}
        for e in st:
            by.setdefault(e['name'], []).append(e['dur'])
        entries = sorted(({'name': n, 'dur_median': med(d)} for n, d in by.items()), key=lambda e: -(e['dur_median'] or 0))
        attr = {'server_timing': entries}
        route = path_of(runs[0]['url'])
        if entries:
            desc = next((e.get('desc') for e in st if e['name'] == entries[0]['name'] and e.get('desc')), None)
            return (f'server time: {entries[0]["name"]} {entries[0]["dur_median"]} ms' + (f' ({desc})' if desc else ''),
                    f'server:{entries[0]["name"]}', attr)
        return f'slow response on {route} (no Server-Timing header)', f'server:{route}', attr
    if metric == 'FCP':
        rb = a[0].get('render_blocking') or []
        attr = {'render_blocking': rb}
        if rb:
            worst = max(rb, key=lambda r: r['duration'])
            return f'render-blocking {path_of(worst["url"])} ({worst["duration"]} ms)', worst['url'], attr
        return 'first paint late without render-blocking resources', 'fcp-unattributed', attr
    return metric, metric, {}


def severity_of(metric: str, median: float, budget: float, critical_path: bool) -> str:
    ratio = median / budget if budget else float('inf')
    if ratio >= 2 or (metric == 'LCP' and median > 4000) or (metric == 'CLS' and median > 0.25):
        s = 'Critical'
    elif ratio >= 1.5:
        s = 'High'
    elif ratio >= 1.2:
        s = 'Medium'
    else:
        s = 'Low'
    if critical_path:
        s = LEVELS[min(LEVELS.index(s) + 1, 3)]
    return s


def confidence_of(m: dict) -> str:
    if m['status'] != 'over':
        return 'low'
    all_over = all(r['value'] is not None and r['value'] > m['budget'] for r in m['runs'])
    if m['cv'] <= 0.1 and all_over:
        return 'high'
    return 'medium'  # over implies stable: CV within the limit or a spread under the floor


def priority_of(severity: str, reach: int, leverage: int) -> tuple[int, str]:
    score = WEIGHT[severity] * reach * leverage
    p = 'P0' if score >= 24 else 'P1' if score >= 12 else 'P2' if score >= 6 else 'P3'
    if severity == 'Critical' and p in ('P2', 'P3'):
        p = 'P1'
    return score, p


def candidates(doc: dict, meas: list[dict]) -> list[dict]:
    sample = {s['url']: s for s in doc['sample']}
    g = groups(doc)
    out: dict[tuple, dict] = {}
    for m in meas:
        if m['budget'] is None or m['median'] is None:
            continue
        if not (m['status'] == 'over' or (m['status'] == 'unstable' and m['median'] > m['budget'])):
            continue
        runs = g[(m['url'], m['profile'], m['cache'], m['browser'], m.get('variant'))]
        ok = counted(runs)
        diag = next((r for r in runs if r['kind'] == 'diagnostic' and r['ok']), None)
        cause, key, attr = attribute(m['metric'], ok, diag)
        # A specific cause (a resource URL, a server step, an element with an id or class) shared by several
        # templates is one site-wide candidate; generic causes stay per template.
        shared = key not in GENERIC_KEYS and ('/' in key or key.startswith(('server:', 'interaction:')) or '#' in key or '.' in key)
        c = out.setdefault((None if shared else m['template'], m['metric'], key),
                           {'template': m['template'], 'templates': [], 'metric': m['metric'], 'cause': cause,
                            'cause_key': key, 'pages': [], 'measurements': [], 'attribution': attr, 'evidence': []})
        if m['template'] not in c['templates']:
            c['templates'].append(m['template'])
        if m['url'] not in c['pages']:
            c['pages'].append(m['url'])
        c['measurements'].append({k: m[k] for k in ('url', 'profile', 'cache', 'browser', 'median', 'budget', 'cv', 'status')} |
                                 ({'variant': m['variant']} if m.get('variant') else {}) |
                                 {'ratio': round(m['median'] / m['budget'], 2) if m['budget'] else None})
        c['evidence'].append({'run_ids': [r['id'] for r in ok], 'diagnostic_run': diag['id'] if diag else None,
                              'trace': (diag or {}).get('artifacts', {}).get('trace'),
                              'filmstrip': ((diag or {}).get('artifacts', {}).get('filmstrip') or [])[-1:]})
    cands = list(out.values())
    metrics_per_key: dict[str, set] = {}
    for c in cands:
        metrics_per_key.setdefault(c['cause_key'], set()).add(c['metric'])
    meas_index = {(m['url'], m['profile'], m['cache'], m['browser'], m.get('variant'), m['metric']): m for m in meas}
    for c in cands:
        worst = max(c['measurements'], key=lambda x: (x['status'] == 'over', x['ratio'] or 0))
        full = meas_index[(worst['url'], worst['profile'], worst['cache'], worst['browser'], worst.get('variant'), c['metric'])]
        crit = any(sample.get(u, {}).get('critical_path') for u in c['pages'])
        c['site_wide'] = len(c['templates']) > 1
        c['template'] = c['templates'][0] if len(c['templates']) == 1 else None
        c['worst'] = {k: worst[k] for k in ('url', 'profile', 'cache', 'browser', 'median', 'budget', 'ratio')} | \
            ({'variant': worst['variant']} if worst.get('variant') else {})
        c['critical_path'] = crit
        c['severity'] = severity_of(c['metric'], worst['median'], worst['budget'], crit)
        c['confidence'] = confidence_of(full)
        c['reach'] = min(len(c['pages']), 3)
        c['leverage'] = 2 if len(metrics_per_key[c['cause_key']]) > 1 else 1
        c['score'], c['priority'] = priority_of(c['severity'], c['reach'], c['leverage'])
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    cands.sort(key=lambda c: (order[c['priority']], -c['score'], c['metric'], c['template'] or ''))
    for i, c in enumerate(cands, 1):
        c['id'] = f'K{i}'
    return [{'id': c['id'], **{k: v for k, v in c.items() if k != 'id'}} for c in cands]


def rel_path(f: Path, out: Path) -> str:
    try:
        return Path(os.path.relpath(f.resolve(), out.resolve().parent)).as_posix()
    except ValueError:  # another drive on Windows
        return f.resolve().as_posix()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('measurements', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--budget', type=Path)
    a = ap.parse_args()
    doc = json.loads(a.measurements.read_text(encoding='utf-8'))
    budget = Budget(a.budget)
    meas = measurements(doc, budget)
    cands = candidates(doc, meas)
    out = {'measurements_file': a.measurements.name,
           'budget': {'source': budget.source, 'note': budget.note,
                      'path': None if budget.builtin else rel_path(budget.path, a.out)},
           'third_party': third_party(doc), 'blocked_first_party': blocked_first_party(doc), 'cdn_cache': cdn_cache(doc),
           'request_summary': request_summary(doc), 'measurements': meas, 'server_timing': server_timing(doc), 'candidates': cands}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    st = Counter(m['status'] for m in meas)
    print(f'{len(meas)} measurement(s) {dict(st)}; {len(cands)} candidate(s) -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
