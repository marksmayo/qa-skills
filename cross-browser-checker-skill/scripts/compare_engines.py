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
movement, within 4 px or 5% in size), native form controls drawn at another size, a box that changed
size because something inside it changed, a box resized with its container, an element that moved
because a box above it grew, an input's box when its type fell back, or an element inside another
missing element.
Only captured runs are compared: a page, viewport and engine with any run errored or skipped
(run_status) is left out, and `comparisons` in the output lists every planned engine-versus-reference
pair, how many were made and why the others weren't. No comparison made means nothing was compared,
never "no differences".
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
INK_TAGS = {'input', 'img', 'button', 'select', 'textarea', 'h1', 'h2', 'h3', 'p', 'a', 'label', 'li', 'span', 'svg', 'canvas'}


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
        obs[('error', 'console_error')] = {'messages': (obs.get(('error', 'console_error')) or {}).get('messages', []) + [e]}
    for s in run.get('steps') or []:
        if s['outcome'] == 'failed':
            obs[('behaviour', 'step_failed', s['index'])] = {'step': s['action'], 'messages': [s['error']]}
    for r in run.get('failed_requests') or []:
        obs[('network', 'request_failed', r.split(' (')[0])] = {'messages': [r]}
    for i in run.get('images') or []:
        if i['complete'] and i['natural'] == [0, 0]:
            obs[('network', 'image_not_decoded', i['sig'])] = {'element': i['sig'], 'src': i['src']}
    if run.get('overflow_x'):
        obs[('layout', 'overflow')] = {'size': run.get('size')}
    n = 0
    for e in run.get('elements') or []:
        obs[('present', e['sig'])] = e
        if e['tag'] == 'input':
            obs[('type', e['sig'])] = {'type': e.get('type'), 'box': e['box']}
        if e['tag'] in INK_TAGS:
            v = inks[n] if inks is not None else None
            n += 1
            if v is not None:
                obs[('ink', e['sig'])] = {'ink': round(v, 4), 'box': e['box'], 'tag': e['tag'], 'type': e.get('type')}
    return obs


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


def comparisons(doc: dict, ref: str) -> dict:
    """Every planned (page or state, viewport, engine) pair against the reference, and whether it could be compared: both
    the engine and the reference need every rerun captured (a difference must hold in every run). Errored, skipped or
    missing runs leave it not compared, with the reason."""
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


