#!/usr/bin/env python3
"""Merge normalised items into de-duplicated, component-level candidates and a draft SC matrix.

    python merge_findings.py items.json --run <run-dir> --out candidates.json

1. Instances: items about the same element (page + XPath) whose SCs overlap merge, across tools,
   viewports and states, keeping every source.
2. Candidates: instances with the same component signature and overlapping SCs merge across pages,
   so a defect in a shared header becomes one candidate listing every affected page.
3. Contrast "needs review" candidates get a `contrast_estimate` from the background the runner
   recorded, and those with the same cause and background collapse into one grouped candidate
   (`group`); any estimated below the threshold, or over an image or video (no ratio), stay separate
   (see group_contrast()). Colours in any CSS Color 4 syntax are converted to sRGB; `--self-test`
   checks the conversions against known ratios.

Each candidate keeps its sources (tool, version, rule, ACT ids), outcome (`failed` if any source
failed, otherwise `cantTell`), kind, a suggested confidence (high when two independent sources
agree or the failing rule implements an ACT rule; medium for one heuristic source or a probe
alone) and the default failure pattern. The agent triages every candidate: finding, needs review,
advisory or dismissed with a checkable reason. `sc_draft` gives every SC at the target level a
draft EARL outcome; it never drafts `passed`.
"""
from __future__ import annotations
import argparse, json, math, re, sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
LEVEL_ORDER = {'A': 0, 'AA': 1, 'AAA': 2}
VERSION_ORDER = {'2.0': 0, '2.1': 1, '2.2': 2}
SEVERITY_ORDER = ['minor', 'moderate', 'serious', 'critical']


def scs_overlap(a, b):
    return bool(set(a) & set(b)) or (not a and not b)


def default_pattern(scs: dict, sc_list, items):
    for i in items:
        if i.get('pattern'):
            return i['pattern']
    best = None
    for s in sc_list:
        for p in scs[s]['failure_patterns']:
            if best is None or SEVERITY_ORDER.index(p['default_severity']) > SEVERITY_ORDER.index(best[1]):
                best = (p['id'], p['default_severity'])
    return best[0] if best else None


def related_xpath(a: str | None, b: str | None) -> bool:
    """Same element, or one is the parent or grandparent of the other (an img inside its link)."""
    if not a or not b:
        return a == b
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short + '/') and long_[len(short) + 1:].count('/') <= 1


