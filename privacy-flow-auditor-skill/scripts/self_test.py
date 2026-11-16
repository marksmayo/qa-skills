#!/usr/bin/env python3
"""Self-test for the privacy flow auditor's static scan, validator and renderer (stdlib only).

    python self_test.py

Builds small synthetic repositories in a temporary directory: a Go command-line tool with an
opt-out telemetry call home, a Rust tool the scan does not analyse, and a server-rendered ferry-timetable
site whose templates load analytics and ad tags with no consent check and send the search query to
analytics. It builds a domain list from synthetic EasyPrivacy rules (host and path rules must not flag a
whole shared domain) and re-merges the example capture with a declaration naming no vendor. Then it copies the
static-only example audit, changes one thing at a time, and checks what the validator and the
renderer say: telemetry rated before consent from a review finding, declared processors that must
name the recipient, inventory recipients from findings, recipients named by findings in the
headline and the brief, observed review findings with runtime evidence, credentials in a URL, and
the limitation an unanalysed language needs. Every host and value is invented.
"""
from __future__ import annotations
import copy, json, re, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ROOT = SKILL.parent
sys.path.insert(0, str(HERE))
import validate_privacy_audit as vpa  # noqa: E402

FAILS: list[str] = []
HOST = 'usage.shelfapp.example-metrics.io'


def check(cond: bool, what: str) -> None:
    print(('ok    ' if cond else 'FAIL  ') + what)
    if not cond:
        FAILS.append(what)


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')


def scan(files: dict[str, str]) -> tuple[dict, str]:
    with tempfile.TemporaryDirectory() as d:
        root, out = Path(d) / 'repo', Path(d) / 'static.json'
        write(root, files)
        r = subprocess.run([sys.executable, str(HERE / 'scan_privacy.py'), str(root), '--out', str(out)], capture_output=True, text=True)
        if r.returncode or not out.is_file():
            raise AssertionError(f'scan_privacy.py failed: {r.stderr[-400:]}')
        return json.loads(out.read_text(encoding='utf-8')), r.stdout + r.stderr


TELEMETRY_GO = f'''package telemetry

import (
\t"bytes"
\t"crypto/sha256"
\t"encoding/hex"
\t"encoding/json"
\t"log"
\t"net/http"
\t"os"
)

const endpoint = "https://{HOST}/v1/events"

// Enabled is true unless the user opted out.
func Enabled() bool {{
\tif os.Getenv("DO_NOT_TRACK") == "1" || os.Getenv("SHELF_NO_TELEMETRY") != "" {{
\t\treturn false
\t}}
\treturn true
}}

func machineID() string {{
\th, _ := os.Hostname()
\tsum := sha256.Sum256([]byte(h))
\treturn hex.EncodeToString(sum[:])
}}

func Send(event string) {{
\tif !Enabled() {{
\t\treturn
\t}}
\tbody, _ := json.Marshal(map[string]string{{"event": event, "machine": machineID()}})
\tresp, err := http.Post(endpoint, "application/json", bytes.NewReader(body))
\tif err != nil {{
\t\tlog.Printf("telemetry: %v", err)
\t\treturn
\t}}
\tresp.Body.Close()
}}
'''

MAIN_GO = '''package main

import (
\t"flag"
\t"log"
)

func main() {
\temail := flag.String("email", "", "account email for the shelf service")
\tflag.Parse()
\tlog.Printf("signing in as %s", *email)
}
'''


def go_scan() -> None:
    st, out = scan({'go.mod': 'module example.test/shelf\n\ngo 1.24\n', 'cmd/shelf/main.go': MAIN_GO,
                    'internal/telemetry/telemetry.go': TELEMETRY_GO, 'internal/store/store.go': 'package store\n',
                    'vendor/github.com/acme/kit/kit.go': 'package kit\n', 'vendor/github.com/acme/kit/more.go': 'package kit\n',
                    'web/report.js': 'console.log("report")\n'})
    cov = st.get('language_coverage') or {}
    check(cov.get('dominant') == 'Go' and 'Go' in (cov.get('supported_languages') or []), 'scan: Go is counted and analysed (language_coverage)')
    check((cov.get('supported') or {}).get('Go') == 3, 'scan: vendored Go files are not counted toward the dominant language')
    tel = [c for c in st['candidates'] if c['type'] == 'telemetry']
    check(len(tel) == 1 and tel[0].get('host') == HOST, 'scan: a Go package posting to a host with an opt-out is a telemetry candidate')
    facts = (tel[0].get('facts') or {}) if tel else {}
    check({'DO_NOT_TRACK', 'SHELF_NO_TELEMETRY'} <= set(facts.get('opt_out_env') or []), 'scan: the telemetry candidate names its opt-out variables')
    check('os.Hostname' in (facts.get('identifiers') or []) and facts.get('hashing') is True,
          'scan: the telemetry candidate records the identifier it reads and that it is hashed')
    check(tel and tel[0].get('side') == 'client', 'scan: a CLI call home runs on the user\'s machine (side client)')
    check(any(s['file'] == 'cmd/shelf/main.go' and s['kind'] == 'log' for s in st['sinks']), 'scan: Go log calls are log sinks')
    check(any(c['kind'] == 'cli_flag' and c['field'] == 'email' and c['category'] == 'contact' for c in st['collection']),
          'scan: a command-line flag named email is a collection point')
    check(any(c['type'] == 'personal_data_in_logs' and c['file'] == 'cmd/shelf/main.go' for c in st['candidates']),
          'scan: logging the email flag is a personal-data-in-logs candidate')


def unanalysed_language() -> None:
    files = {'Cargo.toml': '[package]\nname = "shelf"\n', 'web/app.js': 'console.log("x")\n'}
    files.update({f'src/m{i}.rs': f'pub fn f{i}() {{}}\n' for i in range(4)})
    st, out = scan(files)
    cov = st.get('language_coverage') or {}
    check(cov.get('status') == 'unsupported_dominant' and cov.get('needs_limitation') == ['Rust'], 'scan: an unanalysed dominant language is reported')
    check('WARNING' in out and 'rust' in out.lower(), 'scan: the unanalysed dominant language is a printed warning')


# a server-rendered site (a Flask app rendering Jinja templates): an analytics partial and an ad loader included on
# every page with no consent check, and a GET search form whose query the analytics config sends as the page path
ANALYTICS_PARTIAL = '''<!-- analytics partial, included by the base layout -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TEST0000"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-TEST0000', {
    page_path: window.location.pathname + window.location.search
  });
</script>
'''
FERRY_SITE = {
    'app.py': 'from flask import Flask, render_template, request\napp = Flask(__name__)\n\n\n@app.route("/")\ndef index():\n'
              '    return render_template("index.html", q=request.args.get("q", ""))\n',
    'templates/partials/analytics.html': ANALYTICS_PARTIAL,
    'templates/index.html': '{% include "partials/analytics.html" %}\n<h1>Ferry timetable</h1>\n'
                            '<form action="/" method="get">\n  <input type="search" name="q" placeholder="Search sailings">\n</form>\n',
    'templates/ads.j2': '<div class="ad">\n<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-0000"></script>\n</div>\n',
    'templates/port.html': '<h1>Port</h1>\n<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-0000"></script>\n',
}


