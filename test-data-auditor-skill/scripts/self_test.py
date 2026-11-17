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
audit date (it still passes with the real clock 90 days and ten years ahead), and a tool repository whose test data is
sample projects under examples/ (read by default as fixture folders, files used by commands and self-tests not
unused, HTML/Markdown/diff/TOML/CSS read as text, time-bomb links kept inside one project, decorators in fixture
strings not emails, a near-duplicate check over 751 fixtures that is fast, small and capped, placeholder headlines
rejected, a time bomb confirmed by date arithmetic counted as evidenced). Every fixture value is invented; the
secret-looking strings are assembled at run time so none sits in this file whole.
"""
from __future__ import annotations
import json, sys, tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
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
    copy, with the real clock moved 90 days and ten years ahead underneath it; no test may change result. A test
    that fails both ways depends on the machine, not the clock (on Linux the planted locale-dependent fee test
    fails under the C locale), and is printed as a note. The same comparison on the baseline without its pinned
    date must see the planted expiry, so the check can tell a time bomb at all.
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
    def outcomes(run_args: list[str], days: int | None) -> tuple[dict[str, str], str]:
        """{test id: status} of the baseline line run on a copy, with the real clock moved `days` ahead (None: not
        moved), and an error text when nothing was recorded."""
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / 'clinic-api'
            shutil.copytree(ex / 'clinic-api', copy)
            shutil.copy(HERE / 'clock_shift.py', copy / 'clock_shift.py')
            a = list(run_args)
            for flag, value in (('--cwd', str(copy)), ('--out', str(Path(d) / 'runs.json')), ('--repeat', '1')):
                if flag in a:
                    a[a.index(flag) + 1] = value
                else:
                    a += [flag, value]
            if days is not None:
                a += ['--env', 'PYTEST_PLUGINS=clock_shift', f'TDA_CLOCK_SHIFT=+{days}d']
            r = subprocess.run([sys.executable, str(HERE / 'run_tests.py')] + a, capture_output=True, text=True, timeout=300)
            try:
                data = json.loads((Path(d) / 'runs.json').read_text(encoding='utf-8'))
                rid = data['runs'][-1]['id']
                return {c['id']: c['status'] for c in data['cases'] if c['run_id'] == rid}, ''
            except (OSError, ValueError, KeyError, IndexError):
                return {}, r.stderr.strip()[-200:]

    def clock_dependent(run_args: list[str], days: int) -> tuple[list[str], dict[str, str], str]:
        """Tests whose result changes when the real clock moves `days` ahead under the baseline line. A test that
        fails both ways depends on this machine (for example its locale: the example plants one), not on the clock."""
        now, err = outcomes(run_args, None)
        later, err2 = outcomes(run_args, days)
        changed = sorted(t for t in set(now) | set(later) if now.get(t) != later.get(t))
        return changed, now, err or err2

    # the check is not vacuous: the same comparison on the baseline without its pinned date sees the planted expiry
    unpinned = [w for i, w in enumerate(args) if not (w.startswith('TDA_CLOCK_DATE=')
                                                      or (w == '--env' and i + 1 < len(args) and args[i + 1].startswith('TDA_CLOCK_DATE=')))]
    changed, _, err = clock_dependent(unpinned, 90)
    check(bool(changed), f'commands.sh: without its pinned date the baseline would change with the real clock 90 days '
                         f'ahead (the planted expiry), so this check can see a time bomb (got {changed or err})')
    for days in (90, 3650):
        changed, now, err = clock_dependent(args, days)
        check(bool(now) and not changed, f'commands.sh: no baseline test changes result with the real clock {days} days '
                                         f'ahead (changed {changed or err})')
        env_failures = sorted(t for t, s in now.items() if s != 'passed')
        if env_failures and days == 90:
            print(f'note  the baseline fails on this machine whatever the clock, which is environment, not a time bomb: '
                  f'{", ".join(env_failures)}')


# ---------------------------------------------------------------- a tool repository whose fixtures are sample projects
def tool_mailbox(who: str) -> str:
    # invented mailboxes at an invented domain, assembled at run time
    return who + '@' + 'quaymail' + '.co'


def tool_repo(root: Path) -> None:
    """A small linting tool: scripts/ with a --self-test, a pytest suite (and benchmarks under python_files =
    bench_*.py), and examples/ holding two sample projects with planted defects plus a recorded run that the README's
    commands and examples/run/commands.sh read."""
    files = {
        'README.md': ('# linttool\n\nCheck a project and write a report:\n\n```bash\n'
                      'python scripts/check.py examples/shop --out examples/run/report.json\n'
                      'python scripts/check.py --self-test\n```\n'),
        'pytest.ini': '[pytest]\ntestpaths = tests benchmarks\npython_files = test_*.py bench_*.py\n',
        'scripts/check.py': ('import sys\nfrom pathlib import Path\n\nHERE = Path(__file__).resolve().parent\n\n\n'
                             'def self_test():\n    shop = HERE.parent / "examples" / "shop"\n'
                             '    assert (shop / "data" / "customers.json").exists()\n\n\n'
                             'if __name__ == "__main__":\n    if "--self-test" in sys.argv:\n        self_test()\n'),
        'scripts/test_paths.py': ('def is_test_path(p):\n    """True for files under tests/."""\n'
                                  '    return p.startswith("tests/")\n'),
        # two components of the tool with their own code: a field name shared between them links nothing
        'notify/send.py': 'def send(window):\n    return window["channel"]\n',
        'notify/examples/window.json': '{\n  "channel": "mail",\n  "expires": "2027-05-01"\n}\n',
        'billing/plans.py': ('from datetime import date\n\n\ndef lapsed(plan):\n'
                             '    return plan.expires < date.today()\n'),
        'scripts/seed_report.py': ('import json\n\n\ndef seed_report(findings):\n'
                                   '    return json.dumps({"findings": findings})\n'),
        'scripts/grants.py': ('from datetime import date\n\n\ndef active(grant):\n'
                              '    return date.fromisoformat(grant["valid_until"]) >= date.today()\n\n\n'
                              'def lapsed(card):\n    today = date.today()\n'
                              '    return date.fromisoformat(card["valid_thru"]) < today\n'),
        'examples/run/card.json': '{\n  "card": "K-2",\n  "valid_thru": "2027-02-01"\n}\n',
        'tests/test_check.py': ('from pathlib import Path\n\nORDERS = Path("examples/shop/data/orders.json")\n\n\n'
                                'def test_orders():\n    assert ORDERS.exists()\n'),
        'tests/test_strings.py': ('ROUTER_SRC = "from api import router\\n@router.get(\'/items\')\\ndef items():\\n    return []"\n'
                                  'FIXTURE_SRC = "import pytest\\n@pytest.fixture\\ndef db():\\n    return {}"\n\n\n'
                                  'def test_sources():\n    assert ROUTER_SRC and FIXTURE_SRC\n'),
        'benchmarks/bench_speed.py': 'def test_speed():\n    assert sum(range(10)) == 45\n',
        'examples/shop/package.json': '{"name": "shop", "private": true}\n',
        'examples/shop/src/app.js': 'export const total = (xs) => xs.reduce((a, b) => a + b, 0);\n',
        'examples/shop/data/customers.json': ('[\n  {"id": 1, "contact": "%s", "expires": "2027-03-01"},\n'
                                              '  {"id": 2, "contact": "", "expires": "2027-03-01"}\n]\n' % tool_mailbox('rhoda.vane')),
        'examples/shop/data/orders.json': '[\n  {"id": 1, "total": 12},\n  {"id": 2, "total": 7}\n]\n',
        'examples/other/package.json': '{"name": "other", "private": true}\n',
        'examples/other/src/expiry.py': ('from datetime import date\n\n\ndef lapsed(item):\n'
                                         '    return item.expires < date.today()\n'),
        # a test that lists a temporary folder with a bare "*" glob, in a module that says "run": it loads no fixture
        'tests/test_listing.py': ('MODE = "run"\n\n\ndef test_listing(tmp_path):\n    (tmp_path / "a.txt").write_text("x")\n'
                                  '    assert list(tmp_path.glob("*"))\n'),
        'examples/run/commands.sh': ('#!/usr/bin/env bash\nset -euo pipefail\nRUN=examples/run\n'
                                     'python scripts/check.py examples/shop --baseline $RUN/baseline.json\n'),
        'examples/run/report.json': '{"findings": [], "evidence": ["exchanges/list-001.json"]}\n',
        'examples/run/exchanges/list-001.json': '{"status": 200, "body": []}\n',
        'examples/run/baseline.json': '{"findings": [], "taken": "baseline"}\n',
        'examples/run/stale.json': '{"findings": [], "taken": "before the rename"}\n',
        'examples/run/preauth.json': '{\n  "grant": "G-1",\n  "valid_until": "2027-01-15"\n}\n',
        'examples/run/page.html': '<!doctype html>\n<p>Write to %s for a refund.</p>\n' % tool_mailbox('olwen.marsh'),
        'examples/run/ticket.md': '# SHOP-7\n\nReported by %s on the checkout page.\n' % tool_mailbox('idris.fenn'),
        'examples/run/change.diff': '--- a/owners.txt\n+++ b/owners.txt\n@@ -1 +1 @@\n-owner: nobody\n+owner: %s\n' % tool_mailbox('bea.lusk'),
        'examples/run/settings.toml': '[owner]\ncontact = "%s"\n' % tool_mailbox('cai.morrow'),
        'examples/run/site.css': '/* maintained by %s */\nbody { margin: 0; }\n' % tool_mailbox('dov.harte'),
    }
    write(root, files)


def fixture_dirs_by_default() -> None:
    """The default inventory offers the sample projects' fixture trees (examples/) as data trees, says so, and can be told
    not to; a product script named seed_* is not a seed, and a test_*.py helper with no tests is not a test file."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        tool_repo(root)
        invd = inv.build(root, None, {})
        paths = {i['path']: i for i in invd['inventory']}
        check('examples/shop/data/customers.json' in paths and 'examples/run/preauth.json' in paths,
              f'inventory: files under examples/ (sample projects and recorded runs) are inventoried by default '
              f'(got {sorted(p for p in paths if p.startswith("examples/"))})')
        fd = {f['dir']: f for f in invd.get('fixture_dirs') or []}
        check('examples' in fd and bool(fd['examples'].get('reason')),
              f'inventory: fixture_dirs records examples/ with its reason (got {sorted(fd)})')
        check(any('examples' in w and 'fixture' in w for w in invd.get('warnings', [])),
              'inventory: a warning says which fixture trees were added as data trees')
        check('scripts/seed_report.py' not in paths, 'inventory: a product script named seed_report.py is not a seed')
        check('scripts/test_paths.py' not in paths, 'inventory: scripts/test_paths.py with no test functions is not a test file')
        invd = inv.build(root, None, {}, fixture_dirs=False)
        check(not any(i['path'].startswith('examples/') for i in invd['inventory']),
              'inventory: fixture_dirs=False (--no-fixture-dirs) leaves examples/ out')