def merge(items: list[dict], scs: dict) -> list[dict]:
    # 1. instances per element (tools report an unnamed image link on the img or on the a)
    instances: list[dict] = []
    by_page: dict[tuple, list[dict]] = {}
    for it in items:
        bucket = by_page.setdefault((it['page'], it['kind']), [])
        for inst in bucket:
            same = related_xpath(inst['xpath'], it['xpath']) if (inst['xpath'] and it['xpath']) else \
                (inst['xpath'] is None and it['xpath'] is None and inst['sig'] == it['sig'])
            if same and scs_overlap(inst['sc'], it['sc']):
                inst['items'].append(it)
                inst['sc'] = sorted(set(inst['sc']) | set(it['sc']), key=sc_key)
                break
        else:
            inst = {'page': it['page'], 'xpath': it['xpath'], 'sig': it['sig'], 'kind': it['kind'], 'sc': list(it['sc']), 'items': [it]}
            bucket.append(inst)
            instances.append(inst)
    # 2. candidates per component signature
    cands: list[dict] = []
    by_sig: dict[tuple, list[dict]] = {}
    for inst in instances:
        key = (inst['sig'], inst['kind'])
        bucket = by_sig.setdefault(key, [])
        for c in bucket:
            if scs_overlap(c['sc'], inst['sc']):
                c['instances'].append(inst)
                c['sc'] = sorted(set(c['sc']) | set(inst['sc']), key=sc_key)
                break
        else:
            c = {'sig': inst['sig'], 'kind': inst['kind'], 'sc': list(inst['sc']), 'instances': [inst]}
            bucket.append(c)
            cands.append(c)
    cands = group_contrast(cands)
    out = []
    for c in cands:
        its = [i for inst in c['instances'] for i in inst['items']]
        failed = [i for i in its if i['outcome'] == 'failed']
        outcome = 'failed' if failed else 'cantTell'
        tools = sorted({i['tool'] if i['tool'] != 'probe' else f"probe:{i['rule_id'].split('.')[0]}" for i in (failed or its)})
        act = sorted({a for i in failed for a in i['act_rules']})
        if outcome == 'failed' and (len(tools) >= 2 or act):
            conf = 'high'
        else:
            conf = 'medium'
        sources = {}
        for i in its:
            k = (i['tool'], i['rule_id'])
            s = sources.setdefault(k, {'tool': i['tool'], 'tool_version': i['tool_version'], 'rule_id': i['rule_id'],
                                       'act_rules': i['act_rules'], 'outcomes': set(), 'count': 0, 'message': i['message']})
            s['outcomes'].add(i['outcome']); s['count'] += 1
        pages = sorted({inst['page'] for inst in c['instances']})
        inst_out = []
        for inst in c['instances']:
            first = inst['items'][0]
            inst_out.append({'page': inst['page'], 'url': first['url'], 'xpath': inst['xpath'],
                             'selector': next((i['selector'] for i in inst['items'] if i['selector']), None),
                             'html': next((i['html'] for i in inst['items'] if i['html']), None),
                             'viewports': sorted({i['viewport'] for i in inst['items']}),
                             'states': sorted({i['state'] for i in inst['items']}),
                             'screenshot': first['screenshot'],
                             'items': [i['id'] for i in inst['items']]})
        pattern = default_pattern(scs, c['sc'], failed or its) if c['kind'] == 'wcag' else None
        out.append({
            'sc': c['sc'], 'kind': c['kind'], 'outcome': outcome, 'component': c['sig'],
            'suggested_confidence': conf, 'agreeing_sources': tools if outcome == 'failed' else [],
            'evidence_type': 'probe' if all(i['tool'] == 'probe' for i in its) else 'tool',
            'pattern': pattern,
            'default_severity': next((p['default_severity'] for s in c['sc'] for p in scs[s]['failure_patterns'] if p['id'] == pattern), None),
            'non_interference': any(scs[s]['non_interference'] for s in c['sc']),
            'pages': pages, 'instance_count': len(inst_out), 'instances': inst_out,
            'sources': [{**v, 'outcomes': sorted(v['outcomes'])} for v in sources.values()],
            'related_sc': sorted({r for i in its for r in i['related_sc']}, key=sc_key),
            **({'contrast_estimate': c['contrast_estimate']} if c.get('contrast_estimate') else {}),
            **({'group': c['group']} if c.get('group') else {}),
        })
    order = {'failed': 0, 'cantTell': 1}
    kind_order = {'wcag': 0, 'above_level': 1, 'outside_version': 2, 'best_practice': 3, 'experimental': 4, 'obsolete': 5}
    out.sort(key=lambda c: (kind_order.get(c['kind'], 9), order[c['outcome']], sc_key(c['sc'][0]) if c['sc'] else [99], c['component']))
    for n, c in enumerate(out, 1):
        c['id'] = f'C-{n:03d}'
    return [{'id': c.pop('id'), **c} for c in out]


def sc_key(s):
    return [int(x) for x in s.split('.')]


# --- contrast "needs review" ------------------------------------------------------------------
# A faint page-level gradient makes axe and HTML_CodeSniffer ask for a manual contrast check on
# nearly every piece of text, burying the real failures. run_audit.py records what lies behind each
# such element (the background image, its colour stops, translucent overlays, the text colour), so
# here each instance gets an estimated worst-case ratio, and candidates with the same cause and the
# same background collapse into one grouped candidate. Instances estimated below the threshold
# are never grouped; they stay separate candidates so they aren't buried.

AXE_CAUSE = {'bgGradient': 'background gradient', 'bgImage': 'background image', 'pseudoContent': 'pseudo-element background',
             'bgOverlap': 'overlapping elements', 'elmPartiallyObscured': 'overlapping elements',
             'elmPartiallyObscuring': 'overlapping elements', 'outsideViewport': 'outside the viewport',
             'nonBmp': 'non-text content', 'shortTextContent': 'non-text content', 'equalRatio': 'same text and background colour'}
HTMLCS_CAUSE = {'Alpha': 'transparent colour', 'BgImage': 'background image', 'Abs': 'absolute positioning'}