def template_loaders() -> None:
    st, _ = scan(FERRY_SITE)
    loaders = [c for c in st['candidates'] if c['type'] == 'tracking_before_consent' and c.get('subtype') == 'loader_before_consent']
    vendors = {c.get('vendor') for c in loaders}
    check({'Google Tag Manager', 'Google AdSense'} <= vendors,
          f'scan: analytics and ad loaders in templates with no consent check are tracking_before_consent candidates (got {sorted(map(str, vendors))})')
    check(any(c['file'] == 'templates/ads.j2' for c in loaders), 'scan: a .j2 template is scanned like HTML')
    ads = [c for c in loaders if c.get('vendor') == 'Google AdSense']
    check(len(ads) == 1 and ads[0].get('also_at') == ['templates/port.html:2'], 'scan: one loader candidate per vendor, other pages under also_at')
    url = [c for c in st['candidates'] if c.get('subtype') == 'url_to_analytics']
    check(len(url) == 1 and url[0]['file'] == 'templates/partials/analytics.html' and url[0]['line'] == 7,
          'scan: an analytics config sending location.search is a url_to_analytics candidate')
    check(bool(url) and any('templates/index.html' in x for x in (url[0].get('facts') or {}).get('free_text_get_forms', [])),
          'scan: the url_to_analytics candidate names the GET form that puts free text in the query')
    gated = dict(FERRY_SITE)
    gated['templates/partials/analytics.html'] = '{% if cookie_consent.analytics %}\n' + ANALYTICS_PARTIAL + '{% endif %}\n'
    gated['templates/ads.j2'] = ('<script>gtag("consent", "default", {ad_storage: "denied", analytics_storage: "denied"});</script>\n'
                                 + FERRY_SITE['templates/ads.j2'])
    st, _ = scan(gated)
    check(not [c for c in st['candidates'] if c.get('subtype') == 'loader_before_consent'],
          'scan: a loader inside a consent check, or a site with a consent-mode default, is not a loader candidate')


def fonts_not_tracker() -> None:
    import fetch_lists
    easy = (b'||googleapis.com/aam.js\n||googleapis.com^*/gen_204?\n||clouderrorreporting.googleapis.com^\n||metrics-example.com^$third-party\n'
            b'||cdn-example.net/pixel.gif\n')
    out = fetch_lists.build(easy, b'Platform,Category,Cookie / Data Key name,Domain,Wildcard match\n', {'lists': {}})
    g = out['domains'].get('googleapis.com') or {}
    check(not g.get('tracker') and g.get('category') != 'tracker',
          'lists: a path rule or a subdomain rule does not flag the whole registrable domain (fonts.googleapis.com is no tracker)')
    check((out['domains'].get('metrics-example.com') or {}).get('tracker') is True, 'lists: a rule for the whole domain still flags it')
    check('clouderrorreporting.googleapis.com' in (out.get('tracker_hosts') or {}).get('googleapis.com', []),
          'lists: a subdomain rule is kept per host (tracker_hosts)')
    check('cdn-example.net/pixel.gif' in (out.get('tracker_host_paths') or {}).get('cdn-example.net', []),
          'lists: a host path rule is kept as a path rule (tracker_host_paths)')
    import merge_candidates as mc
    paths = {'googleapis.com': (out.get('tracker_host_paths') or {}).get('googleapis.com', [])}

    def hit(url):
        return mc.list_rule_match(paths, 'googleapis.com', url)
    check(hit('https://www.googleapis.com/x/gen_204?a=1') and hit('https://googleapis.com/aam.js')
          and not hit('https://fonts.googleapis.com/css2?family=X'),
          'lists: a ||domain^*/path rule matches that path on the domain and its subdomains, not every service on it')


def undeclared_without_canary() -> None:
    """A third party contacted on every page that is in no declared processor is an undeclared_processor candidate, canary or not."""
    run = SKILL / 'examples' / 'run'
    with tempfile.TemporaryDirectory() as d:
        decl = Path(d) / 'declared.json'
        decl.write_text(json.dumps({'source': 'synthetic privacy policy naming no vendor', 'processors': []}), encoding='utf-8')
        out = Path(d) / 'candidates.json'
        r = subprocess.run([sys.executable, str(HERE / 'merge_candidates.py'), '--capture', str(run / 'capture.json'), '--static',
                            str(run / 'static.json'), '--flows', str(SKILL / 'examples' / 'flows.json'), '--lists',
                            str(SKILL / 'examples' / 'lists.example.json'), '--declared', str(decl), '--out', str(out)],
                           capture_output=True, text=True, cwd=ROOT)
        c = json.loads(out.read_text(encoding='utf-8')) if out.is_file() else {'candidates': [], 'recipients': []}
    und = {x['domain'] for x in c['candidates'] if x['type'] == 'undeclared_processor'}
    third = {x['domain'] for x in c['recipients'] if x['kind'] == 'third_party'}
    check(bool(third) and third <= und, f'merge: every undeclared third-party recipient is an undeclared_processor candidate '
                                        f'(recipients {sorted(third)}, candidates {sorted(und)}){r.stderr[-200:]}')


# ---------------------------------------------------------------- validator and renderer
BASE =SKILL / 'examples' / 'privacy_audit.static-only.example.json'
CANDS = SKILL / 'examples' / 'static-only' / 'candidates.json'
REPO = SKILL / 'examples' / 'clinic-signup'
LOG_LINE = "logger.info('new patient signup email=%s dob=%s reason=%s', patient['email'], patient['dob'], patient['reason'])"


def telemetry_finding(**kw) -> dict:
    f = {'id': 'F06', 'type': 'telemetry', 'title': 'The command-line tool calls home with a hashed machine id before any choice',
         'source': 'agent', 'basis': [{'file': 'serve.py', 'line': 62, 'excerpt': LOG_LINE}], 'candidates': [],
         'categories': ['online_identifier'], 'categories_basis': 'the payload carries a hashed host name, a stable machine identifier',
         'fields': None, 'vendor': None, 'domain': HOST, 'recipient_kind': 'third_party',
         'consent_states': [], 'flow_basis': 'before_consent', 'default_on': True, 'reach': 2,
         'reach_source': 'every run of the tool unless an opt-out variable is set', 'confidence': 'traced',
         'evidence': {'summary': 'Found by review: telemetry is on unless DO_NOT_TRACK is set.', 'code_path': [{'file': 'serve.py', 'line': 62}]},
         'references': {r: vpa.PROV['types'].get('telemetry', {}).get(r, []) for r in ('EU', 'AU')},
         'fix': 'Ask before the first send, and honour the opt-out for the update check too.',
         'verification': 'Point the telemetry endpoint at a loopback sink and run with no opt-out: nothing arrives before a choice.'}
    f.update(kw)
    return f


def run_validator(rep: dict, extra: list[str] | None = None, cands: dict | None = None, evidence: dict | None = None,
                  repo: Path | None = None) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'audit.json'
        p.write_text(json.dumps(rep, indent=1), encoding='utf-8')
        cp = CANDS
        if cands is not None:
            cp = Path(d) / 'candidates.json'
            cp.write_text(json.dumps(cands), encoding='utf-8')
        args = [sys.executable, str(HERE / 'validate_privacy_audit.py'), str(p), '--candidates', str(cp), '--repo', str(repo or REPO)]
        if evidence:
            ev = Path(d) / 'ev'
            write(ev, evidence)
            args += ['--evidence', str(ev)]
        r = subprocess.run(args + ['--write-summary'] + (extra or []), capture_output=True, text=True, cwd=ROOT)
        return r.stdout + r.stderr


def base() -> dict:
    rep = json.loads(BASE.read_text(encoding='utf-8'))
    for f in rep['findings']:
        for k in vpa.RATING:
            f.pop(k, None)
    return rep


def telemetry_before_consent() -> None:
    rep = base()
    rep['findings'].append(telemetry_finding())
    out = run_validator(rep)
    check('unknown type' not in out, 'validator: telemetry is a finding type')
    rep = base()
    rep['findings'].append(telemetry_finding(type='first_party_beacon', references={r: vpa.PROV['types']['first_party_beacon'][r] for r in ('EU', 'AU')}))
    out = run_validator(rep)
    check('F06: flow_basis before_consent' not in out, 'validator: opt-out-by-default telemetry found by review can be rated before consent')
    rep = base()
    rep['findings'].append(telemetry_finding(default_on=None, type='first_party_beacon',
                                             references={r: vpa.PROV['types']['first_party_beacon'][r] for r in ('EU', 'AU')}))
    out = run_validator(rep)
    check('F06: flow_basis before_consent' in out, 'validator: before consent from review still needs default_on (sent with no choice made)')
    rep = base()
    rep['findings'].append(telemetry_finding(type='personal_data_in_logs', flow_basis='before_consent'))
    check('F06: flow_basis before_consent' in run_validator(rep), 'validator: default_on is for telemetry and first-party beacons only')


