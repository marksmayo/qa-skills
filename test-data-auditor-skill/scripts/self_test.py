#!/usr/bin/env python3
"""Self-test for the test data auditor's scanners and validator rules (stdlib only).

    python self_test.py

Builds small synthetic repositories in a temporary directory and checks what scan_data.py,
schema_drift.py, residue.py and the validator's computed fields say about them: Go test-code rules
(a fixed future date beside time.Now(), os.Setenv and os.Chdir outside t.Setenv and t.Chdir, a
package-level variable a test reassigns, math/rand without a seed), credentials in a URL's
user-info (a secret, never an email address), card numbers only in card-like digit groups,
stock placeholder passwords, the language coverage note, residue outside the tree, and the
severity of an agent finding that names a live endpoint, a product data tree of hundreds of templated
JSON files (listed when not inventoried, inventoried and scanned in seconds, sampled with a cap, the
pairwise duplicate check bounded), cwd-relative data paths in tests, and the leak check on an agent
personal-data finding's basis line, a served folder and a source tree named with --data-dir (site assets are not
fixtures, a records module is a data module, other code is recorded), no "unused" files without tests, the no-tests
statement, metadata.tests_found (schema 1.1), a runs file for run_tests.py, schema_drift.py with nothing to check,
--write-summary keeping line endings, and the example's baseline in examples/run/commands.sh pinned to the
audit date (it still passes with the real clock 90 days and ten years ahead). Every fixture value is invented; the
secret-looking strings are assembled at run time so none sits in this file whole.
"""
from __future__ import annotations
import json, sys, tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inventory as inv, residue, scan_data, schema_drift, validate_test_data_report as vtd  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(('ok    ' if cond else 'FAIL  ') + what)
    if not cond:
        FAILS.append(what)


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')


def scan(files: dict[str, str], today: str = '2026-11-11') -> dict:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, files)
        invd = inv.build(root, None, {})
        return scan_data.Scanner(root, invd, date.fromisoformat(today)).run()


def luhn_complete(prefix: str) -> str:
    """prefix plus the check digit that makes it Luhn-valid."""
    for c in '0123456789':
        if scan_data.luhn(prefix + c):
            return prefix + c
    raise AssertionError


GO_BILLING = '''package billing

import "time"

var nowFunc = time.Now
var rateLimit = 10
var (
\tretries = 3
)

func Charge() int { return rateLimit * retries }
'''

GO_BILLING_TEST = '''package billing

import (
\t"math/rand"
\t"os"
\t"testing"
\t"time"
)

func TestMain(m *testing.M) {
\tos.Setenv("BILLING_MODE", "test")
\tos.Exit(m.Run())
}

func TestTrialStillActive(t *testing.T) {
\tend := time.Date(2027, 3, 1, 0, 0, 0, 0, time.UTC)
\tif time.Now().After(end) {
\t\tt.Fatal("trial ended")
\t}
}

func TestOldStart(t *testing.T) {
\tstart := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
\tif time.Now().Before(start) {
\t\tt.Fatal("clock is wrong")
\t}
}

func TestFixedClock(t *testing.T) {
\tnow := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
\tend := time.Date(2027, 3, 1, 0, 0, 0, 0, time.UTC)
\tif now.After(end) {
\t\tt.Fatal("trial ended")
\t}
}

func TestEnvLeak(t *testing.T) {
\tos.Setenv("BILLING_REGION", "north")
\tdefer os.Unsetenv("BILLING_REGION")
}

func TestEnvSafe(t *testing.T) {
\tt.Setenv("BILLING_REGION", "south")
}

func TestWorkdir(t *testing.T) {
\tos.Chdir(t.TempDir())
}

func TestSwapClock(t *testing.T) {
\tnowFunc = func() time.Time { return time.Unix(0, 0) }
\t_ = Charge()
}

func TestSwapRestored(t *testing.T) {
\torig := rateLimit
\trateLimit = 5
\tt.Cleanup(func() { rateLimit = orig })
}

func swapAll(t *testing.T) {
\toldClock, oldLimit := nowFunc, rateLimit
\tt.Cleanup(func() {
\t\tnowFunc, rateLimit = oldClock, oldLimit
\t})
\t_ = Charge()
\t_ = Charge()
\tnowFunc, rateLimit = time.Now, 1
}

func TestHelperRestores(t *testing.T) {
\tswapAll(t)
\tnowFunc = time.Now
}

func shortRetries(t *testing.T) {
\tfor _, v := range []*int{&retries} {
\t\torig := *v
\t\tt.Cleanup(func() { *v = orig })
\t}
\tretries = 1
}

func TestLocalShadow(t *testing.T) {
\tretries := 9
\tretries = 10
\t_ = retries
}

func TestRandomAmount(t *testing.T) {
\tn := rand.Intn(100)
\t_ = n
}
'''

GO_SEEDED_TEST = '''package ledger

import (
\t"math/rand"
\t"testing"
)

func TestSeeded(t *testing.T) {
\tr := rand.New(rand.NewSource(7))
\t_ = r.Intn(10)
}
'''


