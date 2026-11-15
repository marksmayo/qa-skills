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
A single-locale Next.js site checks: a service worker (public/sw.js) is not a resource; JSX text in any script beside
expressions, text attributes and metadata exports; JSX concatenation; hard-coded locales and hand-made percentages;
Tailwind physical classes and arrows; the sr-only rule is not a fixed width (nor sr-only text truncation in Chromium);
single_locale with hard-coded text Low; the validator rejects readiness overclaims, vague dismissals, missing
limitations and wrong summary counts, and accepts an honest readiness_only report; --brief shows needs review and
limitations.
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


# A single-locale Next.js App Router site (a bike shop) with no i18n library and no resource file.
NEXT_LAYOUT = """import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Spoke & Chain bike shop',
  description: 'Repairs, rentals and route maps for city riders.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a href="#main" className="sr-only focus:not-sr-only focus:left-4">Skip to content</a>
        {children}
      </body>
    </html>
  );
}
"""
NEXT_PAGE = """import Link from 'next/link';

type Bike = { name: string; price: number; done: number };
const pick = <T,>(xs: Array<T>): T => xs[0];

function fmt(d: Date) {
  return d.toLocaleDateString("en-GB", { day: 'numeric', month: 'long' });
}
const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });

export default function Home({ bike, n }: { bike: Bike; n: number }) {
  const small = n < 3 && n > 0;
  return (
    <main id="main">
      <h1>Rentals</h1>
      <p>Last updated 20 June 2026.</p>
      <p>Repair progress: {bike.done}%</p>
      <p className="text-sm">
        Frame {bike.name} · Size {n} · Serviced {fmt(new Date())}
      </p>
      <footer className="sm:ml-8 text-right">
        <Link href="/about">About &amp; contact</Link>
        <Link href="/routes">Next route →</Link>
      </footer>
      <img src="/a.png" alt="A mechanic truing a wheel" />
      <button aria-label={`Close the ${bike.name} panel`} title="Close panel">x</button>
      <input placeholder="Search the workshop" />
      <span>schön geölte Ketten</span>
      <div className="rounded-l-lg pl-2" style={{ width: `${n}%` }} />
    </main>
  );
}
"""
NEXT_CSS = """.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}
.tag {
  width: 80px;
  overflow: hidden;
  white-space: nowrap;
}
"""
NEXT_SW = """// the service worker: caches the shell
const CACHE = 'shop-v1';
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('fetch', (e) => { e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request))); });
"""
NEXT_FILES = {'package.json': json.dumps({'dependencies': {'next': '15.0.0', 'react': '19.0.0'},
                                          'scripts': {'build': 'next build'}}),
              'next.config.ts': 'export default {};\n',
              'src/app/layout.tsx': NEXT_LAYOUT, 'src/app/page.tsx': NEXT_PAGE, 'src/app/globals.css': NEXT_CSS,
              'public/sw.js': NEXT_SW,
              'src/app/register.tsx': "'use client';\nexport function R() { navigator.serviceWorker.register('/sw.js'); return null; }\n"}