def declared_must_name() -> None:
    rep = base()
    rep['findings'].append(telemetry_finding(flow_basis='declared_unexpected', recipient_kind='declared_processor', default_on=None))
    out = run_validator(rep)
    check('no declared processor names the recipient' in out, 'validator: declared_unexpected fails when no declared processor names the recipient')
    rep = base()
    rep['metadata']['declared_processors'] = f'Shelf README, Telemetry section (events go to {HOST})'
    rep['findings'].append(telemetry_finding(flow_basis='declared_unexpected', recipient_kind='declared_processor', default_on=None))
    cands = json.loads(CANDS.read_text(encoding='utf-8'))
    cands['inputs']['declared'] = None
    out = run_validator(rep, cands=cands)
    check('F06: flow_basis declared_unexpected' not in out, 'validator: declared_unexpected passes when the declaration names the recipient')
    rep = base()
    rep['findings'].append(telemetry_finding(flow_basis='declared_unexpected', recipient_kind='declared_processor', default_on=None,
                                             vendor='Replayr', domain=None))
    check('F06: flow_basis declared_unexpected' not in run_validator(rep), 'validator: a vendor in the declared processors file counts')


def inventory_and_headline() -> None:
    rep = base()
    rep['findings'].append(telemetry_finding(fields=['email'], categories=['contact', 'online_identifier']))
    rep['inventory'][0]['recipients'] = [{'domain': HOST, 'vendor': None, 'kind': 'third_party', 'states': [], 'encodings': ['raw'],
                                          'via': 'F06'}]
    out = run_validator(rep)
    check(f'recipient {HOST} has no live candidate' not in out, 'validator: an inventory recipient may come from a finding that names it')
    rep['inventory'][1]['recipients'] = [{'domain': 'elsewhere.example-metrics.io', 'vendor': None, 'kind': 'third_party', 'states': []}]
    out = run_validator(rep)
    check('elsewhere.example-metrics.io' in out, 'validator: an inventory recipient no candidate or finding names still fails')
    rep = base()
    rep['findings'].append(telemetry_finding())
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'audit.json'
        p.write_text(json.dumps(rep, indent=1), encoding='utf-8')
        subprocess.run([sys.executable, str(HERE / 'validate_privacy_audit.py'), str(p), '--candidates', str(CANDS), '--repo', str(REPO),
                        '--write-summary'], capture_output=True, text=True, cwd=ROOT)
        filled = json.loads(p.read_text(encoding='utf-8'))
        check(HOST in (filled['headline'].get('recipients_in_findings') or []), 'headline: recipients named by findings are listed')
        r = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(p), '--brief', '--color', 'never'], capture_output=True,
                           text=True)
        check(any('named by findings' in ln and HOST in ln for ln in r.stdout.splitlines()),
              'render --brief: names the recipients findings name')
    full = json.loads((SKILL / 'examples' / 'privacy_audit.example.json').read_text(encoding='utf-8'))
    full['headline']['consent_banner'] = False
    full['headline']['third_parties_before_consent'] = []
    full['consent_matrix'] = [{'state': 'no_banner', 'status': 'tested', 'third_party_domains': [], 'cookies': [], 'canaries_to_third_parties': []}]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'audit.json'
        p.write_text(json.dumps(full), encoding='utf-8')
        r = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(p), '--brief', '--color', 'never'], capture_output=True, text=True)
    check('Third parties contacted: none' not in r.stdout, 'render: no "Third parties contacted: none" (it names the captured pages)')


def observed_with_runtime_evidence() -> None:
    rep = base()
    beacon = {'type': 'first_party_beacon', 'references': {r: vpa.PROV['types']['first_party_beacon'][r] for r in ('EU', 'AU')}}
    rep['findings'].append(telemetry_finding(confidence='observed', basis=[{'evidence': 'sink.log'}], **beacon))
    out = run_validator(rep, evidence={'sink.log': f'POST /v1/events host={HOST} machine=<sha256>\n'})
    check('F06: observed' not in out and 'F06: found by review, observed' not in out,
          'validator: a review finding with recorded runtime evidence (a sink log) may be observed without a browser run')
    rep = base()
    rep['findings'].append(telemetry_finding(confidence='observed', **beacon))
    out = run_validator(rep)
    check('runtime evidence' in out and 'browser run' not in out.split('F06: ', 1)[-1].split('\n')[0],
          'validator: observed without runtime evidence says any recorded runtime evidence counts')


def url_credentials_message() -> None:
    probs = vpa.pii_problems('see https://alice:' + 'k3y9' + '@grid.example-metrics.io/wd/hub', 'x.json', [])
    check(bool(probs) and all('email' not in p for p in probs) and any('credential' in p for p in probs),
          'validator: credentials in a URL are reported as a credential, not an email address')


def language_limitation() -> None:
    cands = json.loads(CANDS.read_text(encoding='utf-8'))
    st = json.loads((ROOT / cands['static']).read_text(encoding='utf-8'))
    st['language_coverage'] = {'status': 'unsupported_dominant', 'dominant': 'Rust', 'dominant_unsupported_language': 'Rust',
                               'unsupported': {'Rust': 40}, 'supported': {'JavaScript': 3}, 'needs_limitation': ['Rust']}
    with tempfile.TemporaryDirectory() as d:
        sp = Path(d) / 'static.json'
        sp.write_text(json.dumps(st), encoding='utf-8')
        cands['static'] = str(sp)
        out = run_validator(base(), cands=cands)
        check('rust' in out.lower() and 'limitation' in out, 'validator: an unanalysed dominant language needs a limitation naming it')
        rep = base()
        rep['limitations'].append('Rust code (40 files) was not analysed by the static scan; telemetry and logging in it were read by hand only.')
        out = run_validator(rep, cands=cands)
        check('rust' not in out.lower(), 'validator: a limitation naming the language satisfies it')
    cands = json.loads(CANDS.read_text(encoding='utf-8'))
    st = json.loads((ROOT / cands['static']).read_text(encoding='utf-8'))
    st['language_coverage'] = dict(st.get('language_coverage') or {}, needs_limitation_dirs=['site/dist/'])
    with tempfile.TemporaryDirectory() as d:
        sp = Path(d) / 'static.json'
        sp.write_text(json.dumps(st), encoding='utf-8')
        cands['static'] = str(sp)
        out = run_validator(base(), cands=cands)
        check('site/dist/' in out and 'limitation' in out, 'validator: skipped build output holding most of the source needs a limitation naming it')
        rep = base()
        rep['limitations'].append('site/dist/ was skipped as build output and not scanned; its sends and logs were not read.')
        out = run_validator(rep, cands=cands)
        check('site/dist/' not in out, 'validator: a limitation naming the skipped directory satisfies it')