def go_rules() -> None:
    res = scan({'go.mod': 'module example.test/shop\n\ngo 1.24\n', 'billing/billing.go': GO_BILLING,
                'billing/billing_test.go': GO_BILLING_TEST, 'ledger/ledger_test.go': GO_SEEDED_TEST,
                'ledger/ledger.go': 'package ledger\n'})
    cands = res['candidates']
    lines = GO_BILLING_TEST.split('\n')
    at = lambda text, start=0: next(i for i, ln in enumerate(lines, 1) if i > start and text in ln)  # noqa: E731
    bombs = [c for c in cands if c['category'] == 'time_bomb' and c['file'] == 'billing/billing_test.go']
    trial = at('time.Date(2027', 0)
    check(any(c['line'] == trial and c['rule'] == 'future_literal_with_now' for c in bombs),
          'go: a fixed future time.Date in a test that reads time.Now() is a time bomb')
    check(not any(c['line'] == at('time.Date(2020') for c in bombs), 'go: a fixed past date is not a time bomb')
    check(not any(c['line'] == at('time.Date(2027', trial) for c in bombs),
          'go: a test that never reads time.Now() (an injected clock) has no time bomb')
    check(any(c['line'] == trial and 'billing/billing_test.go::TestTrialStillActive' in c['affected_tests'] for c in bombs),
          'go: the time bomb names its test function')
    shared = [c for c in cands if c['category'] == 'shared_mutable']
    env = [c for c in shared if c['rule'] == 'go_os_setenv']
    check(any(c['line'] == at('os.Setenv("BILLING_REGION"') for c in env), 'go: os.Setenv in a test is shared process state')
    check(not any(c['line'] == at('os.Setenv("BILLING_MODE"') for c in env), 'go: os.Setenv in TestMain is package setup, not flagged')
    check(len(env) == 1, f'go: one os.Setenv candidate per test function (got {len(env)})')
    check(any(c['rule'] == 'go_os_chdir' and c['line'] == at('os.Chdir(') for c in shared), 'go: os.Chdir in a test is flagged')
    pv = [c for c in shared if c['rule'] == 'go_package_var_reassigned']
    check(any(c['field'] == 'nowFunc' and c['line'] == at('nowFunc = ') for c in pv),
          'go: a package-level var reassigned in a test with no restore is flagged')
    check(not any(c['field'] == 'rateLimit' for c in pv), 'go: a package-level var restored with t.Cleanup is not flagged')
    check(not any(c['field'] == 'retries' and c['line'] == at('retries = 10') for c in pv),
          'go: a local variable that shadows a package var is not flagged')
    check(not any(c['details'].get('function') == 'swapAll' for c in pv),
          'go: a multi-assignment restored in a t.Cleanup closure (x, y = oldX, oldY) is not flagged')
    check(not any(c['details'].get('function') == 'shortRetries' for c in pv),
          'go: a variable restored through a pointer in t.Cleanup (for _, v := range []*int{&x}) is not flagged')
    check(not any(c['details'].get('function') == 'TestHelperRestores' for c in pv),
          'go: a swap after a helper whose t.Cleanup restores the variable is not flagged')
    rnd = [c for c in cands if c['category'] == 'nondeterministic' and c['rule'] == 'unseeded_random']
    check(any(c['file'] == 'billing/billing_test.go' and c['line'] == at('rand.Intn(') for c in rnd),
          'go: math/rand with no seeded source in the file is unseeded randomness')
    check(not any(c['file'] == 'ledger/ledger_test.go' for c in rnd), 'go: rand.New(rand.NewSource(n)) is seeded')
    cov = res.get('language_coverage') or {}
    check('go' in (cov.get('code_rules') or []), 'scan: language_coverage lists Go as having code rules')


def url_credentials_and_cards() -> None:
    secret_url = 'https://alice:' + 'alpha7' + 'bravo9' + '@hub.cloudgrid.io/wd/hub'
    placeholder_url = 'https://user:key@ondemand.gridcloud.io/wd/hub'
    env_url = 'https://${GRID_USER}:${GRID_KEY}@hub.gridcloud.io/wd/hub'
    swipe_digits = luhn_complete('540156054084030')             # a Luhn-valid run, spaced like screen coordinates
    swipe = f'input swipe {swipe_digits[:3]} {swipe_digits[3:7]} {swipe_digits[7:10]} {swipe_digits[10:13]} {swipe_digits[13:]}'
    ps_digits = luhn_complete('123456712345678')
    ps_line = f'u0_a12 {ps_digits[:4]} {ps_digits[4:8]} {ps_digits[8:12]} {ps_digits[12:14]} {ps_digits[14:]} S app'
    grouped = '4000 0566 5566 5556'                              # a published test card, grouped 4-4-4-4
    plain = luhn_complete('453201511283036')
    amex = '3782 822463 10005'                                   # a published test card, grouped 4-6-5
    src = (f'package grid\n\nimport "testing"\n\nconst hub = "{secret_url}"\nconst demo = "{placeholder_url}"\n'
           f'const fromEnv = "{env_url}"\n\nfunc TestCoords(t *testing.T) {{\n\t_ = "{swipe}"\n\t_ = "{ps_line}"\n}}\n\n'
           f'func TestCards(t *testing.T) {{\n\t_ = "{grouped}"\n\t_ = "{plain}"\n\t_ = "{amex}"\n}}\n')
    res = scan({'go.mod': 'module example.test/grid\n', 'grid/grid.go': 'package grid\n', 'grid/hub_test.go': src})
    cands = [c for c in res['candidates'] if c['file'] == 'grid/hub_test.go']
    line_of = lambda text: next(i for i, ln in enumerate(src.split('\n'), 1) if text in ln)  # noqa: E731
    emails = [c for c in cands if c['rule'] == 'email']
    check(not emails, f'url user-info: user:password@host is never an email address (got {len(emails)} email candidate(s))')
    creds = {c['line']: c for c in cands if c['category'] == 'secret' and c['rule'] == 'url_credentials'}
    check(line_of('const hub') in creds and not creds[line_of('const hub')]['auto_dismiss'],
          'url user-info: a password in a URL is a url_credentials secret')
    check(line_of('const demo') in creds and bool(creds[line_of('const demo')]['auto_dismiss']),
          'url user-info: user:key placeholders are a dismissed url_credentials candidate')
    check(line_of('const fromEnv') not in creds or bool(creds[line_of('const fromEnv')]['auto_dismiss']),
          'url user-info: ${VAR} interpolation is a placeholder, not a credential')
    cards = {c['line'] for c in cands if c['rule'] == 'card'}
    check(line_of('input swipe') not in cards, 'card: space-separated screen coordinates are not a card number')
    check(line_of('u0_a12') not in cards, 'card: process-listing columns are not a card number')
    check(line_of(grouped) in cards, 'card: a 4-4-4-4 grouped Luhn-valid number is still a card candidate')
    check(line_of(plain) in cards, 'card: an ungrouped 16-digit Luhn-valid number is still a card candidate')
    check(line_of(amex) in cards, 'card: a 4-6-5 grouped Luhn-valid number is still a card candidate')


