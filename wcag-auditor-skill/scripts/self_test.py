#!/usr/bin/env python3
"""Self-test for the WCAG auditor's scripts: merge_findings.py --self-test (colour maths, generated ids),
template grouping for one generated page in many folders, inner scroll panels a full-page screenshot
cannot show (needs Playwright with Chromium; skipped without it), and validator cases (blocked_requests
as run.json records it, an agent finding's re-readable basis).

    python self_test.py

Writes only to a temporary directory. Exit 0 when every check passes.
"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import merge_findings  # noqa: E402
import run_audit  # noqa: E402
import validate_audit  # noqa: E402

EXAMPLES = HERE.parent / 'examples'


def template_checks() -> list[str]:
    fails = []
    bare = {'landmarks': '', 'main_shape': 'div,script'}
    a = run_audit.template_key({'url': 'http://127.0.0.1:8191/run-a/report.html', 'template_sig': bare})
    b = run_audit.template_key({'url': 'http://127.0.0.1:8191/run-b/report.html', 'template_sig': bare})
    if a != b:
        fails.append(f'one generated page with no landmarks in two folders gives two templates ({a} / {b})')
    marked = {'landmarks': 'banner+main+contentinfo', 'main_shape': 'h1,p'}
    c = run_audit.template_key({'url': 'http://127.0.0.1:8191/docs/a.html', 'template_sig': marked})
    d = run_audit.template_key({'url': 'http://127.0.0.1:8191/blog/a.html', 'template_sig': marked})
    if c == d:
        fails.append('pages with landmarks in different sections no longer keep their section in the template key')
    return fails


SCROLL_PAGE = """<!doctype html><html><body style="margin:0">
<div style="height:60px">Run summary</div>
<div style="display:flex; height:calc(100vh - 60px)">
  <div class="list" style="width:300px; overflow-y:auto">""" + ''.join(f'<p>Row {i}</p>' for i in range(80)) + """</div>
  <div class="detail" style="flex:1; overflow:auto"><p>Short detail</p></div>
</div></body></html>"""


def scroll_checks() -> tuple[list[str], str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # no browser installed
            return [], f'skipped ({type(exc).__name__})'
        page = browser.new_page(viewport={'width': 1000, 'height': 600})
        page.set_content(SCROLL_PAGE)
        try:
            found = run_audit.inner_scroll_containers(page)
        except AttributeError:
            found = None
        browser.close()
    if not found or len(found) != 1 or 'list' not in json.dumps(found):
        fails.append(f'the inner scroll panel (.list, most rows below its box) was not reported: {found}')
    else:
        note = run_audit.scroll_limitation([{'id': 'p01', 'url': 'http://x/', 'inner_scroll': found}])
        if not note or 'p01' not in note:
            fails.append(f'no limitation text for pages with inner scroll panels: {note}')
    return fails, 'ran'


def validator_checks() -> list[str]:
    fails = []
    base = json.loads((EXAMPLES / 'audit.example.json').read_text(encoding='utf-8'))
    schema = json.loads(validate_audit.SCHEMA.read_text(encoding='utf-8'))
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    d = json.loads(json.dumps(base))
    d['metadata']['blocked_requests'] = list(run['blocked_requests'])  # copied from run.json as it is recorded there
    errs = validate_audit.schema_errors(d, schema, schema)
    if errs:
        fails.append(f'blocked_requests copied from run.json (a list) is a schema error: {errs[:2]}')
    d = json.loads(json.dumps(base))
    d['metadata']['blocked_requests'] = 3
    try:
        errs = validate_audit.check(d, EXAMPLES / 'run', None)
    except TypeError:
        errs = validate_audit.check(d, EXAMPLES / 'run', None, None)
    if not any('blocked_requests' in e for e in errs):
        fails.append('blocked_requests 3 against a run.json that blocked nothing is accepted')
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / 'report.go').write_text('package report\n\nconst page = `<script>\n'
                                        "document.addEventListener('keydown', (e) => { if (e.key === 'j') next(); });\n"
                                        '</script>`\n', encoding='utf-8')
        agent = next(f for f in base['findings'] if f['evidence_type'] == 'agent_judgment')
        def with_basis(excerpt):
            x = json.loads(json.dumps(base))
            f = next(y for y in x['findings'] if y['id'] == agent['id'])
            f['basis'] = [{'file': 'report.go', 'line': 4, 'excerpt': excerpt}]
            return x
        good, bad = with_basis("if (e.key === 'j') next();"), with_basis("if (e.key === 'k') prev();")
        for x, want_ok in ((good, True), (bad, False)):
            errs = validate_audit.schema_errors(x, schema, schema)
            try:
                errs += validate_audit.check(x, None, None, repo)
            except TypeError:
                errs += ['check() takes no repo']
            ok = not any('basis' in e or 'repo' in e or 'unexpected field' in e for e in errs)
            if ok != want_ok:
                fails.append(f"an agent finding's basis {'matching' if want_ok else 'not matching'} report.go:4 was "
                             f"{'rejected' if want_ok else 'accepted'} with --repo: {[e for e in errs if 'basis' in e or 'repo' in e or 'unexpected' in e][:2]}")
        x = json.loads(json.dumps(base))
        f = next(y for y in x['findings'] if y['id'] == agent['id'])
        f['resolution'] = (f.get('resolution') or '') + ' The keydown handler is at report.go:4.'
        try:
            errs = validate_audit.check(x, None, None, None)
        except TypeError:
            errs = validate_audit.check(x, None, None)
        if not any('basis' in e for e in errs):
            fails.append('an agent finding that cites report.go:4 in prose but has no basis is accepted')
    return fails


def main() -> int:
    ok = merge_findings.self_test() == 0
    for name, fn in (('run_audit.py: template grouping without landmarks', template_checks),
                     ('validate_audit.py: blocked_requests and agent basis', validator_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = scroll_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: inner scroll panels ({how})" + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