def is_contrast(it) -> bool:
    return it['outcome'] == 'cantTell' and it['kind'] == 'wcag' and (
        it['rule_id'].startswith('color-contrast') or it['rule_id'] == 'text_contrast_sufficient' or bool(re.search(r'1_4_[36]\.', it['rule_id'])))


def contrast_cause(it) -> str:
    d = it.get('data') or {}
    ctx = d.get('contrast') or {}
    if ctx.get('visually_hidden'):
        return 'visually hidden text'
    if ctx.get('bg_image_on') and not ctx.get('opaque_first'):
        return 'background gradient' if 'gradient(' in (ctx.get('bg_image') or '') else 'background image'
    key = (d.get('axe_check') or {}).get('messageKey')
    if key in AXE_CAUSE:
        return AXE_CAUSE[key]
    if it['tool'] == 'htmlcs':
        return HTMLCS_CAUSE.get(it['rule_id'].rsplit('.', 1)[-1], it['rule_id'].rsplit('.', 1)[-1])
    msg = re.sub(r'^Fix any of the following:\s*', '', it.get('message') or '').strip()
    return re.sub(r'[\d.#]+', '', msg)[:80] or it['rule_id']


# --- CSS colours -------------------------------------------------------------------------------
# Browsers report computed colours as rgb()/rgba() for legacy colours but keep CSS Color 4 spaces as
# written (Tailwind 4 emits oklch(); Chromium serialises some as lab()). Each is converted to sRGB
# (CSS Color 4 conversion formulas, out-of-gamut channels clipped); anything not understood gives
# None, so there is no estimate rather than a wrong one.

COLOR_FN = r'(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\([^()]*\)|#[0-9a-fA-F]{3,8}\b|\btransparent\b'
_XYZ65_TO_LSRGB = [[3.2409699419045226, -1.537383177570094, -0.4986107602930034],
                   [-0.9692436362808796, 1.8759675015077202, 0.04155505740717559],
                   [0.05563007969699366, -0.20397695888897652, 1.0569715142428786]]
_D50_TO_D65 = [[0.955473421488075, -0.02309845494876471, 0.06325924320057072],
               [-0.0283697093338637, 1.0099953980813041, 0.021041441191917323],
               [0.012314014864481998, -0.020507649298898964, 1.330365926242124]]
_LP3_TO_XYZ65 = [[0.4865709486482162, 0.26566769316909306, 0.1982172852343625],
                 [0.2289745640697488, 0.6917385218365064, 0.079286914093745],
                 [0.0, 0.04511338185890264, 1.043944368900976]]
_D50_WHITE = [0.3457 / 0.3585, 1.0, (1 - 0.3457 - 0.3585) / 0.3585]