def placeholders() -> None:
    word = 'hunter' + '2'
    src = f'package auth\n\nimport "testing"\n\nfunc TestLogin(t *testing.T) {{\n\tcfg := map[string]string{{"password": "{word}-and-more"}}\n\t_ = cfg\n}}\n'
    res = scan({'go.mod': 'module example.test/auth\n', 'auth/auth.go': 'package auth\n', 'auth/login_test.go': src})
    sec = [c for c in res['candidates'] if c['category'] == 'secret']
    check(bool(sec) and all(c['auto_dismiss'] for c in sec), 'placeholder: the stock example password is a dismissed placeholder')


def coverage_and_drift() -> None:
    files = {'Cargo.toml': '[package]\nname = "shelf"\n', 'src/lib.rs': 'pub fn add(a: i32, b: i32) -> i32 { a + b }\n',
             'src/shelf.rs': 'pub struct Shelf;\n', 'tests/add.rs': '#[test]\nfn adds() { assert_eq!(shelf::add(1, 2), 3); }\n',
             'tests/fixtures/books.json': '[{"title": "A", "isbn": "1"}, {"title": "B", "isbn": "2"}]\n'}
    res = scan(files)
    cov = res.get('language_coverage') or {}
    check('rust' in (cov.get('text_only') or {}), 'scan: a Rust test file is listed as scanned as text only (no code rules)')
    check(any('rust' in n.lower() for n in res.get('notes', [])), 'scan: the notes say Rust test code had no code rules')
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'go.mod': 'module example.test/shelf\n', 'shelf.go': 'package shelf\n\ntype Book struct {\n\tTitle string `json:"title"`\n}\n',
                     'store.go': 'package shelf\n', 'testdata/books.json': '[{"title": "A", "isbn": "1"}]\n',
                     'shelf_test.go': 'package shelf\n'})
        out = schema_drift.drift(root, inv.build(root, None, {}))
    lc = out.get('language_coverage') or {}
    check(lc.get('status') == 'unsupported_dominant' and lc.get('dominant') == 'Go' and bool(lc.get('warning')),
          'schema_drift: warns (from the shared language census) when it reads no models of the repo\'s language (Go)')


def residue_outside_tree() -> None:
    with tempfile.TemporaryDirectory() as d:
        root, home = Path(d) / 'repo', Path(d) / 'home'
        write(root, {'a.txt': 'a\n'})
        write(home, {'.config/keep.txt': 'k\n'})
        before = residue.snapshot_all(root, {'home': home})
        write(home, {'.toolcache/browsers/x.bin': 'x\n'})
        res = residue.diff(before, residue.snapshot_all(root, {'home': home}))
    check('home:.toolcache/browsers/x.bin' in res['added'], 'residue: a file written under an --also directory (an isolated HOME) is listed')


def live_endpoint_severity() -> None:
    f = {'id': 'F1', 'category': 'environment_coupling', 'source': 'agent', 'candidates': [], 'confidence': 'static',
         'host': 'updates.toolmaker.example', 'live_endpoint': True, 'location': {'file': 'cli/main_test.go', 'line': 3},
         'basis': [{'evidence': 'proxy.log'}], 'affected_tests': ['cli/main_test.go::*']}
    check(vtd.severity(f, {}, date(2026, 11, 11)) == 'serious', 'validator: an agent finding with a live endpoint is Serious')
    with tempfile.TemporaryDirectory() as d:
        ev = Path(d)
        (ev / 'proxy.log').write_text('CONNECT other.example:443\n', encoding='utf-8')
        errs = vtd.live_host_errors(f, ev)
        check(any('host' in e for e in errs), 'validator: live_endpoint fails when no basis names the host')
        (ev / 'proxy.log').write_text('CONNECT updates.toolmaker.example:443\n', encoding='utf-8')
        check(vtd.live_host_errors(f, ev) == [], 'validator: live_endpoint passes when the evidence names the host')
        tool = dict(f, source='tool', basis=[])
        check(any('agent' in e for e in vtd.live_host_errors(tool, ev)), 'validator: live_endpoint is for agent findings with a basis')


N_PLACES = 400
PLACE_EMAIL_LINE = 4