def commands_count_as_use() -> None:
    """A data file a documented command (README, commands.sh) or a self-test script names is used, not "unused": the
    inventory lists it under used_by_commands (or loaded_by for a self-test), and only files nothing names are unused."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        tool_repo(root)
        invd = inv.build(root, None, {})
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    unused = set(invd['summary']['unused'])
    by_cmd = invd['summary'].get('used_by_commands') or []
    check('examples/run/report.json' in by_cmd and 'examples/run/baseline.json' in by_cmd,
          f'inventory: files named by the README command and by commands.sh (through $RUN) are used by commands (got {by_cmd})')
    check(not unused & {'examples/run/report.json', 'examples/run/baseline.json', 'examples/shop/data/customers.json',
                        'examples/shop/data/orders.json'},
          f'inventory: files read by commands, a self-test or a test are not unused (got {sorted(unused)})')
    check('examples/run/stale.json' in unused, 'inventory: a file nothing names is still unused')
    ex = next((i for i in invd['inventory'] if i['path'] == 'examples/run/exchanges/list-001.json'), {})
    check('examples/run/exchanges/list-001.json' not in unused and bool(ex.get('used_by_commands')),
          f'inventory: evidence named by a data file a command reads (report.json lists exchanges/list-001.json) is used '
          f'(got {ex.get("used_by_commands")})')
    cust = next((i for i in invd['inventory'] if i['path'] == 'examples/shop/data/customers.json'), {})
    check(any(t.startswith('scripts/check.py::') for t in cust.get('loaded_by', [])),
          f'inventory: a self-test that builds the path of the sample project loads its files (got {cust.get("loaded_by")})')
    flagged = {c['file'] for c in res['candidates'] if c['rule'] == 'unused'}
    check('examples/run/report.json' not in flagged and 'examples/run/stale.json' in flagged,
          f'scan: no unused candidate for a file a command reads (got {sorted(flagged)})')
    check(any('used by commands' in n for n in res['notes']), 'scan: the notes count the files used by commands, not tests')


def prose_formats() -> None:
    """HTML, Markdown, diffs, TOML and CSS under a data tree are read, and an address in their text is a candidate."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        tool_repo(root)
        invd = inv.build(root, None, {})
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    st = {i['path']: i['status'] for i in invd['inventory']}
    for ext in ('page.html', 'ticket.md', 'change.diff', 'settings.toml', 'site.css'):
        rel = f'examples/run/{ext}'
        check(st.get(rel) == 'scanned', f'inventory: {rel} is scanned, not an unknown format (got {st.get(rel)})')
        check(any(c['file'] == rel and c['rule'] == 'email' and not c['auto_dismiss'] for c in res['candidates']),
              f'scan: an address in the text of {rel} is a personal-data candidate')