def _mul(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def _encode(x):   # linear-light to sRGB (also display-p3's transfer curve)
    s = -1 if x < 0 else 1
    x = abs(x)
    return s * (1.055 * x ** (1 / 2.4) - 0.055 if x > 0.0031308 else 12.92 * x)


def _decode(x):
    s = -1 if x < 0 else 1
    x = abs(x)
    return s * (x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4)


def _lab_to_xyz65(L, a, b):
    k, e = 24389 / 27, 216 / 24389
    f1 = (L + 16) / 116
    f0, f2 = a / 500 + f1, f1 - b / 200
    x = f0 ** 3 if f0 ** 3 > e else (116 * f0 - 16) / k
    y = f1 ** 3 if L > k * e else L / k
    z = f2 ** 3 if f2 ** 3 > e else (116 * f2 - 16) / k
    return _mul(_D50_TO_D65, [x * _D50_WHITE[0], y * _D50_WHITE[1], z * _D50_WHITE[2]])


def _oklab_to_lsrgb(L, a, b):
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return [4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
            -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
            -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_]


def _num(tok: str, pct_scale: float = 1.0) -> float:
    """A CSS number, percentage (scaled so 100% = pct_scale) or `none` (0)."""
    t = tok.strip().lower()
    if t == 'none':
        return 0.0
    if t.endswith('%'):
        return float(t[:-1]) / 100 * pct_scale
    return float(t)


def _hue(tok: str) -> float:
    t = tok.strip().lower()
    if t == 'none':
        return 0.0
    for unit, f in (('grad', 0.9), ('turn', 360.0), ('rad', 180 / math.pi), ('deg', 1.0)):
        if t.endswith(unit):
            return float(t[:-len(unit)]) * f
    return float(t)


def _hsl(h, s, l):
    h %= 360
    f = lambda n: l - s * min(l, 1 - l) * max(-1, min((n + h / 30) % 12 - 3, 9 - (n + h / 30) % 12, 1))
    return [f(0), f(8), f(4)]


def parse_color(s):
    """[r, g, b, alpha] with r, g, b on 0-255 (sRGB, clipped to the gamut) and alpha on 0-1, or None."""
    if not s:
        return None
    s = s.strip()
    low = s.lower()
    if low == 'transparent':
        return [0.0, 0.0, 0.0, 0.0]
    if low.startswith('#'):
        h = low[1:]
        if len(h) in (3, 4):
            h = ''.join(c * 2 for c in h)
        if len(h) not in (6, 8) or not re.fullmatch(r'[0-9a-f]+', h):
            return None
        v = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
        return [float(v[0]), float(v[1]), float(v[2]), v[3] / 255 if len(v) == 4 else 1.0]
    m = re.fullmatch(r'([a-z-]+)\((.*)\)', low, re.S)
    if not m:
        return None
    fn, body = m.group(1), m.group(2).strip()
    alpha = 1.0
    try:
        if '/' in body:
            body, a = body.rsplit('/', 1)
            alpha = _num(a)
        parts = [p for p in re.split(r'[\s,]+', body.strip()) if p]
        if fn in ('rgb', 'rgba', 'hsl', 'hsla') and len(parts) == 4:
            alpha = _num(parts.pop())
        alpha = max(0.0, min(1.0, alpha))
        if fn == 'color':
            space, parts = parts[0], parts[1:]
        if len(parts) != 3:
            return None
        if fn in ('rgb', 'rgba'):   # kept on 0-255 so legacy colours give exactly the numbers the browser reported
            return [max(0.0, min(255.0, _num(p, 255))) for p in parts] + [alpha]
        if fn in ('hsl', 'hsla'):
            rgb = _hsl(_hue(parts[0]), _num(parts[1], 100) / 100, _num(parts[2], 100) / 100)
        elif fn == 'hwb':
            w, bk = _num(parts[1], 100) / 100, _num(parts[2], 100) / 100
            if w + bk >= 1:
                rgb = [w / (w + bk)] * 3
            else:
                rgb = [c * (1 - w - bk) + w for c in _hsl(_hue(parts[0]), 1, 0.5)]
        elif fn in ('lab', 'lch'):
            L = _num(parts[0], 100)
            if fn == 'lab':
                a, b = _num(parts[1], 125), _num(parts[2], 125)
            else:
                c, h = _num(parts[1], 150), math.radians(_hue(parts[2]))
                a, b = c * math.cos(h), c * math.sin(h)
            rgb = [_encode(x) for x in _mul(_XYZ65_TO_LSRGB, _lab_to_xyz65(L, a, b))]
        elif fn in ('oklab', 'oklch'):
            L = _num(parts[0], 1)
            if fn == 'oklab':
                a, b = _num(parts[1], 0.4), _num(parts[2], 0.4)
            else:
                c, h = _num(parts[1], 0.4), math.radians(_hue(parts[2]))
                a, b = c * math.cos(h), c * math.sin(h)
            rgb = [_encode(x) for x in _oklab_to_lsrgb(L, a, b)]
        elif fn == 'color':
            v = [_num(p, 1) for p in parts]
            if space == 'srgb':
                rgb = v
            elif space == 'srgb-linear':
                rgb = [_encode(x) for x in v]
            elif space == 'display-p3':
                rgb = [_encode(x) for x in _mul(_XYZ65_TO_LSRGB, _mul(_LP3_TO_XYZ65, [_decode(x) for x in v]))]
            elif space in ('xyz', 'xyz-d65'):
                rgb = [_encode(x) for x in _mul(_XYZ65_TO_LSRGB, v)]
            elif space == 'xyz-d50':
                rgb = [_encode(x) for x in _mul(_XYZ65_TO_LSRGB, _mul(_D50_TO_D65, v))]
            else:
                return None   # a98-rgb, prophoto-rgb, rec2020: not converted
        else:
            return None
    except (ValueError, IndexError):
        return None
    return [max(0.0, min(1.0, c)) * 255 for c in rgb] + [alpha]


def rgba(s):
    return parse_color(s)


def colours_in(text: str) -> list:
    """Every colour in a gradient or other CSS value, in order (unparseable ones as None)."""
    return [parse_color(m) for m in re.findall(COLOR_FN, text or '', re.I)]


def ratio(fg, bg) -> float:
    a, b = lum(fg) + 0.05, lum(bg) + 0.05
    return max(a, b) / min(a, b)


def over(top, base):
    a = top[3]
    return [top[k] * a + base[k] * (1 - a) for k in range(3)] + [1.0]


def lum(c):
    f = lambda x: x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0] / 255) + 0.7152 * f(c[1] / 255) + 0.0722 * f(c[2] / 255)