def place_mailbox() -> str:
    # an invented mailbox at an invented domain, assembled at run time
    return 'pat.lowmoor' + '@' + 'quaymail' + '.co'


def places_repo(root: Path) -> None:
    """A product data directory of N_PLACES small templated JSON files that a parametrised test suite globs, as a
    timetable or catalogue site keeps one file per record. One record carries a contact address."""
    files = {
        'pytest.ini': '[pytest]\naddopts = -v\ntestpaths = tests\npython_files = test_*.py\n',
        'app.py': ('import json\nfrom pathlib import Path\nPLACES_DIR = Path(__file__).parent / "places"\n\n\n'
                   'def load_places():\n    return {p.stem: json.loads(p.read_text()) for p in PLACES_DIR.glob("*.json")}\n'),
        'tests/conftest.py': ('import json\nimport pytest\nfrom pathlib import Path\n\n'
                              'PLACES_DIR = Path(__file__).parent.parent / "places"\n\n\n@pytest.fixture\ndef places():\n'
                              '    return {p.stem: json.loads(p.read_text()) for p in PLACES_DIR.glob("*.json")}\n'),
        'tests/test_places.py': (
            'import json\nimport pytest\nfrom pathlib import Path\n\n'
            'PLACES_DIR = Path(__file__).parent.parent / "places"\nFILES = sorted(PLACES_DIR.glob("*.json"))\n\n\n'
            '@pytest.mark.parametrize("path", FILES)\ndef test_has_code(path):\n    assert json.loads(path.read_text())["code"]\n\n\n'
            '@pytest.mark.parametrize("path", FILES)\ndef test_has_name(path):\n    assert json.loads(path.read_text())["name"]\n'),
    }
    for k in range(40):        # page tests that import the app (which loads the tree) or request the fixture
        files[f'tests/test_page{k:02d}.py'] = 'from app import load_places\n\n\n' + ''.join(
            f'def test_page{k:02d}_{j}(places):\n    assert load_places()["p{j:03d}"] == places["p{j:03d}"]\n\n\n'
            for j in range(15))
    for i in range(N_PLACES):
        contact = place_mailbox() if i == 123 else ''
        files[f'places/p{i:03d}.json'] = (
            '{\n  "code": "P%03d",\n  "name": "Place %d",\n  "contact": "%s",\n  "lat": %d.25,\n  "lon": %d.5,\n'
            '  "pier": %d,\n  "notes": "Ferry stop on the harbour loop, timetable from the shared template."\n}\n'
            % (i, i, contact, i % 80, i % 170, i % 9))
    write(root, files)


def big_data_tree() -> None:
    import time
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        places_repo(root)

        # without --data-dir the tree is not inventoried, but it is reported, never silently skipped
        invd = inv.build(root, None, {})
        un = {u['dir']: u for u in invd.get('unlisted_data_dirs', [])}
        check('places' in un and un['places'].get('files') == N_PLACES and 'tests/test_places.py' in un['places'].get('named_in', []),
              'inventory: a product data tree the tests glob, but not inventoried, is listed under unlisted_data_dirs '
              f'(got {sorted(un)})')

        # with --data-dir the whole tree is inventoried quickly, every file mapped to the tests that glob it (timed warm: the
        # first read of freshly written files is slow on hosts with an on-access virus scanner, which is not the walker's cost).
        # The budgets are CPU time of this process (time.process_time), not wall-clock time: a loaded machine stretches the
        # wall clock several-fold, but a walker that turns quadratic (the per-node string cache lost, say) still blows them.
        for p in sorted((root / 'places').iterdir()):
            p.read_bytes()
        t0 = time.process_time()
        invd = inv.build(root, None, {}, ['places'])
        took = time.process_time() - t0
        places = [i for i in invd['inventory'] if i['path'].startswith('places/')]
        check(len(places) == N_PLACES, f'inventory: --data-dir lists all {N_PLACES} data files (got {len(places)})')
        check(took < 2.0, f'inventory: {N_PLACES} templated data files inventory in under 2 s of CPU time (took {took:.1f} s)')
        check(bool(places) and all('tests/test_places.py::test_has_code' in i['loaded_by'] for i in places),
              'inventory: each data file is loaded by the parametrised tests that glob its directory')

        # the scan finds the address embedded in one record, and the pairwise duplicate check stays bounded
        t0 = time.process_time()
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
        took = time.process_time() - t0
        emails = [c for c in res['candidates'] if c['rule'] == 'email' and not c['auto_dismiss']]
        check(any(c['file'] == 'places/p123.json' and c['line'] == PLACE_EMAIL_LINE for c in emails),
              'scan: an email address inside a record of a --data-dir JSON tree is a personal-data candidate')
        check(took < 20.0, f'scan: {N_PLACES} templated data files scan in under 20 s of CPU time (took {took:.1f} s)')
        check(any('near-duplicate' in n and 'places' in n for n in res['notes']),
              'scan: a directory too large for the pairwise near-duplicate check is named in the notes as not compared')

        # a cap samples the tree: every file listed, the ones over the cap not_assessed with the reason
        invd = inv.build(root, None, {}, ['places'], max_files_per_dir=50)
        places = [i for i in invd['inventory'] if i['path'].startswith('places/')]
        scanned = [i for i in places if i['status'] == 'scanned']
        skipped = [i for i in places if i['status'] == 'not_assessed']
        check(len(places) == N_PLACES and len(scanned) == 50 and len(skipped) == N_PLACES - 50
              and all('not sampled' in (i['reason'] or '') for i in skipped),
              f'inventory: --max-files-per-dir 50 scans 50 of {N_PLACES} files and records the rest as not sampled '
              f'(got {len(scanned)} scanned, {len(skipped)} not assessed)')
        check((invd.get('sampled_dirs') or {}).get('places', {}).get('files') == N_PLACES,
              'inventory: sampled_dirs records the directory and its file count')