def time_bombs_stay_in_project() -> None:
    """A fixed expiry is linked only to a comparison in the same fixture project (or, outside every sample project, in
    the host's own code), never to another sample project's code that happens to use the same field name."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        tool_repo(root)
        invd = inv.build(root, None, {})
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    bombs = {c['file']: c for c in res['candidates'] if c['rule'] == 'fixed_expiry'}
    shop = bombs.get('examples/shop/data/customers.json', {}).get('details', {})
    check('examples/shop/data/customers.json' in bombs and not shop.get('compared_with_now'),
          f'scan: an expiry in one sample project is not linked to another sample project\'s comparison (got {shop.get("compared_with_now")})')
    pre = bombs.get('examples/run/preauth.json', {}).get('details', {})
    check(any(c['file'] == 'scripts/grants.py' for c in pre.get('compared_with_now') or []),
          f'scan: an expiry in host data is linked to the host code comparing it through date.fromisoformat (got {pre.get("compared_with_now")})')
    win = bombs.get('notify/examples/window.json', {}).get('details', {})
    check('notify/examples/window.json' in bombs and not win.get('compared_with_now'),
          f'scan: an expiry in one component (notify/, with its own code) is not linked to another component\'s comparison '
          f'(got {win.get("compared_with_now")})')
    card = bombs.get('examples/run/card.json', {}).get('details', {})
    check(any(c['file'] == 'scripts/grants.py' and c['line'] == 10 for c in card.get('compared_with_now') or []),
          f'scan: a comparison with a variable that holds today (today = date.today()) is a comparison with now '
          f'(got {card.get("compared_with_now")})')


def code_is_not_an_email() -> None:
    """An escaped newline before a decorator inside a fixture string ("\\n@router.get") is code, not a mailbox; a value
    assembled from an f-string placeholder is not a secret literal."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        tool_repo(root)
        word = 'hunter' + '2'
        write(root, {'tests/test_login.py': 'WORD = "' + word + '"\n\n\ndef test_login():\n'
                                            '    cfg = f\'{{"password": "{WORD}-and-more"}}\'\n    assert cfg\n'})
        invd = inv.build(root, None, {})
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    emails = [c for c in res['candidates'] if c['file'] == 'tests/test_strings.py' and c['rule'] == 'email']
    check(not emails, f'scan: "\\n@router.get" and "\\n@pytest.fixture" in a fixture string are not emails (got {len(emails)})')
    sec = [c for c in res['candidates'] if c['file'] == 'tests/test_login.py' and c['category'] == 'secret' and c['line'] == 5]
    check(all(c['auto_dismiss'] for c in sec), 'scan: a password filled in from an f-string placeholder is a dismissed placeholder')


DUP_SCALE_CHILD = r'''
import json, sys, time, tracemalloc
from datetime import date
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import scan_data
root = Path(sys.argv[2])
invd = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
s = scan_data.Scanner(root, invd, date(2026, 11, 11))
tracemalloc.start()
t0 = time.process_time()
s.snapshots_and_duplicates()
took = time.process_time() - t0
peak = tracemalloc.get_traced_memory()[1]
dups = [{'file': c['file'], 'similar_to': c['details'].get('similar_to')} for c in s.cands if c['rule'] == 'near_duplicate']
print(json.dumps({'took': took, 'peak': peak, 'dups': dups, 'stats': getattr(s, 'duplicate_stats', None), 'notes': s.notes}))
'''