def next_static_checks(tmp: Path) -> list[str]:
    """A single-locale Next.js App Router site: a service worker is not a Swahili resource, the JSX rule reads
    non-ASCII text, digits, '&', text beside {expressions}, attributes and metadata exports, formats with a
    hard-coded locale and hand-made percentages are found, Tailwind physical classes and arrow glyphs are RTL
    problems, and the visually-hidden (sr-only) rule is not a fixed-width problem."""
    fails = []
    repo = write(tmp / 'nextshop', NEXT_FILES)
    rc, log = run('discover.py', '--repo', repo, '--out', tmp / 'nres.json')
    res = json.loads((tmp / 'nres.json').read_text(encoding='utf-8')) if (tmp / 'nres.json').is_file() else {}
    if any(x['file'] == 'public/sw.js' for x in res.get('not_parsed', [])):
        fails.append('public/sw.js (a service worker) is listed as a locale resource not parsed (sw read as Swahili)')
    rc, log = run('scan_code.py', '--repo', repo, '--out', tmp / 'ncode.json')
    code = json.loads((tmp / 'ncode.json').read_text(encoding='utf-8')) if (tmp / 'ncode.json').is_file() else {'findings': []}
    fs = code['findings']
    texts = {f.get('text') for f in fs if f['kind'] == 'hardcoded_string'}
    for want in ('Last updated 20 June 2026.', 'Repair progress: {…}%', 'About & contact', 'schön geölte Ketten',
                 'A mechanic truing a wheel', 'Close the {…} panel', 'Close panel', 'Search the workshop',
                 'Spoke & Chain bike shop', 'Repairs, rentals and route maps for city riders.', 'Rentals'):
        if want not in texts:
            fails.append(f'scan_code misses JSX/metadata text {want!r} (found {sorted(t for t in texts if t)[:14]})')
    if any(t and ('xs[0]' in t or 'return' in t or 'small' in t) for t in texts):
        fails.append(f'scan_code reads TypeScript generics or comparisons as JSX text: {sorted(t for t in texts if t)}')
    if not any(f['kind'] == 'concatenation' and f['file'] == 'src/app/page.tsx' and f['line'] == 19 for f in fs):
        fails.append('a JSX sentence assembled from text and several {expressions} (page.tsx:19) is not a concatenation')

    def at(kind, line, file='src/app/page.tsx'):
        return any(f['kind'] == kind and f['file'] == file and f['line'] == line for f in fs)
    if not at('format', 7):
        fails.append('toLocaleDateString("en-GB") (a hard-coded locale) is not a format finding')
    if not at('format', 9):
        fails.append("new Intl.NumberFormat('en-US') (a hard-coded locale) is not a format finding")
    if not at('format', 17):
        fails.append('a hand-formatted percentage ({x}%) is not a format finding')
    if at('format', 29):
        fails.append('a CSS width in a style object (`${n}%`) is reported as a hand-formatted percentage')
    if not at('rtl_physical', 21):
        fails.append('Tailwind physical classes (sm:ml-8 text-right) are not rtl_physical findings')
    if not at('rtl_physical', 23):
        fails.append('an arrow glyph in JSX text (→) is not an rtl_physical finding')
    if not at('rtl_physical', 13, 'src/app/layout.tsx'):
        fails.append('focus:left-4 in a className is not an rtl_physical finding')
    fw = [f for f in fs if f['kind'] == 'fixed_width']
    if any('sr-only' in (f.get('selector') or '') for f in fw):
        fails.append(f'the visually-hidden .sr-only rule is reported as a fixed width that hides overflow: {fw}')
    if not any('.tag' in (f.get('selector') or '') for f in fw):
        fails.append('a real fixed-width rule (.tag) is no longer reported')
    # merge: a single-locale product (no resource set, no second locale) is a readiness review
    if res:
        rc, log = run('check_keys.py', tmp / 'nres.json', '--repo', repo, '--out', tmp / 'nkeys.json')
        rc, log = run('merge_findings.py', '--resources', tmp / 'nres.json', '--keys', tmp / 'nkeys.json', '--code',
                      tmp / 'ncode.json', '--repo', repo, '--out', tmp / 'ncands.json')
        cands = json.loads((tmp / 'ncands.json').read_text(encoding='utf-8')) if rc == 0 else {}
        if cands.get('single_locale') is not True:
            fails.append(f'candidates.json does not mark a product with no second locale as single_locale: {log[-200:]}')
        hs = [c for c in cands.get('candidates', []) if c['kind'] == 'hardcoded_string']
        if any(c['severity'] != 'Low' for c in hs):
            fails.append('hard-coded text in a single-locale product is rated above Low (it is a readiness finding)')
    return fails


def single_locale_report(cands: dict) -> dict:
    """A report on the single-locale fixture that dismisses everything, claims readiness and lists no limitation."""
    rep = json.loads((EX / 'report.example.json').read_text(encoding='utf-8'))
    rep['scope'].update(repo='nextshop', site=None, pages=[], viewports=[], locales=[
        {'code': 'en', 'status': 'checked', 'reason': None, 'files': [], 'keys': 0}], rtl_locales=[], pseudo=None)
    rep.update(findings=[], needs_review=[], proposed_patch=None, limitations=[],
               triage=[{'candidate_id': c['id'], 'decision': 'dismissed', 'finding_id': None,
                        'reason': 'not needed for this English site'} for c in cands['candidates']])
    rep['headline'].update(verdict='none_detected', severity_counts={}, key_coverage={}, evidence_completeness='partial',
                           overall_confidence='medium',
                           summary='No i18n issues detected. The site is RTL-ready and ready for localisation into any language.')
    return rep