# A FastAPI app mounting StaticFiles on a committed, hand-written dist/ folder (no bundler, not in .gitignore), with a
# proxy that forwards the visitor's headers to an asset host through an httpx AsyncClient. The front end calls a sync
# service with fetch(), puts the signed-in profile's email in a storage URL, logs the profile, asks for an access key in
# an input with no name or id, and ships an Emscripten-style loader whose global FS object is not FullStory. A debug
# panel takes a memory address (0x...), which is not a postal address.
SYNC_HOST = 'api.example-sync.io'
BLOB_HOST = 'store.example-blobs.io'
CDN_HOST = 'cdn.example-assets.io'
STATIC_SITE = {
    '.gitignore': '__pycache__/\n*.pyc\n',
    'requirements.txt': 'fastapi\nhttpx\n',
    'server.py': ('import os\nimport httpx\nfrom fastapi import FastAPI, Request\nfrom fastapi.responses import Response\n'
                  'from fastapi.staticfiles import StaticFiles\n\n'
                  f'ASSET_BASE = os.environ.get("ASSET_BASE", "https://{CDN_HOST}/pack/")\napp = FastAPI()\n\n\n'
                  '@app.get("/assets/{path:path}")\nasync def assets(path: str, request: Request):\n'
                  '    async with httpx.AsyncClient() as client:\n'
                  '        req = client.build_request("GET", ASSET_BASE + path, headers=dict(request.headers.items()))\n'
                  '        resp = await client.send(req)\n'
                  '    return Response(resp.content, status_code=resp.status_code)\n\n'
                  'app.mount("/", StaticFiles(directory="dist", html=True), name="static")\n'),
    'dist/index.html': ('<!doctype html>\n<html><body>\n<div class="login">\n'
                        '  <input type="text" class="access-key-input" placeholder="Enter access key">\n'
                        '  <button id="go">Go</button>\n</div>\n<script src="cloud.js"></script>\n<script src="modules/fs.js"></script>\n'
                        '<script src="debug.js"></script>\n</body></html>\n'),
    'dist/cloud.js': ('// sync client for the save service\n'
                      f'const storageEndpoint = "https://{BLOB_HOST}/saves";\n\n'
                      'async function resolveToken(key) {\n'
                      f'  const res = await fetch("https://{SYNC_HOST}/token/get?id=" + key);\n'
                      '  const profile = await res.json();\n'
                      "  console.log('[Sync] resolveToken', profile);\n"
                      '  return profile;\n}\n\n'
                      'async function listSaves(profile) {\n'
                      '  const r = await fetch(storageEndpoint + "/" + profile.email + "/index.json");\n'
                      '  return r.json();\n}\n'),
    'dist/modules/fs.js': ('var FS = {\n  initialized: false,\n  init: function () {\n    FS.initialized = true;\n  }\n};\n'
                           'FS.initialized = true;\n'),
    'dist/debug.js': ('function openPanel(panel) {\n'
                      '  panel.innerHTML = \'<label>Address</label><input type="text" id="dbg-manual-addr" placeholder="0xAddress">\';\n}\n'),
}


def scan_args(files: dict[str, str], args: list[str]) -> tuple[dict, str]:
    with tempfile.TemporaryDirectory() as d:
        root, out = Path(d) / 'repo', Path(d) / 'static.json'
        write(root, files)
        r = subprocess.run([sys.executable, str(HERE / 'scan_privacy.py'), str(root), '--out', str(out)] + args, capture_output=True, text=True)
        if r.returncode or not out.is_file():
            raise AssertionError(f'scan_privacy.py failed: {r.stderr[-400:]}')
        return json.loads(out.read_text(encoding='utf-8')), r.stdout + r.stderr


def committed_dist() -> None:
    st, out = scan(STATIC_SITE)
    check(any(s['file'].startswith('dist/') for s in st['sinks']),
          'scan: a committed, hand-written dist/ served by the app is scanned as source (not skipped as build output)')
    dec = [x for x in st.get('build_output_dirs') or [] if x.get('dir') == 'dist/']
    check(bool(dec) and dec[0].get('decision') == 'scanned' and dec[0].get('reason'),
          'scan: the decision on dist/ is recorded with its reason (build_output_dirs)')
    built = dict(STATIC_SITE)
    built['.gitignore'] = 'dist/\n'
    built['dist/cloud.js'] = STATIC_SITE['dist/cloud.js'] + '//# sourceMappingURL=cloud.js.map\n'
    st, out = scan(built)
    check(not any(s['file'].startswith('dist/') for s in st['sinks']), 'scan: a git-ignored dist/ with source maps is build output and skipped')
    dec = [x for x in st.get('build_output_dirs') or [] if x.get('dir') == 'dist/']
    check(bool(dec) and dec[0].get('decision') == 'skipped' and 'gitignore' in (dec[0].get('reason') or ''),
          'scan: a skipped build-output directory says why (git-ignored, source maps)')
    check('dist/' in ((st.get('language_coverage') or {}).get('needs_limitation_dirs') or []) and 'dist/' in out and 'WARNING' in out,
          'scan: skipped build output holding more source than was scanned needs a limitation and a printed warning')
    st, _ = scan_args(built, ['--include', 'dist'])
    check(any(s['file'].startswith('dist/') for s in st['sinks']), 'scan: --include dist scans a skipped build-output directory')


def static_front_end_flows() -> None:
    st, _ = scan(STATIC_SITE)
    hosts = {(h['host'], h.get('side')) for h in st.get('outbound_hosts') or []}
    check((SYNC_HOST, 'client') in hosts, f'scan: a literal third-party URL in browser fetch() is an outbound host (got {sorted(hosts)})')
    check((CDN_HOST, 'server') in hosts, 'scan: a literal third-party URL in server code with an outbound client is an outbound host')
    em = [c for c in st['candidates'] if c['file'] == 'dist/cloud.js' and 'email' in (c.get('fields') or [])]
    check(bool(em) and em[0]['type'] == 'personal_data_to_third_party',
          'scan: an email read from an object (an API response) and put into a fetch URL is a candidate')
    logs = [c for c in st['candidates'] if c['file'] == 'dist/cloud.js' and c['type'] == 'personal_data_in_logs']
    check(bool(logs) and 'profile' in (logs[0].get('fields') or []), 'scan: logging a whole profile object is a personal-data-in-logs candidate')
    key = [c for c in st['collection'] if c['file'] == 'dist/index.html' and c['kind'] == 'form_field']
    check(bool(key) and key[0].get('field') and key[0].get('category') == 'online_identifier',
          f'scan: an input with no name or id is classified from its class and placeholder (got {key[:1]})')
    check(not any(s['file'] == 'dist/modules/fs.js' and s['sink'] == 'fullstory' for s in st['sinks']),
          'scan: an Emscripten FS object (FS.initialized) is not a FullStory sink')
    addr = [c for c in st['collection'] if c['file'] == 'dist/debug.js' and c.get('id') == 'dbg-manual-addr']
    check(bool(addr) and addr[0].get('category') != 'contact', f'scan: a 0x memory-address field is not contact data (got {addr[:1]})')
    py = [s for s in st['sinks'] if s['file'] == 'server.py' and s['kind'] == 'http']
    check(len({s['line'] for s in py}) >= 2, 'scan: httpx AsyncClient build_request and send are outbound HTTP sinks')
    hp = [c for c in st['candidates'] if c.get('subtype') == 'header_passthrough']
    check(bool(hp) and hp[0]['file'] == 'server.py' and hp[0].get('side') == 'server',
          'scan: forwarding the request headers to an outbound call is a header_passthrough candidate')


def agent_undeclared_third_party() -> None:
    f = telemetry_finding(type='personal_data_to_third_party', flow_basis='undeclared_third_party', default_on=None, recipient_kind='third_party',
                          references={r: vpa.PROV['types']['personal_data_to_third_party'][r] for r in ('EU', 'AU')})
    rep = base()
    rep['findings'].append(f)
    out = run_validator(rep)
    check('F06: flow_basis undeclared_third_party' not in out,
          'validator: a review finding may be undeclared_third_party when the declared processors (--declared) do not name its domain')
    rep = base()
    rep['findings'].append(dict(f, vendor='Replayr', domain='replayr.test'))
    check('F06: flow_basis undeclared_third_party' in run_validator(rep),
          'validator: a review finding whose recipient is declared is not undeclared_third_party')