def near_duplicates_scale() -> None:
    """The near-duplicate check compares files within one project, finds candidate pairs by a cheap sketch instead of
    comparing every pair, caps the pairs it confirms, and stays fast and small: 30 directories of 25 distinct fixtures
    (about 280,000 same-format pairs) take a few seconds of CPU, where comparing every pair took far longer."""
    import random, subprocess, time
    rnd = random.Random(7)
    words = ['harbour', 'ferry', 'ticket', 'pier', 'tide', 'route', 'cabin', 'deck', 'fare', 'quay', 'buoy', 'wharf']
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        files = {'tests/test_load.py': 'from pathlib import Path\n\n\ndef test_load():\n    assert Path("tests/fixtures").is_dir()\n'}
        body = ''
        for k in range(30):
            for j in range(25):
                rows = ',\n'.join('  {"id": %d, "route": "%s-%s-%d", "note": "%s"}' % (
                    n, rnd.choice(words), rnd.choice(words), rnd.randrange(10_000),
                    ' '.join(rnd.choice(words) for _ in range(8))) for n in range(40))
                text = '[\n' + rows + '\n]\n'
                files[f'tests/fixtures/d{k:02d}/f{j:02d}.json'] = text
                if (k, j) == (3, 1):
                    body = text
        # planted: a copy with one row changed, in another directory of the same project
        files['tests/fixtures/d17/copy.json'] = body.replace('"id": 5,', '"id": 505,')
        # the same copy in a separate sample project is not compared with this one
        files['examples/other/package.json'] = '{"name": "other"}\n'
        files['examples/other/fixtures/copy.json'] = body
        write(root, files)
        invd = inv.build(root, None, {})
        ip = Path(d) / 'inv.json'
        ip.write_text(json.dumps(invd), encoding='utf-8')
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, '-c', DUP_SCALE_CHILD, str(HERE), str(root), str(ip)],
                               capture_output=True, text=True, timeout=90)
            out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
        except subprocess.TimeoutExpired:
            out = {}
        wall = time.monotonic() - t0
    took = out.get('took')
    check(took is not None and took < 15.0,
          f'duplicates: 751 fixtures in 30 directories are compared in under 15 s of CPU time (took '
          f'{"over 90 s wall, killed" if took is None else f"{took:.1f} s"}; {wall:.0f} s wall)')
    check(out.get('peak', 1 << 40) < 64 * 1024 * 1024,
          f'duplicates: peak traced memory stays under 64 MB (got {out.get("peak", 0) / 1e6:.0f} MB)')
    dups = out.get('dups') or []
    check(any({x['file'], x['similar_to']} == {'tests/fixtures/d03/f01.json', 'tests/fixtures/d17/copy.json'} for x in dups),
          f'duplicates: a near copy in another directory of the same project is found (got {dups[:5]})')
    check(not any('examples/other/fixtures/copy.json' in (x['file'], x['similar_to']) for x in dups),
          'duplicates: a copy in a separate sample project is not compared across projects')
    stats = out.get('stats') or {}
    check(bool(stats) and stats.get('pairs_compared', 1 << 30) <= stats.get('pair_cap', 0),
          f'duplicates: the scan records how many pairs it confirmed, within its cap (got {stats})')


