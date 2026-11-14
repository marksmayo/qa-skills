"""Benchmarks for skill-specific scanners and scorers, run on the example fixtures shipped in the repo."""
from __future__ import annotations

import itertools
import re

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