def console_and_key_canaries() -> None:
    import match_canaries as mcan
    cs = mcan.new_set('7f3a', key_length=5)
    key = (cs['fields'].get('key') or {}).get('value', '')
    check(len(key) == 5 and cs['fields']['key'].get('category') == 'online_identifier', 'canaries: a key canary of a given length exists')
    m = mcan.Matcher(cs)
    check(f'<canary:key:raw>' in (m.redact(f'https://{SYNC_HOST}/token/get?id={key}') or ''), 'canaries: a key canary is redacted from URLs')
    import capture_flows as cf
    cfg = {'flows': [{'id': 'f1', 'steps': [{'name': 's', 'actions': [{'fill': '#k', 'value': 'zq9x7'}]}]}]}
    m2 = cf.matcher_for(cs, cfg)
    check('zq9x7' not in (m2.redact('GET /token?id=zq9x7') or 'zq9x7'), 'capture: a literal value typed in a flow is redacted like a canary')

    class Msg:
        type = 'log'
        text = f'[Sync] resolveToken {{"email": "{cs["fields"]["email"]["value"]}"}}'
    rec = cf.Recorder(m)
    rec.phase = 'f1/s'
    rec.on_console(Msg())
    hits = getattr(rec, 'console', [])
    check(bool(hits) and hits[0]['matches'] and cs['fields']['email']['value'] not in json.dumps(hits),
          'capture: a canary in the browser console is recorded (redacted)')
    run = SKILL / 'examples' / 'run'
    cap = json.loads((run / 'capture.json').read_text(encoding='utf-8'))
    cap['states'][0]['console'] = [{'phase': 'signup/submit', 'type': 'log', 'text': '<canary:email:raw> signed in',
                                    'matches': [{'field': 'email', 'category': 'contact', 'encoding': 'raw'}]}]
    with tempfile.TemporaryDirectory() as d:
        cp, out = Path(d) / 'capture.json', Path(d) / 'candidates.json'
        cp.write_text(json.dumps(cap), encoding='utf-8')
        subprocess.run([sys.executable, str(HERE / 'merge_candidates.py'), '--capture', str(cp), '--static', str(run / 'static.json'), '--flows',
                        str(SKILL / 'examples' / 'flows.json'), '--lists', str(SKILL / 'examples' / 'lists.example.json'), '--out', str(out)],
                       capture_output=True, text=True, cwd=ROOT)
        c = json.loads(out.read_text(encoding='utf-8')) if out.is_file() else {'candidates': []}
    check(any(x.get('sink') == 'browser_console' and x['type'] == 'personal_data_in_logs' for x in c['candidates']),
          'merge: a canary in the browser console is a personal-data-in-logs candidate')


# A bird-watching site on the Next.js App Router: the root layout loads an analytics tag, an ad loader, an SEO analytics
# vendor and an unknown statistics host through next/script, after dns-prefetch and preconnect hints for some of the
# hosts; next.config headers() sets a CSP whose script-src leaves out the ad vendor's consent-message host and the
# unknown host, and whose connect-src allows a widget host nothing uses. Every host and id is invented or a vendor's
# documented loader host.
NEXT_LAYOUT = '''import Script from 'next/script';
import './globals.css';

export const metadata = { title: 'Bird log' };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="dns-prefetch" href="https://www.google-analytics.com" />
        <link rel="preconnect" href="https://pagead2.googlesyndication.com" />
      </head>
      <body>
        {children}
        <Script src="https://www.googletagmanager.com/gtag/js?id=G-TEST0000" strategy="afterInteractive" />
        <Script id="ga-init" strategy="afterInteractive">
          {`window.dataLayer = window.dataLayer || [];
            function gtag(){dataLayer.push(arguments);}
            gtag('js', new Date());
            gtag('config', 'G-TEST0000');`}
        </Script>
        <Script
          src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-0000"
          strategy="afterInteractive"
          crossOrigin="anonymous"
        />
        <Script src="https://analytics.ahrefs.com/analytics.js" data-key="test-key" strategy="afterInteractive" />
        <Script src="https://stats.birdcount-metrics.io/s.js" strategy="lazyOnload" />
      </body>
    </html>
  );
}
'''
NEXT_CONFIG = '''import type { NextConfig } from 'next';

const csp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://pagead2.googlesyndication.com https://analytics.ahrefs.com",
  "connect-src 'self' https://www.google-analytics.com https://region1.google-analytics.com https://widgets.example-unused.io",
  "img-src 'self' data:",
].join('; ');

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'Content-Security-Policy', value: csp },
          { key: 'X-Frame-Options', value: 'DENY' },
        ],
      },
    ];
  },
};

export default nextConfig;
'''
NEXT_SITE = {
    'package.json': json.dumps({'name': 'bird-log', 'dependencies': {'next': '15.0.0', 'react': '19.0.0', 'react-dom': '19.0.0'}}),
    'next.config.ts': NEXT_CONFIG,
    'app/layout.tsx': NEXT_LAYOUT,
    'app/page.tsx': 'export default function Home() {\n  return <h1>Sightings</h1>;\n}\n',
    'app/globals.css': 'body { margin: 0; }\n',
}


def next_script_loaders() -> None:
    st, out = scan(NEXT_SITE)
    loaders = {c.get('vendor'): c for c in st['candidates'] if c.get('subtype') == 'loader_before_consent'}
    check({'Google Tag Manager', 'Google AdSense', 'Ahrefs Web Analytics'} <= set(loaders),
          f'scan: next/script loaders with no consent check are loader_before_consent candidates (got {sorted(map(str, loaders))})')
    check((loaders.get('Google AdSense') or {}).get('line') == 22 and (loaders.get('Google Tag Manager') or {}).get('line') == 15,
          'scan: a loader candidate sits at its <Script> line, not at a preconnect hint or the gtag config call '
          f'(got AdSense {(loaders.get("Google AdSense") or {}).get("line")}, GTM {(loaders.get("Google Tag Manager") or {}).get("line")})')
    check(((loaders.get('Google AdSense') or {}).get('facts') or {}).get('source') == 'next_script',
          'scan: a next/script loader is recorded with source next_script')
    sdk_hosts = {x.get('host') for x in st['sdks'] if x.get('host')}
    check('www.google-analytics.com' not in sdk_hosts,
          'scan: a host named only by a dns-prefetch hint or the CSP is not an SDK (nothing loads from it)')
    ment = [m for m in st.get('host_mentions') or [] if m.get('host') == 'www.google-analytics.com']
    kinds = {m.get('kind') for m in ment}
    check({'dns-prefetch', 'csp'} <= kinds, f'scan: every mention of a host is recorded with its kind (got {sorted(map(str, kinds))})')
    ads = [x for x in st['sdks'] if x.get('host') == 'pagead2.googlesyndication.com']
    check(len(ads) == 1 and ads[0]['line'] == 22 and {'loader', 'preconnect'} <= {m.get('kind') for m in ads[0].get('mentions') or []},
          'scan: the SDK entry is at the loader line and lists the other mentions (preconnect) with their kinds')
    unk = [x for x in st['sdks'] if x.get('host') == 'stats.birdcount-metrics.io']
    check(len(unk) == 1 and unk[0].get('vendor') is None, 'scan: a third-party script host missing from data/vendors.json is still listed (vendor unknown)')
    uc = [c for c in st['candidates'] if c.get('host') == 'stats.birdcount-metrics.io']
    check(len(uc) == 1 and uc[0].get('subtype') == 'unknown_third_party_loader' and uc[0].get('vendor') is None,
          'scan: an unknown third-party loader with no consent check is a candidate to triage (vendor unknown)')
    check('stats.birdcount-metrics.io' in out, 'scan: the unknown script host is printed')
    gated = dict(NEXT_SITE)
    gated['app/layout.tsx'] = NEXT_LAYOUT.replace('        {children}\n', '        {children}\n        {hasConsent && (<>\n').replace(
        '      </body>', '        </>)}\n      </body>')
    st, _ = scan(gated)
    check(not [c for c in st['candidates'] if c.get('subtype') in ('loader_before_consent', 'unknown_third_party_loader')],
          'scan: next/script loaders behind a consent check are not loader candidates')
    st, _ = scan({'package.json': json.dumps({'name': 'trail-map', 'dependencies': {'react': '19.0.0'}}),
                  'src/Analytics.jsx': 'export function Analytics() {\n  return <script async src="https://cdn.usefathom.com/script.js" data-site="ABCD" />;\n}\n'})
    fa = [c for c in st['candidates'] if c.get('subtype') == 'loader_before_consent']
    check(len(fa) == 1 and fa[0].get('vendor') == 'Fathom' and fa[0]['line'] == 2,
          f'scan: a JSX <script src> outside Next.js is a loader (got {[(c.get("vendor"), c.get("line")) for c in fa]})')