def capped_pairs_are_noted() -> None:
    """When more candidate pairs than the cap turn up (a templated tree), the extra pairs are not compared and the notes
    say so."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        files = {'tests/test_load.py': 'def test_load():\n    assert True\n'}
        for j in range(30):
            files[f'tests/fixtures/t{j:02d}.json'] = ('[\n' + ',\n'.join('  {"id": %d, "name": "row %d"}' % (n, n) for n in range(20))
                                                      + ',\n  {"id": 99, "name": "variant %d"}\n]\n' % j)
        write(root, files)
        invd = inv.build(root, None, {})
        s = scan_data.Scanner(root, invd, date(2026, 11, 11))
        s.duplicate_pair_cap = 50
        s.snapshots_and_duplicates()
    stats = getattr(s, 'duplicate_stats', None) or {}
    check(stats.get('pairs_compared') == 50 and stats.get('pairs_skipped', 0) > 0,
          f'duplicates: the pair cap stops the confirmation at 50 pairs and counts the rest (got {stats})')
    check(any('near-duplicate' in n and 'cap' in n for n in s.notes), 'duplicates: the notes say the pair cap was reached')


def placeholder_headline() -> None:
    """A headline left as a draft placeholder is rejected."""
    for text in ('Draft headline to be replaced after review.', 'TODO: headline', 'Placeholder headline.', 'TBD'):
        ex = report_with(limitations=['Static rules only.'])
        ex['summary']['headline'] = text
        errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None)
        check(any('placeholder' in e for e in errs), f'validator: the headline "{text}" is a placeholder and is rejected')
    ex = report_with(limitations=['Static rules only.'])
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None)
    check(not any('placeholder' in e for e in errs), 'validator: a written headline is not a placeholder')


def arithmetic_time_bomb() -> None:
    """A time bomb confirmed by date arithmetic (scan_data.py computed the fail date from a comparison with now) needs no
    experiment: it counts as evidenced in the completeness, and the renderer says how it was confirmed."""
    import subprocess
    ex = report_with(limitations=['Static rules only.'])
    ex['inventory'] = [{'path': 'tests/fixtures/plan.json', 'kind': 'fixture', 'format': 'json', 'loaded_by': ['t::a'],
                        'status': 'scanned', 'reason': None}]
    f = {'id': 'F1', 'category': 'time_bomb', 'title': 'Plan expiry fixed at 2027-01-01', 'candidates': ['C1'],
         'location': {'file': 'tests/fixtures/plan.json', 'line': 3, 'field': 'expires'}, 'data_type': 'date',
         'why_flagged': 'expires is fixed and compared with today', 'severity': 'serious', 'confidence': 'confirmed',
         'fails_on_date': '2027-01-01', 'experiment': {'run_ids': [], 'variation': 'none', 'result': 'not_run'},
         'affected_tests': ['t::a'], 'fix': 'Compute the expiry relative to today.',
         'verification_step': 'Rerun with the clock moved past the date.', 'reach': 1, 'score': 3, 'priority': 'P1'}
    ex['findings'] = [f]
    cands = {'C1': {'id': 'C1', 'category': 'time_bomb', 'rule': 'fixed_expiry', 'file': 'tests/fixtures/plan.json', 'line': 3,
                    'details': {'compared_with_now': [{'file': 'app/plan.py', 'line': 4}], 'fails_on_date': '2027-01-01'}}}
    got = vtd.completeness(ex, cands)
    check(got == 1.0, f'validator: a time bomb confirmed by date arithmetic counts as evidenced (got {got})')
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'r.json'
        p.write_text(json.dumps(ex), encoding='utf-8')
        out = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(p), '--color', 'never'],
                             capture_output=True, text=True).stdout
    check('date arithmetic' in out and 'not run ()' not in out,
          'renderer: a bomb confirmed by date arithmetic says so, with no empty run list')


# ---------------------------------------------------------------- gaps found on a Rust workspace with Python tooling
def long_line_is_linear() -> None:
    """A report page with its data embedded on one line (hundreds of thousands of characters, thousands of quoted
    strings): the attribute look-behind reads a short window before each string, never the whole line so far."""
    import time
    cells = ''.join('<td class="n" data-k="v%d">row %d</td>' % (i, i) for i in range(9000))
    page = '<!doctype html>\n<html><body><table><tr>' + cells + '</tr></table>\n<p title="Contact">Ada Example, ' \
           + place_mailbox() + '</p></body></html>\n'
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'tests/test_page.py': 'def test_page():\n    assert open("tests/fixtures/report.html").read()\n',
                     'tests/fixtures/report.html': page})
        invd = inv.build(root, None, {})
        t0 = time.process_time()
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
        took = time.process_time() - t0
    check(len(cells) > 300_000 and took < 6.0,
          f'scan: one {len(cells) // 1000}k-character line with 18,000 quoted strings scans in under 6 s of CPU time '
          f'(took {took:.1f} s)')
    check(any(c['rule'] == 'email' and c['file'] == 'tests/fixtures/report.html' and c['line'] == 3 for c in res['candidates']),
          'scan: the address after the long line is still found, on its own line')


def archive_mailbox() -> str:
    # invented, assembled at run time
    return 'wren.ottley' + '@' + 'fernmail' + '.co'


def archives_and_logs() -> None:
    """A zip of run logs committed beside the tests, and a loose .log: both are read (the zip in memory), every member
    is listed, candidates cite <archive>!<member> and the line in the member, and an unread member says why."""
    import io, zipfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'tests/test_runs.py': ('from pathlib import Path\n\n\ndef test_runs():\n'
                                            '    assert (Path(__file__).parent / "fixtures" / "runs.zip").exists()\n'),
                     'tests/fixtures/player.log': 'started\nwrote /home/quillfern/media/clip.mp4\n'})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('logs/run1.log', 'start\ncontact ' + archive_mailbox() + '\nend\n')
            z.writestr('logs/run2.json', '{"cmd": "/Users/quillfern/bin/player"}\n')
            z.writestr('inner/older.tar', b'\x00' * 64)
            z.writestr('shots/frame.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * 32)
        (root / 'tests/fixtures/runs.zip').write_bytes(buf.getvalue())
        invd = inv.build(root, None, {})
        by = {i['path']: i for i in invd['inventory']}
        z = by.get('tests/fixtures/runs.zip', {})
        m1, m2 = by.get('tests/fixtures/runs.zip!logs/run1.log', {}), by.get('tests/fixtures/runs.zip!inner/older.tar', {})
        check(m1.get('status') == 'scanned' and m1.get('lines') == 3 and 'tests/test_runs.py::test_runs' in m1.get('loaded_by', []),
              f'inventory: a text member of a committed zip is listed as <archive>!<member>, scanned, loaded by the archive\'s '
              f'tests (got {m1})')
        check(m2.get('status') == 'not_assessed' and 'nested archive' in (m2.get('reason') or ''),
              f'inventory: a nested archive inside a zip is listed not_assessed with the reason (got {m2.get("reason")})')
        check(z.get('status') == 'not_assessed' and 'partly read' in (z.get('reason') or '')
              and (z.get('archive_members') or {}).get('members') == 4,
              f'inventory: a zip with a member it could not read is not_assessed, partly read, with its member count (got {z})')
        check(any('runs.zip' in w for w in invd.get('warnings', [])), 'inventory: a partly read archive is a WARNING')
        check(by.get('tests/fixtures/player.log', {}).get('status') == 'scanned', 'inventory: a .log file is read as text')
        check('tests/fixtures/runs.zip!logs/run1.log' not in invd['summary']['unused'], 'inventory: archive members are never "unused"')
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
        cs = {(c['rule'], c['file'], c['line']) for c in res['candidates']}
        check(('email', 'tests/fixtures/runs.zip!logs/run1.log', 2) in cs,
              f'scan: an address inside a zip member is a candidate citing the archive, the member and the line (got {sorted(cs)})')
        check(('local_path', 'tests/fixtures/runs.zip!logs/run2.json', 1) in cs and ('local_path', 'tests/fixtures/player.log', 2) in cs,
              'scan: home-folder paths in a zip member and in a loose .log are found')
        # the validator re-reads a member: a basis line in it, and the values on it are protected
        f = {'id': 'F1', 'category': 'personal_data', 'source': 'agent', 'candidates': [], 'confidence': 'static',
             'location': {'file': 'tests/fixtures/runs.zip!logs/run1.log', 'line': 2},
             'basis': [{'file': 'tests/fixtures/runs.zip!logs/run1.log', 'line': 2, 'excerpt': 'contact'}]}
        errs = vtd.basis_errors(f, root, None)
        check(errs == [], f'validator: a basis line inside a zip member is re-read (got {errs})')
        per = vtd.protected_values({}, root, {'findings': [f], 'dismissed': []})
        check(archive_mailbox() in per.get('F1', set()), 'validator: the values on a basis line inside a zip member are protected')
        ev_only = dict(f, basis=[{'evidence': 'zip_members.json'}])
        check(any('evidence file alone' in e for e in vtd.basis_errors(ev_only, root, None)),
              'validator: a review personal_data finding with only an evidence basis is rejected (nothing would be protected)')
        cands = {c['id']: c for c in res['candidates']}
        cid = next((c['id'] for c in res['candidates'] if c['rule'] == 'email' and '!' in c['file']), None)
        prot = vtd.protected_values(cands, root, {'findings': [], 'dismissed': []})
        check(cid is not None and archive_mailbox() in prot.get(cid, set()),
              'validator: a candidate inside a zip member has its value protected (the report must not quote it)')


def local_path_false_positives() -> None:
    """Paths that are not someone's machine: a placeholder account fed to a redaction test, a reserved-domain URL whose
    path starts /home/, and a CI runner's home (Windows and macOS runners too). A real-looking account still counts."""
    res = scan({
        'tests/test_redact.py': (
            'from tool import redact\n\n\ndef test_redacts_accounts():\n'
            "    raw = [r'C:\\Users\\private-account\\movie.mp4', '/home/private-account/movie.mp4', '/Users/some-user/a.mp4']\n"
            '    assert all("account" not in redact(x) for x in raw)\n\n\n'
            'def test_source_url():\n'
            "    assert redact('https://example.org/home/contributor/media') == 'https://example.org/home/contributor/media'\n\n\n"
            'def test_runner_home():\n'
            "    env = {'LOCALAPPDATA': 'C:/Users/runner/AppData/Local', 'HOME': '/Users/runner/', 'X': r'C:\\Users\\runneradmin\\x'}\n"
            '    assert env\n\n\n'
            'def test_real_looking():\n'
            "    assert redact('/home/tamsin.oakridge/clip.mp4')\n"),
        'tool.py': 'def redact(s):\n    return s\n'})
    lp = [c for c in res['candidates'] if c['rule'] == 'local_path']
    live = {c['line'] for c in lp if not c['auto_dismiss']}
    dismissed = {c['line'] for c in lp if c['auto_dismiss']}
    check(live == {19}, f'scan: local_path stays only for a real-looking account (line 19); got live lines {sorted(live)}')
    check(5 in dismissed, f'scan: a placeholder account (private-account, some-user) is dismissed by rule, not dropped '
                          f'(got dismissed lines {sorted(dismissed)})')
    check(not any(c['line'] in (10, 14) for c in lp), 'scan: a reserved-domain URL path and a CI runner home are not local paths')