def compare(doc: dict, base: Path, ref: str, jobs: int | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Differences, flaky observations and automatic dismissals, from the captured runs only: a run that errored or was
    skipped has no observations, and comparing it would turn the error into differences."""
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
            keys = set().union(*(r['obs'].keys() for r in R + E))
            for k in sorted(keys, key=str):
                if k[0] == 'present' and all(k in r['obs'] for r in R + E):
                    rb, eb = R[0]['obs'][k]['box'], E[0]['obs'][k]['box']
                    if all(r['obs'][k]['box'] == rb for r in R) and all(e['obs'][k]['box'] == eb for e in E):
                        moves.append((k[1], rb, [eb[i] - rb[i] for i in range(4)]))
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
                                                                                   runs={ref: [True] * len(R), eng: [True] * len(E)},
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
                if all(low):
                    o = E[0]['obs'][k]
                    diffs.append({'page': page, 'viewport': vp, 'engine': eng, 'ref': ref, 'kind': 'layout', 'subkind': 'content_not_rendered',
                                  'key': k[1], 'element': k[1], 'element_tag': o['tag'], 'element_type': o.get('type'),
                                  'ref_box': R[0]['obs'][k]['box'], **({'src': img_src(E, k[1])} if img_src(E, k[1]) else {}),
                                  'ink': {ref: rv, eng: ev}, 'runs': {ref: [False] * len(R), eng: low},
                                  'detail': f'{k[1]} shows {round(100 * max(ev), 1)}% ink in {eng} against {round(100 * min(rv), 1)}% in {ref}'})
                elif any(low):
                    flaky.append({'page': page, 'viewport': vp, 'kind': 'layout', 'subkind': 'content_not_rendered', 'key': k[1],
                                  'runs': {ref: [False] * len(R), eng: low}})
            drift(diffs, first, dismissed, moves)
    return diffs, flaky, dismissed


def drift(diffs: list[dict], first: int, dismissed: list[dict], moves: list[tuple[str, list, list]]) -> None:
    """Cumulative vertical drift: small text-metric differences add up down a page, so an element far down can sit 17-22 px
    lower in one engine although nothing near it differs. An element that only moved (no size change) is a consequence of
    the content above it when the nearest element above it (overlapping it horizontally) moved by nearly the same amount:
    within 16 px, the threshold for a move. A real jump (a step of more than 16 px between neighbours) stays a difference,
    and the elements below it are dismissed as moving with it."""
    keep = diffs[:first]
    for d in diffs[first:]:
        if d['subkind'] != 'box_differs' or d.get('size') or not d.get('delta'):
            keep.append(d)
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
    if tag in ('input', 'button', 'select', 'textarea') and not pos:
        return {'report': False, 'entry': {'kind': 'layout', 'subkind': 'box_differs', 'key': k[1], 'element': k[1],
                                           'delta': [dx, dy, dw, dh], 'ref_box': rb,
                                           'reason': f'native control rendering: engines draw <{tag}> at different sizes'}}
    return {'report': True, 'entry': {'kind': 'layout', 'subkind': 'box_differs', 'key': k[1], 'element': k[1], 'delta': [dx, dy, dw, dh],
                                      'ref_box': rb, 'size': size,
                                      'detail': f'{k[1]} box differs by dx {dx}, dy {dy}, dw {dw}, dh {dh}'}}


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
            above = next((c for c in same if c.get('ref_box') and d.get('ref_box') and c['subkind'] != 'box_differs' or
                          (c['subkind'] == 'box_differs' and c.get('size')) and c.get('ref_box') and d.get('ref_box') and
                          c['ref_box'][1] + c['ref_box'][3] <= d['ref_box'][1] and not d.get('size')), None)
            if above and not d.get('size') and above.get('ref_box') and above['ref_box'][1] < d['ref_box'][1]:
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
    if c['subkind'] in ('overflow', 'box_differs', 'element_extra', 'request_failed', 'input_type_fallback'):
        return 'Medium'
    return 'Low'


def candidates(diffs: list[dict], dismissed: list[dict], doc: dict, caniuse: dict | None, features: dict | None,
               critical_states: set) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for d in consequences(diffs, dismissed):
        base = d['page'].split('~')[0]
        k = (base, d['viewport'], d['kind'], d['subkind'], d['key'])
        c = merged.setdefault(k, {k2: d.get(k2) for k2 in ('kind', 'subkind', 'key', 'element', 'src', 'step', 'detail', 'element_tag',
                                                           'element_type', 'delta')}
                              | {'page': base, 'viewport': d['viewport'], 'states': [], 'engines': [], 'reference': d['ref'], 'runs': {},
                                 'messages': {}, 'ink': {}})
        if d['page'] not in c['states']:
            c['states'].append(d['page'])
        if d['engine'] not in c['engines']:
            c['engines'].append(d['engine'])
        for eng, rs in d['runs'].items():
            c['runs'][f'{d["page"]}|{eng}'] = rs
        if d.get('messages'):
            c['messages'][d['engine']] = d['messages']
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
        c['build_limitations'] = build_limitations(c, platform)
        sev = severity_of(c, cands)
        explained = bool(c['messages']) or bool(c['features'])
        conf = 'low' if c['build_limitations'] else 'high' if explained else 'medium'
        sh = share(c['engines'], caniuse)
        band = 1 if sh is None or sh < 5 else 2 if sh <= 20 else 3
        pages = 1
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
    return [{'id': c['id'], **{k: v for k, v in c.items() if k != 'id'}} for c in cands]


def load_limitations() -> list[dict]:
    return json.loads((HERE.parent / 'data' / 'engine-limitations.json').read_text(encoding='utf-8'))['limitations']


def build_limitations(c: dict, platform: str | None) -> list[str]:
    """Ids of known Playwright engine-build limitations (data/engine-limitations.json) this candidate matches. A capture
    without a recorded platform matches every platform: the difference may be the build's, not the browser's."""
    text = ' '.join([c.get('src') or '', c.get('element') or '', c.get('detail') or ''] +
                    [m for ms in c['messages'].values() for m in ms])
    return [L['id'] for L in load_limitations()
            if L['engine'] in c['engines'] and c['subkind'] in L['subkinds']
            and (platform is None or L['platforms'] is None or platform in L['platforms']) and re.search(L['match'], text)]


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
    diffs, flaky, dismissed = compare(doc, a.runs.parent, a.reference, a.jobs)
    cands = candidates(diffs, dismissed, doc, caniuse, features, critical)
    if features:
        for f in features['features']:
            f['linked_differences'] = [c['id'] for c in cands if f['feature'] in c['features']]
            f['status'] = 'confirmed_at_runtime' if f['linked_differences'] else f['status']
    out = {'runs_file': a.runs.name, 'reference': a.reference, 'engines': doc['engines'], 'reruns': doc['reruns'],
           'third_party': third_party(doc), 'candidates': cands, 'flaky': flaky, 'dismissed_automatically': dismissed, 'features': features,
           'comparisons': comparisons(doc, a.reference)}
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    cmp = out['comparisons']
    print(f'{len(cands)} candidate(s), {len(flaky)} flaky, {len(dismissed)} dismissed automatically; {cmp["made"]} of '
          f'{cmp["planned"]} engine comparison(s) made -> {a.out}', file=sys.stderr)
    if cmp['not_made']:
        print(f'warning: {len(cmp["not_made"])} comparison(s) not made (runs errored or skipped): the verdict can only be '
              'inconclusive for them', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
