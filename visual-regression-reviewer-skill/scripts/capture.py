#!/usr/bin/env python3
"""Capture full-page screenshots and DOM boxes for one set (baseline or current), stabilised and repeated.

    python capture.py targets.json --set current --out run/current [--takes 2]

targets.json:
    {"sites": {"baseline": "http://127.0.0.1:8143/", "current": "http://127.0.0.1:8144/"},
     "pages": [{"name": "home", "path": "index.html", "states": [{"name": "menu-open", "actions": [{"click": "#menu"}]}]}],
     "viewports": ["1280x800", "375x812"], "browsers": ["chromium"], "clock": null,
     "authorised_by": null, "auth": null}

Each page (and state) x viewport x browser is loaded in a fresh context with animations,
transitions and the caret disabled, then waits for network idle and document.fonts.ready (retrying
once when fonts haven't loaded). Take 1 is <out>/<browser>/<viewport>/<page>[~state].png; with
--takes 2 a second, independent load is saved as .take2.png, so pixels that differ between the two
takes can be treated as flaky. Each take also saves .dom.json: every visible element's signature,
tag, own text, page-coordinate box and the styles that show (colour, background, font, image source).

A signature is the element's path from the nearest ancestor with an id (or <html>): tag, first
class, and :nth-of-type only where siblings would otherwise share it. It ignores text, so a text
change keeps the signature. `path` is the full ancestry (tags with id or first class), used for nesting.

manifest.json records the base URL, browser versions, font status per capture and the SHA-256 of
every file. A host that isn't loopback needs authorised_by (browser_harness.check_authorised).
"""
from __future__ import annotations
import argparse, hashlib, json, sys, time
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now  # noqa: E402

STABLE_CSS = ('*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important; '
              'transition: none !important; caret-color: transparent !important; scroll-behavior: auto !important; }')

DOM = r"""
() => {
  const sigOf = (el) => {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1) {
      if (n.id) { parts.unshift('#' + n.id); break; }
      let s = n.tagName.toLowerCase();
      const cls = (typeof n.className === 'string' && n.className.trim()) ? n.className.trim().split(/\s+/)[0] : '';
      if (cls) s += '.' + cls;
      const p = n.parentElement;
      if (p) {
        const same = [...p.children].filter(c => c.tagName === n.tagName &&
              ((typeof c.className === 'string' && c.className.trim()) ? c.className.trim().split(/\s+/)[0] : '') === cls);
        if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      }
      parts.unshift(s);
      if (n.tagName === 'HTML') break;
      n = p;
    }
    return parts.join('>');
  };
  const pathOf = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cls = (typeof n.className === 'string' && n.className.trim()) ? '.' + n.className.trim().split(/\s+/)[0] : '';
      parts.unshift(n.tagName.toLowerCase() + (n.id ? '#' + n.id : cls));
    }
    return parts.join('>');
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
    const own = [...el.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(' ').replace(/\s+/g, ' ').trim();
    out.push({sig: sigOf(el), path: pathOf(el), tag: el.tagName.toLowerCase(), text: own.slice(0, 120),
              box: [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)],
              color: cs.color, background: cs.backgroundColor, font: cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily,
              image: el.tagName === 'IMG' ? el.currentSrc : (cs.backgroundImage !== 'none' ? cs.backgroundImage.slice(0, 200) : null)});
    if (out.length >= 3000) break;
  }
  return {elements: out, fonts: document.fonts.status, scroll: [document.documentElement.scrollWidth, document.documentElement.scrollHeight]};
}
"""


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def capture_one(h: Harness, url: str, viewport: str, clock: str | None, actions: list, png: Path, dom: Path) -> dict:
    page = h.new_page(viewport=viewport, clock=clock, reduced_motion='reduce')
    info = {'url': url, 'ok': False, 'status': None, 'fonts': None, 'error': None}
    try:
        h.throttle()
        resp = page.goto(url, wait_until='load', timeout=45000)
        info['status'] = resp.status if resp else None
        page.add_style_tag(content=STABLE_CSS)
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:  # noqa: BLE001
            h.events.append({'url': url, 'event': 'network not idle after 10 s', 'at': now()})
        for attempt in range(2):
            status = page.evaluate('() => document.fonts.ready.then(() => document.fonts.status)')
            if status == 'loaded':
                break
            page.wait_for_timeout(1000)
        info['fonts'] = status
        if actions:
            h.act(page, actions)
        page.wait_for_timeout(300)
        d = page.evaluate(DOM)
        h.screenshot(page, png, full_page=True)
        dom.write_text(json.dumps(d, indent=0) + '\n', encoding='utf-8')
        info['ok'] = bool(resp and resp.status < 400)
    except Exception as e:  # noqa: BLE001
        info['error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'
    finally:
        h.close_page(page)
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('config', type=Path)
    ap.add_argument('--set', required=True, choices=['baseline', 'current'])
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--takes', type=int, default=None, help='default: 2 for current, 1 for baseline')
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding='utf-8'))
    base_url = cfg['sites'][a.set]
    takes = a.takes or (2 if a.set == 'current' else 1)
    urls = [urljoin(base_url, p['path']) for p in cfg['pages']]
    try:
        check_authorised(urls, cfg.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    manifest = {'set': a.set, 'base_url': base_url, 'captured_at': now(), 'takes': takes, 'browsers': {}, 'captures': [], 'files': {}}
    for browser in cfg.get('browsers') or ['chromium']:
        with Harness({'browser': browser, 'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'), 'rate_limit_seconds': 0.2}) as h:
            manifest['browsers'][browser] = h.version
            for p in cfg['pages']:
                for state in [None] + (p.get('states') or []):
                    name = p['name'] + (f'~{state["name"]}' if state else '')
                    for vp in cfg['viewports']:
                        d = a.out / browser / vp
                        d.mkdir(parents=True, exist_ok=True)
                        for t in range(1, takes + 1):
                            suffix = '' if t == 1 else f'.take{t}'
                            info = capture_one(h, urljoin(base_url, p['path']), vp, cfg.get('clock'), (state or {}).get('actions') or [],
                                               d / f'{name}{suffix}.png', d / f'{name}{suffix}.dom.json')
                            manifest['captures'].append({'page': name, 'viewport': vp, 'browser': browser, 'take': t, **info})
                            if info['fonts'] not in ('loaded', None):
                                print(f'warning: fonts {info["fonts"]} for {name} {vp}', file=sys.stderr)
            manifest['events'] = manifest.get('events', []) + h.events
            manifest['blocked_requests'] = manifest.get('blocked_requests', []) + h.blocked
    for f in sorted(a.out.rglob('*')):
        if f.is_file() and f.name != 'manifest.json':
            manifest['files'][f.relative_to(a.out).as_posix()] = sha(f)
    (a.out / 'manifest.json').write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
    bad = [c for c in manifest['captures'] if not c['ok']]
    print(f'{a.set}: {len(manifest["captures"])} capture(s), {len(bad)} failed -> {a.out}', file=sys.stderr)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
