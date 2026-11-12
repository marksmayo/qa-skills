#!/usr/bin/env python3
"""Render pages in the source locale, a pseudo-locale, every shipped locale and RTL locales, and measure what breaks.

    python render_locales.py targets.json --out run/runtime [--resources run/resources.json]

targets.json:
    {"site": "http://127.0.0.1:8146/", "pages": [{"name": "home", "path": "index.html", "critical_path": false}],
     "viewports": ["1280x800"], "source_locale": "en", "locales": ["en", "de", "fr", "ar"], "rtl_locales": ["ar", "he"],
     "locale_strategy": {"query": "lang"},              # or {"cookie": "locale"} or {"path_prefix": true} or {"accept_language": true}
     "resources": "locales/{locale}.json",              # where the page loads its messages; enables pseudo by interception
     "pseudo": true, "authorised_by": null, "auth": null}

"auth": {"storage_state": "<file outside the repo>"} renders an app behind sign-in in a session a person
captured with `python browser_harness.py capture-session --url <app> --out <file>`; the output records only
that a session was used ("auth": {"method": ...}).

Pseudo-locale: the source resource file is rewritten as it is served (Playwright route): letters become accented
look-alikes, text grows 30-50% (short strings grow more) and each message is wrapped in ⟦ ⟧ so truncation shows.
ICU arguments, plural keys and # are left alone. Without `resources`, a text-node walk does it after load (it
can't reach attributes or text added later), and the run records which method was used.

Measured per run (each with a full-page screenshot):
  truncation      an element whose text is wider than its box while overflow is hidden or clipped (px overflow)
  overlap         two text elements whose boxes intersect (px area)
  untranslated    in the pseudo run, visible text outside ⟦ ⟧ (hard-coded, or concatenated around a translation)
  raw_placeholder text still showing {name} or {{name}}
  missing_key     text that is a message key (nav.events), as loaders show for a missing translation
  format          numbers, prices and dates whose separators or currency position don't match CLDR for the locale
  rtl_mirroring   in an RTL locale: dir not rtl; physical left/right spacing that doesn't flip; arrows that point the
                  wrong way
A locale in rtl_locales or locales with no resources is not_run, with the reason.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from urllib.parse import urljoin, urlencode

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now  # noqa: E402

ACCENT = dict(zip('aeiouyAEIOUYcnsCNSgGlLzZ', 'áéíöüýÅÉÎÖÜÝçñšÇÑŠĝĜļĻžŽ'))
PROTECT = re.compile(r'\{\s*\w+\s*\}|\{\{\s*[\w.]+\s*\}\}|\{\s*\w+\s*,\s*\w+\s*,|(?:=\d+|zero|one|two|few|many|other)\s*\{|[{}#]|%\d*\$?[sd@]')
SNAP = r"""
() => {
  const out = {dir: document.documentElement.dir || getComputedStyle(document.documentElement).direction, lang: document.documentElement.lang,
               width: document.documentElement.clientWidth, texts: [], sided: []};
  const sel = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/)[0] : '')
                      + (el.parentElement ? ':in(' + (el.parentElement.id ? '#' + el.parentElement.id : el.parentElement.tagName.toLowerCase()) + ')' : '');
  const textEls = [];
  for (const el of document.querySelectorAll('body *')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (own && /[^\s\u00b7\u2022|]/.test(own)) textEls.push(el);
    if (own) out.texts.push({sel: sel(el), tag: el.tagName.toLowerCase(), text: own, box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                             overflow_x: cs.overflowX, scroll_w: el.scrollWidth, client_w: el.clientWidth, text_overflow: cs.textOverflow});
    const sides = {};
    for (const p of ['marginLeft', 'marginRight', 'paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']) sides[p] = parseFloat(cs[p]) || 0;
    if (sides.marginLeft !== sides.marginRight || sides.paddingLeft !== sides.paddingRight || sides.borderLeftWidth !== sides.borderRightWidth)
      out.sided.push({sel: sel(el), tag: el.tagName.toLowerCase(), sides, box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                      transform: cs.transform});
  }
  out.overlaps = [];
  for (let i = 0; i < textEls.length; i++) for (let j = i + 1; j < textEls.length; j++) {
    const a = textEls[i], b = textEls[j];
    if (a.contains(b) || b.contains(a)) continue;
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if (w > 2 && h > 2) out.overlaps.push({a: sel(a), b: sel(b), text: (a.textContent.trim().slice(0, 40) + ' / ' + b.textContent.trim().slice(0, 40)),
                                           box: [Math.round(ra.left), Math.round(ra.top), Math.round(ra.width), Math.round(ra.height)], area: Math.round(w * h)});
  }
  return out;
}
"""


def pseudo_text(s: str) -> str:
    out, pos, letters = [], 0, 0
    for m in PROTECT.finditer(s):
        seg = s[pos:m.start()]
        out.append(''.join(ACCENT.get(c, c) for c in seg))
        letters += sum(c.isalpha() for c in seg)
        out.append(m.group(0))
        pos = m.end()
    seg = s[pos:]
    out.append(''.join(ACCENT.get(c, c) for c in seg))
    letters += sum(c.isalpha() for c in seg)
    grow = 0.5 if letters <= 10 else 0.4 if letters <= 20 else 0.3
    pad = '·' * max(1, round(letters * grow))
    body = ''.join(out)
    if body.startswith('{') and body.endswith('}'):
        return '⟦' + body + pad + '⟧'
    return '⟦' + body + pad + '⟧'


def pseudo_json(obj):
    if isinstance(obj, dict):
        return {k: pseudo_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [pseudo_json(v) for v in obj]
    return pseudo_text(obj) if isinstance(obj, str) else obj


WALK = r"""
(fn) => { const map = {}; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const ac = {a:'á',e:'é',i:'í',o:'ö',u:'ü',y:'ý',A:'Å',E:'É',I:'Î',O:'Ö',U:'Ü',c:'ç',n:'ñ',s:'š'};
  let n; while ((n = w.nextNode())) { if (!n.textContent.trim() || ['SCRIPT','STYLE'].includes(n.parentElement.tagName)) continue;
    const t = n.textContent.trim(); const pad = '·'.repeat(Math.max(1, Math.round(t.length * (t.length <= 10 ? 0.5 : t.length <= 20 ? 0.4 : 0.3))));
    n.textContent = '⟦' + t.replace(/[aeiouyAEIOUcns]/g, c => ac[c] || c) + pad + '⟧'; } }