def cwd_relative_paths() -> None:
    res = scan({
        'places/p001.json': '{"code": "P001"}\n',
        'app.py': 'x = 1\n',
        'tests/test_sitemap.py': ('import os\nfrom pathlib import Path\n\n\ndef test_every_place_listed():\n'
                                  '    names = os.listdir("places")\n    assert names\n\n\n'
                                  'def test_one_place():\n    with open("places/p001.json") as fh:\n        assert fh.read()\n\n\n'
                                  'def test_anchored():\n    base = Path(__file__).parent.parent / "places"\n'
                                  '    assert (base / "p001.json").exists()\n'),
        'tests/test_chdir.py': ('import os\n\n\ndef test_in_root(monkeypatch, tmp_path):\n    monkeypatch.chdir(tmp_path)\n'
                                '    assert not os.path.exists("places")\n'),
    })
    rel = [c for c in res['candidates'] if c['rule'] == 'cwd_relative_path']
    lines = {(c['file'], c['line']) for c in rel}
    check(('tests/test_sitemap.py', 6) in lines and ('tests/test_sitemap.py', 11) in lines,
          f'scan: a test opening a repo directory by a cwd-relative path is environment coupling (got {sorted(lines)})')
    check(not any(f == 'tests/test_chdir.py' for f, _ in lines) and ('tests/test_sitemap.py', 15) not in lines,
          'scan: a path anchored on __file__, or a test that changes directory itself, is not flagged')


def agent_personal_data_leak() -> None:
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        addr = place_mailbox()
        write(repo, {'places/p123.json': '{\n  "code": "P123",\n  "contact": "%s"\n}\n' % addr})
        f = {'id': 'F2', 'category': 'personal_data', 'source': 'agent', 'candidates': [],
             'location': {'file': 'places/p123.json', 'line': 3},
             'basis': [{'file': 'places/p123.json', 'line': 3, 'excerpt': '"contact": <a mailbox>'}],
             'why_flagged': f'A real-looking address {addr} sits in a record every per-place test loads.'}
        per = vtd.protected_values({}, repo, {'findings': [f], 'dismissed': []})
        check(addr in per.get('F2', set()),
              'validator: the values on an agent personal_data finding\'s basis line are protected like a candidate\'s')
        check(bool(vtd.leaks(per.get('F2', set()), f['why_flagged'])),
              'validator: an agent personal_data finding that pastes the address into why_flagged leaks it')


# a repository with no tests at all: a small web service (a FastAPI-style app serving a committed static front end) whose
# product code holds a canned profile; nothing in it is test data
NO_TESTS_REPO = {'server.py': 'from pathlib import Path\n\nPROFILE = {"name": "Sample Person", "plan": "free"}\n\n\ndef home():\n'
                              '    return Path("static/index.html").read_text()\n',
                 'static/index.html': '<!doctype html><title>Sample</title><script src="app.js"></script>\n',
                 'static/app.js': 'console.log("ready");\n'}


