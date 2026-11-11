#!/usr/bin/env python3
"""Run the same pages and states in each engine, several times, and record what each run saw.

    python capture.py targets.json --out run/capture [--engines chromium,firefox,webkit] [--reruns 3]

targets.json:
    {"site": "http://127.0.0.1:8145/", "pages": [{"name": "home", "path": "index.html",
       "states": [{"name": "recommendations", "critical_path": true, "actions": [{"click": "#more"}, {"wait_for": "#recs-list"}]}]}],
     "viewports": ["1280x800"], "engines": ["chromium", "firefox", "webkit"], "reruns": 3,
     "authorised_by": null, "auth": null, "allow_form_submission": false}

Per page (and state) x viewport x engine x rerun, in a fresh context: a full-page screenshot; console
errors, page errors and failed requests; every visible element's signature, box and own text; images
with their natural size; whether the page overflows horizontally; the scripts loaded (to spot
user-agent sniffing); and for a state, the outcome of each step (ok, or the error). An engine that
isn't installed is recorded as not_run.

Output: <out>/runs.json and <out>/<engine>/<viewport>/<page>[~state].r<N>.png. Playwright's WebKit
is not Safari; the scope says so, and nothing here calls it Safari.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised, NotAuthorised, now  # noqa: E402

STABLE_CSS = ('*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important; '
              'transition: none !important; caret-color: transparent !important; }')

SNAP = r"""
() => {
  const sigOf = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
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
    }
    return parts.join('>');
  };
  const els = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const own = [...el.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(' ').replace(/\s+/g, ' ').trim();
    els.push({sig: sigOf(el), tag: el.tagName.toLowerCase(), type: el.type || null, text: own.slice(0, 80),
              box: [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)]});
    if (els.length >= 2000) break;
  }
  const imgs = [...document.images].map(i => ({sig: sigOf(i), src: i.currentSrc || i.src, complete: i.complete, natural: [i.naturalWidth, i.naturalHeight]}));
  const scripts = [...document.scripts].map(s => s.src || ('inline:' + s.textContent.length));
  const de = document.documentElement;
  return {elements: els, images: imgs, scripts, overflow_x: de.scrollWidth > de.clientWidth + 1, size: [de.scrollWidth, de.scrollHeight]};
}
"""


def run_once(h: Harness, url: str, viewport: str, actions: list, shot: Path) -> dict:
    page = h.new_page(viewport=viewport, reduced_motion='reduce')
    log = h.log(page)
    rec = {'ok': False, 'status': None, 'error': None, 'steps': []}
    try:
        h.throttle()
        resp = page.goto(url, wait_until='load', timeout=45000)
        rec['status'] = resp.status if resp else None
        page.add_style_tag(content=STABLE_CSS)
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(200)
        for i, a in enumerate(actions):
            try:
                h.act(page, [a])
                rec['steps'].append({'index': i, 'action': a, 'outcome': 'ok', 'error': None})
            except Exception as e:  # noqa: BLE001
                rec['steps'].append({'index': i, 'action': a, 'outcome': 'failed', 'error': f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'})
                break
        page.wait_for_timeout(200)
        rec.update(page.evaluate(SNAP))
        h.screenshot(page, shot, full_page=True)
        rec['screenshot'] = shot.as_posix()
        rec['ok'] = bool(resp and resp.status < 400)
    except Exception as e:  # noqa: BLE001
        rec['error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'
    finally:
        rec['console_errors'] = [c['text'][:300] for c in log['console'] if c['type'] == 'error']
        rec['page_errors'] = [f'{e["name"]}: {e["message"]}'[:300] for e in log['page_errors']]
        rec['failed_requests'] = [f'{r["method"]} {r["url"]} ({r["failure"]})' for r in log['failed_requests']]
        h.close_page(page)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('targets', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--engines')
    ap.add_argument('--reruns', type=int)
    a = ap.parse_args()
    cfg = json.loads(a.targets.read_text(encoding='utf-8'))
    engines = (a.engines or ','.join(cfg.get('engines') or ['chromium', 'firefox', 'webkit'])).split(',')
    reruns = a.reruns or cfg.get('reruns', 3)
    urls = [urljoin(cfg['site'], p['path']) for p in cfg['pages']]
    try:
        check_authorised(urls, cfg.get('authorised_by'))
    except NotAuthorised as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    doc = {'site': cfg['site'], 'captured_at': now(), 'reruns': reruns, 'engines': [], 'runs': []}
    for eng in engines:
        try:
            with Harness({'browser': eng, 'authorised_by': cfg.get('authorised_by'), 'auth': cfg.get('auth'),
                          'allow_form_submission': cfg.get('allow_form_submission', False), 'rate_limit_seconds': 0.1}) as h:
                doc['engines'].append({'name': eng, 'version': h.version, 'status': 'ran', 'note': None})
                for p in cfg['pages']:
                    for st in [None] + (p.get('states') or []):
                        name = p['name'] + (f'~{st["name"]}' if st else '')
                        for vp in cfg['viewports']:
                            for r in range(1, reruns + 1):
                                shot = a.out / eng / vp / f'{name}.r{r}.png'
                                rec = run_once(h, urljoin(cfg['site'], p['path']), vp, (st or {}).get('actions') or [], shot)
                                doc['runs'].append({'engine': eng, 'page': name, 'viewport': vp, 'run': r,
                                                    'critical_path': bool((st or {}).get('critical_path')), **rec})
                doc.setdefault('blocked_requests', []).extend(h.blocked)
        except Exception as e:  # noqa: BLE001 - engine not installed or failed to launch
            doc['engines'].append({'name': eng, 'version': None, 'status': 'not_run', 'note': f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'})
    for r in doc['runs']:
        if r.get('screenshot'):
            r['screenshot'] = Path(r['screenshot']).relative_to(a.out).as_posix()
            r['screenshot_sha256'] = hashlib.sha256((a.out / r['screenshot']).read_bytes()).hexdigest()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'runs.json').write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'{len(doc["runs"])} run(s) over {", ".join(e["name"] + " " + e["status"] for e in doc["engines"])} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