def estimate(ctx) -> dict | None:
    """Worst-case contrast of the text against what is behind it, from the recorded colours.

    A gradient interpolates channel by channel between its stops, and relative luminance rises
    with every channel, so along each segment luminance moves monotonically: the background's
    luminance range is spanned by its stops, and the lowest ratio is at the stop nearest the
    text's luminance (or 1:1 if the text's luminance falls inside the range). Translucent layers
    are composited over each stop, which keeps the interpolation linear. Images (url()) can't be
    estimated. Text over an <img>, <video>, <canvas> or <picture> that isn't its CSS background
    (recorded as media_behind) has an unknown background: the estimate says so, with no ratio, and the
    candidate needs review rather than a ratio against the section colour. A colour that can't be
    parsed gives no estimate at all."""
    if not ctx or ctx.get('visually_hidden'):
        return None
    px, wt = ctx.get('font_px') or 16, ctx.get('font_weight') or 400
    need = 3.0 if px >= 24 or (px >= 18.66 and wt >= 700) else 4.5
    if ctx.get('media_behind'):
        mb = ctx['media_behind']
        return {'min_ratio': None, 'required': need, 'below': None, 'unknown_background': True,
                'basis': f"text over <{mb.get('tag') or 'media'}>: the background is an image or video; check the screenshot"}
    fg, base = rgba(ctx.get('fg')), rgba(ctx.get('bg_color'))
    if not fg or fg[3] < 1 or not base:
        return None
    image = ctx.get('bg_image') or ''
    if ctx.get('opaque_first') or not ctx.get('bg_image_on'):
        stops, basis = [base], 'opaque background'
    elif 'url(' in image or 'gradient(' not in image:
        return None
    else:
        found = colours_in(image)
        if not found or any(c is None for c in found):
            return None
        stops = [over(c, base) for c in found]
        basis = 'gradient colour stops'
    layers = [rgba(o) for o in reversed(ctx.get('overlays') or [])]
    if any(o is None for o in layers):
        return None
    shown = []
    for s in stops:
        for o in layers:
            if o:
                s = over(o, s)
        shown.append(lum(s))
    lf, lo, hi = lum(fg), min(shown), max(shown)
    worst = 1.0 if lo <= lf <= hi else (lo + 0.05) / (lf + 0.05) if lf < lo else (lf + 0.05) / (hi + 0.05)
    return {'min_ratio': round(worst, 2), 'required': need, 'below': worst < need, 'basis': basis}


def margin(e: dict) -> float:
    """How far an estimate clears its threshold (lowest first); an unknown background sorts first."""
    return -1e9 if e.get('min_ratio') is None else e['min_ratio'] - e['required']


SELF_TEST = [
    # (text colour, background, expected ratio, tolerance) - hex values checked against the WCAG formula by hand
    ('#00d492', '#ffffff', 1.94, 0.01),                      # Tailwind 4 emerald-400 on white: L = 0.4917, (1.05 / 0.5417)
    ('oklch(76.5% 0.177 163.223)', 'rgb(255, 255, 255)', 1.94, 0.02),   # the same colour as Tailwind 4 writes it
    ('lab(75.0771 -60.7313 19.4147)', '#fff', 1.94, 0.02),   # and as Chromium serialises it (the minus signs matter)
    ('oklab(0.765 -0.1695 0.0511)', '#fff', 1.94, 0.02),
    ('lch(75.0771 63.76 162.27)', '#fff', 1.94, 0.02),
    ('color(srgb 0 0.831 0.573)', '#ffffff', 1.94, 0.01),
    ('#78746c', '#fafaf9', 4.46, 0.02),                      # a muted grey just under 4.5:1
    ('#000', '#fff', 21.0, 0.001),
    ('rgb(0 0 0 / 100%)', 'rgb(255 255 255)', 21.0, 0.001),  # space-separated syntax
    ('hsl(0 0% 46.7%)', '#fff', 4.5, 0.03),
]


