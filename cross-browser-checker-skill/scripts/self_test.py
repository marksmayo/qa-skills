#!/usr/bin/env python3
"""Self-test for the cross-browser checker's scripts:

- feature_scan.py on a synthetic Go tool whose only browser code is an HTML page in a Go string constant
  (beside a vendored tree), and on a Go tool with no browser code at all (needs the cached feature data);
- capture.py's page snapshot: computed font family, occlusion by a fixed overlay, inner scroll panels
  (needs Playwright with Chromium);
- compare_engines.py on synthetic runs: a font-family change is its own difference, a box covered by an
  overlay is dismissed, and boxes inside a panel scrolled differently by automation are dismissed;
- validate_report.py ties feature_findings to --features; render_report.py --brief shows needs review
  and limitations.

    python self_test.py

Writes only to a temporary directory. Exit 0 when every check passes.
"""
from __future__ import annotations
import json, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import capture  # noqa: E402
import compare_engines as ce  # noqa: E402

SKILL = HERE.parent
EXAMPLES = SKILL / 'examples'

GO_PAGE = ('package report\n\nimport "html/template"\n\nvar page = template.Must(template.New("r").Parse(pageHTML))\n\n'
           'const pageHTML = `<!DOCTYPE html>\n<html>\n<head>\n<style>\n.list { overflow-y: auto; }\n'
           '.list::-webkit-scrollbar { width: 6px; }\n</style>\n</head>\n<body><div class="list"></div>\n'
           '<script>\nconst q = new URLSearchParams(location.search);\n</script>\n</body>\n</html>`\n')
GO_TREE = {
    'go.mod': 'module example.test/tally\n\ngo 1.22\n',
    'main.go': 'package main\n\nfunc main() {}\n',
    'internal/run/run.go': 'package run\n\n// Run runs.\nfunc Run() {}\n',
    'internal/run/retry.go': 'package run\n\n// Retry retries.\nfunc Retry() {}\n',
    'internal/report/report.go': 'package report\n\n// Write writes the report.\nfunc Write() {}\n',
    'internal/report/page.go': GO_PAGE,
    'vendor/example.test/kit/kit.go': 'package kit\n',
    'third_party/widget/widget.css': '.w::-webkit-scrollbar { width: 2px; }\n',
}


def feature_scan_checks() -> tuple[list[str], str]:
    try:
        from fetch_data import asset_path
        asset_path('web-features'); asset_path('caniuse-db')
    except (FileNotFoundError, ImportError, KeyError):
        return [], 'skipped (feature data not cached: fetch_data.py)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'tally'
        for rel, text in GO_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        out = Path(tmp) / 'features.json'
        r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                            '--query', 'defaults'], capture_output=True, text=True, encoding='utf-8')
        try:
            d = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [f'feature_scan.py failed: {r.stderr[-300:]}'], 'ran'
        line = GO_PAGE.splitlines().index('.list::-webkit-scrollbar { width: 6px; }') + 1
        hits = [(u['file'], u['line']) for f in d['features'] for u in f['files']]
        if ('internal/report/page.go', line) not in hits:
            fails.append(f'the <style> in a Go string constant was not scanned (want internal/report/page.go:{line}; got {hits})')
        if any(h[0].startswith(('vendor/', 'third_party/')) for h in hits):
            fails.append(f'a vendored tree was scanned: {hits}')
        lc = d.get('language_coverage') or {}
        if lc.get('dominant') != 'Go' or lc.get('status') != 'unsupported_dominant' or not lc.get('embedded_html'):
            fails.append(f'language_coverage does not say Go is dominant with HTML in string constants: {lc or "missing"}')
        # a Go tool with no browser code at all: the empty result must say it is not evidence
        (root / 'internal/report/page.go').write_text('package report\n\nconst name = "tally"\n', encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                            '--query', 'defaults'], capture_output=True, text=True, encoding='utf-8')
        d = json.loads(out.read_text(encoding='utf-8'))
        if not any('Go' in x for x in d.get('limitations') or []) or 'WARN' not in r.stderr:
            fails.append(f'a Go repository with 0 uses has no loud language limitation: limitations '
                         f'{d.get("limitations")}, stderr {r.stderr.strip()[-200:]!r}')
    return fails, 'ran'


SNAP_PAGE = """<!doctype html><html><head><style>body { font-family: Verdana, sans-serif; margin: 0 }
.btn { font-family: Georgia, serif } .panel { height: 120px; overflow-y: auto }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.6) }</style></head><body>
<p class="intro">Runs</p><button class="btn">Filter</button>
<div class="panel" id="detail">""" + ''.join(f'<p>Step {i}</p>' for i in range(30)) + """</div>
<div class="modal"></div></body></html>"""