def csp_compared() -> None:
    st, out = scan(NEXT_SITE)
    csp = st.get('csp') or {}
    check(bool(csp.get('policies')) and csp['policies'][0].get('file') == 'next.config.ts',
          'scan: the CSP set in next.config headers() is read')
    blocked = {(x.get('host'), x.get('role')) for x in csp.get('blocked') or []}
    check(('fundingchoicesmessages.google.com', 'consent_message') in blocked,
          f'scan: a consent-message host the CSP would block is reported (got {sorted(map(str, blocked))})')
    check(('stats.birdcount-metrics.io', 'loader') in blocked, 'scan: a loader the CSP would block is reported')
    cc = [c for c in st['candidates'] if c.get('subtype') == 'consent_message_blocked_by_csp']
    check(len(cc) == 1 and cc[0].get('host') == 'fundingchoicesmessages.google.com' and cc[0]['file'] == 'next.config.ts'
          and cc[0].get('vendor') == 'Google AdSense', 'scan: the blocked consent-message host is a candidate at the CSP line, naming the loader vendor')
    unused = {x.get('source') for x in csp.get('unused_allowances') or []}
    check('https://widgets.example-unused.io' in unused and not {u for u in unused if 'google-analytics' in (u or '')},
          f'scan: CSP allowances nothing uses are listed; hosts a loader needs (gtag collection) are not (got {sorted(map(str, unused))})')
    ads = next((c for c in st['candidates'] if c.get('vendor') == 'Google AdSense' and c.get('subtype') == 'loader_before_consent'), {})
    unk = next((c for c in st['candidates'] if c.get('subtype') == 'unknown_third_party_loader'), {})
    check((ads.get('facts') or {}).get('csp') == 'allowed' and (unk.get('facts') or {}).get('csp') == 'blocked',
          'scan: each loader candidate says whether the CSP allows it')
    check('CSP' in out and 'fundingchoicesmessages.google.com' in out, 'scan: the CSP comparison is printed')


def vendors_added() -> None:
    vend = json.loads((SKILL / 'data' / 'vendors.json').read_text(encoding='utf-8'))['domains']
    names = {v['vendor'] for v in vend.values()}
    want = {'Ahrefs Web Analytics', 'Plausible', 'Fathom', 'Simple Analytics', 'Umami', 'Matomo', 'Microsoft Clarity', 'Hotjar'}
    check(want <= names, f'vendors: common analytics vendors are listed (missing {sorted(want - names)})')
    cap = {'site': 'http://birds.test:8000', 'third_party': 'block', 'states': [{
        'state': 'no_banner', 'status': 'tested', 'snapshots': [{'phase': 'initial', 'cookies': [], 'storage': []}],
        'requests': [{'n': 1, 'host': 'analytics.ahrefs.com', 'url': 'https://analytics.ahrefs.com/analytics.js', 'path': '/analytics.js',
                      'method': 'GET', 'phase': 'initial', 'matches': [], 'blocked': 'third_party'}]}]}
    with tempfile.TemporaryDirectory() as d:
        cp, fl, out = Path(d) / 'capture.json', Path(d) / 'flows.json', Path(d) / 'c.json'
        cp.write_text(json.dumps(cap), encoding='utf-8')
        fl.write_text(json.dumps({'site': cap['site'], 'consent': {'banner': None}, 'flows': []}), encoding='utf-8')
        subprocess.run([sys.executable, str(HERE / 'merge_candidates.py'), '--capture', str(cp), '--flows', str(fl), '--lists',
                        str(SKILL / 'examples' / 'lists.example.json'), '--out', str(out)], capture_output=True, text=True)
        c = json.loads(out.read_text(encoding='utf-8')) if out.is_file() else {'candidates': []}
    v = [x.get('vendor') for x in c['candidates'] if x.get('domain') == 'ahrefs.com']
    check(v == ['Ahrefs Web Analytics'], f'merge: a vendor data/vendors.json names is named even when the domain list lacks it (got {v})')


POLICY_PAGE = ('<h1>Privacy</h1>\n<p>We use Trackly to count visits.</p>\n'
               '<p>Visitors in the EEA and UK see a consent message before any analytics cookie is set.</p>\n')


def repo_with_policy(d: str) -> Path:
    import shutil
    root = Path(d) / 'clinic'
    shutil.copytree(REPO, root, ignore=shutil.ignore_patterns('stub-vendors'))
    (root / 'site' / 'privacy.html').write_text(POLICY_PAGE, encoding='utf-8')
    return root


def policy_finding(**kw) -> dict:
    f = {'id': 'F06', 'type': 'policy_mismatch', 'title': 'The privacy page promises a consent message, but the code loads analytics with no consent check',
         'source': 'agent', 'policy': {'file': 'site/privacy.html', 'line': 3, 'promise': 'consent_step',
                                       'excerpt': 'Visitors in the EEA and UK see a consent message before any analytics cookie is set.'},
         'basis': [{'file': 'site/index.html', 'line': 9, 'excerpt': '<script src="http://cdn.trackly.test:8153/t.js"></script>'}],
         'candidates': [], 'categories': [], 'fields': None, 'vendor': None, 'domain': None, 'recipient_kind': None, 'consent_states': [],
         'flow_basis': 'before_consent', 'reach': 3, 'reach_source': 'every page loads the analytics script', 'confidence': 'traced',
         'evidence': {'summary': 'Found by review: the policy promises a consent message; the page head loads the analytics script unconditionally.',
                      'code_path': [{'file': 'site/index.html', 'line': 9}]},
         'references': {r: vpa.PROV['types'].get('policy_mismatch', {}).get(r, []) for r in ('EU', 'AU')},
         'fix': 'Load the analytics script only after the visitor agrees, or correct the policy.',
         'verification': 'Run the live capture: no analytics request before a choice.'}
    f.update(kw)
    return f


def policy_mismatch() -> None:
    with tempfile.TemporaryDirectory() as d:
        repo = repo_with_policy(d)
        rep = base()
        rep['findings'].append(policy_finding())
        out = run_validator(rep, repo=repo)
        check(': valid (' in out, f'validator: a policy_mismatch finding with an in-repo policy basis may rate before consent ({out.strip()[-300:]})')
        rep = base()
        rep['findings'].append(policy_finding(policy=dict(policy_finding()['policy'], line=2)))
        check('F06: policy excerpt is not on site/privacy.html:2' in run_validator(rep, repo=repo),
              'validator: the policy basis is re-read with --repo')
        rep = base()
        f = policy_finding()
        f.pop('policy')
        check('F06: policy_mismatch needs a policy basis' in run_validator(rep | {'findings': rep['findings'] + [f]}, repo=repo),
              'validator: a policy_mismatch finding needs a policy basis')
        rep = base()
        rep['findings'].append(telemetry_finding(type='personal_data_to_third_party', default_on=None,
                                                 references={r: vpa.PROV['types']['personal_data_to_third_party'][r] for r in ('EU', 'AU')}))
        out = run_validator(rep, repo=repo)
        check('no candidate shows the before state' not in out and 'F06: flow_basis before_consent needs' in out,
              'validator: an unsupported before_consent says what would support it (not "no candidate shows the before state")')


def declared_page() -> None:
    run = SKILL / 'examples' / 'run'
    with tempfile.TemporaryDirectory() as d:
        repo = repo_with_policy(d)
        out = Path(d) / 'c.json'
        r = subprocess.run([sys.executable, str(HERE / 'merge_candidates.py'), '--capture', str(run / 'capture.json'), '--static',
                            str(run / 'static.json'), '--flows', str(SKILL / 'examples' / 'flows.json'), '--lists',
                            str(SKILL / 'examples' / 'lists.example.json'), '--declared', str(repo / 'site' / 'privacy.html'), '--out', str(out)],
                           capture_output=True, text=True, cwd=ROOT)
        c = json.loads(out.read_text(encoding='utf-8')) if out.is_file() else {'recipients': []}
    kinds = {x['domain']: (x['kind'], (x.get('declared_as') or {}).get('name')) for x in c['recipients']}
    check(kinds.get('trackly.test') == ('declared_processor', 'Trackly') and kinds.get('adnetic.test', ('',))[0] == 'third_party',
          f'merge: --declared takes an in-repo policy page and declares the vendors it names (got {kinds}){r.stderr[-200:]}')
    check('privacy.html' in str(c.get('declared') or ''), 'merge: the declared source names the policy page')