def self_test() -> int:
    bad = 0
    for fg, bg, want, tol in SELF_TEST:
        a, b = parse_color(fg), parse_color(bg)
        if want is None:
            continue
        got = ratio(a, b) if a and b else None
        ok = got is not None and abs(got - want) <= tol
        bad += not ok
        print(f'{"ok  " if ok else "FAIL"} {fg} on {bg}: {got if got is None else round(got, 3)} (expected {want})')
    checks = [(parse_color('transparent'), [0.0, 0.0, 0.0, 0.0]), (parse_color('white'), None), (parse_color('foo(1 2 3)'), None),
              (parse_color('rgba(0, 0, 0, 0.5)'), [0.0, 0.0, 0.0, 0.5]), (parse_color('lab(50 0 0 / 0.25)')[3], 0.25),
              (parse_color('color(rec2020 1 0 0)'), None), (round(parse_color('oklch(0.6 none 0)')[0]), round(parse_color('oklab(0.6 0 0)')[0])),
              (len(colours_in('linear-gradient(90deg, oklch(0.9 0.02 100) 0%, rgb(255, 255, 255) 100%)')), 2)]
    for got, want in checks:
        ok = got == want
        bad += not ok
        print(f'{"ok  " if ok else "FAIL"} {got} == {want}')
    est = estimate({'fg': 'lab(75.0771 -60.7313 19.4147)', 'bg_color': 'rgb(250, 250, 249)', 'font_px': 16})
    ok = est and est['below'] and est['min_ratio'] < 2
    bad += not ok
    print(f'{"ok  " if ok else "FAIL"} estimate for lab() text on near-white is below 4.5:1 ({est})')
    est = estimate({'fg': 'rgb(16, 185, 129)', 'bg_color': 'rgb(250, 250, 249)', 'media_behind': {'tag': 'video'}})
    ok = est and est['min_ratio'] is None and est['unknown_background']
    bad += not ok
    print(f'{"ok  " if ok else "FAIL"} text over a video has an unknown background ({est})')
    print('self-test passed' if not bad else f'self-test FAILED: {bad} check(s)')
    return 1 if bad else 0


def group_contrast(cands: list[dict], min_group: int = 2) -> list[dict]:
    keyed: dict[tuple, list[dict]] = {}
    for c in cands:
        its = [i for inst in c['instances'] for i in inst['items']]
        if c['kind'] != 'wcag' or not its or not all(is_contrast(i) for i in its):
            continue
        ests = [e for e in (estimate((i.get('data') or {}).get('contrast')) for i in its) if e]
        if ests:
            c['contrast_estimate'] = min(ests, key=margin)
        if any(e['below'] or e.get('unknown_background') for e in ests):
            continue  # possibly a real failure, or text over an image or video: keep it as its own candidate
        if not all((i.get('data') or {}).get('contrast') for i in its):
            continue  # no record of the background (older runs): grouping would be a guess
        causes = tuple(sorted({contrast_cause(i) for i in its}))
        bgs = tuple(sorted({((i['data']['contrast'].get('bg_image_on') or {}).get('sig') or '', i['data']['contrast'].get('bg_image') or '')
                            for i in its if not i['data']['contrast'].get('visually_hidden')}))
        keyed.setdefault((causes, bgs), []).append(c)
    grouped_ids = set()
    out = []
    for (causes, bgs), members in keyed.items():
        if len(members) < min_group:
            continue
        insts = [inst for m in members for inst in m['instances']]
        ests = [m['contrast_estimate'] for m in members if m.get('contrast_estimate')]
        fgs = sorted({i['data']['contrast'].get('fg') for inst in insts for i in inst['items']})
        on = ', '.join(sorted({b[0] for b in bgs if b[0]})) or 'none'
        g = {'sig': f"contrast needs review: {' + '.join(causes)} (background on {on})", 'kind': 'wcag',
             'sc': sorted({s for m in members for s in m['sc']}, key=sc_key), 'instances': insts,
             'contrast_estimate': min(ests, key=margin) if ests else None,
             'group': {'cause': list(causes), 'backgrounds': [{'on': b[0] or None, 'image': b[1] or None} for b in bgs],
                       'candidate_count': len(members), 'instance_count': len(insts), 'text_colours': fgs[:10],
                       'estimated': len(ests), 'members': [{'component': m['sig'], 'pages': sorted({i['page'] for i in m['instances']}),
                                                            'instance_count': len(m['instances'])} for m in members],
                       'sample_selectors': [s for s in dict.fromkeys(next((i['selector'] for i in inst['items'] if i['selector']), None) for inst in insts) if s][:8]}}
        grouped_ids |= {id(m) for m in members}
        out.append(g)
    return [c for c in cands if id(c) not in grouped_ids] + out


