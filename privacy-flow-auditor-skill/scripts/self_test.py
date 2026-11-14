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
import copy, json, re, subprocess, sys, tempfile
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
         'categories': ['online_identifier'], 'fields': None, 'vendor': None, 'domain': HOST, 'recipient_kind': 'third_party',
         'consent_states': [], 'flow_basis': 'before_consent', 'default_on': True, 'reach': 2,
         'reach_source': 'every run of the tool unless an opt-out variable is set', 'confidence': 'traced',
         'evidence': {'summary': 'Found by review: telemetry is on unless DO_NOT_TRACK is set.', 'code_path': [{'file': 'serve.py', 'line': 62}]},
         'references': {r: vpa.PROV['types'].get('telemetry', {}).get(r, []) for r in ('EU', 'AU')},
         'fix': 'Ask before the first send, and honour the opt-out for the update check too.',
         'verification': 'Point the telemetry endpoint at a loopback sink and run with no opt-out: nothing arrives before a choice.'}
    f.update(kw)
    return f


def run_validator(rep: dict, extra: list[str] | None = None, cands: dict | None = None, evidence: dict | None = None) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'audit.json'
        p.write_text(json.dumps(rep, indent=1), encoding='utf-8')
        cp = CANDS
        if cands is not None:
            cp = Path(d) / 'candidates.json'
            cp.write_text(json.dumps(cands), encoding='utf-8')
        args = [sys.executable, str(HERE / 'validate_privacy_audit.py'), str(p), '--candidates', str(cp), '--repo', str(REPO)]
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


def main() -> int:
    for t in (go_scan, unanalysed_language, template_loaders, fonts_not_tracker, undeclared_without_canary, telemetry_before_consent, declared_must_name, inventory_and_headline,
              observed_with_runtime_evidence, url_credentials_message, language_limitation):
        try:
            t()
        except Exception as exc:  # a crash in one group is a failure, not the end of the run
            check(False, f'{t.__name__} raised {exc.__class__.__name__}: {exc}')
    print(f'\n{len(FAILS)} failure(s)' if FAILS else '\nall checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