def empty_repository() -> None:
    """A repository with no tests: the tools say so, the evidence share for the inventory is 0, and the report must say so."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, NO_TESTS_REPO)
        invd = inv.build(root, None, {})
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    check(invd.get('no_tests') is True and any('no test files' in w for w in invd.get('warnings', [])),
          'inventory: a repository with no test files says so (no_tests, a warning)')
    check(any('no test files' in n for n in res.get('notes', [])), 'scan: the notes repeat that the repository has no test files')
    check(res.get('tests_found') == 0, 'scan: candidates record tests_found 0')
    check(any('code-security-auditor' in n and 'privacy-flow-auditor' in n for n in res.get('notes', [])),
          'scan: the notes say secrets and personal data in product code are out of scope, and which skills cover them')
    rep = {'inventory': [], 'findings': [], 'tools': [{'name': 'scan_data', 'status': 'ran'}]}
    check(vtd.completeness(rep) == 0, f'validator: an empty inventory is not complete evidence (got {vtd.completeness(rep)})')
    ex = json.loads((HERE.parent / 'examples' / 'test_data_report.example.json').read_text(encoding='utf-8'))
    ex['inventory'], ex['findings'], ex['dismissed'], ex['handoffs'] = [], [], [], []
    ex['limitations'] = ['Static rules only.']
    ex['summary']['headline'] = 'Nothing was flagged.'
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc={'tests_found': 0, 'candidates': []})
    check(any('no tests' in e for e in errs), 'validator: a report on a repository with no tests must say so in the headline or a limitation')
    ex['limitations'].append('The repository has no tests: no test files, runner configuration or test directories were found.')
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc={'tests_found': 0, 'candidates': []})
    check(not any('no tests' in e for e in errs), 'validator: a limitation that says the repository has no tests satisfies it')


def data_dir_unknown_format() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'saves/slot1.sav': 'x\n', 'saves/notes.json': '{"a": 1}\n', 'tests/test_x.py': 'def test_x():\n    assert True\n'})
        invd = inv.build(root, None, {}, ['saves'])
    item = next((i for i in invd['inventory'] if i['path'] == 'saves/slot1.sav'), None)
    check(item is not None and item['status'] == 'not_assessed' and 'unknown format' in (item.get('reason') or ''),
          'inventory: a file of an unknown format under --data-dir is listed as not_assessed (unknown format), never dropped')


# a small static site with no tests: a web app whose served folder holds the usual crawler and install files
SERVED_SITE = {'package.json': '{"name": "birding-notes", "private": true}\n',
               'app/page.tsx': 'export default function Home() { return <main>Sightings</main>; }\n',
               'public/ads.txt': 'placeholder.example, pub-0000, DIRECT\n',
               'public/manifest.json': '{"name": "Birding notes", "start_url": "/"}\n',
               'public/robots.txt': 'User-agent: *\nAllow: /\n',
               'samples/sightings.json': '[{"species": "wren", "count": 2}]\n'}


def served_data_dir() -> None:
    """--data-dir naming a served asset folder (public/, static/) does not turn the site's assets into test fixtures, and
    with no tests no file is called unused."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, SERVED_SITE)
        invd = inv.build(root, None, {}, ['public', 'samples'])
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
        served = [i for i in invd['inventory'] if i['path'].startswith('public/')]
        check(not served, f'inventory: files in a served folder named with --data-dir are not test fixtures (got {[i["path"] for i in served]})')
        sd = (invd.get('served_dirs') or {}).get('public') or {}
        check(sd.get('files') == 3 and any('served' in w and 'public' in w for w in invd.get('warnings', [])),
              'inventory: a served --data-dir is recorded under served_dirs with its file count, and warned about')
        unused = [c for c in res['candidates'] if c['rule'] == 'unused']
        check(not unused, f'scan: with no tests no file is "unused" (got {[c["file"] for c in unused]})')
        check(any('unused' in n and 'no tests' in n for n in res['notes']), 'scan: the notes say the unused-file check needs tests')
        # a file in a served folder that a test reads is still test data
        write(root, {'tests/test_feed.py': 'from pathlib import Path\n\n\ndef test_manifest():\n'
                                           '    assert Path("public/manifest.json").read_text()\n'})
        invd = inv.build(root, None, {}, ['public'])
        kept = {i['path']: i for i in invd['inventory'] if i['path'].startswith('public/')}
        check(list(kept) == ['public/manifest.json'] and bool(kept['public/manifest.json']['loaded_by']),
              f'inventory: a served file a test loads stays listed with its tests (got {sorted(kept)})')
        sd = (invd.get('served_dirs') or {}).get('public') or {}
        check(sd.get('files') == 3 and sd.get('loaded_by_tests') == 1,
              'inventory: served_dirs counts the files and the ones tests load')


BIRDS_TS = ('export type Sighting = { species: string; observer: string; contact: string; count: number };\n\n'
            'export const sightings: Sighting[] = [\n'
            '  { species: "wren", observer: "Field team", contact: "", count: 2 },\n'
            '  { species: "heron", observer: "Field team", contact: "%s", count: 1 },\n'
            '  { species: "kite", observer: "Field team", contact: "", count: 4 },\n'
            '];\n')
CARD_TSX = ('import { sightings } from "../data/birds";\n\nexport function Card() {\n'
            '  const style = { color: "green", margin: 4 };\n  return <ul style={style}>{sightings.map(s => <li>{s.species}</li>)}</ul>;\n}\n')