# a tooling repository: scripts/ that tests load by file path, catalogues in catalog/ that tests read by repository
# path, a results tree a script globs, and an output file name a script writes
TOOL_SCRIPTS = {
    'scripts/build-page.py': ('from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n\n\n'
                              'def render(data):\n    template = (ROOT / "pages/page.template.html").read_text()\n'
                              '    return template.replace("<!-- DATA -->", data)\n'),
    'scripts/refresh.py': ('import json\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n\n\n'
                           'def collect(bundles=ROOT / "catalog/results"):\n'
                           '    return [json.loads(p.read_text()) for p in sorted(bundles.glob("*.json"))]\n\n\n'
                           'def features():\n    return json.loads((ROOT / "catalog/features.json").read_text())\n'),
    'scripts/compare.py': ('import json\nfrom pathlib import Path\n\n\ndef save(output, payload):\n'
                           '    path = output / "comparison.json"\n    if (output / "comparison.json").exists():\n'
                           '        raise SystemExit("exists")\n    path.write_text(json.dumps(payload))\n'),
    'tests/test_page.py': ('import importlib.util\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n'
                           'SPEC = importlib.util.spec_from_file_location("build_page", ROOT / "scripts/build-page.py")\n'
                           'page = importlib.util.module_from_spec(SPEC)\nSPEC.loader.exec_module(page)\n\n\n'
                           'def test_render():\n    assert "x" in page.render("x")\n'),
    'tests/test_refresh.py': ('import sys\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n'
                              'sys.path.insert(0, str(ROOT / "scripts"))\nimport refresh\n\n\n'
                              'def test_features():\n    assert refresh.features()\n\n\n'
                              'def test_catalogue_by_path():\n'
                              '    assert (ROOT / "catalog/events.json").read_text()\n'),
    'tests/test_compare.py': ('import sys\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n'
                              'sys.path.insert(0, str(ROOT / "scripts"))\nimport compare\n\n\n'
                              'def test_save(tmp_path):\n    compare.save(tmp_path, {})\n    assert (tmp_path / "comparison.json").exists()\n'),
    'pages/page.template.html': '<html><body><!-- DATA --></body></html>\n',
    'catalog/features.json': '{"features": ["a"]}\n',
    'catalog/events.json': '{"events": ["b"]}\n',
    'catalog/other-inventory.json': '{"other": 1}\n',
    'catalog/results/r1.json': '{"run": 1}\n',
    'results/old/comparison.json': '{"trials": []}\n',
}


