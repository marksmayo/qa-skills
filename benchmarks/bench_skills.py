"""Benchmarks for skill-specific scanners and scorers, run on the example fixtures shipped in the repo."""
from __future__ import annotations

import copy
import itertools
import json
import re
from datetime import date

from conftest import ROOT, load_script

cvss4 = load_script('code-security-auditor-skill', 'cvss4')
sec_scan = load_script('code-security-auditor-skill', 'scan_pitfalls')
ui_scan = load_script('ui-crash-hunter-skill', 'scan_pitfalls')
lang_census = load_script('code-security-auditor-skill', 'lang_census')


def test_cvss4_score_vectors(benchmark):
    vectors = [
        f'CVSS:4.0/AV:{av}/AC:{ac}/AT:N/PR:{pr}/UI:{ui}/VC:{vc}/VI:{vi}/VA:N/SC:N/SI:N/SA:N'
        for av, ac, pr, ui, vc, vi in itertools.product('NALP', 'LH', 'NLH', 'NPA', 'HLN', 'HL')
    ]

    def run():
        return [cvss4.severity(cvss4.score(v)) for v in vectors]
    assert benchmark(run)


def test_security_scan_pitfalls_invoicer(benchmark):
    catalogue = sec_scan.load_catalogue()
    root = ROOT / 'code-security-auditor-skill' / 'examples' / 'invoicer'
    result = benchmark(sec_scan.scan, root, catalogue)
    assert result['status'] == 'ran'


def test_security_scan_pitfalls_ferry_ops(benchmark):
    catalogue = sec_scan.load_catalogue()
    root = ROOT / 'code-security-auditor-skill' / 'examples' / 'ferry-ops'
    result = benchmark(sec_scan.scan, root, catalogue)
    assert result['status'] == 'ran'


def _ui_pitfalls(catalogue: dict) -> list[dict]:
    """Compile the catalogue the way scan_pitfalls.main() does (every pack, no framework filter)."""
    active = []
    for p in catalogue['pitfalls']:
        if not p.get('regex') or p.get('requires_framework'):
            continue
        q = dict(p)
        q['_rx'] = re.compile(p['regex'])
        for k in ('suppress_before', 'suppress_after', 'require_before', 'suppress_line'):
            if p.get(k):
                q['_' + k] = re.compile(p[k])
        active.append(q)
    return active


def test_ui_crash_scan_text_pantry(benchmark):
    pitfalls = _ui_pitfalls(ui_scan.load_catalogue())
    src = ROOT / 'ui-crash-hunter-skill' / 'examples' / 'pantry' / 'src'
    texts = [(p.read_text(encoding='utf-8'), p.relative_to(src).as_posix(), p.suffix)
             for p in sorted(src.rglob('*')) if p.suffix in ('.js', '.jsx', '.ts', '.tsx')]
    assert texts

    def run():
        return [ui_scan.scan_text(t, rel, pitfalls, suffix) for t, rel, suffix in texts]
    assert any(benchmark(run))


def test_ui_crash_mask_code(benchmark):
    src = ROOT / 'ui-crash-hunter-skill' / 'examples' / 'pantry' / 'src'
    texts = [p.read_text(encoding='utf-8') for p in sorted(src.rglob('*')) if p.suffix in ('.js', '.jsx', '.ts', '.tsx')]
    benchmark(lambda: [ui_scan.mask_code(t) for t in texts])


def test_lang_census_beacon_cli(benchmark):
    """Census of a fixed fixture with vendored header trees (not the whole repo, which changes every commit)."""
    result = benchmark(lang_census.census, ROOT / 'code-security-auditor-skill' / 'examples' / 'beacon-cli')
    assert result['excluded']


# --- static scanners on the sample repositories (each walks a fixed fixture) ------------------

flaky_scan = load_script('flaky-test-hunter-skill', 'scan_pitfalls')
dead_scan = load_script('dead-test-finder-skill', 'scan_tests')
data_scan = load_script('test-data-auditor-skill', 'scan_data')
surface = load_script('journey-mapper-skill', 'extract_surface')
imports = load_script('test-impact-selector-skill', 'import_graph')


def test_flaky_scan_pitfalls_ferry_booking(benchmark):
    data = json.loads(flaky_scan.DATA.read_text(encoding='utf-8'))
    root = ROOT / 'flaky-test-hunter-skill' / 'examples' / 'ferry-booking'
    assert benchmark(flaky_scan.scan, root, data)


def test_dead_test_scan_ledger(benchmark):
    """Python and TypeScript test bodies: assertion search, skip guards and near-duplicate (LCS) comparison."""
    result = benchmark(dead_scan.scan, ROOT / 'dead-test-finder-skill' / 'examples' / 'ledger')
    assert result['candidates']