def data_dir_code() -> None:
    """--data-dir over a source tree: a module of records written as code is listed and scanned as data; other code is
    not silently dropped but recorded (data_dir_code) and named in the notes."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'package.json': '{"name": "birding-notes"}\n', 'src/data/birds.ts': BIRDS_TS % place_mailbox(),
                     'src/components/Card.tsx': CARD_TSX, 'src/lib/fmt.py': 'def fmt(n):\n    return f"{n:,}"\n'})
        invd = inv.build(root, None, {}, ['src'])
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    item = next((i for i in invd['inventory'] if i['path'] == 'src/data/birds.ts'), None)
    check(item is not None and item['status'] == 'scanned' and 'data module' in (item.get('note') or ''),
          'inventory: a module exporting a records array under --data-dir is listed as a data module and scanned')
    check(not any(i['path'] in ('src/components/Card.tsx', 'src/lib/fmt.py') for i in invd['inventory']),
          'inventory: ordinary code under --data-dir is not listed as test data')
    dc = (invd.get('data_dir_code') or {}).get('src') or {}
    check(dc.get('files') == 2 and 'src/components/Card.tsx' in dc.get('paths', []),
          f'inventory: code under --data-dir that is not a data module is recorded under data_dir_code (got {dc})')
    check(any('src/' in w and 'not read as test data' in w for w in invd.get('warnings', [])),
          'inventory: a warning names the code files under --data-dir that were not read as data')
    check(any('src/' in n and 'not read as test data' in n for n in res['notes']),
          'scan: the notes say the code under --data-dir was not read as test data')
    check(any(c['file'] == 'src/data/birds.ts' and c['rule'] == 'email' and not c['auto_dismiss'] for c in res['candidates']),
          'scan: an address in a record of a data module under --data-dir is a personal-data candidate')


def report_with(**changes) -> dict:
    ex = json.loads((HERE.parent / 'examples' / 'test_data_report.example.json').read_text(encoding='utf-8'))
    ex['inventory'], ex['findings'], ex['dismissed'], ex['handoffs'] = [], [], [], []
    ex['tools'] = [{'name': 'inventory.py, scan_data.py', 'status': 'ran', 'reason': 'stdlib static rules'}]
    ex['metadata']['runs_file'] = None
    ex['limitations'] = ['Static rules only.']
    ex['summary']['headline'] = 'Nothing was flagged.'
    for k, v in changes.items():
        ex[k] = v
    return ex


def no_tests_statement() -> None:
    """The no-tests statement must say the repository has no tests: "no test data" or "without tests failing" is not it."""
    nt = {'tests_found': 0, 'candidates': []}
    for text in ('No test data was found.', 'Nothing was flagged without tests failing.', 'No tests failed.',
                 'Inline literals without tests attached were skipped.'):
        ex = report_with(limitations=[text])
        errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc=nt)
        check(any('no tests' in e for e in errs), f'validator: "{text}" is not a statement that the repository has no tests')
    for text in ('The repository has no tests.', 'There are no tests in this repository.', 'No test files were found: the repo has no tests.',
                 'No tests were found.'):
        ex = report_with(limitations=[text])
        errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc=nt)
        check(not any('no tests' in e for e in errs), f'validator: "{text}" says the repository has no tests')


def tests_found_field() -> None:
    """Schema 1.1 records metadata.tests_found; the validator checks it against the candidates, and the renderer says
    "no tests" when it is 0. A report without schema_version (1.0) still validates."""
    schema = json.loads(vtd.SCHEMA.read_text(encoding='utf-8'))
    ex = report_with(limitations=['The repository has no tests.'])
    check(not vtd.schema_errors(ex, schema, schema), 'schema: a 1.0 report (no schema_version, no tests_found) is still valid')
    ex['schema_version'] = '1.1'
    ex['metadata']['tests_found'] = 0
    check(not vtd.schema_errors(ex, schema, schema), 'schema: a 1.1 report with metadata.tests_found is valid')
    del ex['metadata']['tests_found']
    errs = vtd.schema_errors(ex, schema, schema) + vtd.check(ex, None, None, None, None, json.dumps(ex), None, None,
                                                             cand_doc={'tests_found': 0, 'candidates': []})
    check(any('tests_found' in e for e in errs), 'validator: a 1.1 report must record metadata.tests_found')
    ex['metadata']['tests_found'] = 7
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc={'tests_found': 0, 'candidates': []})
    check(any('tests_found' in e for e in errs), 'validator: metadata.tests_found must equal the candidates\' tests_found')
    ex['metadata']['tests_found'] = 0
    ex['limitations'] = ['Static rules only.']
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc=None)
    check(any('no tests' in e for e in errs), 'validator: tests_found 0 in the report alone requires the no-tests statement')
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'r.json'
        ex['limitations'] = ['The repository has no tests.']
        p.write_text(json.dumps(ex), encoding='utf-8')
        out = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(p), '--color', 'never'],
                             capture_output=True, text=True).stdout
    check('no tests' in out.split('\n\n')[0].lower(), 'renderer: the header says the repository has no tests when tests_found is 0')


def runs_required() -> None:
    """A report whose run_tests.py tool ran must name its runs file, and the runs must be passed to the validator."""
    ex = json.loads((HERE.parent / 'examples' / 'test_data_report.example.json').read_text(encoding='utf-8'))
    ex['metadata']['runs_file'] = None
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None)
    check(any('runs_file' in e and 'run_tests' in e for e in errs), 'validator: run_tests.py ran but metadata.runs_file is null')
    ex = json.loads((HERE.parent / 'examples' / 'test_data_report.example.json').read_text(encoding='utf-8'))
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None)
    check(any('--runs' in e and 'run_tests' in e for e in errs), 'validator: run_tests.py ran, so the runs file must be passed with --runs')
    errs = vtd.check(ex, None, {'runs': [], 'cases': []}, None, None, json.dumps(ex), None, None)
    check(any('no runs' in e for e in errs), 'validator: run_tests.py ran but the runs file holds no runs')


def drift_nothing_to_check() -> None:
    """schema_drift.py with no data records to compare is quick and quiet; an unread dominant language with records to
    compare exits 3, like the other skills' scanners."""
    import time
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        files = {f'app/section{i}/page.tsx': 'export default function P() { return null; }\n' for i in range(60)}
        files.update(SERVED_SITE)
        del files['samples/sightings.json']
        write(root, files)
        invd = inv.build(root, None, {})
        t0 = time.process_time()
        out = schema_drift.drift(root, invd)
        took = time.process_time() - t0
        lc = out.get('language_coverage') or {}
        check(out.get('status') == 'nothing_to_check' and not lc.get('warning'),
              f'schema_drift: an inventory with no data records is nothing_to_check, with no language warning '
              f'(got {out.get("status")}, {lc.get("warning")})')
        check(took < 0.5, f'schema_drift: nothing to check returns at once (took {took:.2f} s of CPU time)')
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
        check(not any(n.startswith('schema drift:') for n in res['notes']), 'scan: no schema-drift language warning when there was nothing to compare')
        ip = Path(d) / 'inv.json'
        ip.write_text(json.dumps(invd), encoding='utf-8')
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = schema_drift.main([str(root), '--inventory', str(ip), '--out', str(Path(d) / 'o.json')])
        check(code == 0 and 'WARNING' not in err.getvalue(), 'schema_drift: nothing to check exits 0 with no warning')
        root2 = Path(d) / 'go'
        write(root2, {'go.mod': 'module example.test/shelf\n', 'shelf.go': 'package shelf\n', 'store.go': 'package shelf\n',
                      'testdata/books.json': '[{"title": "A", "isbn": "1"}]\n', 'shelf_test.go': 'package shelf\n'})
        ip.write_text(json.dumps(inv.build(root2, None, {})), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = schema_drift.main([str(root2), '--inventory', str(ip), '--out', str(Path(d) / 'o2.json')])
        check(code == 3, f'schema_drift: records to compare in a repository whose models it cannot read (Go) exit 3 (got {code})')


def write_summary_line_endings() -> None:
    """--write-summary keeps the report's line endings (LF stays LF, CRLF stays CRLF)."""
    import contextlib, io
    src = HERE.parent / 'examples'
    for nl in ('\n', '\r\n'):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'r.json'
            text = (src / 'test_data_report.example.json').read_text(encoding='utf-8').replace('\r\n', '\n')
            p.write_bytes(text.replace('\n', nl).encode('utf-8'))
            with contextlib.redirect_stdout(io.StringIO()):
                vtd.main([str(p), '--candidates', str(src / 'run' / 'candidates.json'), '--write-summary'])
            raw = p.read_bytes()
        crlf, lf = raw.count(b'\r\n'), raw.count(b'\n')
        check((crlf == 0) if nl == '\n' else (crlf == lf and lf > 0),
              f'validator: --write-summary keeps {"LF" if nl == chr(10) else "CRLF"} line endings (got {crlf} CRLF of {lf} lines)')


def baseline_on_audit_date() -> None:
    """The example's baseline is pinned to the audit date, so examples/run/commands.sh can be rerun on any date.

    The planted membership expiry fails on the real clock after 2026-12-01 (that is the point of it), so a
    baseline on the real clock would stop passing. The baseline line in commands.sh is run as written, on a
    copy, with the real clock moved 90 days and ten years ahead underneath it; every test must still pass.
    """
    import re, shlex, shutil, subprocess
    ex = HERE.parent / 'examples'
    text = (ex / 'run' / 'commands.sh').read_text(encoding='utf-8').replace('\\\n', ' ')
    today = re.search(r'scan_data\.py .*--today (\d{4}-\d{2}-\d{2})', text)
    check(bool(today), 'commands.sh: the scan names its audit date (--today)')
    if not today:
        return
    vars_: dict[str, str] = {}
    base = None
    for line in text.splitlines():
        line = line.strip()
        m = re.fullmatch(r'([A-Z_]+)=(?:"([^"]*)"|(\S+))', line)
        if m:
            vars_[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
            continue
        if '--label baseline' in line:
            for _ in range(5):   # variables defined through other variables
                for k in sorted(vars_, key=len, reverse=True):
                    line = line.replace('$' + k, vars_[k])
            base = line
    check(base is not None, 'commands.sh: a baseline run (--label baseline) is listed')
    if base is None:
        return
    words = shlex.split(base)
    args = words[words.index('scripts/run_tests.py') + 1:] if 'scripts/run_tests.py' in words else words[2:]
    envs = [w for i, w in enumerate(args) if i and args[i - 1] == '--env']
    pinned = f'TDA_CLOCK_DATE={today.group(1)}' in envs and '-p clock_shift' in base
    check(pinned, f'commands.sh: the baseline runs with clock_shift on the audit date (TDA_CLOCK_DATE={today.group(1)}), '
                  f'not on the real clock (got: {base.split("run_tests.py")[-1].strip()[:160]})')
    try:
        import pytest  # noqa: F401
    except ImportError:
        print('skip  baseline on a moved clock: pytest is not installed')
        return
    for days in (90, 3650):
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / 'clinic-api'
            shutil.copytree(ex / 'clinic-api', copy)
            shutil.copy(HERE / 'clock_shift.py', copy / 'clock_shift.py')
            a = list(args)
            for flag, value in (('--cwd', str(copy)), ('--out', str(Path(d) / 'runs.json')), ('--repeat', '1')):
                if flag in a:
                    a[a.index(flag) + 1] = value
                else:
                    a += [flag, value]
            a += ['--env', 'PYTEST_PLUGINS=clock_shift', f'TDA_CLOCK_SHIFT=+{days}d']
            r = subprocess.run([sys.executable, str(HERE / 'run_tests.py')] + a, capture_output=True, text=True, timeout=300)
            try:
                run = json.loads((Path(d) / 'runs.json').read_text(encoding='utf-8'))['runs'][-1]
                tot = run['totals']
            except (OSError, ValueError, KeyError, IndexError):
                run, tot = {}, {}
            ok = bool(tot) and tot.get('tests', 0) > 0 and tot.get('passed') == tot.get('tests') and not run.get('errored')
            check(ok, f'commands.sh: the baseline still passes with the real clock {days} days ahead '
                      f'(got {tot or r.stderr.strip()[-200:]})')


def main() -> int:
    for t in (baseline_on_audit_date, go_rules, url_credentials_and_cards, placeholders, coverage_and_drift, residue_outside_tree, live_endpoint_severity,
              big_data_tree, cwd_relative_paths, agent_personal_data_leak, empty_repository, data_dir_unknown_format,
              served_data_dir, data_dir_code, no_tests_statement, tests_found_field, runs_required, drift_nothing_to_check,
              write_summary_line_endings):
        try:
            t()
        except Exception as exc:  # a crash in one group is a failure, not the end of the run
            check(False, f'{t.__name__} raised {exc.__class__.__name__}: {exc}')
    print(f'\n{len(FAILS)} failure(s)' if FAILS else '\nall checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