def categories_reproducible() -> None:
    rep = base()
    rep['findings'][4]['categories'] = ['contact', 'health']
    out = run_validator(rep)
    check('F05: categories' in out and 'categories_basis' in out,
          'validator: a category the candidates, fields and vendor do not give needs a categories_basis')
    rep = base()
    rep['findings'][4]['categories'] = ['contact', 'health']
    rep['findings'][4]['categories_basis'] = 'the welcome page also shows the appointment reason, which the redirect carries'
    check('categories_basis' not in run_validator(rep), 'validator: a stated categories_basis lets the agent set categories')
    full = json.loads((SKILL / 'examples' / 'privacy_audit.example.json').read_text(encoding='utf-8'))
    f4 = next(f for f in full['findings'] if f['id'] == 'F04')
    f4['categories'] = ['online_identifier']
    run = SKILL / 'examples' / 'run'
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'audit.json'
        p.write_text(json.dumps(full), encoding='utf-8')
        r = subprocess.run([sys.executable, str(HERE / 'validate_privacy_audit.py'), str(p), '--candidates', str(run / 'candidates.json'),
                            '--capture', str(run / 'capture.json'), '--canaries', str(run / 'canaries.json'), '--repo', str(REPO)],
                           capture_output=True, text=True, cwd=ROOT)
    check('F04: categories' in r.stdout and 'browsing_behaviour' in r.stdout,
          'validator: a tracking finding for a vendor of known category carries that category\'s data categories (or a categories_basis)')


# A weather-station logger: a Rust workspace (the product: few long files) with many short Python helper scripts in
# scripts/ and Python tests in tests/. Python has more files; Rust has far more lines of product code.
def rust_workspace(extra: dict | None = None) -> dict:
    body = ''.join(f'pub fn reading_{j}(raw: u32) -> f64 {{\n    let scaled = raw as f64 / 10.0;\n    scaled + {j}.0\n}}\n\n'
                   for j in range(60))
    files = {'Cargo.toml': '[workspace]\nmembers = ["station"]\n', 'station/Cargo.toml': '[package]\nname = "station"\nedition = "2024"\n'}
    files.update({f'station/src/m{i}.rs': body for i in range(3)})
    files.update({f'scripts/tool{i}.py': f'import json\n\n\ndef main():\n    print(json.dumps({{"n": {i}}}))\n' for i in range(9)})
    files.update({f'tests/test_tool{i}.py': f'import unittest\n\n\nclass T(unittest.TestCase):\n    def test_{i}(self):\n        self.assertTrue(True)\n'
                  for i in range(8)})
    files.update(extra or {})
    return files


def rust_dominant_by_lines() -> None:
    st, out = scan(rust_workspace())
    cov = st.get('language_coverage') or {}
    check(cov.get('status') == 'unsupported_dominant' and cov.get('needs_limitation') == ['Rust'] and cov.get('decided_by') == 'lines',
          f'scan: Rust product code with more lines than the Python scripts and tests is the dominant unanalysed language '
          f'(got {cov.get("status")}, {cov.get("needs_limitation")}, {cov.get("decided_by")})')
    check('WARNING' in out and 'Rust' in out, 'scan: the Rust-dominant repository is a printed warning')
    dist = {'web/dist/index.html': '<!doctype html>\n<html><body><h1>Station</h1><script src="app.js"></script></body></html>\n',
            'web/dist/app.js': 'document.querySelector("h1").textContent = "Readings";\n'}
    st, _ = scan(rust_workspace(dist))
    cov = st.get('language_coverage') or {}
    check(cov.get('status') == 'unsupported_dominant' and cov.get('decided_by') == 'lines',
          f'scan: a committed dist/ merged into the counts keeps the line rule (got {cov.get("status")}, {cov.get("decided_by")})')
    # the validator, given this scan's coverage, rejects a report that does not name Rust in a limitation
    cands = json.loads(CANDS.read_text(encoding='utf-8'))
    ex = json.loads((ROOT / cands['static']).read_text(encoding='utf-8'))
    ex['language_coverage'] = cov
    with tempfile.TemporaryDirectory() as d:
        sp = Path(d) / 'static.json'
        sp.write_text(json.dumps(ex), encoding='utf-8')
        cands['static'] = str(sp)
        out = run_validator(base(), cands=cands)
        check('does not analyse Rust' in out, 'validator: a report on a Rust-dominant repository with no Rust limitation fails')


# helper scripts that record the machine's host name and the OS account name, and write them into a results file
# committed with the repository (a scoreboard), plus a Rust runner reading the same from the environment
ID_SCRIPT = '''import getpass
import json
import os
import platform
import socket
from pathlib import Path

HOST = platform.node()


def manifest(out_path):
    host = socket.gethostname()
    user = os.environ.get("USERNAME") or os.environ["USER"]
    who = getpass.getuser()
    home = Path.home()
    machine = os.environ.get("COMPUTERNAME", "")
    data = {"host": host, "user": user}
    with open(out_path, "w") as fh:
        json.dump(data, fh)
    print(f"running on {host}")
    return who, home, machine
'''
ID_RUST = '''use std::env;

pub fn machine() -> String {
    env::var("COMPUTERNAME").or_else(|_| env::var("HOSTNAME")).unwrap_or_default()
}

pub fn account() -> Option<String> {
    let h = hostname::get().ok();
    let _ = h;
    whoami::fallible::username().ok()
}
'''


def host_and_account_names() -> None:
    st, _ = scan({'scripts/record_run.py': ID_SCRIPT, 'runner/Cargo.toml': '[package]\nname = "runner"\n', 'runner/src/lib.rs': ID_RUST})
    pts = [c for c in st['collection'] if c['file'] == 'scripts/record_run.py' and c['kind'] == 'machine_identifier']
    got = {c['field'] for c in pts}
    want = {'platform.node', 'socket.gethostname', 'USERNAME', 'USER', 'getpass.getuser', 'Path.home', 'COMPUTERNAME'}
    check(want <= got, f'scan: host-name and account-name reads in Python are collection points (missing {sorted(want - got)})')
    cats = {c['field']: c['category'] for c in pts}
    check(cats.get('socket.gethostname') == 'device_identifier' and cats.get('getpass.getuser') == 'identity',
          f'scan: a host name is a device identifier and an account name identity (got {cats})')
    rs = {c['field'] for c in st['collection'] if c['file'] == 'runner/src/lib.rs' and c['kind'] == 'machine_identifier'}
    check({'COMPUTERNAME', 'HOSTNAME', 'hostname::get', 'whoami::username'} <= rs,
          f'scan: Rust host-name and account-name reads are collection points (got {sorted(rs)})')
    art = [c for c in st['candidates'] if c['type'] == 'identifier_in_artifact']
    check(len(art) == 1 and art[0]['file'] == 'scripts/record_run.py' and art[0]['line'] == 19,
          f'scan: a host name written to a file is an identifier_in_artifact candidate (got {[(c["file"], c["line"]) for c in art]})')
    logs = [c for c in st['candidates'] if c['type'] == 'personal_data_in_logs' and c['file'] == 'scripts/record_run.py']
    check(len(logs) == 1 and logs[0]['line'] == 20, 'scan: printing a variable holding the host name is a personal-data-in-logs candidate')
    check(not [c for c in st['candidates'] if c['type'] == 'excess_collection'], 'scan: machine identifiers are never excess_collection')


INSTALL_PY = '''import json
import urllib.request

ARCHIVE_URL = "https://downloads.example-tools.io/pkg/tool-1.0.zip"


def main(index_path):
    urllib.request.urlretrieve(ARCHIVE_URL, "tool.zip")
    package = json.load(open(index_path))
    with urllib.request.urlopen(package["url"]) as resp:
        return resp.read()
'''