def single_locale_validator_checks(tmp: Path) -> list[str]:
    import validate_report as vr
    fails = []
    load = lambda p: json.loads(p.read_text(encoding='utf-8'))  # noqa: E731
    if not (tmp / 'ncands.json').is_file():
        return ['the single-locale fixture was not merged (see the Next.js checks)']
    cands, keys, res = load(tmp / 'ncands.json'), load(tmp / 'nkeys.json'), load(tmp / 'nres.json')
    repo = tmp / 'nextshop'
    rep = single_locale_report(cands)
    errs = vr.validate(rep, cands, keys, res, repo, None)
    for what, needle in (('an "RTL-ready" claim with no RTL locale checked', 'RTL'),
                         ('a "ready for localisation into any language" claim', 'locali'),
                         ('dismissals with the vague reason "not needed for this English site"', 'vague'),
                         ('no limitation saying no translation resource exists', 'limitations'),
                         ('verdict none_detected for a single-locale product', 'verdict')):
        if not any(needle in e for e in errs):
            fails.append(f'the validator accepts {what}: {errs[:4]}')
    # the honest version: readiness only, specific dismissals, the limitations it needs
    rep['headline'].update(verdict='readiness_only', summary='Single-locale product: it ships English only, with no translation '
                           'resources, so localisation checks were not assessed; 0 findings, readiness review only.')
    for t in rep['triage']:
        t.update(decision='needs_review', reason='Copy text; the owner decides whether localisation is planned.')
    rep['needs_review'] = [{'candidate_id': t['candidate_id'], 'reason': t['reason']} for t in rep['triage']]
    rep['limitations'] = ['No translation resources or i18n library: the product ships one locale (en), so key, placeholder '
                          'and plural checks were not assessed.']
    errs = vr.validate(rep, cands, keys, res, repo, None)
    if errs:
        fails.append(f'an honest readiness-only report of a single-locale product is rejected: {errs[:3]}')
    rep['headline']['summary'] = (f'Readiness review of an English-only site; 0 findings and {len(rep["needs_review"]) + 1} '
                                  'items need review.')
    errs = vr.validate(rep, cands, keys, res, repo, None)
    if not any('need' in e and 'review' in e for e in errs):
        fails.append(f'a summary whose needs-review count is wrong is accepted: {errs[:3]}')
    return fails


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
    n_issues = len(rep['findings'])
    r['headline']['summary'] = r['headline']['summary'].replace(f'{n_issues} issues', f'{n_issues + 1} issues')
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
    # limitations must name a locale that was not run; the summary's counts must match the report
    r = json.loads(json.dumps(rep))
    r['limitations'] = [x for x in r['limitations'] if 'Hebrew' not in x]
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if not any('limitations' in e and 'he' in e for e in errs):
        fails.append(f'a report whose limitations never mention the not_run locale he is accepted: {errs[:3]}')
    r = json.loads(json.dumps(rep))
    n_issues = len(rep['findings'])
    r['headline']['summary'] = r['headline']['summary'].replace(f'{n_issues} issues', f'{n_issues - 1} issues')
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if not any('summary' in e and str(n_issues - 1) in e for e in errs):
        fails.append(f'a summary that says {n_issues - 1} issues for {n_issues} findings is accepted: {errs[:3]}')
    r = json.loads(json.dumps(rep))
    for t in r['triage']:
        if t['decision'] == 'dismissed':
            t['reason'] = 'false positive'
    errs = fn(r, cands, keys, res, EX / 'bookclub', rt)
    if not any('vague' in e for e in errs):
        fails.append('a dismissal whose whole reason is "false positive" is accepted')
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
    try:
        nores = rl.pseudo_record('text_walk', no_resources=True)
    except TypeError:
        nores = {}
    if 'only' not in (nores.get('reason') or '') or nores.get('untranslated_check') != 'not_assessed':
        fails.append(f'a text walk on a site with no resource file does not say it is the only possible pseudo mode: {nores}')
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
    rc, out = run('render_report.py', EX / 'report.example.json', '--color', 'never', '--brief')
    if 'Needs review' not in out or 'Newsletter' not in out:
        fails.append('--brief leaves out the items that need review')
    if 'Limitations' not in out or 'Hebrew' not in out:
        fails.append('--brief leaves out the limitations')
    d['headline']['verdict'] = 'readiness_only'
    p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    rc, out = run('render_report.py', p, '--color', 'never', '--brief')
    if rc != 0 or 'READINESS' not in out:
        fails.append(f'the renderer does not show a readiness-only verdict: {out[-200:]}')
    return fails


WRAP_PAGE = """<!doctype html><html><body style="font:16px sans-serif">
<p style="width:220px"><a href="#">A link whose text wraps onto a second line here</a> <span>and a tail</span></p>
<div style="position:relative;height:40px"><span style="position:absolute;left:0;top:0">Stacked one</span>
<span style="position:absolute;left:10px;top:4px">Stacked two</span></div>
<input style="width:120px" placeholder="Введите ключ доступа из пяти букв">
<a class="sr-only" href="#main" style="position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap">Skip to the main content of this page</a>
<div class="vh" style="position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap"><span>Navigated to the second page of results</span></div>
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
    if any(f['kind'] == 'truncation' and ('Skip to' in f['text'] or 'Navigated' in f['text']) for f in found):
        fails.append('visually-hidden (sr-only) text is reported as truncation: '
                     f'{[f["element"] for f in found if f["kind"] == "truncation"]}')
    if not any(f['kind'] == 'truncation' and 'Введите' in f['text'] for f in found):
        fails.append('a placeholder wider than its 120 px input is not reported as truncation')
    return fails, 'ran'


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, fn in (('discover/scan_code: committed dist/ front end, JS messages, non-Latin text', static_front_checks),
                         ('discover/scan_code/merge: single-locale Next.js site (service worker, JSX, formats, RTL classes, '
                          'sr-only)', next_static_checks),
                         ('validate_report: single-locale overclaim, vague dismissals, limitations, counts',
                          single_locale_validator_checks),
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