def loaded_by_reach() -> None:
    """Files tests reach through scripts they load by path count as loaded; a glob of one folder does not reach its
    parent's files; a name a script only writes is not a load; and data files tests read by repository path are
    inventoried even without --data-dir."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, TOOL_SCRIPTS)
        invd = inv.build(root, None, {}, ['catalog', 'pages', 'results'])
        by = {i['path']: i for i in invd['inventory']}
        tpl = by.get('pages/page.template.html', {}).get('loaded_by', [])
        check('tests/test_page.py::test_render' in tpl,
              f'inventory: a template read by a script the test loads with spec_from_file_location is loaded by it (got {tpl})')
        feats = by.get('catalog/features.json', {}).get('loaded_by', [])
        check('tests/test_refresh.py::test_features' in feats,
              f'inventory: a file read by a script imported from a sys.path folder is loaded by the importing tests (got {feats})')
        other = by.get('catalog/other-inventory.json', {}).get('loaded_by', [])
        check(other == [], f'inventory: a glob over catalog/results does not reach files directly under catalog/ (got {other})')
        r1 = by.get('catalog/results/r1.json', {}).get('loaded_by', [])
        check('tests/test_refresh.py::test_features' in r1, f'inventory: the globbed folder\'s own files are reached (got {r1})')
        cmp_ = by.get('results/old/comparison.json', {}).get('loaded_by', [])
        check(cmp_ == [], f'inventory: a file name a script and a test only write or probe is not a load (got {cmp_})')
        invd = inv.build(root, None, {})
        by = {i['path']: i for i in invd['inventory']}
        ev = by.get('catalog/events.json', {})
        check(ev.get('status') == 'scanned' and 'tests/test_refresh.py::test_catalogue_by_path' in ev.get('loaded_by', []),
              f'inventory: a data file outside the test areas that a test names by its repository path is inventoried by '
              f'default (got {ev})')
        check(any('catalog' in w for w in invd.get('warnings', [])) or any(
            u['dir'] == 'catalog' for u in invd.get('unlisted_data_dirs', [])),
              'inventory: a data folder tests read by path but not inventoried whole is named in a warning')


def log_ids_and_grouped_paths() -> None:
    """A player's debug log: bracketed object addresses and labelled ids that happen to be Luhn-valid digit runs are not
    card numbers; a home path repeated on many lines of one file is one local_path candidate listing its lines; and an
    evidence tree (--evidence-dir) is scanned but never called unused."""
    addr = luhn_complete('400000000000000')            # a 16-digit run that passes Luhn
    lines = ['[%s] main stream debug: looking for module "any": 12 candidates' % addr,
             '[00000a1b2c3d4e5f] demux debug: |   + TrackUID: %s' % luhn_complete('411111111111111111'),
             'opened /home/quillfern/media/a.mkv', 'opened /home/quillfern/media/b.mkv', 'opened /home/quillfern/media/c.mkv']
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'tests/test_x.py': 'def test_x():\n    assert True\n', 'runs/r1/stderr.log': '\n'.join(lines) + '\n',
                     'runs/r1/summary.json': '{"ok": true}\n'})
        invd = inv.build(root, None, {}, evidence_dirs=['runs'])
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    cards = [c for c in res['candidates'] if c['rule'] == 'card']
    check(cards == [], f'scan: a bracketed log address and a labelled TrackUID are not card numbers (got {[c["line"] for c in cards]})')
    lp = [c for c in res['candidates'] if c['rule'] == 'local_path']
    check(len(lp) == 1 and lp[0]['line'] == 3 and lp[0]['details'].get('lines') == [3, 4, 5],
          f'scan: one local_path candidate per file, listing every line (got {[(c["line"], c["details"]) for c in lp]})')
    check(not [c for c in res['candidates'] if c['rule'] == 'unused'] and any(
        i['path'] == 'runs/r1/summary.json' for i in invd['inventory']),
          'inventory: files under --evidence-dir are inventoried and scanned, never called unused')


RUST_WORKSPACE = {
    'Cargo.toml': '[workspace]\nmembers = ["crates/reel"]\n',
    'crates/reel/Cargo.toml': '[package]\nname = "reel"\nversion = "0.1.0"\nedition = "2021"\n',
    'crates/reel/src/lib.rs': ('pub fn frames(n: u32) -> u32 { n * 2 }\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n'
                               '    #[test]\n    fn doubles() {\n        assert_eq!(frames(2), 4);\n    }\n\n'
                               '    #[test]\n    fn reads_script() {\n'
                               '        let s = std::fs::read_to_string("tests/fixtures/controls.lua").unwrap();\n'
                               '        assert!(s.contains("play"));\n    }\n}\n'),
    'crates/reel/tests/cli.rs': ('use std::process::Command;\n\n#[test]\nfn runs_helper() {\n'
                                 '    let out = Command::new(env!("CARGO_BIN_EXE_reel")).output();\n    assert!(out.is_ok());\n}\n\n'
                                 '#[test]\nfn reads_fixture() {\n'
                                 '    assert!(std::fs::read_to_string("tests/fixtures/clip.json").is_ok());\n}\n'),
    'crates/reel/tests/fixtures/player.rs': 'fn main() {\n    println!("{}", "ready");\n}\n',
    'crates/reel/tests/fixtures/controls.lua': 'mp.add_key_binding("p", "play", function() end)\n',
    'crates/reel/tests/fixtures/clip.json': '{"frames": 4}\n',
    'tests/test_tool.py': 'def test_tool():\n    assert True\n',
}


def rust_tests_modelled() -> None:
    """A Rust workspace read through cargo_project.py: #[test] functions in #[cfg(test)] modules of src/ files are
    inventoried, a helper program under tests/fixtures is not a test, tests_found counts test functions, and a fixture
    of an unknown type is listed (not_assessed) instead of dropped."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, RUST_WORKSPACE)
        invd = inv.build(root, None, {})
        res = scan_data.Scanner(root, invd, date(2026, 11, 11)).run()
    by = {i['path']: i for i in invd['inventory']}
    lib = by.get('crates/reel/src/lib.rs', {})
    check(lib.get('kind') == 'inline' and len(lib.get('loaded_by', [])) == 2,
          f'inventory: a src/ file with #[cfg(test)] tests is inventoried as test code with its test ids (got {lib})')
    helper = by.get('crates/reel/tests/fixtures/player.rs', {})
    check(helper.get('kind') != 'inline' and not any('player.rs' in t for t in invd['tests']),
          f'inventory: a helper program under tests/fixtures is not a test (got {helper.get("kind")}, '
          f'{[t for t in invd["tests"] if "player" in t]})')
    check(res['tests_found'] == 5, f'scan: tests_found counts test functions: 4 Rust and 1 Python (got {res["tests_found"]}: '
                                   f'{invd["tests"]})')
    lua = by.get('crates/reel/tests/fixtures/controls.lua', {})
    check(lua.get('status') == 'not_assessed' and 'unknown format' in (lua.get('reason') or ''),
          f'inventory: a fixture of an unknown type is listed not_assessed, not dropped (got {lua})')
    clip = by.get('crates/reel/tests/fixtures/clip.json', {}).get('loaded_by', [])
    check(any(t.endswith('reads_fixture') for t in clip), f'inventory: a fixture a Rust test names is loaded by that test (got {clip})')


