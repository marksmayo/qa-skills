#!/usr/bin/env python3
"""Self-test for the i18n checker's scripts, on synthetic repositories written to a temporary directory.

    python self_test.py

Covers: a committed, hand-written front end in dist/ (scanned, not skipped as build output; real build output
skipped and recorded; --include), a JS translations object with en and ru keys read as a resource set,
locale-named JS files that hold no messages listed as not parsed, non-Latin hard-coded text, the validator's
evidence completeness when nothing was parsed or the pseudo-locale was a text walk, an honest partial headline,
agent findings with a re-readable basis, CRLF-safe CLDR hashes, runtime checks (lang attribute and text left in
the source language, locales assumed without a resource pattern, the text-walk pseudo-locale), the renderer's
empty fields, and in Chromium (skipped without Playwright) wrapped inline elements and cut-off placeholders.
Exit 0 when every check passes.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
EX = SKILL / 'examples'
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
ENV = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}

INDEX = """<!doctype html><html lang="en"><head><title>Player</title><script src="app.js"></script></head><body>
<h1 data-i18n="title">Player</h1>
<p>Follow the news and releases</p>
<p>Подписывайтесь на новости и релизы</p>
<input id="key" placeholder="Enter the access key">
</body></html>
"""
APP_JS = """// a translations object with en and ru keys, read through t(key)
const translations = {
  en: {
    title: "Player",
    downloading: "Downloading",
    notice: "",
    'menu.open': 'Open menu',
  },
  ru: {
    title: "Плеер",
    downloading: "Загрузка",
    notice: "<b>Внимание</b>",
    'menu.open': 'Открыть меню',
  },
};
function t(key) { return translations[lang][key] || key; }
status.textContent = t("downloading") + ` ${percent}%`;
"""
PACKAGE_JS = "var DATA_PACKAGE = {files: [{filename: '/data/a.bin', start: 0, end: 10}], remote_package_size: 10};\n"
SERVER_PY = """from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
app = FastAPI()
app.mount('/', StaticFiles(directory='dist', html=True), name='static')
"""


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    return root


def run(script: str, *args) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(HERE / script), *map(str, args)], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=ENV)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def static_front_checks(tmp: Path) -> list[str]:
    fails = []
    repo = write(tmp / 'static', {'server.py': SERVER_PY, 'dist/index.html': INDEX, 'dist/app.js': APP_JS,
                                  'dist/modules/packages/en.js': PACKAGE_JS, 'dist/modules/packages/ru.js': PACKAGE_JS,
                                  'dist/modules/fs.js': 'var FS = {};\n'})
    rc, log = run('discover.py', '--repo', repo, '--out', tmp / 'res.json')
    res = json.loads((tmp / 'res.json').read_text(encoding='utf-8')) if (tmp / 'res.json').is_file() else {}
    locs = {l for s in res.get('sets', {}).values() for l in s['locales']}
    if not {'en', 'ru'} <= locs:
        fails.append(f'a translations object {{en, ru}} in a committed dist/app.js is not read as a resource set '
                     f'(locales {sorted(locs)}; stderr: {log.strip()[-160:]})')
    ent = res.get('entries', {})
    en = next((v.get('en') for v in ent.values() if 'en' in v), []) or []
    if en and not any(e['key'] == 'menu.open' and e['line'] for e in en):
        fails.append(f'the JS object entries carry no key or line: {en[:2]}')
    np_files = {x['file'] for x in res.get('not_parsed', [])}
    if 'dist/modules/packages/en.js' not in np_files:
        fails.append(f'a locale-named JS file with no message object (packages/en.js) is not listed in not_parsed: {sorted(np_files)}')
    if 'dist/modules/fs.js' in np_files:
        fails.append('fs.js (not a language code) is listed as a locale-named file')
    if any(s.get('dir') == 'dist' for s in res.get('skipped_dirs', [{'dir': 'dist'}])):
        fails.append(f'a committed, hand-written dist/ (no build config) is skipped: {res.get("skipped_dirs")}')
    if res and res.get('sets'):
        rc, log = run('check_keys.py', tmp / 'res.json', '--repo', repo, '--out', tmp / 'keys.json')
        keys = json.loads((tmp / 'keys.json').read_text(encoding='utf-8')) if rc == 0 else {'findings': []}
        if not any(f['kind'] == 'empty_value' for f in keys['findings']):
            fails.append(f'check_keys finds no empty value in the JS translations object: {log[-200:]}')
    rc, log = run('scan_code.py', '--repo', repo, '--out', tmp / 'code.json')
    code = json.loads((tmp / 'code.json').read_text(encoding='utf-8')) if (tmp / 'code.json').is_file() else {'findings': []}
    texts = [f.get('text') for f in code['findings'] if f['kind'] == 'hardcoded_string']
    if 'Follow the news and releases' not in texts:
        fails.append(f'scan_code skips the committed dist/ front end: hard-coded strings {texts}')
    if 'Подписывайтесь на новости и релизы' not in texts:
        fails.append(f'scan_code misses Cyrillic hard-coded text: {texts}')
    if not code.get('files_scanned'):
        fails.append(f'code.json does not record how many files were scanned: {sorted(code)}')
    # real build output: a package with a build script; dist/ is skipped and recorded, and --include scans it
    built = write(tmp / 'built', {'package.json': json.dumps({'scripts': {'build': 'vite build'}}),
                                  'src/index.html': INDEX, 'dist/index.html': INDEX.replace('Follow', 'Built copy:')})
    rc, log = run('scan_code.py', '--repo', built, '--out', tmp / 'code2.json')
    code = json.loads((tmp / 'code2.json').read_text(encoding='utf-8')) if (tmp / 'code2.json').is_file() else {}
    sk = code.get('skipped_dirs') or []
    if not any(s.get('dir') == 'dist' and s.get('reason') and s.get('files') for s in sk):
        fails.append(f'build output dist/ (package.json has a build script) is not recorded as skipped with a reason and file count: {sk}')
    if 'dist' not in log:
        fails.append('scan_code says nothing on stderr about skipping dist/')
    rc, log = run('scan_code.py', '--repo', built, '--out', tmp / 'code3.json', '--include', 'dist')
    code = json.loads((tmp / 'code3.json').read_text(encoding='utf-8')) if rc == 0 else {'findings': []}
    if not any('Built copy' in (f.get('text') or '') for f in code['findings']):
        fails.append(f'--include dist does not scan the build output ({log.strip()[-160:]})')
    return fails


def validator_checks(tmp: Path) -> list[str]:
    import validate_report as vr
    fails = []
    load = lambda p: json.loads(p.read_text(encoding='utf-8'))  # noqa: E731
    rep, cands, keys, res = (load(EX / 'report.example.json'), load(EX / 'run' / 'candidates.json'),
                             load(EX / 'run' / 'keys.json'), load(EX / 'run' / 'resources.json'))
    rt = load(EX / 'run' / 'runtime' / 'runtime.json')
    fn = getattr(vr, 'validate', None)
    if fn is None:
        return ['validate_report.validate() is missing: the checks are only reachable through main()']
    # nothing parsed: no resource sets, no code file read, no runtime
    empty_res = {**res, 'sets': {}, 'entries': {}, 'not_parsed': []}
    empty_keys = {**keys, 'findings': [], 'key_coverage': {}, 'unused_check': []}
    empty_cands = {'source_locale': 'en', 'shipped_locales': [], 'candidates': [], 'proposed_patch': None,
                   'inputs': {'resource_sets': 0, 'code_files_scanned': 0, 'skipped_dirs': []}}
    r = json.loads(json.dumps(rep))
    r.update(findings=[], triage=[], needs_review=[], proposed_patch=None)
    r['scope']['locales'] = [{'code': 'en', 'status': 'checked', 'reason': None, 'files': [], 'keys': 0}]
    r['headline'].update(verdict='none_detected', severity_counts={}, key_coverage={}, evidence_completeness='complete',
                         overall_confidence='high', summary='Nothing found in the locales, files and pages checked.')
    errs = fn(r, empty_cands, empty_keys, empty_res, EX / 'bookclub', None)
    if not any('evidence_completeness' in e for e in errs):
        fails.append('a report that parsed no resources and read no code is accepted as complete / high')
    r['headline'].update(verdict='not_assessed', evidence_completeness='partial', overall_confidence='low')
    errs = fn(r, empty_cands, empty_keys, empty_res, EX / 'bookclub', None)
    if errs:
        fails.append(f'an honest not_assessed / partial / low report of an empty input is rejected: {errs[:3]}')
    # an honest, more cautious headline than computed is accepted
    r = json.loads(json.dumps(rep))
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if errs:
        fails.append(f'the example report no longer validates through validate(): {errs[:3]}')
    r['headline']['overall_confidence'] = 'low'
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if errs:
        fails.append(f'a more cautious overall_confidence (low, computed medium) is rejected: {errs[:2]}')
    # the pseudo-locale was a text walk: untranslated text can't be told apart, so evidence is partial
    rt2 = {**rt, 'pseudo_method': 'text_walk'}
    exp = vr.expected_headline(rep, cands, keys, {**res, 'not_parsed': []}, rt2) if hasattr(vr, 'expected_headline') else {}
    if exp.get('evidence_completeness') != 'partial':
        fails.append(f'a text-walk pseudo-locale run does not make evidence partial: {exp}')
    # agent findings with a basis re-read against the repository
    line = (EX / 'bookclub' / 'site' / 'index.html').read_text(encoding='utf-8').splitlines()
    n = next(i for i, t in enumerate(line, 1) if t.strip())
    agent = {'id': 'F99', 'candidate_id': None, 'source': 'agent', 'title': 'The page language is never updated after a switch',
             'kind': 'hardcoded_string', 'status': 'possible', 'locales': ['de'], 'severity': 'Medium', 'confidence': 'medium',
             'priority': 'P2', 'score': 2, 'reach': 1, 'leverage': 1, 'locations': [{'file': 'site/index.html', 'line': n}],
             'keys': [], 'evidence': [], 'basis': [{'file': 'site/index.html', 'line': n, 'excerpt': line[n - 1].strip()[:40]}],
             'remediation': 'Set document.documentElement.lang when the locale changes.',
             'verification_step': 'Switch to de and read the html lang attribute.'}
    r = json.loads(json.dumps(rep))
    r['findings'].append(agent)
    r['headline']['severity_counts']['Medium'] += 1
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if errs:
        fails.append(f'an agent finding whose basis is on its line is rejected: {errs[:3]}')
    agent['basis'][0]['excerpt'] = 'this text is not on that line'
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if not any('basis' in e for e in errs):
        fails.append('an agent finding whose basis excerpt is not on its line is accepted')
    agent['basis'] = []
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if not any('basis' in e for e in errs):
        fails.append('an agent finding with no basis is accepted')
    return fails


def cldr_checks(tmp: Path) -> list[str]:
    import fetch_cldr
    fails = []
    src = next(iter(sorted((SKILL / 'data').glob('cldr-*.json'))))
    crlf = tmp / src.name
    crlf.write_bytes(src.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    h = getattr(fetch_cldr, 'file_sha', None)
    if h is None:
        return ['fetch_cldr.file_sha is missing: --check hashes raw bytes, so a CRLF checkout reports a mismatch']
    if h(crlf) != h(src):
        fails.append('fetch_cldr hashes a CRLF copy of a CLDR subset differently from the LF file')
    return fails


def runtime_checks() -> list[str]:
    import render_locales as rl
    fails = []
    src = {'lang': 'en', 'dir': 'ltr', 'texts': [{'sel': 'p.credit', 'text': 'Made by the sample team', 'tag': 'p'},
                                                 {'sel': 'h1', 'text': 'Player', 'tag': 'h1'},
                                                 {'sel': 'button', 'text': 'Start', 'tag': 'button'}]}
    ru = {'lang': 'en-us', 'dir': 'ltr', 'texts': [{'sel': 'p.credit', 'text': 'Made by the sample team', 'tag': 'p'},
                                                   {'sel': 'h1', 'text': 'Плеер', 'tag': 'h1'},
                                                   {'sel': 'button', 'text': 'Start', 'tag': 'button'}]}
    fn = getattr(rl, 'compare_to_source', None)
    got = fn(src, ru, 'ru') if fn else []
    if not any(f['kind'] == 'untranslated' and f['element'] == 'html' for f in got):
        fails.append(f'a ru render whose <html lang> stays en-us is not flagged: {got}')
    if not any(f['kind'] == 'untranslated' and 'sample team' in f['text'] for f in got):
        fails.append(f'Latin text left unchanged in a ru render (expected Cyrillic) is not flagged: {got}')
    av = rl.availability({'locales': ['en', 'ru'], 'rtl_locales': ['ar', 'he'], 'source_locale': 'en'}, None) \
        if hasattr(rl, 'availability') else None
    if not av or av[0].get('ar') or not any(x['locale'] == 'ar' for x in av[1]):
        fails.append(f'without a resource pattern the RTL locales ar and he are rendered as if they shipped: {av}')
    # a text walk brackets every text node, hard-coded or not: the run must say untranslated text was not assessed
    note = rl.pseudo_record('text_walk') if hasattr(rl, 'pseudo_record') else {}
    if note.get('untranslated_check') != 'not_assessed' or not note.get('reason'):
        fails.append(f'a text-walk pseudo run does not record that it cannot find untranslated text: {note}')
    if (rl.pseudo_record('intercept') if hasattr(rl, 'pseudo_record') else {}).get('untranslated_check') != 'ran':
        fails.append('an intercepted pseudo run does not record its untranslated check as ran')
    return fails


def render_checks(tmp: Path) -> list[str]:
    fails = []
    d = json.loads((EX / 'report.example.json').read_text(encoding='utf-8'))
    d['scope']['rtl_locales'] = []
    d['headline']['key_coverage'] = {}
    p = tmp / 'r.json'
    p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    rc, out = run('render_report.py', p, '--color', 'never')
    if 'RTL .' in out or 'Key coverage: .' in out:
        fails.append('the renderer prints empty values ("RTL ." / "Key coverage: .")')
    return fails


WRAP_PAGE = """<!doctype html><html><body style="font:16px sans-serif">
<p style="width:220px"><a href="#">A link whose text wraps onto a second line here</a> <span>and a tail</span></p>
<div style="position:relative;height:40px"><span style="position:absolute;left:0;top:0">Stacked one</span>
<span style="position:absolute;left:10px;top:4px">Stacked two</span></div>
<input style="width:120px" placeholder="Введите ключ доступа из пяти букв">
</body></html>"""


def browser_checks() -> tuple[list[str], str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    import render_locales as rl
    fails = []
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            return [], f'skipped ({type(exc).__name__})'
        page = b.new_page(viewport={'width': 800, 'height': 600})
        page.set_content(WRAP_PAGE)
        snap = page.evaluate(rl.SNAP)
        b.close()
    found = rl.check(snap, 'locale', 'ru', {}, set())
    ov = [f for f in found if f['kind'] == 'overlap']
    if any('a:' in f['element'] or f['element'].startswith('a') for f in ov):
        fails.append(f'a wrapped inline link is reported as overlapping the text after it: {[f["element"] for f in ov]}')
    if not any('Stacked' in f['text'] for f in ov):
        fails.append(f'two absolutely positioned texts drawn on top of each other are not reported: {[f["text"] for f in ov]}')
    if not any(f['kind'] == 'truncation' and 'Введите' in f['text'] for f in found):
        fails.append('a placeholder wider than its 120 px input is not reported as truncation')
    return fails, 'ran'


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, fn in (('discover/scan_code: committed dist/ front end, JS messages, non-Latin text', static_front_checks),
                         ('validate_report: nothing parsed, honest partial, agent basis', validator_checks),
                         ('fetch_cldr: CRLF-safe hashes', cldr_checks),
                         ('render_report: empty fields', render_checks)):
            fails = fn(tmp)
            ok &= not fails
            print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    fails = runtime_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: render_locales: lang, untranslated, availability, text walk"
          + ''.join(f'\n  - {f}' for f in fails))
    fails, how = browser_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: render_locales in Chromium: wrapped inline, placeholders ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