def test_test_data_scan_clinic_api(benchmark):
    """Personal data, secrets, time bombs and duplicates in test data (Luhn, IBAN, entropy checks)."""
    root = ROOT / 'test-data-auditor-skill' / 'examples' / 'clinic-api'
    inventory = json.loads((ROOT / 'test-data-auditor-skill' / 'examples' / 'run' / 'inventory.json')
                           .read_text(encoding='utf-8'))
    result = benchmark(lambda: data_scan.Scanner(root, inventory, date(2026, 11, 11)).run())
    assert result['candidates']


def test_journey_extract_surface_kite_notes(benchmark):
    """Routes, API endpoints and goals of a Next.js app (App Router pages, route handlers, middleware)."""
    root = ROOT / 'code-security-auditor-skill' / 'examples' / 'kite-notes'
    result = benchmark(lambda: surface.Extractor(root, 20000).run())
    assert result['stats']['counts']['endpoints']


def test_impact_import_graph_harbourline(benchmark):
    """Python and TypeScript import graph of a monorepo, the input to every test selection."""
    root = ROOT / 'test-impact-selector-skill' / 'examples' / 'harbourline'
    graph = benchmark(imports.build, root)
    assert graph


# --- pure text and number crunching ------------------------------------------------------------

icu = load_script('i18n-checker-skill', 'icu_lite')
lint = load_script('ticket-readiness-reviewer-skill', 'lint_criteria')
wcag_merge = load_script('wcag-auditor-skill', 'merge_findings')
extract = load_script('ci-failure-triager-skill', 'extract_failures')
detect = load_script('hypercare-anomaly-watcher-skill', 'detect')
evaluation = load_script('test-suite-evaluator-skill', 'validate_evaluation')


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def test_icu_parse_bookclub_locales(benchmark):
    locales = ROOT / 'i18n-checker-skill' / 'examples' / 'bookclub' / 'site' / 'locales'
    messages = [s for p in sorted(locales.glob('*.json'))
                for s in _strings(json.loads(p.read_text(encoding='utf-8')))]
    assert any('{' in m for m in messages)

    def run():
        return [(icu.parse(m), icu.placeholders(m)) for m in messages]
    benchmark(run)


def test_lint_criteria_tickets(benchmark):
    data = json.loads((ROOT / 'ticket-readiness-reviewer-skill' / 'examples' / 'tickets.json').read_text(encoding='utf-8'))
    lex = lint.load_lexicon()
    result = benchmark(lambda: [lint.lint_ticket(t, lex, {}) for t in data['tickets']])
    assert any(r['flags'] for r in result)


def test_wcag_merge_findings(benchmark):
    """Deduplicating engine results into candidates, including the colour parsing and contrast maths."""
    items = json.loads((ROOT / 'wcag-auditor-skill' / 'examples' / 'run' / 'items.json').read_text(encoding='utf-8'))
    scs = {s['sc']: s for s in json.loads((wcag_merge.DATA / 'wcag-sc.json').read_text(encoding='utf-8'))['success_criteria']}
    cands = benchmark(lambda: wcag_merge.merge(copy.deepcopy(items['items']), scs))
    assert cands


def test_ci_extract_failures(benchmark):
    run_dir = ROOT / 'ci-failure-triager-skill' / 'examples' / 'run'
    run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
    assert benchmark(extract.extract, run_dir, run, 1)


def test_hypercare_detect_grid(benchmark):
    """Robust (median/MAD) and Poisson tail decisions over a week of hourly baselines."""
    base = [100 + (i * 37) % 23 - 11 for i in range(168)]
    counts = [3 + (i * 11) % 5 for i in range(168)]
    gauge = {'kind': 'gauge', 'direction': 'up'}
    count = {'kind': 'count', 'direction': 'up'}

    def run():
        return [detect.evaluate(v, base, gauge) for v in range(60, 160)] + \
               [detect.evaluate(k, counts, count) for k in range(0, 40)]
    assert benchmark(run)


def test_validate_evaluation_example(benchmark):
    """The rubric re-computation every evaluation goes through: schema, weights, caps, band and claim checks."""
    rep = json.loads((ROOT / 'test-suite-evaluator-skill' / 'examples' / 'evaluation.example.json').read_text(encoding='utf-8'))
    schema = json.loads(evaluation.SCHEMA.read_text(encoding='utf-8'))

    def run():
        return evaluation.schema_errors(rep, schema, schema), evaluation.check(rep)
    errs, (problems, _) = benchmark(run)
    assert not errs and not problems