"""


def url_for(cfg: dict, path: str, locale: str) -> str:
    st = cfg.get('locale_strategy') or {'query': 'lang'}
    base = urljoin(cfg['site'], path)
    if 'query' in st:
        return base + ('&' if '?' in base else '?') + urlencode({st['query']: locale})
    if st.get('path_prefix'):
        return urljoin(cfg['site'], f'{locale}/{path}')
    return base


def outside_brackets(text: str) -> str:
    return re.sub(r'⟦[^⟧]*⟧', ' ', text)


def check(snap: dict, mode: str, locale: str, formats: dict, keys: set[str]) -> list[dict]:
    out = []
    texts = snap['texts']
    for t in texts:
        if t['overflow_x'] in ('hidden', 'clip') and t['scroll_w'] > t['client_w'] + 1:
            out.append({'kind': 'truncation', 'element': t['sel'], 'text': t['text'], 'box': t['box'],
                        'measure': {'overflow_px': t['scroll_w'] - t['client_w'], 'box_width': t['client_w'], 'text_width': t['scroll_w']}})
        if re.search(r'(?<!\{)\{\s*[A-Za-z_]\w*\s*\}(?!\})|\{\{\s*[\w.]+\s*\}\}', t['text']):
            out.append({'kind': 'raw_placeholder', 'element': t['sel'], 'text': t['text'], 'box': t['box'],
                        'measure': {'placeholders': re.findall(r'\{\{?\s*[\w.]+\s*\}?\}', t['text'])}})
        if t['text'] in keys:
            out.append({'kind': 'missing_key', 'element': t['sel'], 'text': t['text'], 'box': t['box'], 'measure': {'key_shown': t['text']}})
        if mode == 'pseudo':
            rest = outside_brackets(t['text'])
            if re.search(r'[A-Za-z]{2,}', rest):
                out.append({'kind': 'untranslated', 'element': t['sel'], 'text': t['text'], 'box': t['box'],
                            'measure': {'outside_pseudo': ' '.join(rest.split()), 'partly_translated': '⟦' in t['text']}})
        if mode in ('locale', 'rtl') and locale in formats:
            out += format_issues(t, locale, formats[locale])
    for o in snap.get('overlaps') or []:
        out.append({'kind': 'overlap', 'element': f'{o["a"]} / {o["b"]}', 'text': o['text'], 'box': o['box'], 'measure': {'overlap_px': o['area']}})
    if mode == 'rtl':
        if snap['dir'] != 'rtl':
            out.append({'kind': 'rtl_mirroring', 'element': 'html', 'text': '', 'box': [0, 0, 0, 0], 'measure': {'dir': snap['dir']}})
        for s in snap['sided']:
            sd = s['sides']
            for prop, other in (('marginLeft', 'marginRight'), ('paddingLeft', 'paddingRight'), ('borderLeftWidth', 'borderRightWidth')):
                if sd[prop] > 0 and sd[other] == 0:
                    out.append({'kind': 'rtl_mirroring', 'element': s['sel'], 'text': '', 'box': s['box'],
                                'measure': {'property': re.sub(r'([A-Z])', r'-\1', prop).lower(), 'px': sd[prop],
                                            'expected': 'the inline-start side (right in RTL)'}})
        for t in texts:
            if re.search(r'[→⇒➔›»]', t['text']):
                out.append({'kind': 'rtl_mirroring', 'element': t['sel'], 'text': t['text'], 'box': t['box'],
                            'measure': {'glyph': ''.join(re.findall(r'[→⇒➔›»]', t['text'])), 'expected': 'a glyph pointing left in RTL'}})
    return out


NUM = re.compile(r'([$€£¥])?\s?(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{2}))\s?([$€£¥])?')
DATE = re.compile(r'\b(\d{1,2})([/.-])(\d{1,2})\2(\d{2,4})\b')


def format_issues(t: dict, locale: str, f: dict) -> list[dict]:
    out = []
    for m in NUM.finditer(t['text']):
        num = m.group(2)
        dec = num[-3]
        sym = m.group(1) or m.group(3)
        problems = []
        if dec != f['decimal']:
            problems.append(f'decimal separator {dec!r}, CLDR {locale} uses {f["decimal"]!r}')
        if sym:
            pattern = f['currency_pattern'].split(';')[0]
            symbol_first = pattern.strip('‏‎ ').startswith('¤')
            if bool(m.group(1)) != symbol_first:
                problems.append(f'currency symbol {"before" if m.group(1) else "after"} the number; CLDR {locale} puts it '
                                f'{"before" if symbol_first else "after"} ({pattern})')
        if problems:
            out.append({'kind': 'format', 'element': t['sel'], 'text': t['text'], 'box': t['box'],
                        'measure': {'value': m.group(0).strip(), 'problems': problems, 'cldr': {k: f[k] for k in ('decimal', 'group', 'currency_pattern')}}})
    for m in DATE.finditer(t['text']):
        pattern = f['date_short']
        sep = re.sub(r'[‎‏]', '', re.sub(r'[dMyG]', '', pattern))[:1]
        if sep and m.group(2) != sep:
            out.append({'kind': 'format', 'element': t['sel'], 'text': t['text'], 'box': t['box'],
                        'measure': {'value': m.group(0), 'problems': [f'date separator {m.group(2)!r}; CLDR {locale} short date is {pattern}'],
                                    'cldr': {'date_short': pattern}}})
    return out


def flatten_keys(obj, prefix='') -> set[str]:
    out = set()
    for k, v in obj.items():
        key = f'{prefix}.{k}' if prefix else k
        out |= flatten_keys(v, key) if isinstance(v, dict) else {key}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('targets', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cfg = json.loads(a.targets.read_text(encoding='utf-8'))
    formats = json.loads((SKILL / 'data' / 'cldr-formats.json').read_text(encoding='utf-8'))
    try:
        check_authorised([cfg['site']], cfg.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    src = cfg['source_locale']
    res_pat = cfg.get('resources')
    doc = {'site': cfg['site'], 'captured_at': now(), 'pseudo_method': None, 'runs': [], 'not_run': [], 'locale_strategy': cfg.get('locale_strategy')}
    with Harness({'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'), 'rate_limit_seconds': 0.1}) as h:
        doc['browser'] = {'name': 'chromium', 'version': h.version}
        if h.auth_info:
            doc['auth'] = {'method': h.auth_info['method']}   # that a session was used; never its contents or path
        # which locales have resources
        available, keys = {}, set()
        for loc in sorted(set(cfg['locales']) | set(cfg.get('rtl_locales') or [])):
            if not res_pat:
                available[loc] = True
                continue
            page = h.new_page()
            r = page.request.get(urljoin(cfg['site'], res_pat.format(locale=loc)))
            available[loc] = r.ok
            if r.ok and loc == src:
                source_messages = r.json()
                keys = flatten_keys(source_messages)
            h.close_page(page)
        for loc, ok in available.items():
            if not ok:
                doc['not_run'].append({'locale': loc, 'reason': f'no resources at {res_pat.format(locale=loc)} (HTTP 404)'})
        plans = [('source', src)] + ([('pseudo', src)] if cfg.get('pseudo', True) else []) + \
                [('locale', l) for l in cfg['locales'] if l != src and available.get(l) and l not in (cfg.get('rtl_locales') or [])] + \
                [('rtl', l) for l in (cfg.get('rtl_locales') or []) if available.get(l)]
        for p in cfg['pages']:
            for vp in cfg['viewports']:
                for mode, loc in plans:
                    page = h.new_page(viewport=vp, locale=loc if mode != 'pseudo' else src)
                    method = None
                    if mode == 'pseudo' and res_pat:
                        pseudo_body = json.dumps(pseudo_json(source_messages), ensure_ascii=False)
                        page.route('**/' + res_pat.format(locale=src), lambda route: route.fulfill(status=200, body=pseudo_body,
                                                                                                    content_type='application/json; charset=utf-8'))
                        method = 'intercept'
                    rec = {'page': p['name'], 'viewport': vp, 'mode': mode, 'locale': loc, 'critical_path': bool(p.get('critical_path')),
                           'method': method, 'ok': False, 'findings': []}
                    try:
                        h.throttle()
                        resp = page.goto(url_for(cfg, p['path'], loc), wait_until='load', timeout=45000)
                        page.wait_for_load_state('networkidle', timeout=10000)
                        page.wait_for_timeout(300)
                        if mode == 'pseudo' and not res_pat:
                            page.evaluate(WALK)
                            rec['method'] = 'text_walk'
                        snap = page.evaluate(SNAP)
                        shot = a.out / f'{p["name"]}.{mode}.{loc}.{vp}.png'
                        h.screenshot(page, shot)
                        rec.update(ok=bool(resp and resp.status < 400), screenshot=shot.name, dir=snap['dir'], lang=snap['lang'],
                                   findings=check(snap, mode, loc, formats, keys))
                    except Exception as e:  # noqa: BLE001
                        rec['error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'
                    finally:
                        h.close_page(page)
                    if mode == 'pseudo':
                        doc['pseudo_method'] = rec['method']
                    doc['runs'].append(rec)
                    print(f'{p["name"]} {vp} {mode} {loc}: {len(rec["findings"])} finding(s)', file=sys.stderr)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'runtime.json').write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