def draft_matrix(cands, scs, version, level, run, evidence):
    probes_ran = {}
    for pg in run.get('pages', []):
        for vp in (pg.get('viewports') or {}).values():
            for k, v in (vp.get('probes') or {}).items():
                probes_ran.setdefault(k, set()).add(v)
    media_none = 'ran' in probes_ran.get('media', set()) and not any(
        c for c in cands if any(s.startswith('1.2.') for s in c['sc']))
    rows = []
    for s in sorted(scs.values(), key=lambda x: sc_key(x['sc'])):
        if VERSION_ORDER[s['added_in']] > VERSION_ORDER[version] or LEVEL_ORDER[s['level']] > LEVEL_ORDER[level]:
            continue
        ids_f = [c['id'] for c in cands if c['kind'] == 'wcag' and s['sc'] in c['sc'] and c['outcome'] == 'failed']
        ids_c = [c['id'] for c in cands if c['kind'] == 'wcag' and s['sc'] in c['sc'] and c['outcome'] == 'cantTell']
        if s.get('obsolete_in') and VERSION_ORDER[version] >= VERSION_ORDER[s['obsolete_in']]:
            outcome, note = 'inapplicable', f"Obsolete in WCAG {s['obsolete_in']}"
        elif ids_f:
            outcome, note = 'failed', 'Draft: failing candidates found; confirm in triage'
        elif ids_c:
            outcome, note = 'cantTell', 'Draft: only needs-review candidates'
        elif s['sc'].startswith('1.2.') and media_none:
            outcome, note = 'inapplicable', 'Draft: no video, audio or embedded player in the sampled pages; confirm'
        else:
            outcome, note = 'untested', 'Draft: no automated failure; tool passes never establish a pass'
        rows.append({'sc': s['sc'], 'title': s['title'], 'level': s['level'], 'automatable': s['automatable'],
                     'draft_outcome': outcome, 'failed_candidates': ids_f, 'cant_tell_candidates': ids_c,
                     'evidence': evidence.get(s['sc'], {}), 'note': note})
    return rows


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('items')
    ap.add_argument('--run', required=True, help='run directory (for run.json)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--self-test', action='store_true', help='check the colour parser and contrast maths, then exit')
    args = ap.parse_args()
    d = json.loads(Path(args.items).read_text(encoding='utf-8'))
    run = json.loads((Path(args.run) / 'run.json').read_text(encoding='utf-8'))
    scs = {s['sc']: s for s in json.loads((DATA / 'wcag-sc.json').read_text(encoding='utf-8'))['success_criteria']}
    cands = merge(d['items'], scs)
    matrix = draft_matrix(cands, scs, d['wcag_version'], d['level'], run, d['sc_evidence'])
    out = {'tool': 'merge_findings.py', 'wcag_version': d['wcag_version'], 'level': d['level'], 'item_count': len(d['items']),
           'candidates': cands, 'sc_draft': matrix}
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding='utf-8')
    summary = {}
    for c in cands:
        summary[f"{c['kind']}/{c['outcome']}"] = summary.get(f"{c['kind']}/{c['outcome']}", 0) + 1
    print(json.dumps({'items': len(d['items']), 'candidates': len(cands), 'by_kind': summary,
                      'sc_draft': {o: sum(1 for r in matrix if r['draft_outcome'] == o) for o in ('failed', 'cantTell', 'inapplicable', 'untested')}}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