def required_limitations() -> None:
    """The limitations SKILL.md asks for are enforced: languages read as text only, schema drift not assessed for the
    dominant language, a data tree tests name but nothing scanned, a partly read archive. The word placeholder in a
    headline that describes placeholder data is fine; a stock draft headline still is not."""
    cand_doc = {'tests_found': 12, 'candidates': [], 'language_coverage': {'code_rules': ['python'], 'text_only': {'rust': 7}},
                'notes': ['schema drift: This repository is predominantly Rust ... NOT a schema drift check',
                          'catalog/ holds 60 data files that test code names, but the inventory does not cover it: NOT scanned '
                          'for personal data, time bombs or duplicates (inventory it with --data-dir catalog)'],
                'inventory_status': {'results/runs.zip': {'status': 'not_assessed', 'reason': 'zip archive partly read: 3 '
                                                          'member(s) read as text, not read: a.tar (nested archive: not opened)'}}}
    ex = report_with(limitations=['Static rules only.'])
    ex['metadata']['tests_found'] = 12
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc=cand_doc)
    for what in ('rust', 'schema drift', 'catalog/', 'results/runs.zip'):
        check(any('limitation' in e and what in e for e in errs),
              f'validator: a report whose limitations leave out {what} is rejected (got {[e[:90] for e in errs]})')
    ex['limitations'] = ['Rust test code (7 files) was read as text only: no Rust code rules ran.',
                         'Schema drift was not assessed: the models are Rust structs, which schema_drift.py does not read.',
                         'catalog/ (60 data files the tests read) was not inventoried or scanned.',
                         'results/runs.zip was only partly read: a nested archive in it was not opened.']
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None, cand_doc=cand_doc)
    check(not any('limitation' in e for e in errs), f'validator: limitations that name them pass (got {errs})')
    ex['summary']['headline'] = 'The unpacked copies carry a placeholder instead of the account name; the archive does not.'
    errs = vtd.check(ex, None, None, None, None, json.dumps(ex), None, None)
    check(not any('is a placeholder' in e for e in errs), 'validator: a headline about placeholder data is not itself a placeholder')


def residue_directories() -> None:
    """Tests that keep scratch folders inside the repository and remove only their own subfolder leave the parents
    behind, empty: the diff lists directories a run created (dirs_added), empty ones included."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'a.txt': 'a\n', 'results/keep.md': 'k\n'})
        before = residue.snapshot_all(root)
        (root / 'results' / 'local').mkdir()
        (root / 'scratch' / 'test-tmp').mkdir(parents=True)
        res = residue.diff(before, residue.snapshot_all(root))
    check(res.get('dirs_added') == ['results/local', 'scratch', 'scratch/test-tmp'] and res['summary'].get('dirs_added') == 3,
          f'residue: empty directories a run created are listed under dirs_added (got {res.get("dirs_added")})')


GROUPS: list = [long_line_is_linear, archives_and_logs, local_path_false_positives, loaded_by_reach, log_ids_and_grouped_paths,
                rust_tests_modelled, required_limitations, residue_directories]


def empty_data_dir() -> None:
    """A --data-dir (or --evidence-dir) that yields no source says so, naming what it held, instead of printing nothing."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        write(root, {'tests/test_x.py': 'def test_x():\n    assert True\n', 'media/README.md': '# Media\n\nNot committed.\n'})
        invd = inv.build(root, None, {}, ['media', 'nowhere'])
    ws = invd.get('warnings', [])
    check(any(w.startswith('media/') and 'no data source' in w and 'README.md' in w for w in ws),
          f'inventory: a --data-dir holding only documentation is named in a warning (got {ws})')
    check(any(w.startswith('nowhere/') and 'does not exist' in w for w in ws), 'inventory: a --data-dir that does not exist is named')


GROUPS.append(empty_data_dir)


def main(argv=None) -> int:
    """self_test.py [group ...]: all groups, or only the named ones."""
    names = list(sys.argv[1:] if argv is None else argv)
    for t in (fixture_dirs_by_default, commands_count_as_use, prose_formats, time_bombs_stay_in_project, code_is_not_an_email,
              near_duplicates_scale, capped_pairs_are_noted, placeholder_headline, arithmetic_time_bomb,
              baseline_on_audit_date, go_rules, url_credentials_and_cards, placeholders, coverage_and_drift, residue_outside_tree, live_endpoint_severity,
              big_data_tree, cwd_relative_paths, agent_personal_data_leak, empty_repository, data_dir_unknown_format,
              served_data_dir, data_dir_code, no_tests_statement, tests_found_field, runs_required, drift_nothing_to_check,
              write_summary_line_endings, *GROUPS):
        if names and t.__name__ not in names:
            continue
        try:
            t()
        except Exception as exc:  # a crash in one group is a failure, not the end of the run
            check(False, f'{t.__name__} raised {exc.__class__.__name__}: {exc}')
    print(f'\n{len(FAILS)} failure(s)' if FAILS else '\nall checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