def snapshot_checks() -> tuple[list[str], str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            return [], f'skipped ({type(exc).__name__})'
        page = browser.new_page(viewport={'width': 800, 'height': 600})
        page.set_content(SNAP_PAGE)
        snap = page.evaluate(capture.SNAP)
        browser.close()
    by = {e['sig'].split('>')[-1]: e for e in snap['elements']}
    btn, intro = by.get('button.btn') or {}, by.get('p.intro') or {}
    if 'Georgia' not in str(btn.get('font')) or 'Verdana' not in str(intro.get('font')):
        fails.append(f"the snapshot does not record each text element's computed font family: button {btn.get('font')}, "
                     f"p {intro.get('font')}")
    if not intro.get('occluded'):
        fails.append(f'the snapshot does not mark an element under a fixed overlay as occluded: {intro}')
    sc = snap.get('scrollers') or []
    if not any(s.get('sig') == '#detail' and s.get('hidden_px', 0) > 100 for s in sc):
        fails.append(f'the snapshot does not record the inner scroll panel #detail: {sc}')
    return fails, 'ran'


def run(engine: str, rerun: int, elements: list[dict], scrollers: list | None = None) -> dict:
    r = {'page': 'home', 'viewport': '1280x800', 'engine': engine, 'rerun': rerun, 'run_status': 'ran',
         'elements': elements, 'images': [], 'page_errors': [], 'console_errors': [], 'failed_requests': [],
         'steps': [], 'overflow_x': False, 'size': [1280, 800]}
    if scrollers is not None:
        r['scrollers'] = scrollers
    return r


def compare_checks() -> list[str]:
    fails = []
    def el(sig, box, font='Verdana, sans-serif', occluded=False, tag='div', text='x'):
        return {'sig': sig, 'tag': tag, 'type': None, 'text': text, 'box': box, 'font': font,
                **({'occluded': True} if occluded else {})}
    ref = [el('body>div.bar>div.f1', [0, 0, 100, 20], font='"Segoe UI", sans-serif'),
           el('body>div.bar>div.f2', [0, 30, 100, 20], font='"Segoe UI", sans-serif'),
           el('body>div.under', [0, 100, 300, 50], occluded=True),
           el('#detail>div.step:nth-of-type(9)', [0, 400, 200, 20])]
    eng = [el('body>div.bar>div.f1', [0, 0, 100, 20], font='Times New Roman'),
           el('body>div.bar>div.f2', [0, 30, 100, 20], font='Times New Roman'),
           el('body>div.under', [0, 100, 260, 50], occluded=True),
           el('#detail>div.step:nth-of-type(9)', [0, 347, 200, 20])]
    sc_ref = [{'sig': '#detail', 'scroll_top': 0, 'scroll_left': 0, 'hidden_px': 500}]
    sc_eng = [{'sig': '#detail', 'scroll_top': 53, 'scroll_left': 0, 'hidden_px': 500}]
    doc = {'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 2,
           'runs': [run('chromium', i, ref, sc_ref) for i in (1, 2)] + [run('webkit', i, eng, sc_eng) for i in (1, 2)]}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    fam = [d for d in diffs if d['subkind'] == 'font_family_differs']
    if len(fam) != 1 or len(fam[0].get('elements') or []) != 2:
        fails.append(f'a font-family change is not one font_family_differs difference listing its elements: {fam or diffs}')
    if any(d.get('element') == 'body>div.under' for d in diffs) or \
            not any(d.get('element') == 'body>div.under' and 'covered' in d.get('reason', '') for d in dismissed):
        fails.append(f'a box under an overlay (occluded) was reported, not dismissed: {[d for d in diffs if d.get("element") == "body>div.under"]}')
    if any(d.get('element', '').startswith('#detail>') for d in diffs) or \
            not any(d.get('element', '').startswith('#detail>') and 'scroll' in d.get('reason', '') for d in dismissed):
        fails.append('a box inside a panel the automation scrolled differently was reported as layout')
    return fails


def validator_render_checks() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        feats = json.loads((EXAMPLES / 'run' / 'features.json').read_text(encoding='utf-8'))
        empty = dict(feats, features=[], uses=0)
        (t / 'features.empty.json').write_text(json.dumps(empty), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(EXAMPLES / 'report.static.example.json'),
                            '--candidates', str(EXAMPLES / 'static-only' / 'candidates.json'), '--features',
                            str(t / 'features.empty.json'), '--targets', str(EXAMPLES / 'targets.json')],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if r.returncode == 0:
            fails.append('validate_report.py accepted feature findings that are not in the --features file passed: '
                         + r.stdout.strip()[-160:])
        rep = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
        rep['needs_review'] = [{'candidate_id': 'K99', 'reason': 'controls draw in a serif face in WebKit only; check by eye'}]
        rep['limitations'] = (rep.get('limitations') or []) + ['Inner scroll panels were compared by box only.']
        (t / 'r.json').write_text(json.dumps(rep), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'render_report.py'), str(t / 'r.json'), '--brief', '--color', 'never'],
                           capture_output=True, text=True, encoding='utf-8')
        if 'K99' not in r.stdout or 'Inner scroll panels were compared by box only.' not in r.stdout:
            fails.append('render_report.py --brief omits the needs-review items or the limitations')
    return fails


def main() -> int:
    ok = True
    for name, fn in (('feature_scan.py: Go string constants, vendored trees, language guard', feature_scan_checks),
                     ('capture.py snapshot: font family, occlusion, inner scroll panels', snapshot_checks)):
        fails, how = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name} ({how})" + ''.join(f'\n  - {f}' for f in fails))
    for name, fn in (('compare_engines.py: font family, occlusion, scroll offsets', compare_checks),
                     ('validate_report.py --features and render_report.py --brief', validator_render_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