def outbound_constant() -> None:
    st, _ = scan({'scripts/install.py': INSTALL_PY})
    hosts = {(h['host'], h['line']) for h in st['outbound_hosts']}
    check(('downloads.example-tools.io', 8) in hosts,
          f'scan: urlretrieve with a module-level URL constant is an outbound host (got {sorted(hosts)})')
    unres = [(u['file'], u['line']) for u in st.get('outbound_unresolved') or []]
    check(('scripts/install.py', 10) in unres, f'scan: an HTTP call whose host is not a literal is listed as unresolved (got {unres})')


def subtitles_not_title() -> None:
    st, _ = scan({'report.html': '<input class="filter" placeholder="Subtitles, HDR, playlists">\n<input name="job_title">\n'
                                 '<input name="untitled_note">\n'})
    cats = {c['line']: c['category'] for c in st['collection']}
    check(cats.get(1) is None and cats.get(3) is None, f'scan: "title" inside "Subtitles" or "untitled" is not identity (got {cats})')
    check(cats.get(2) == 'identity', 'scan: job_title is still identity')


def regex_literal_not_sink() -> None:
    js = ('import assert from "node:assert";\n'
          'assert(!/fetch\\(|XMLHttpRequest|WebSocket/.test(script), "the report must not call out");\n'
          'const banned = ["fetch(", "sendBeacon("];\n'
          'const r = await fetch("https://api.example-sync.io/v1");\n')
    st, _ = scan({'scripts/check-report.mjs': js})
    lines = sorted(s['line'] for s in st['sinks'] if s['kind'] == 'http')
    check(lines == [4], f'scan: a regex or string literal naming fetch( is no HTTP sink; a real fetch is (got lines {lines})')


def minified_speed() -> None:
    parts = ['<!doctype html><html><head><meta charset="utf-8"></head><body><div id="app">']
    i = 0
    while sum(map(len, parts)) < 300_000:
        parts.append(f'<a href="https://docs{i % 50}.example-docs.io/p/{i}">entry {i}</a>'
                     f'<script>var u{i}="https://www.google-analytics.com/collect?v=1&i={i}";</script>'
                     f'<span data-src="//cdn{i % 7}.example-assets.io/img/{i}.png">consent {i}</span>')
        i += 1
    parts.append('</div></body></html>')
    html = ''.join(parts)
    with tempfile.TemporaryDirectory() as d:
        root, out = Path(d) / 'repo', Path(d) / 'static.json'
        write(root, {'docs/report.html': html})
        (root / 'docs' / 'report.html').read_bytes()   # let an on-access virus scanner see the new file before timing
        t0 = time.time()
        subprocess.run([sys.executable, str(HERE / 'scan_privacy.py'), str(root), '--out', str(out)], capture_output=True, text=True)
        secs = time.time() - t0
        st = json.loads(out.read_text(encoding='utf-8')) if out.is_file() else {}
    # the scan took over three minutes on such a file when every mention re-read the whole line; well under a second now
    check(secs < 6, f'scan: a 300 KB single-line HTML file is scanned in under 6 s (took {secs:.1f} s)')
    check(any(m['host'] == 'www.google-analytics.com' for m in st.get('host_mentions') or []), 'scan: hosts in the long line are still found')


def by_the_code_statement() -> None:
    rep = base()
    rep['headline']['statement'] = (rep['headline'].get('statement', '') + ' By the code, nothing is sent to an analytics or telemetry vendor.').strip()
    out = run_validator(rep)
    check('which only a browser run could show' not in out,
          f'validator: a not_run statement saying "by the code, nothing is sent" is accepted ({out.strip()[-200:]})')
    rep = base()
    rep['headline']['statement'] = (rep['headline'].get('statement', '') + ' Nothing is sent to an analytics vendor.').strip()
    check('which only a browser run could show' in run_validator(rep), 'validator: an unqualified "nothing is sent" in a not_run audit still fails')
    rep = base()
    rep['headline']['statement'] = (rep['headline'].get('statement', '') + ' By the code, the capture showed no trackers.').strip()
    check('which only a browser run could show' in run_validator(rep), 'validator: "the capture showed" fails whatever the qualifier')


def open_line(p: Path, n: int) -> str:
    return p.read_text(encoding='utf-8').splitlines()[n - 1].strip()


def artifact_finding() -> None:
    f = telemetry_finding(type='identifier_in_artifact', title='The run recorder writes the host name into a committed results file',
                          categories=['device_identifier'], flow_basis='committed', default_on=None, reach=3,
                          reach_source='the repository is public', fields=['host'],
                          basis=[{'file': 'serve.py', 'line': 1, 'excerpt': open_line(REPO / 'serve.py', 1)}],
                          artifacts=[{'file': 'serve.py', 'line': 2, 'what': 'host name'}],
                          references={r: vpa.PROV['types'].get('identifier_in_artifact', {}).get(r, []) for r in ('EU', 'AU')},
                          evidence={'summary': 'Found by review: the recorder writes the host name into a committed file.',
                                    'code_path': [{'file': 'serve.py', 'line': 1}]})
    f.pop('default_on')
    rep = base()
    rep['findings'].append(f)
    out = run_validator(rep)
    check(': valid (' in out, f'validator: an identifier_in_artifact finding with committed flow and an artifact location is valid ({out.strip()[-300:]})')
    rep = base()
    rep['findings'].append(dict(f, artifacts=[{'file': 'serve.py', 'line': 99999, 'what': 'host name'}]))
    check('F06: artifact serve.py:99999' in run_validator(rep), 'validator: an artifact location is re-read with --repo')
    rep = base()
    g = dict(f)
    g.pop('artifacts')
    rep['findings'].append(g)
    check('F06: identifier_in_artifact needs artifacts' in run_validator(rep), 'validator: identifier_in_artifact needs the artifact locations')
    rep = base()
    rep['findings'].append(telemetry_finding(flow_basis='committed'))
    check('F06: flow_basis committed' in run_validator(rep), 'validator: flow_basis committed is for identifier_in_artifact only')


def fetch_lists_checks_before_write() -> None:
    """A download whose hash differs from the pin is refused before anything reaches the cache."""
    import contextlib, io
    import fetch_lists
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d) / 'cache'
        saved_get, saved_argv = fetch_lists.get, sys.argv
        fetch_lists.get = lambda url, accept=None: b'qa-marker: not the pinned list\n'
        sys.argv = ['fetch_lists.py', '--cache', str(cache)]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fetch_lists.main()
        finally:
            fetch_lists.get, sys.argv = saved_get, saved_argv
        left = sorted(p.name for p in cache.rglob('*') if p.is_file()) if cache.exists() else []
        check(rc == 1 and not left, f'fetch_lists: a download whose sha256 differs from the pin is never written to the cache '
                                    f'(rc {rc}, cache holds {left})')


def main() -> int:
    for t in (fetch_lists_checks_before_write, rust_dominant_by_lines, host_and_account_names, outbound_constant, subtitles_not_title, regex_literal_not_sink,
              minified_speed, by_the_code_statement, artifact_finding, go_scan, unanalysed_language, template_loaders, fonts_not_tracker, undeclared_without_canary, telemetry_before_consent, declared_must_name, inventory_and_headline,
              observed_with_runtime_evidence, url_credentials_message, language_limitation, committed_dist, static_front_end_flows,
              agent_undeclared_third_party, console_and_key_canaries, next_script_loaders, csp_compared, vendors_added, policy_mismatch,
              declared_page, categories_reproducible):
        if sys.argv[1:] and t.__name__ not in sys.argv[1:]:  # python self_test.py <group> ... runs only those groups
            continue
        try:
            t()
        except Exception as exc:  # a crash in one group is a failure, not the end of the run
            check(False, f'{t.__name__} raised {exc.__class__.__name__}: {exc}')
    print(f'\n{len(FAILS)} failure(s)' if FAILS else '\nall checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
