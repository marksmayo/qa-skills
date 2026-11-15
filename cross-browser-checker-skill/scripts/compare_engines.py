#!/usr/bin/env python3
"""Compare each engine's runs with the reference engine's, keep only differences that repeat, rate them (stdlib).

    python compare_engines.py run/capture/runs.json --out run/candidates.json [--features run/features.json]
        [--reference chromium] [--critical-path]

Per page (and state) and viewport, each observation is evaluated in every run of every engine:
  error      a page error, or a console error, seen in one engine
  behaviour  a scripted step that failed
  network    a failed request; an image that didn't decode (natural size 0)
  layout     an element present in one engine only; an input whose type falls back (no date control); a box
             differing by more than 4 px and 5% in width or height, or moving more than 16 px; horizontal
             overflow; content not rendered (an element whose ink, the share of pixels unlike its
             background, is under 40% of the reference engine's)
Differences are dismissed automatically, with the reason, when they are font metrics (under 16 px of
movement, within 4 px or 5% in size), native form controls drawn at another size (judged apart from a
move: a control that also moved is then judged as a move), a line wrap (a row of items under one
parent that keep their order but wrap onto another line, with no item resized beyond font metrics:
every item of the row, and what moved with them, is one dismissal), a box that changed size because
something inside it changed, a box resized with its container, an element that moved because a box
above it grew, an input's box when its type fell back, an element inside another missing element, or
an ink measure between boxes of different shape (a different number of line boxes, or the area
changed more than twofold: an inline element that wrapped, a list box drawn at another size).
A layout difference of one element (by its signature) on several pages is one candidate, whose reach
counts the pages (`pages`); so is an undecoded image of one component, and one console message (compared one
message at a time, without its source position). Repeated siblings (`:nth-of-type(N)`, `:nth-child(N)`) group
as one element (`elements` lists them), viewports merge (`viewports`), and `occurrences` counts the raw
differences a candidate stands for. Candidates that follow from an undecoded image with a known build
limitation on the same page and engine inherit it, and a console message about an HTTP error response logged
by one engine only is dismissed when every run recorded that status (`http_errors`). When comparisons were made and nothing at all came out, `self_check`
says how many element boxes were compared and how many were identical, to be checked by eye.
Also dismissed: an inline element that moved inside a parent block that kept its size (inline reflow), and
every layout difference of a pair of runs laid out at different effective viewports (`inner`, `fullscreen`
from capture.py). Media requests cancelled with ERR_ABORTED and Firefox's NS_ERROR_PARSED_DATA_CACHED are
not compared (`benign_requests` lists them); one failed request on several pages is one candidate. When the
reference shows under 2% ink on an element, the other engines are compared with each other (the candidate's
`reference` is then the engine that drew it). A box or ink difference of an element whose font family
differs with a known build limitation inherits it. `same_everywhere` lists steps that failed, and page
errors thrown, in every run of every engine: no difference, but a state that was not exercised.
Only captured runs are compared: a page, viewport and engine with any run errored or skipped
(run_status) is left out, and `comparisons` in the output lists every planned engine-versus-reference
pair, how many were made and why the others weren't. No comparison made means nothing was compared,
never "no differences".
Runs that captured but can't be compared are screened first (screen_runs, `screened_runs` in the output): a run
whose own requests to the site were upgraded to https and failed (a CSP upgrade-insecure-requests on a plain-http
target: one `environment` entry, cause https_upgrade, instead of a difference per request), and a blank or incomplete
rerun (under half its sibling reruns' median element count, or a one-colour screenshot when theirs aren't). Both are
errored, so their comparisons are not made. A box difference's runs record that the box differed (reference false),
so it can be promoted. Also dismissed: an SVG shape whose box grew evenly by its stroke width (one engine includes the
stroke), low ink on an image that decoded and was drawn (photographic content), and aborted image requests (a srcset
candidate or /_next/image swapped: benign). Text with the same computed family drawn in another face (capture's
`faces` widths differ: system-ui in Windows WebKit) is a font_family_differs difference.
A difference counts only when the observation holds in every run of the affected engine and in no run
of the reference (or the reverse). Anything in between is flaky, listed with its run counts. The same
observation in several engines, or several states of one page, is one candidate.

Ratings (RUBRIC.md): severity from the kind; confidence high when the difference is explained by an
error message or a linked feature-support gap, medium otherwise; priority = severity weight x usage
share of the affected engines (caniuse global usage by engine family: under 5% 1, 5-20% 2, over 20% 3)
x reach (pages: 1, 2, 3+). A failing critical-path step is at least P1. With --features, a feature
finding whose pattern appears in an error message, or whose CSS selector matches an affected element,
is linked and becomes confirmed_at_runtime.

    python compare_engines.py --static-only --features run/features.json [--targets targets.json] --out candidates.json

--static-only is for a check in which no browser ran (the feature scan only): it writes the same
candidates file with every engine not_run, no candidates, every feature finding possible (or
not_assessed), the planned checks from --targets, and 0 comparisons made, for a report whose
run_status is not_run.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import png_io  # noqa: E402

WEIGHT = {'Critical': 4, 'High': 3, 'Medium': 2, 'Low': 1}
FAMILIES = {'blink': ['chrome', 'edge', 'opera', 'and_chr', 'samsung', 'op_mob', 'android', 'and_uc', 'and_qq', 'baidu'],
            'gecko': ['firefox', 'and_ff'], 'webkit': ['safari', 'ios_saf']}
ENGINE_FAMILY = {'chromium': 'blink', 'firefox': 'gecko', 'webkit': 'webkit'}
INK_TAGS = {'input', 'img', 'button', 'select', 'textarea', 'h1', 'h2', 'h3', 'p', 'a', 'label', 'li', 'span', 'svg', 'canvas',
            'video'}
NATIVE_TAGS = ('input', 'button', 'select', 'textarea')
# elements that flow inside a line of text: one that moves along (or to another line of) its unchanged parent block moved
# because the text before it broke at another word
INLINE_TAGS = {'a', 'span', 'em', 'strong', 'b', 'i', 'u', 'code', 'small', 'abbr', 'label', 'kbd', 'sup', 'sub', 'mark',
               'time', 'cite', 'q', 'img', 'button', 'br', 'wbr', 's', 'var', 'samp', 'bdi', 'bdo'}
# request failures that are not failures: a media request the engine cancels once it has the metadata (or a range it
# no longer needs), and Firefox's notice that a response was served from its parsed-data cache
MEDIA_URL = re.compile(r'\.(mp4|m4v|webm|ogv|ogg|oga|mp3|m4a|aac|wav|flac|opus|mov|m3u8|mpd|ts)(\?|#|\s|$)', re.I)
MEDIA_ABORT = re.compile(r'net::ERR_ABORTED|NS_BINDING_ABORTED|\bcancell?ed\b', re.I)
# an image request the page cancels: a responsive image swapping its srcset candidate, a framework image endpoint
# (next/image's /_next/image) replacing a preload with the size it settled on, a lazy image scrolled past
IMAGE_URL = re.compile(r'\.(png|jpe?g|gif|webp|avif|svg|ico|bmp)(\?|#|\s|$)|/_next/image\?|/_vercel/image\?|/cdn-cgi/image/', re.I)
# SVG shapes: one engine's getBoundingClientRect includes the stroke (Firefox), another's doesn't
SVG_SHAPES = {'circle', 'ellipse', 'rect', 'line', 'polyline', 'polygon', 'path'}
# a stylesheet request (to tell a default face that follows from a stylesheet the engine failed to load)
CSS_URL = re.compile(r'\.css(\?|#|\s|$)', re.I)
# a decoded image whose ink stays above this share was drawn: its "low ink" is the ink metric on photographic content
DECODED_INK_FLOOR = 0.05
# a run is a failed capture when it has under this share of its sibling reruns' elements (and this many fewer or more)
OUTLIER_SHARE, OUTLIER_MIN = 0.5, 20
# a font face (a test string's width measured with the family) differing by more than this share is another face
FACE_TOLERANCE = 0.005
ALWAYS_BENIGN = re.compile(r'NS_ERROR_PARSED_DATA_CACHED')
# Ink is compared only between boxes of comparable shape: a box whose area changed by more than this factor (an inline
# element that wrapped onto two lines, a list box drawn at another size) spreads the same ink over a different area.
INK_AREA_FACTOR = 2.0


def ink(img: png_io.Image, box: list[int]) -> float | None:
    """Share of the box's pixels (inside a 2 px border) more than 60 levels from its most common colour.

    Colours are counted a row at a time in C (Counter over zip of the row's R, G and B bytes), then the
    distance test runs once per distinct colour, weighted by its count: the same result as testing every
    pixel, including which colour wins a tie (the first seen, top to bottom and left to right)."""
    x, y, w, h = box
    x, y, w, h = x + 2, y + 2, w - 4, h - 4
    if w < 6 or h < 6 or x < 0 or y < 0 or x + w > img.width or y + h > img.height:
        return None   # outside the captured area (or too small): no ink
    counts: Counter = Counter()
    a, b = 3 * x, 3 * (x + w)
    for row in img.rows[y:y + h]:
        s = row[a:b]
        counts.update(zip(s[0::3], s[1::3], s[2::3]))
    bg = counts.most_common(1)[0][0]
    far = sum(n for p, n in counts.items() if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 60)
    return far / (w * h)


def screenshot_inks(task: tuple) -> list[float | None]:
    """Decode one screenshot once and measure the ink of every box in it (run in a worker process)."""
    path, boxes = task
    img = png_io.read(path)
    return [ink(img, b) for b in boxes]


def ink_boxes(run: dict) -> list[list[int]]:
    return [e['box'] for e in run.get('elements') or [] if e['tag'] in INK_TAGS]


def all_inks(doc: dict, base: Path, ref: str, jobs: int) -> dict[int, list | None]:
    """Ink per run (aligned with ink_boxes), decoding each screenshot once, in parallel when jobs > 1.
    Runs on a page and viewport the reference engine didn't capture are never compared, so not decoded."""
    with_ref = {(r['page'], r['viewport']) for r in doc['runs'] if r['engine'] == ref}
    tasks = {i: (base / r['screenshot'], ink_boxes(r)) for i, r in enumerate(doc['runs'])
             if r.get('screenshot') and (r['page'], r['viewport']) in with_ref}
    out: dict[int, list | None] = {i: None for i in range(len(doc['runs']))}
    if jobs > 1 and len(tasks) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as ex:
            for i, v in zip(tasks, ex.map(screenshot_inks, tasks.values())):
                out[i] = v
    else:
        for i, t in tasks.items():
            out[i] = screenshot_inks(t)
    return out


def default_jobs() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def observations(run: dict, inks: list | None) -> dict[tuple, dict]:
    """Every observation a run makes, keyed so the same thing in another engine has the same key."""
    obs: dict[tuple, dict] = {}
    for e in run.get('page_errors') or []:
        obs[('error', 'page_error')] = {'messages': (obs.get(('error', 'page_error')) or {}).get('messages', []) + [e]}
    for e in run.get('console_errors') or []:
        # one observation per message, so the same message on several pages groups into one candidate and another
        # message on the same page stays its own
        k = ('error', 'console_error', console_key(e))
        obs[k] = {'messages': (obs.get(k) or {}).get('messages', []) + [e]}
    for s in run.get('steps') or []:
        if s['outcome'] == 'failed':
            obs[('behaviour', 'step_failed', s['index'])] = {'step': s['action'], 'messages': [s['error']]}
    for r in run.get('failed_requests') or []:
        if benign_failure(r):
            continue   # listed by benign_requests(), never compared
        obs[('network', 'request_failed', r.split(' (')[0])] = {'messages': [r]}
    for i in run.get('images') or []:
        if i['complete'] and i['natural'] == [0, 0]:
            obs[('network', 'image_not_decoded', i['sig'])] = {'element': i['sig'], 'src': i['src']}
    if run.get('overflow_x'):
        obs[('layout', 'overflow')] = {'size': run.get('size')}
    n = 0
    decoded = {i['sig']: bool(i.get('complete')) and list(i.get('natural') or [0, 0]) != [0, 0] for i in run.get('images') or []}
    for e in run.get('elements') or []:
        obs[('present', e['sig'])] = e
        if e['tag'] == 'input':
            obs[('type', e['sig'])] = {'type': e.get('type'), 'box': e['box']}
        if e['tag'] in INK_TAGS:
            v = inks[n] if inks is not None else None
            n += 1
            if v is not None:
                obs[('ink', e['sig'])] = {'ink': round(v, 4), 'box': e['box'], 'tag': e['tag'], 'type': e.get('type'),
                                          'lines': e.get('lines'), **({'decoded': decoded[e['sig']]} if e['sig'] in decoded else {})}
    return obs


def benign_failure(entry: str) -> bool:
    """A recorded failed request ("GET <url> (<failure>)") that is the engine's normal behaviour, not a failure: a media
    request cancelled once the metadata or a range arrived (net::ERR_ABORTED, NS_BINDING_ABORTED), an image request the
    page cancelled (a srcset candidate swapped, a framework image endpoint such as /_next/image replaced), or Firefox's
    NS_ERROR_PARSED_DATA_CACHED notice. A codec or network problem shows as another error (a media error event, a
    connection failure), which stays compared."""
    url, _, failure = entry.partition(' (')
    target = url.split(' ', 1)[-1]
    return bool(ALWAYS_BENIGN.search(failure) or (MEDIA_ABORT.search(failure) and (MEDIA_URL.search(target) or
                                                                                 IMAGE_URL.search(target))))


def benign_requests(doc: dict) -> list[dict]:
    """Every benign request failure (benign_failure) the runs recorded, grouped by request and failure, with the engines
    and the number of runs: left out of the comparison, and listed so a reader can see what was."""
    out: dict[tuple, dict] = {}
    for r in doc.get('runs') or []:
        for f in r.get('failed_requests') or []:
            if benign_failure(f):
                req, _, failure = f.partition(' (')
                e = out.setdefault((req, failure.rstrip(')')), {'request': req, 'failure': failure.rstrip(')'), 'engines': [],
                                                                'pages': [], 'runs': 0})
                e['runs'] += 1
                for k, v in (('engines', r['engine']), ('pages', r['page'])):
                    if v not in e[k]:
                        e[k].append(v)
    return sorted(out.values(), key=lambda e: (e['request'], e['failure']))


def same_everywhere(doc: dict) -> list[dict]:
    """Scripted steps that failed, and page errors thrown, in every captured run of every engine (two engines or more):
    the comparison sees no difference, yet the state did not do what the targets asked (a step timing out on a covered
    element, a script error every engine throws). One entry per page or state, kind and step or message, with the
    viewports it held at; the report has to say so (a limitation or needs_review), and the bug goes to a crash hunt."""
    screen_runs(doc)
    by: dict[tuple, dict[str, list]] = {}
    for r in doc.get('runs') or []:
        if captured(r):
            by.setdefault((r['page'], r['viewport']), {}).setdefault(r['engine'], []).append(r)
    out: dict[tuple, dict] = {}
    for (page, vp), engs in sorted(by.items()):
        if len(engs) < 2:
            continue
        runs = [r for rs in engs.values() for r in rs]
        failed = [{s['index']: s for s in r.get('steps') or [] if s.get('outcome') == 'failed'} for r in runs]
        for i in sorted(set.intersection(*(set(f) for f in failed))):
            k = (page, 'step_failed', str(i))
            e = out.setdefault(k, {'page': page, 'viewports': [], 'kind': 'step_failed', 'step': i,
                                   'action': failed[0][i].get('action'), 'engines': sorted(engs),
                                   'messages': sorted({f[i].get('error') or '' for f in failed})[:3]})
            e['viewports'].append(vp)
        errs = [{console_key(m) for m in r.get('page_errors') or []} for r in runs]
        for m in sorted(set.intersection(*errs)):
            k = (page, 'page_error', m)
            e = out.setdefault(k, {'page': page, 'viewports': [], 'kind': 'page_error', 'message': m, 'engines': sorted(engs)})
            e['viewports'].append(vp)
    return list(out.values())


def viewport_mismatch(R: list[dict], E: list[dict], vp: str, eng: str, ref: str) -> str | None:
    """Why the two engines' runs were not laid out at the same viewport, or None: a step that entered fullscreen (click to
    play) resizes the viewport to the screen in some engines, so boxes, presence and overflow can't be compared."""
    sizes = {tuple(r['inner']) for r in R + E if r.get('inner')}
    fs = {bool(r.get('fullscreen')) for r in R + E if 'fullscreen' in r}
    if len(sizes) <= 1 and len(fs) <= 1:
        return None
    def desc(runs):
        s = sorted({'x'.join(map(str, r['inner'])) for r in runs if r.get('inner')})
        f = any(r.get('fullscreen') for r in runs)
        return (', '.join(s) or 'unrecorded') + (' (fullscreen)' if f else '')
    return (f'effective viewport differs: {eng} ran at {desc(E)} and {ref} at {desc(R)} (planned {vp}), so boxes and '
            'layout are not comparable; compare this state by its screenshots, or keep the page out of fullscreen')


def console_key(message: str) -> str:
    """A console message without the source position it was logged from ("... app.js:12:5"), which moves between builds."""
    return re.sub(r'(:\d+){1,2}\)?\s*$', '', ' '.join(str(message).split()))[:200]


# a repeated sibling (a card in a list) is the same component whatever its position: card:nth-of-type(7) is card
NTH = re.compile(r':nth-(?:of-type|child)\(\d+\)')


def group_key(key: str) -> str:
    return NTH.sub('', key or '')


# regional-indicator pairs (flags) and other pictographs, whose glyphs depend on the platform's emoji font
EMOJI = re.compile('[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]')
HTTP_LOGGED = re.compile(r'^Failed to load resource: the server responded with a status of (\d{3})\b')


def http_logged(diffs: list[dict], first: int, dismissed: list[dict], R: list[dict], E: list[dict]) -> None:
    """A console message an engine writes for an HTTP error response ("Failed to load resource: the server responded with a
    status of 400"), logged in one engine only, while every run of both engines recorded a response with that status
    (http_errors, from capture.py): the request failed alike everywhere; only the logging differs (Firefox doesn't log
    HTTP error responses of fetch() to the console). Captures without http_errors are left as they are."""
    keep = []
    for d in diffs[first:]:
        st = {HTTP_LOGGED.match(m).group(1) if HTTP_LOGGED.match(m) else None for m in d.get('messages') or []}
        if d['subkind'] == 'console_error' and st and None not in st and all('http_errors' in r for r in R + E) and \
                all(any(h.split(' ', 1)[0] == s for h in r['http_errors']) for r in R + E for s in st):
            dismissed.append(dict(d, reason=f'every run of both engines received the HTTP {"/".join(sorted(st))} response '
                                            '(http_errors); only the console logging of it differs between the engines'))
        else:
            keep.append(d)
    diffs[first:] = keep


def img_src(runs: list[dict], sig: str) -> str | None:
    """The image an element shows, when it is an <img>: for matching known engine-build limitations (AVIF in WebKit)."""
    return next((i['src'] for i in runs[0].get('images') or [] if i['sig'] == sig), None)


def planned_checks(targets: dict) -> list[dict]:
    """Every (page or state, viewport) a targets file asks for, in capture order. A page or state may list "viewports", a
    subset of the top-level list; a state inherits its page's. The planned run count is len() x engines x reruns."""
    out = []
    for p in targets['pages']:
        page_vps = p.get('viewports') or targets['viewports']
        for st in [None] + (p.get('states') or []):
            vps = (st or {}).get('viewports') or page_vps
            name = p['name'] + (f'~{st["name"]}' if st else '')
            unknown = [v for v in vps if v not in targets['viewports']]
            if unknown:
                raise ValueError(f'{name}: viewports {unknown} are not in the top-level viewports')
            out += [{'page': name, 'viewport': v} for v in targets['viewports'] if v in vps]
    return out


def third_party(doc: dict) -> dict | None:
    """What the harness did with third-party requests, when the capture recorded it: mode, allowed hosts, blocked hosts."""
    cap = doc.get('capture')
    if not cap or 'third_party' not in cap:
        return None
    hosts = Counter(urlparse(b['url']).hostname or b['url'] for b in doc.get('blocked_requests') or [] if b.get('reason') == 'third_party')
    return {'mode': cap['third_party'], 'allowed_hosts': cap.get('allowed_hosts') or [],
            'blocked': [{'host': h, 'requests': n} for h, n in sorted(hosts.items(), key=lambda x: (-x[1], x[0]))]}


def all_runs(runs: list[dict], key, cond=lambda v: True) -> list[bool]:
    return [key in r['obs'] and cond(r['obs'][key]) for r in runs]


def captured(run: dict) -> bool:
    """A run that produced data. capture.py records run_status; an older capture's run captured unless it has an error."""
    status = run.get('run_status') or ('errored' if run.get('error') else 'ran')
    return status == 'ran' and 'elements' in run


def upgraded_requests(run: dict, site: str | None) -> list[str]:
    """Failed requests to the site's own host and port rewritten from http to https: on a plain-http target, a
    Content-Security-Policy with upgrade-insecure-requests makes some engines (Playwright WebKit, Safari) fetch every
    subresource over https, which the server doesn't speak. An environment artefact, not an engine difference."""
    if not site:
        return []
    s = urlparse(site)
    if s.scheme != 'http' or not s.hostname:
        return []
    port = s.port or 80
    out = []
    for f in run.get('failed_requests') or []:
        u = urlparse(f.partition(' (')[0].split(' ', 1)[-1])
        try:
            uport = u.port or 443
        except ValueError:
            continue
        if u.scheme == 'https' and u.hostname == s.hostname and uport == port:
            out.append(f)
    return out


def screen_runs(doc: dict) -> list[dict]:
    """Mark the runs that captured but can't be compared as errored, with the reason (in place; the same result however
    often it is called): a run whose own requests to the site were upgraded to https and failed (an environment cause,
    see upgraded_requests), and a rerun far below its sibling reruns of the same page, viewport and engine (under
    OUTLIER_SHARE of their median element count and OUTLIER_MIN or more fewer, or a uniform screenshot when theirs
    aren't: a blank capture). Comparing such a run turns one bad load into hundreds of differences or flaky entries.
    Returns one entry per screened run."""
    site = doc.get('site')
    groups: dict[tuple, list[dict]] = {}
    for r in doc.get('runs') or []:
        if r.get('screened') or captured(r):
            groups.setdefault((r['page'], r['viewport'], r['engine']), []).append(r)
    out = []
    for (page, vp, eng), rs in groups.items():
        for r in rs:
            why = r.get('screened')
            if not why:
                up = upgraded_requests(r, site)
                others = [o for o in rs if o is not r]
                n = len(r.get('elements') or [])
                if up:
                    why = (f'environment: {len(up)} request(s) to the site were upgraded to https and failed (e.g. '
                           f'{up[0][:140]}): a Content-Security-Policy upgrade-insecure-requests on a plain-http target'
                           + (' (the page sends one)' if r.get('csp_upgrade_insecure_requests') else '') +
                           '; serve the site over https or without the directive and capture again')
                elif others:
                    counts = sorted(len(o.get('elements') or []) for o in others)
                    med = counts[len(counts) // 2]
                    uni = (r.get('uniformity') or {}).get('uniform')
                    if n < OUTLIER_SHARE * med and med - n >= OUTLIER_MIN:
                        heights = sorted(o['size'][1] for o in others if o.get('size'))
                        why = (f'blank or incomplete capture: {n} elements against {med} in the other runs of {page} at '
                               f'{vp} in {eng}' + (f', page height {r["size"][1]} against {heights[len(heights) // 2]}'
                                                   if r.get('size') and heights else ''))
                    elif uni and not all((o.get('uniformity') or {}).get('uniform') for o in others):
                        why = (f'blank capture: the screenshot is one colour ({r["uniformity"].get("dominant_share")} of '
                               f'its pixels) while the other runs of {page} at {vp} in {eng} are not')
            if why:
                if not r.get('screened'):
                    r.update(screened=why, run_status='errored', ok=False, error=why)
                out.append({'page': page, 'viewport': vp, 'engine': eng, 'run': r.get('run', r.get('rerun')), 'reason': why})
    return out


def environment(doc: dict) -> list[dict]:
    """Causes in the test environment that stopped runs from being compared, one entry per cause rather than one
    difference per failed request: requests to the site upgraded to https that failed (cause https_upgrade). The report
    must name each in a limitation."""
    hit = [r for r in doc.get('runs') or [] if upgraded_requests(r, doc.get('site'))]
    if not hit:
        return []
    reqs = sorted({f.partition(' (')[0] for r in hit for f in upgraded_requests(r, doc.get('site'))})
    engines = sorted({r['engine'] for r in hit})
    csp = any(r.get('csp_upgrade_insecure_requests') for r in hit)
    return [{'cause': 'https_upgrade', 'engines': engines, 'runs': len(hit), 'pages': sorted({r['page'] for r in hit}),
             'requests': len(reqs), 'example': reqs[0][:200], 'csp_upgrade_insecure_requests': csp,
             'detail': (f'{", ".join(engines)} fetched the site\'s own subresources over https on a plain-http target '
                        f'({doc.get("site")}) and every such request failed, so the pages rendered without those '
                        'stylesheets and scripts: a Content-Security-Policy upgrade-insecure-requests directive'
                        + (' (the page sends one)' if csp else ' (the likely cause)') + '. Playwright\'s bypass_csp does '
                        'not cover it, and it does nothing on https hosting. Serve the site over https, or without the '
                        'directive for the capture, and capture again. These runs were not compared.')}]


def comparisons(doc: dict, ref: str) -> dict:
    """Every planned (page or state, viewport, engine) pair against the reference, and whether it could be compared: both
    the engine and the reference need every rerun captured (a difference must hold in every run). Errored, skipped or
    missing runs leave it not compared, with the reason."""
    screen_runs(doc)
    plan = doc.get('planned') or [{'page': p, 'viewport': v} for p, v in sorted({(r['page'], r['viewport']) for r in doc['runs']})]
    engines = [e['name'] for e in doc.get('engines') or []] or sorted({r['engine'] for r in doc['runs']})
    reruns = doc.get('reruns') or 1
    by: dict[tuple, list] = {}
    for r in doc['runs']:
        by.setdefault((r['page'], r['viewport'], r['engine']), []).append(r)
    rows = []
    for chk in plan:
        for eng in engines:
            if eng == ref:
                continue
            why = []
            for e in (ref, eng):
                rs = by.get((chk['page'], chk['viewport'], e), [])
                ok = sum(1 for r in rs if captured(r))
                if ok < reruns:
                    err = next((r.get('error') for r in rs if not captured(r) and r.get('error')), None)
                    why.append(f'{e}: {ok} of {reruns} runs captured' + (f' ({err[:160]})' if err else ''))
            rows.append({'page': chk['page'], 'viewport': chk['viewport'], 'engine': eng, 'compared': not why,
                         'reason': '; '.join(why) or None})
    return {'planned': len(rows), 'made': sum(1 for r in rows if r['compared']),
            'not_made': [r for r in rows if not r['compared']]}


def compare(doc: dict, base: Path, ref: str, jobs: int | None = None,
            stats: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Differences, flaky observations and automatic dismissals, from the captured runs only: a run that errored or was
    skipped has no observations, and comparing it would turn the error into differences."""
    screen_runs(doc)
    inks = all_inks(doc, base, ref, default_jobs() if jobs is None else jobs)
    groups: dict[tuple, dict[str, list]] = {}
    complete = {(r['page'], r['viewport'], r['engine']) for r in doc['runs']}
    complete -= {(r['page'], r['viewport'], r['engine']) for r in doc['runs'] if not captured(r)}
    for i, r in enumerate(doc['runs']):
        if (r['page'], r['viewport'], r['engine']) not in complete:
            continue
        r = dict(r, obs=observations(r, inks[i]))
        groups.setdefault((r['page'], r['viewport']), {}).setdefault(r['engine'], []).append(r)
    diffs, flaky, dismissed = [], [], []
    for (page, vp), by in groups.items():
        if ref not in by:
            continue
        R = by[ref]
        for eng, E in by.items():
            if eng == ref:
                continue
            first = len(diffs)
            moves: list[tuple[str, list, list]] = []   # (sig, ref box, delta) of every element with a stable box in both
            sizes: dict[str, bool] = {}   # sig -> its size changed beyond font metrics (native controls excepted)
            keys = set().union(*(r['obs'].keys() for r in R + E))
            for k in sorted(keys, key=str):
                if k[0] == 'present' and all(k in r['obs'] for r in R + E):
                    rb, eb = R[0]['obs'][k]['box'], E[0]['obs'][k]['box']
                    if all(r['obs'][k]['box'] == rb for r in R) and all(e['obs'][k]['box'] == eb for e in E):
                        delta = [eb[i] - rb[i] for i in range(4)]
                        moves.append((k[1], rb, delta))
                        sizes[k[1]] = R[0]['obs'][k]['tag'] not in NATIVE_TAGS and any(
                            abs(dd) > 4 and abs(dd) > 0.05 * max(1, rb[2 + i]) for i, dd in enumerate(delta[2:]))
                        if stats is not None:
                            stats['boxes'] = stats.get('boxes', 0) + 1
                            stats['identical'] = stats.get('identical', 0) + (not any(delta))
                kind = k[0]
                if kind == 'ink':
                    continue
                if kind == 'type':
                    rt = {r['obs'][k]['type'] for r in R if k in r['obs']}
                    et = {r['obs'][k]['type'] for r in E if k in r['obs']}
                    if len(rt) == 1 and len(et) == 1 and rt != et and all(k in r['obs'] for r in R + E):
                        diffs.append({'page': page, 'viewport': vp, 'engine': eng, 'ref': ref, 'kind': 'layout', 'subkind': 'input_type_fallback',
                                      'key': k[1], 'element': k[1], 'ref_box': R[0]['obs'][k]['box'], 'element_tag': 'input',
                                      'element_type': next(iter(rt)), 'runs': {ref: [False] * len(R), eng: [True] * len(E)},
                                      'detail': f'{k[1]} is an input of type {next(iter(et))} in {eng} but {next(iter(rt))} in {ref}: '
                                                f'{eng} has no {next(iter(rt))} control'})
                    continue
                if kind == 'present':
                    rin, ein = all_runs(R, k), all_runs(E, k)
                    if all(rin) and not any(ein):
                        subkind, side = 'element_missing', eng
                    elif all(ein) and not any(rin):
                        subkind, side = 'element_extra', eng
                    elif any(rin) or any(ein):
                        if not (all(rin) and all(ein)):
                            flaky.append({'page': page, 'viewport': vp, 'kind': 'layout', 'subkind': 'element_presence', 'key': k[1],
                                          'runs': {ref: rin, eng: ein}})
                        if all(rin) and all(ein):
                            d = box_diff(R, E, k)
                            if d:
                                src = img_src(E, k[1])
                                (diffs if d['report'] else dismissed).append(dict(d['entry'], page=page, viewport=vp, engine=eng, ref=ref,
                                                                                   runs={ref: [False] * len(R), eng: [True] * len(E)},
                                                                                   **({'src': src} if src else {})))
                        continue
                    else:
                        continue
                    diffs.append({'page': page, 'viewport': vp, 'engine': eng, 'ref': ref, 'kind': 'layout', 'subkind': subkind,
                                  'key': k[1], 'element': k[1], 'runs': {ref: rin, eng: ein},
                                  'ref_box': (R[0]['obs'][k] if subkind == 'element_missing' else E[0]['obs'][k])['box'],
                                  'detail': f'{k[1]} is {"missing" if subkind == "element_missing" else "only"} in {eng}'})
                    continue
                rin, ein = all_runs(R, k), all_runs(E, k)
                if all(ein) and not any(rin):
                    who = eng
                elif all(rin) and not any(ein):
                    who = ref
                elif any(rin) or any(ein):
                    if not (all(rin) and all(ein)):
                        flaky.append({'page': page, 'viewport': vp, 'kind': kind, 'subkind': k[1], 'key': '/'.join(map(str, k[2:])),
                                      'runs': {ref: rin, eng: ein}})
                    continue
                else:
                    continue
                src = E if who == eng else R
                o = src[0]['obs'][k]
                diffs.append({'page': page, 'viewport': vp, 'engine': who, 'ref': ref if who == eng else eng, 'kind': kind, 'subkind': k[1],
                              'key': '/'.join(map(str, k[2:])), 'element': o.get('element'),
                              **({'src': o['src']} if o.get('src') else {}),
                              'messages': sorted({m for r in src for m in r['obs'][k].get('messages', [])}),
                              'step': o.get('step'), 'runs': {ref: rin, eng: ein}})
            # content not rendered: ink well below the reference's in every run
            for k in sorted({k for r in R for k in r['obs'] if k[0] == 'ink'}, key=str):
                rv = [r['obs'][k]['ink'] if k in r['obs'] else None for r in R]
                ev = [r['obs'][k]['ink'] if k in r['obs'] else None for r in E]
                if None in rv or None in ev or min(rv) < 0.02:
                    continue
                low = [v < 0.4 * min(rv) for v in ev]
                if any(low):
                    why = ink_not_comparable(R[0]['obs'][k], E[0]['obs'][k]) or decoded_photo(E, k, ev)
                    if why:
                        o = E[0]['obs'][k]
                        dismissed.append({'page': page, 'viewport': vp, 'engine': eng, 'ref': ref, 'kind': 'layout',
                                          'subkind': 'content_not_rendered', 'key': k[1], 'element': k[1],
                                          'ref_box': R[0]['obs'][k]['box'], 'ink': {ref: rv, eng: ev},
                                          'runs': {ref: [False] * len(R), eng: low}, 'reason': why})
                        continue
                if all(low):
                    o = E[0]['obs'][k]
                    text = (E[0]['obs'].get(('present', k[1])) or {}).get('text') or ''
                    diffs.append({**({'_text': text} if EMOJI.search(text) else {}),'page': page, 'viewport': vp, 'engine': eng, 'ref': ref, 'kind': 'layout', 'subkind': 'content_not_rendered',
                                  'key': k[1], 'element': k[1], 'element_tag': o['tag'], 'element_type': o.get('type'),
                                  'ref_box': R[0]['obs'][k]['box'], **({'src': img_src(E, k[1])} if img_src(E, k[1]) else {}),
                                  'ink': {ref: rv, eng: ev}, 'runs': {ref: [False] * len(R), eng: low},
                                  'detail': f'{k[1]} shows {round(100 * max(ev), 1)}% ink in {eng} against {round(100 * min(rv), 1)}% in {ref}'})
                elif any(low):
                    flaky.append({'page': page, 'viewport': vp, 'kind': 'layout', 'subkind': 'content_not_rendered', 'key': k[1],
                                  'runs': {ref: [False] * len(R), eng: low}})
            http_logged(diffs, first, dismissed, R, E)
            wraps(diffs, first, dismissed, moves, sizes)
            inline_reflow(diffs, first, dismissed, moves, sizes, R)
            drift(diffs, first, dismissed, moves)
            scrolled(diffs, first, dismissed, R, E, eng)
            covered(diffs, first, dismissed, R, E)
            font_families(diffs, R, E, page, vp, eng, ref)
            why = viewport_mismatch(R, E, vp, eng, ref)
            if why:   # nothing about layout compares across two viewports
                dismissed.extend(dict(d, reason=why) for d in diffs[first:] if d['kind'] == 'layout')
                diffs[first:] = [d for d in diffs[first:] if d['kind'] != 'layout']
        second_ink_pass(by, ref, page, vp, diffs, dismissed)
    return diffs, flaky, dismissed


def second_ink_pass(by: dict[str, list], ref: str, page: str, vp: str, diffs: list[dict], dismissed: list[dict]) -> None:
    """Content not rendered, judged against another engine when the reference shows almost no ink (under 2%: a video
    still on a near-uniform frame, an empty canvas), so the reference comparison makes no judgement: an engine whose ink
    is under 40% of a third engine's in every run, while that engine shows ink in every run, is content_not_rendered
    with that engine as its reference. Runs at another viewport, or boxes of another shape, are not compared."""
    if ref not in by or len(by) < 3:
        return
    R = by[ref]
    for k in sorted({k for r in R for k in r['obs'] if k[0] == 'ink'}, key=str):
        rv = [r['obs'][k]['ink'] if k in r['obs'] else None for r in R]
        if None in rv or max(rv) >= 0.02:
            continue
        others = sorted(e for e in by if e != ref)
        vals = {e: [r['obs'][k]['ink'] if k in r['obs'] else None for r in by[e]] for e in others}
        for b in others:
            if None in vals[b] or any(d['subkind'] == 'content_not_rendered' and d['engine'] == b and d['page'] == page and
                                      d['viewport'] == vp and d['key'] == k[1] for d in diffs):
                continue
            for a_eng in others:
                av = vals[a_eng]
                if a_eng == b or None in av or min(av) < 0.02 or not all(v < 0.4 * min(av) for v in vals[b]):
                    continue
                A, B = by[a_eng], by[b]
                if viewport_mismatch(A, B, vp, b, a_eng):
                    continue
                why = ink_not_comparable(A[0]['obs'][k], B[0]['obs'][k])
                entry = {'page': page, 'viewport': vp, 'engine': b, 'ref': a_eng, 'kind': 'layout',
                         'subkind': 'content_not_rendered', 'key': k[1], 'element': k[1],
                         'element_tag': B[0]['obs'][k]['tag'], 'element_type': B[0]['obs'][k].get('type'),
                         'ref_box': A[0]['obs'][k]['box'], 'ink': {ref: rv, a_eng: av, b: vals[b]},
                         'runs': {a_eng: [False] * len(A), b: [True] * len(B)},
                         'detail': f'{k[1]} shows {round(100 * max(vals[b]), 1)}% ink in {b} against '
                                   f'{round(100 * min(av), 1)}% in {a_eng} ({ref}, the reference, shows almost none: '
                                   f'{round(100 * max(rv), 1)}%, so {b} was compared with {a_eng})'}
                if why:
                    dismissed.append(dict(entry, reason=why))
                else:
                    diffs.append(entry)
                break


def _family(f: str | None) -> str:
    return ', '.join(x.strip().strip('"\'').lower() for x in (f or '').split(',') if x.strip())


def font_families(diffs: list[dict], R: list[dict], E: list[dict], page: str, vp: str, eng: str, ref: str) -> None:
    """Text and controls whose computed font family differs between the engines in every run (a control that does
    not inherit the page font is drawn in the engine's default face): one difference per pair of families, listing
    its elements, instead of box deltas dismissed as font metrics. Runs recorded before font capture have no font."""
    groups: dict[tuple, list[str]] = {}
    tags: dict[tuple, str] = {}
    for k in sorted({k for r in R for k in r['obs'] if k[0] == 'present'}, key=str):
        if not all(k in r['obs'] and r['obs'][k].get('font') for r in R + E):
            continue
        rf = {_family(r['obs'][k]['font']) for r in R}
        ef = {_family(r['obs'][k]['font']) for r in E}
        if len(rf) == 1 and len(ef) == 1 and rf != ef:
            pair = (next(iter(rf)), next(iter(ef)))
            groups.setdefault(pair, []).append(k[1])
            tags.setdefault(pair, R[0]['obs'][k]['tag'])
    # the same computed family resolving to another face (system-ui, a generic family, a local font missing in one
    # engine): capture measures a test string with each family (faces); a width that differs in every run is a face
    rfaces, efaces = [r.get('faces') or {} for r in R], [r.get('faces') or {} for r in E]
    for k in sorted({k for r in R for k in r['obs'] if k[0] == 'present'}, key=str):
        if not all(k in r['obs'] and r['obs'][k].get('font') for r in R + E):
            continue
        fams = {r['obs'][k]['font'] for r in R + E}
        if len(fams) != 1:
            continue
        fam = next(iter(fams))
        rw = {f.get(fam) for f in rfaces}
        ew = {f.get(fam) for f in efaces}
        if len(rw) == 1 and len(ew) == 1 and None not in rw | ew:
            a, b = next(iter(rw)), next(iter(ew))
            if a and b and abs(a - b) > FACE_TOLERANCE * max(a, b):
                pair = (f'{_family(fam)} [face {a:g}px]', f'{_family(fam)} [face {b:g}px]')
                groups.setdefault(pair, []).append(k[1])
                tags.setdefault(pair, R[0]['obs'][k]['tag'])
    css_failed = sorted({f.partition(' (')[0].split(' ', 1)[-1] for f in E[0].get('failed_requests') or []
                         if CSS_URL.search(f.partition(' (')[0]) and not benign_failure(f)
                         and all(f in (r.get('failed_requests') or []) for r in E)})
    for (rf, ef), els in sorted(groups.items()):
        if ' [face ' in ef:
            fam = rf.split(' [face ')[0]
            detail = (f'{len(els)} element(s) with font family "{fam}" draw in another face in {eng} than in {ref} '
                      f'(a test string measures {ef.split(" [face ")[1][:-1]} against {rf.split(" [face ")[1][:-1]}; '
                      f'e.g. {", ".join(els[:3])}): name the face first in the family list, or load it as a web font')
        else:
            detail = (f'{len(els)} element(s) draw in font family "{ef}" in {eng} but "{rf}" in {ref} '
                      f'(e.g. {", ".join(els[:3])}): set font-family (or font: inherit) on them')
        if css_failed:
            detail += (f'; the stylesheet failed to load in every {eng} run ({css_failed[0][:120]}), which explains a '
                       'default face: fix the request before judging the font')
        diffs.append({'page': page, 'viewport': vp, 'engine': eng, 'ref': ref, 'kind': 'layout', 'subkind': 'font_family_differs',
                      'key': f'{rf} -> {ef}', 'element': els[0], 'elements': els[:50], 'element_tag': tags[(rf, ef)],
                      'runs': {ref: [False] * len(R), eng: [True] * len(E)}, 'detail': detail})


def covered(diffs: list[dict], first: int, dismissed: list[dict], R: list[dict], E: list[dict]) -> None:
    """A box or ink difference of an element that another element covers at its centre (a modal overlay, a sticky
    panel) in any run: what was measured is the cover, not the element."""
    keep = []
    for d in diffs[first:]:
        el = d.get('element')
        cover = None
        if el and d['subkind'] in ('box_differs', 'content_not_rendered'):
            cover = next((r['obs'][('present', el)].get('covered_by') or 'another element' for r in R + E
                          if ('present', el) in r['obs'] and r['obs'][('present', el)].get('occluded')), None)
        if cover:
            dismissed.append(dict(d, reason=f'covered by {cover} (an overlay or sticky element lies over it), so its '
                                            'box and ink are not what a user sees'))
        else:
            keep.append(d)
    diffs[first:] = keep


def scrolled(diffs: list[dict], first: int, dismissed: list[dict], R: list[dict], E: list[dict], eng: str) -> None:
    """Boxes inside a panel that scrolls its own content, moved by the panel's scroll offset: the automation scrolled
    the panel (to reach an element) by a different amount in each engine, which is not layout."""
    def tops(runs):
        out: dict[str, set] = {}
        for r in runs:
            for s in r.get('scrollers') or []:
                out.setdefault(s['sig'], set()).add(s.get('scroll_top', 0))
        return out
    rt, et = tops(R), tops(E)
    offsets = {sig: next(iter(et[sig])) - next(iter(rt[sig])) for sig in rt.keys() & et.keys()
               if len(rt[sig]) == 1 and len(et[sig]) == 1 and et[sig] != rt[sig]}
    if not offsets:
        return
    keep = []
    for d in diffs[first:]:
        sig = next((s for s in offsets if (d.get('element') or '').startswith(s + '>')), None)
        delta = d.get('delta')
        if sig and d['subkind'] == 'box_differs' and delta and abs(delta[0]) <= 4 and abs(delta[1] + offsets[sig]) <= 4:
            dismissed.append(dict(d, reason=f'inside {sig}, which the automation scrolled {offsets[sig]} px further in {eng}: '
                                            'a scroll offset of the panel, not a layout difference'))
        else:
            keep.append(d)
    diffs[first:] = keep


def drift(diffs: list[dict], first: int, dismissed: list[dict], moves: list[tuple[str, list, list]]) -> None:
    """Cumulative vertical drift: small text-metric differences add up down a page, so an element far down can sit 17-22 px
    lower in one engine although nothing near it differs. An element that only moved (no size change) is a consequence of
    the content above it when the nearest element above it (overlapping it horizontally) moved by nearly the same amount:
    within 16 px, the threshold for a move. A real jump (a step of more than 16 px between neighbours) stays a difference,
    and the elements below it are dismissed as moving with it."""
    keep = diffs[:first]
    fell_back = {d['element'] for d in diffs[first:] if d['subkind'] == 'input_type_fallback'}
    for d in diffs[first:]:
        if d['subkind'] != 'box_differs' or d.get('size') or not d.get('delta') or d['element'] in fell_back:
            keep.append(d)   # (an input whose type fell back: its box follows from that, said in consequences())
            continue
        top, left, right = d['ref_box'][1], d['ref_box'][0], d['ref_box'][0] + d['ref_box'][2]
        above = [m for m in moves if m[1][1] + m[1][3] <= top and m[1][0] < right and m[1][0] + m[1][2] > left]
        if not above:
            keep.append(d)
            continue
        bottom = max(m[1][1] + m[1][3] for m in above)
        near = min((m for m in above if m[1][1] + m[1][3] == bottom),
                   key=lambda m: (abs(m[2][1] - d['delta'][1]) + abs(m[2][0] - d['delta'][0]), m[0]))
        ddx, ddy = d['delta'][0] - near[2][0], d['delta'][1] - near[2][1]
        if max(abs(ddx), abs(ddy)) <= 16 and (near[2][0] or near[2][1]):
            dismissed.append(dict(d, reason=f'cumulative drift: moved with {near[0]} just above it (dx {near[2][0]}, dy {near[2][1]} '
                                            f'there; dx {d["delta"][0]}, dy {d["delta"][1]} here), a step under 16 px'))
        else:
            keep.append(d)
    diffs[first:] = keep[first:]


def decoded_photo(E: list[dict], k: tuple, ev: list[float]) -> str | None:
    """Why low ink on an <img> the engine decoded and drew is not content that wasn't rendered, or None. Ink counts pixels
    far from the box's most common colour, so a dark or busy photo whose most common colour lands elsewhere in one engine
    (another tone of a gradient) reads as low ink although the image is there. An image is content not rendered only
    when it didn't decode (image_not_decoded) or its box is nearly blank (ink under DECODED_INK_FLOOR)."""
    o = E[0]['obs'][k]
    if o.get('tag') != 'img' or not all(r['obs'][k].get('decoded') for r in E if k in r['obs']):
        return None
    if min(ev) < DECODED_INK_FLOOR:
        return None
    return (f'decoded image drawn in every run ({round(100 * min(ev), 1)}% ink or more): the ink metric measures pixels '
            'far from the most common colour, which on a photograph is not a sign the image is missing')


def ink_not_comparable(ro: dict, eo: dict) -> str | None:
    """Why two ink measures of one element can't be compared, or None. Ink is a share of the box's pixels, so it only
    compares boxes of the same shape: an inline element that wrapped onto another number of lines (its line boxes, from
    getClientRects, when the capture recorded them), or a box whose area changed by more than INK_AREA_FACTOR (a label
    whose bounding box now spans two lines, a list box or other control drawn at another size), spreads the same ink
    over a different area and would read as content not rendered."""
    rl, el = ro.get('lines'), eo.get('lines')
    if rl and el and rl != el:
        return f'ink not comparable: the element is on {rl} line(s) in the reference and {el} here (it wrapped)'
    ra, ea = ro['box'][2] * ro['box'][3], eo['box'][2] * eo['box'][3]
    if ra and ea and max(ra, ea) > INK_AREA_FACTOR * min(ra, ea):
        return (f'ink not comparable: the box changed shape ({ro["box"][2]}x{ro["box"][3]} -> {eo["box"][2]}x{eo["box"][3]}), '
                f'so the same content covers a different share of it')
    return None


def parent_sig(sig: str) -> str | None:
    return sig.rsplit('>', 1)[0] if '>' in sig else None


def wraps(diffs: list[dict], first: int, dismissed: list[dict], moves: list[tuple[str, list, list]],
          sizes: dict[str, bool]) -> None:
    """Line-wrap cascades: a row of inline items (links in a nav bar, chips, form controls) whose last items no longer fit
    on the line in one engine (wider text, a wider control) wrap to the next line there. The first item that wrapped
    moves far left and one line down, and every item after it moves with it, so one cause would otherwise read as one
    difference per item, on every page that shares the row.

    A row is the children of one parent that share a line in one engine. It wrapped when, in the other engine, the items
    keep their order (each next item is either further right on the same line, or starts a new line below) and at least
    one starts a new line; and it wrapped from font metrics or native controls when no item in it changed size beyond
    that (within 4 px or 5%, or a native control). Then every move in the row, and the moves of anything inside an item
    of it by the same amount, are dismissed as one wrap. Rows are checked both ways (wrapped in the engine, or in the
    reference)."""
    pos = {m[0]: (m[1], [m[1][i] + m[2][i] for i in range(4)]) for m in moves}
    rows: dict[str, list[str]] = {}
    for sig in pos:
        p = parent_sig(sig)
        if p is not None:
            rows.setdefault(p, []).append(sig)
    wrapped: dict[str, str] = {}   # sig -> reason
    for parent, sigs in rows.items():
        if len(sigs) < 2:
            continue
        for a_side, b_side, where in ((0, 1, 'here'), (1, 0, 'in the reference')):
            # lines in side a: cluster by top
            items = sorted(sigs, key=lambda s: (pos[s][a_side][1], pos[s][a_side][0]))
            lines: list[list[str]] = []
            for s in items:
                if lines and abs(pos[s][a_side][1] - pos[lines[-1][0]][a_side][1]) <= 8:
                    lines[-1].append(s)
                else:
                    lines.append([s])
            for line in lines:
                if len(line) < 2:
                    continue
                line.sort(key=lambda s: pos[s][a_side][0])
                ok, breaks = True, []
                for x, y in zip(line, line[1:]):
                    bx, by = pos[x][b_side], pos[y][b_side]
                    if abs(by[1] - bx[1]) <= 8 and by[0] > bx[0]:
                        continue
                    if by[1] > bx[1] + 8 and by[0] <= bx[0]:
                        breaks.append(y)
                        continue
                    ok = False
                    break
                if not ok or not breaks:
                    continue
                if any(sizes.get(s) for s in line):
                    continue   # an item changed size beyond font metrics: not a plain wrap
                why = (f'line wrap: the row of {len(line)} items in {parent} wraps {where} from {breaks[0]} on '
                       f'(items keep their order; no item changed size beyond font metrics or native control rendering)')
                for s in line:
                    wrapped.setdefault(s, why)
    if not wrapped:
        return
    keep = diffs[:first]
    for d in diffs[first:]:
        why = None
        if d['subkind'] == 'box_differs' and not d.get('size') and d.get('delta'):
            why = wrapped.get(d['element'])
            if why is None:
                # inside a wrapped item, moved with it
                for s, w in wrapped.items():
                    if d['element'].startswith(s + '>') and s in pos:
                        sd = [pos[s][1][i] - pos[s][0][i] for i in range(2)]
                        if abs(sd[0] - d['delta'][0]) <= 4 and abs(sd[1] - d['delta'][1]) <= 4:
                            why = f'moved with {s}, inside it; ' + w
                            break
        if why:
            dismissed.append(dict(d, reason=why))
        else:
            keep.append(d)
    diffs[first:] = keep[first:]


def inline_reflow(diffs: list[dict], first: int, dismissed: list[dict], moves: list[tuple[str, list, list]],
                  sizes: dict[str, bool], R: list[dict]) -> None:
    """An inline element (a link, a span) that moved inside its parent block, which itself kept its size (within font
    metrics): the text before it broke at another word, so it moved along its line or onto the next one. Its own size
    is unchanged (within font metrics) and it stays inside the parent in both engines. The row rule (wraps) covers rows
    of separate items; this covers text runs, where the words between the items are not elements."""
    pos = {m[0]: (m[1], [m[1][i] + m[2][i] for i in range(4)]) for m in moves}
    def within(a, b, tol=2):
        return a[0] >= b[0] - tol and a[1] >= b[1] - tol and a[0] + a[2] <= b[0] + b[2] + tol and a[1] + a[3] <= b[1] + b[3] + tol
    keep = diffs[:first]
    for d in diffs[first:]:
        el = d.get('element') or ''
        parent = parent_sig(el)
        tag = ((R[0]['obs'].get(('present', el)) or {}).get('tag') if R else None) or ''
        if d['subkind'] == 'box_differs' and not d.get('size') and d.get('delta') and tag in INLINE_TAGS and \
                parent in pos and el in pos and not sizes.get(parent) and \
                within(pos[el][0], pos[parent][0]) and within(pos[el][1], pos[parent][1]):
            dismissed.append(dict(d, reason=f'inline reflow: <{tag}> moved within {parent} (dx {d["delta"][0]}, dy '
                                            f'{d["delta"][1]}), which kept its size: the text before it breaks at another '
                                            'word; its own size is unchanged'))
        else:
            keep.append(d)
    diffs[first:] = keep[first:]


def box_diff(R, E, k):
    rb = R[0]['obs'][k]['box']
    eb = E[0]['obs'][k]['box']
    if any(r['obs'][k]['box'] != rb for r in R) or any(e['obs'][k]['box'] != eb for e in E):
        return None   # boxes not stable within an engine: leave it to flaky element checks
    dx, dy, dw, dh = (eb[i] - rb[i] for i in range(4))
    size = any(abs(d) > 4 and abs(d) > 0.05 * max(1, rb[2 + i]) for i, d in enumerate((dw, dh)))
    pos = max(abs(dx), abs(dy)) > 16
    if not (size or pos):
        if max(abs(dx), abs(dy), abs(dw), abs(dh)) > 4:
            return {'report': False, 'entry': {'kind': 'layout', 'subkind': 'box_differs', 'key': k[1], 'element': k[1],
                                               'delta': [dx, dy, dw, dh], 'ref_box': rb,
                                               'reason': 'font metrics: under 16 px of movement and within 4 px or 5% in size'}}
        return None
    tag = R[0]['obs'][k]['tag']
    stroke = svg_stroke(R[0]['obs'][k], E[0]['obs'][k], rb, (dx, dy, dw, dh))
    if stroke:
        return {'report': False, 'entry': {'kind': 'layout', 'subkind': 'box_differs', 'key': k[1], 'element': k[1],
                                           'delta': [dx, dy, dw, dh], 'ref_box': rb, 'reason': stroke}}
    native = tag in NATIVE_TAGS
    if native and not pos:
        return {'report': False, 'entry': {'kind': 'layout', 'subkind': 'box_differs', 'key': k[1], 'element': k[1],
                                           'delta': [dx, dy, dw, dh], 'ref_box': rb,
                                           'reason': f'native control rendering: engines draw <{tag}> at different sizes'}}
    # A native control that also moved: its size change is the engine's control rendering, judged apart from the move,
    # so what is left is a move, which the wrap, drift and consequence rules then judge like any other element's.
    entry = {'kind': 'layout', 'subkind': 'box_differs', 'key': k[1], 'element': k[1], 'delta': [dx, dy, dw, dh],
             'ref_box': rb, 'size': size and not native,
             'detail': f'{k[1]} box differs by dx {dx}, dy {dy}, dw {dw}, dh {dh}'}
    if native and size:
        entry['native_size'] = True
        entry['detail'] += f' (the size change is native <{tag}> rendering; the move is what differs)'
    return {'report': True, 'entry': entry}


def svg_stroke(ro: dict, eo: dict, rb: list, d: tuple) -> str | None:
    """Why a box difference of an SVG shape is its stroke, or None: one engine's getBoundingClientRect includes the
    stroke (Firefox) and another's doesn't, so the box grows by the stroke width on every side (dx = -dw/2, dy = -dh/2,
    dw = dh), with the shape drawn identically. With the stroke width recorded (capture.py), the growth must be within it;
    without it, within a quarter of the box."""
    if ro.get('tag') not in SVG_SHAPES:
        return None
    dx, dy, dw, dh = d
    if abs(dw - dh) > 1 or abs(2 * dx + dw) > 2 or abs(2 * dy + dh) > 2 or not dw:
        return None
    stroke = max(float(ro.get('stroke') or 0), float(eo.get('stroke') or 0))
    if (stroke and abs(abs(dw) - stroke) > 2) or (not stroke and abs(dw) > 0.25 * max(rb[2], rb[3], 1)):
        return None
    return (f'SVG stroke: one engine includes the stroke ({stroke:g} px) of this <{ro["tag"]}> in its box and the other '
            f'does not (it grew by {abs(dw)} px evenly on every side); the shape is drawn alike')


def inside(a: list | None, b: list | None) -> bool:
    """Is box a within box b (and not the same box)?"""
    return bool(a and b) and a != b and b[0] <= a[0] and b[1] <= a[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]


def consequences(diffs: list[dict], dismissed: list[dict]) -> list[dict]:
    """Drop box differences that follow from another difference: a box whose size changed because something inside it
    changed (a missing element, a different control), and an element that only moved because a box above it changed size."""
    causes = [d for d in diffs + dismissed if d['subkind'] in ('element_missing', 'element_extra', 'input_type_fallback', 'box_differs')]
    keep = []
    for d in diffs:
        same_all = [c for c in diffs if c is not d and c['page'] == d['page'] and c['engine'] == d['engine']]
        if d['subkind'] in ('element_missing', 'element_extra'):
            outer = next((c for c in same_all if c['subkind'] == d['subkind'] and
                          (d['element'].startswith(c['element'] + '>') or inside(d.get('ref_box'), c.get('ref_box')))), None)
            if outer:
                dismissed.append(dict(d, reason=f'inside {outer["element"]}, which is also {"missing" if d["subkind"] == "element_missing" else "extra"}'))
                continue
        if d['subkind'] == 'box_differs':
            fallback = next((c for c in same_all if c['subkind'] == 'input_type_fallback' and c['element'] == d['element']), None)
            if fallback:
                dismissed.append(dict(d, reason='follows from its input type fallback'))
                continue
            container = next((c for c in causes if c is not d and c['page'] == d['page'] and c['engine'] == d['engine'] and
                              c['subkind'] == 'box_differs' and c.get('delta') and d.get('delta') and inside(d.get('ref_box'), c.get('ref_box'))
                              and c['delta'][2] == d['delta'][2] and c['delta'][2] != 0), None)
            if container:
                dismissed.append(dict(d, reason=f'resized with its container {container["element"]}'))
                continue
            same = [c for c in causes if c is not d and c['page'] == d['page'] and c['engine'] == d['engine']]
            inner = next((c for c in same if inside(c.get('ref_box'), d.get('ref_box'))), None)
            if inner and d.get('size'):
                dismissed.append(dict(d, reason=f'follows from {inner["subkind"]} of {inner["element"]} inside it'))
                continue
            # a cause above it: anything but a plain move (a missing or extra element, a fallback, a box that changed size)
            # that ends above this element's top
            above = next((c for c in same if c.get('ref_box') and d.get('ref_box')
                          and (c['subkind'] != 'box_differs' or c.get('size'))
                          and c['ref_box'][1] + c['ref_box'][3] <= d['ref_box'][1]), None)
            if above and not d.get('size'):
                dismissed.append(dict(d, reason=f'moved because {above["element"]} above it changed'))
                continue
        keep.append(d)
    return keep


def share(engines: list[str], caniuse: dict | None) -> float | None:
    if not caniuse:
        return None
    total = 0.0
    for e in engines:
        for b in FAMILIES.get(ENGINE_FAMILY.get(e, ''), []):
            total += sum(v or 0 for v in caniuse['agents'][b]['usage_global'].values())
    return round(total, 2)


def severity_of(c: dict, all_c: list[dict]) -> str:
    if c['subkind'] == 'step_failed':
        return 'Critical'
    if c['subkind'] == 'page_error':
        broke = any(o['page'] == c['page'] and set(o['engines']) & set(c['engines'])
                    and o['subkind'] in ('step_failed', 'element_missing') for o in all_c)
        return 'Critical' if broke else 'High'
    if c['subkind'] in ('content_not_rendered', 'element_missing', 'image_not_decoded'):
        return 'High'
    if c['subkind'] in ('overflow', 'box_differs', 'element_extra', 'request_failed', 'input_type_fallback', 'font_family_differs'):
        return 'Medium'
    return 'Low'


def candidates(diffs: list[dict], dismissed: list[dict], doc: dict, caniuse: dict | None, features: dict | None,
               critical_states: set) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for d in consequences(diffs, dismissed):
        base = d['page'].split('~')[0]
        # a layout difference of one element (a shared header, nav or footer component, by its signature) is one cause on
        # every page that shows it: one candidate, whose reach counts the pages
        # an undecoded image of one component, and one console message, likewise; a repeated sibling (a card at
        # :nth-of-type(7)) is the same component as the others, and the viewports a cause shows at are one candidate
        # a failed request (one URL) likewise: the same asset failing on every page that loads it is one cause
        shared = (d['kind'] == 'layout' and bool(d.get('element'))) or d['subkind'] == 'image_not_decoded' or \
            (d['subkind'] in ('console_error', 'request_failed') and bool(d['key']))
        gk = group_key(d['key'])
        k = (None if shared else base, d['kind'], d['subkind'], gk)
        c = merged.setdefault(k, {k2: d.get(k2) for k2 in ('kind', 'subkind', 'key', 'element', 'src', 'step', 'detail', 'element_tag',
                                                           'element_type', 'delta')}
                              | {'key': gk, 'page': base, 'pages': [], 'viewport': d['viewport'], 'viewports': [], 'occurrences': 0,
                                 'states': [], 'engines': [], 'reference': d['ref'], 'runs': {}, 'messages': {}, 'ink': {},
                                 '_elements': [], '_text': d.get('_text'), '_members': []})
        c['occurrences'] += 1
        c['_members'] += [e for e in d.get('elements') or [] if e not in c['_members']]
        if d['viewport'] not in c['viewports']:
            c['viewports'].append(d['viewport'])
        if d.get('element') and d['element'] not in c['_elements']:
            c['_elements'].append(d['element'])
        if base not in c['pages']:
            c['pages'].append(base)
        if d['page'] not in c['states']:
            c['states'].append(d['page'])
        if d['engine'] not in c['engines']:
            c['engines'].append(d['engine'])
        for eng, rs in d['runs'].items():
            c['runs'].setdefault(f'{d["page"]}|{eng}', rs)   # the first viewport's or sibling's runs stand for the rest
        if d.get('messages'):
            c['messages'][d['engine']] = sorted(set(c['messages'].get(d['engine'], [])) | set(d['messages']))
        if d.get('ink'):
            c['ink'].update(d['ink'])
    cands = list(merged.values())
    # link static features
    for c in cands:
        c['features'] = []
    if features:
        for f in features['features']:
            pat_tokens = {'ua-client-hints': 'userAgentData'}.get(f['feature'], f['name'].strip('()<>:'))
            for c in cands:
                text = ' '.join(m for ms in c['messages'].values() for m in ms)
                sel_hit = any(selector_matches(u.get('selector'), c) for u in f['files'])
                if (pat_tokens and pat_tokens in text) or sel_hit:
                    affected_families = {ENGINE_FAMILY[e] for e in c['engines']}
                    if affected_families & {u['engine'] for u in f['unsupported_in']}:
                        c['features'].append(f['feature'])
    platform = doc.get('platform')
    for c in cands:
        c['engines'].sort()
        c['viewports'].sort()
        if len(c['_elements']) > 1:   # siblings grouped by their component: which ones
            c['elements'] = c['_elements'][:50]
        c['build_limitations'] = build_limitations(c, platform)
    inherit_limitations(cands)
    for c in cands:
        sev = severity_of(c, cands)
        explained = bool(c['messages']) or bool(c['features'])
        conf = 'low' if c['build_limitations'] else 'high' if explained else 'medium'
        sh = share(c['engines'], caniuse)
        band = 1 if sh is None or sh < 5 else 2 if sh <= 20 else 3
        pages = min(3, len(c['pages']))
        score = WEIGHT[sev] * band * pages
        prio = 'P0' if score >= 24 else 'P1' if score >= 12 else 'P2' if score >= 6 else 'P3'
        crit = c['subkind'] == 'step_failed' and bool(set(c['states']) & critical_states)
        if crit and prio in ('P2', 'P3'):
            prio = 'P1'
        c.update(severity=sev, confidence=conf, usage_share=sh, share_band=band, reach=pages, score=score, priority=prio,
                 critical_path=crit)
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    cands.sort(key=lambda c: (order[c['priority']], -c['score'], c['page'], c['kind'], c['subkind'], c['key']))
    for i, c in enumerate(cands, 1):
        c['id'] = f'K{i}'
    return [{'id': c['id'], **{k: v for k, v in c.items() if k != 'id' and not k.startswith('_')}} for c in cands]


def inherit_limitations(cands: list[dict]) -> None:
    """A known build limitation of an undecoded image reaches what follows from it in the same engine and page: the <img>
    an error handler then hides (element_missing), the fallback it shows beside it (element_extra) and boxes resized
    around it. Those candidates carry the image's limitation (and say so), instead of reading as real differences."""
    imgs = [c for c in cands if c['subkind'] == 'image_not_decoded' and c['build_limitations']]
    # a font-family difference with a build limitation (a control drawn in the Windows WebKit default face) reaches the
    # boxes of the elements drawn in that face, and of what is inside them: a narrower input follows from its font
    fonts = [c for c in cands if c['subkind'] == 'font_family_differs' and c['build_limitations']]
    for c in cands:
        if c['build_limitations'] or c['subkind'] not in ('element_missing', 'element_extra', 'box_differs', 'content_not_rendered'):
            continue
        key = c['key'] or ''
        els = c.get('_elements') or ([c['element']] if c.get('element') else [])
        if c['subkind'] in ('box_differs', 'content_not_rendered'):
            src = next((f for f in fonts if set(f['engines']) & set(c['engines']) and set(f['pages']) & set(c['pages'])
                        and any(e == m or e.startswith(m + '>') for e in els for m in f.get('_members') or [])), None)
            if src:
                c['build_limitations'] = list(src['build_limitations'])
                c['detail'] = (c.get('detail') or '') + (f' (follows from its font family, drawn in another face in '
                                                         f'{", ".join(src["engines"])} ({src["key"]}), a known build '
                                                         'limitation)')
                continue
        for i in imgs:
            ik = i['key'] or ''
            related = key == ik or (parent_sig(key) is not None and parent_sig(key) == parent_sig(ik)) or ik.startswith(key + '>')
            if related and set(i['engines']) & set(c['engines']) and set(i['pages']) & set(c['pages']):
                c['build_limitations'] = list(i['build_limitations'])
                c['detail'] = (c.get('detail') or '') + f' (follows from the undecoded image {ik}, a known build limitation)'
                break


def load_limitations() -> list[dict]:
    return json.loads((HERE.parent / 'data' / 'engine-limitations.json').read_text(encoding='utf-8'))['limitations']


def build_limitations(c: dict, platform: str | None) -> list[str]:
    """Ids of known Playwright engine-build limitations (data/engine-limitations.json) this candidate matches. A capture
    without a recorded platform matches every platform: the difference may be the build's, not the browser's."""
    text = ' '.join([c.get('src') or '', c.get('element') or '', c.get('detail') or ''] +
                    [m for ms in c['messages'].values() for m in ms])
    text += ' ' + (c.get('_text') or '')
    return [L['id'] for L in load_limitations()
            if set(L['engine'] if isinstance(L['engine'], list) else [L['engine']]) & set(c['engines']) and c['subkind'] in L['subkinds']
            and (platform is None or L['platforms'] is None or platform in L['platforms']) and re.search(L['match'], text)
            and not (L.get('not_if') and re.search(L['not_if'], text))]


def selector_matches(selector: str | None, c: dict) -> bool:
    """Does the base of a CSS selector (before any pseudo-element) describe the candidate's element?"""
    if not selector or not c.get('element_tag'):
        return False
    base = re.split(r'::|:(?!not)', selector.split(',')[0].strip().split()[-1])[0]
    m = re.fullmatch(r'([a-z0-9]*)((?:\[[^\]]+\])*)', base)
    if not m:
        return False
    tag, attrs = m.group(1), re.findall(r'\[([a-z-]+)(?:="?([^"\]]*)"?)?\]', m.group(2))
    if tag and tag != c['element_tag']:
        return False
    for name, val in attrs:
        if name == 'type' and val and val != (c.get('element_type') or ''):
            return False
    return bool(tag or attrs)


STATIC_NOTE = 'static-only run: no browser was started'


def static_doc(targets: dict | None) -> dict:
    """The runs document of a run in which no browser started: every engine not_run, no runs, the plan from targets."""
    engines = (targets or {}).get('engines') or ['chromium', 'firefox', 'webkit']
    return {'engines': [{'name': e, 'version': None, 'status': 'not_run', 'note': STATIC_NOTE} for e in engines],
            'reruns': (targets or {}).get('reruns') or 1, 'planned': planned_checks(targets) if targets else [], 'runs': []}


def static_only(a) -> int:
    """Candidates file for a static-only check (run_status not_run): the feature scan, nothing compared."""
    if not a.features:
        print('--static-only needs --features (the feature scan is the whole static check)', file=sys.stderr)
        return 2
    targets = json.loads(a.targets.read_text(encoding='utf-8')) if a.targets else None
    doc = static_doc(targets)
    features = json.loads(a.features.read_text(encoding='utf-8'))
    for f in features['features']:
        f['linked_differences'] = []
    out = {'runs_file': None, 'run_status': 'not_run', 'reference': a.reference, 'engines': doc['engines'],
           'reruns': doc['reruns'], 'planned': doc['planned'], 'third_party': None, 'candidates': [], 'flaky': [],
           'dismissed_automatically': [], 'features': features, 'comparisons': comparisons(doc, a.reference)}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    cmp = out['comparisons']
    print(f'static only: {len(features["features"])} feature finding(s); 0 of {cmp["planned"]} engine comparison(s) made '
          f'(no browser ran) -> {a.out}', file=sys.stderr)
    return 0


def self_check(cands: list, flaky: list, dismissed: list, cmp: dict, stats: dict) -> str | None:
    """A line for a comparison that found nothing at all: no candidate, nothing flaky and nothing dismissed automatically
    although comparisons were made. Three engines rarely draw text identically, so say how many element boxes were
    compared and how many were identical, for the agent to check a screenshot pair by eye before writing "no
    differences detected" (fixed-size, inline-styled pages can legitimately match)."""
    if cands or flaky or dismissed or not cmp['made']:
        return None
    boxes, same = stats.get('boxes', 0), stats.get('identical', 0)
    return (f'0 candidates, 0 flaky and 0 automatic dismissals across {cmp["made"]} comparison(s): {same} of {boxes} element '
            'box(es) compared were identical to the pixel in both engines' + (' (no element box was compared)' if not boxes else '')
            + '. Open one screenshot pair per engine and confirm the pages really rendered alike before reporting no differences')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs', type=Path, nargs='?')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--features', type=Path)
    ap.add_argument('--targets', type=Path, help='targets.json, for critical-path states (and the plan with --static-only)')
    ap.add_argument('--reference', default='chromium')
    ap.add_argument('--static-only', action='store_true',
                    help='no browser ran: write the candidates file of a feature scan alone (engines not_run, no comparison '
                         'made, the planned checks from --targets if given), for a report with run_status not_run')
    ap.add_argument('--jobs', type=int, default=default_jobs(),
                    help='screenshots decoded in parallel (default: up to 4); 1 decodes in this process')
    a = ap.parse_args()
    if a.static_only:
        return static_only(a)
    if a.runs is None:
        ap.error('runs.json is required (or --static-only)')
    doc = json.loads(a.runs.read_text(encoding='utf-8'))
    try:
        from fetch_data import asset_path
        caniuse = json.loads(asset_path('caniuse-db').read_text(encoding='utf-8'))
    except FileNotFoundError:
        caniuse = None
    features = json.loads(a.features.read_text(encoding='utf-8')) if a.features else None
    critical = {r['page'] for r in doc['runs'] if r.get('critical_path')}
    stats: dict = {}
    screened = screen_runs(doc)
    env = environment(doc)
    diffs, flaky, dismissed = compare(doc, a.runs.parent, a.reference, a.jobs, stats)
    cands = candidates(diffs, dismissed, doc, caniuse, features, critical)
    if features:
        for f in features['features']:
            f['linked_differences'] = [c['id'] for c in cands if f['feature'] in c['features']]
            f['status'] = 'confirmed_at_runtime' if f['linked_differences'] else f['status']
    out = {'runs_file': a.runs.name, 'reference': a.reference, 'engines': doc['engines'], 'reruns': doc['reruns'],
           'third_party': third_party(doc), 'candidates': cands, 'flaky': flaky, 'dismissed_automatically': dismissed, 'features': features,
           'comparisons': comparisons(doc, a.reference), 'benign_requests': benign_requests(doc),
           'same_everywhere': same_everywhere(doc)}
    if screened:   # runs captured but not compared (an environment cause, a blank rerun): see screen_runs
        out['screened_runs'] = screened
    if env:
        out['environment'] = env
        for e in env:
            print(f'warning: environment: {e["detail"]}', file=sys.stderr)
    for s in out['same_everywhere']:
        print(f'warning: {s["page"]} ({", ".join(s["viewports"])}): ' +
              (f'step {s["step"] + 1} failed in every run of every engine ({(s["messages"] or [""])[0][:120]})'
               if s['kind'] == 'step_failed' else f'page error in every run of every engine: {s["message"][:120]}') +
              ': no engine difference, but the state is broken; say so in the report', file=sys.stderr)
    check = self_check(cands, flaky, dismissed, out['comparisons'], stats)
    if check:
        out['self_check'] = check
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    cmp = out['comparisons']
    if check:
        print(f'self-check: {check}', file=sys.stderr)
    print(f'{len(cands)} candidate(s), {len(flaky)} flaky, {len(dismissed)} dismissed automatically; {cmp["made"]} of '
          f'{cmp["planned"]} engine comparison(s) made -> {a.out}', file=sys.stderr)
    if cmp['not_made']:
        print(f'warning: {len(cmp["not_made"])} comparison(s) not made (runs errored or skipped): the verdict can only be '
              'inconclusive for them', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
