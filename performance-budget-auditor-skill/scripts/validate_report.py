#!/usr/bin/env python3
"""Validate a performance budget report against the schema, the measured runs and the rubric.

    python validate_report.py report.json --measurements run/measurements.json --candidates run/candidates.json
        [--budget budget.json] [--plan plan.json] [--regressions run/regressions.json] [--write-summary]

Structural, not exact timings: lab numbers depend on the machine, so the check is that every number
follows from the recorded runs and every rule holds. Recomputed and compared:
  * every measurement's statistics and status from its runs (attribute.py), and the candidates;
  * each finding's metric, pages, cause, severity, confidence, reach, leverage, score and priority;
  * the headline: verdict, over-budget counts, unstable count, evidence completeness, confidence;
  * regressions, when --regressions is given.

Also fails when:
  * a measurement has fewer runs than configured without a recorded reason, or an unstable metric is
    reported as within or used for a finding; a finding has low confidence (it belongs in needs_review);
  * the verdict is within_budget_in_lab while a measurement is over, or the summary or a title calls
    the site fast, performant, snappy or similar; a within verdict lacks "field performance not
    established";
  * data_kind isn't lab, field data appears in the measurements, or field data isn't labelled field;
  * INP is reported for a page without an interaction, or LCP/CLS/INP/TBT for a non-Chromium browser;
  * an evidence trace or filmstrip file is missing; a candidate is missing from triage or triaged twice;
  * web-vitals isn't the pinned version and hash, or Lighthouse isn't recorded as ran/not_run/errored;
  * the run recorded its third-party mode and scope.third_party doesn't match it, or third-party requests
    were blocked and no limitation says third-party cost and behaviour were not measured;
  * scope.min_runs / max_runs differ from the config, or a target's variants or setup differ from the sample;
  * first-party requests were blocked (beacons, CSP reports, telemetry) and scope.blocked_first_party
    doesn't list them or no limitation names them; cdn_cache or request_summary don't follow from the runs;
  * a target is not on loopback and no limitation says the cold runs had a warm CDN (the warm-up warms it);
  * an unstable measurement reached the run cap and its note doesn't say so; a finding is site_wide with
    one template.

Without --budget, the budget file recorded in candidates.json (budget.path, relative to it) is used,
so the check reads the same budget attribute.py did.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import attribute as at  # noqa: E402
import compare as cmp  # noqa: E402
import schema_check  # noqa: E402

PRAISE = re.compile(r'\b(fast|faster|fastest|performant|snappy|speedy|blazing|lightning|quick|instant|optimi[sz]ed)\b', re.I)
THIRD_PARTY = re.compile(r'third[- ]part', re.I)
NOT_MEASURED = re.compile(r'\bnot (been )?measured\b|\bunmeasured\b', re.I)
WITHIN_PHRASE = 'field performance not established'
CDN = re.compile(r'\bCDN\b|edge cache', re.I)
FIRST_PARTY = re.compile(r'first[- ]party', re.I)
BLOCKED = re.compile(r'\bblock', re.I)
FINDING_FIELDS = ('metric', 'template', 'templates', 'site_wide', 'pages', 'cause', 'severity', 'confidence', 'reach', 'leverage', 'score', 'priority',
                  'critical_path', 'worst', 'measurements', 'attribution', 'evidence')


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    return host in ('localhost', '::1') or host.startswith('127.') or host.endswith('.localhost')


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def expected_headline(rep: dict, meas: list[dict], doc: dict) -> dict:
    over = {}
    for m in meas:
        if m['status'] == 'over':
            over[m['metric']] = over.get(m['metric'], 0) + 1
    unstable = sum(m['status'] == 'unstable' for m in meas)
    partial = (unstable or any(m['note'] for m in meas) or doc['calibration']['busy'] or
               any(t['status'] == 'errored' for t in rep['metadata']['tools']) or bool(rep['needs_review']))
    completeness = 'partial' if partial else 'complete'
    if completeness == 'partial' or any(f['confidence'] != 'high' for f in rep['findings']):
        conf = 'medium'
    else:
        conf = 'high'
    if not any(m['status'] in ('within', 'over') for m in meas):
        conf = 'low'
    return {'verdict': 'over_budget' if over else 'within_budget_in_lab', 'over_budget_counts': over, 'unstable_metrics': unstable,
            'evidence_completeness': completeness, 'overall_confidence': conf}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--measurements', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--budget', type=Path)
    ap.add_argument('--plan', type=Path)
    ap.add_argument('--regressions', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    rep, doc, cands_doc = load(a.report), load(a.measurements), load(a.candidates)
    errs: list[str] = []
    schema = load(HERE.parent / 'schemas' / 'perf_report.schema.json')
    errs += [f'schema: {e}' for e in schema_check.errors(rep, schema)]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1
    base = a.measurements.parent
    sample = {s['url']: s for s in doc['sample']}

    # scope and metadata
    if [t['url'] for t in rep['scope']['targets']] != [s['url'] for s in doc['sample']]:
        errs.append('scope.targets do not match the measured sample')
    for t in rep['scope']['targets']:
        s = sample.get(t['url'], {})
        if t['template'] != s.get('template') or t['critical_path'] != bool(s.get('critical_path')) or t['interaction'] != s.get('interaction'):
            errs.append(f'scope.targets: {t["url"]} differs from the sample')
    if rep['scope']['runs'] != doc['config']['runs']:
        errs.append('scope.runs does not match the configured runs')
    _, min_runs, cap = at.run_limits(doc)
    if rep['scope']['min_runs'] != min_runs or rep['scope']['max_runs'] != cap:
        errs.append(f'scope.min_runs / max_runs should be {min_runs} / {cap} (from the measurements config)')
    for t in rep['scope']['targets']:
        s = sample.get(t['url'], {})
        want = [v['name'] for v in s.get('variants') or []]
        if want != (t.get('variants') or []):
            errs.append(f'scope.targets: {t["url"]} variants should be {want}')
        if (t.get('setup') or None) != (s.get('setup') or None):
            errs.append(f'scope.targets: {t["url"]} setup should be {json.dumps(s.get("setup"))}')
    remote = [t['url'] for t in rep['scope']['targets'] if not is_loopback(t['url'])]
    if remote and not any(CDN.search(x) for x in rep['limitations']):
        errs.append('limitations: a remote target is measured; say that "cold" is a cold browser cache, not a cold CDN '
                    '(the warm-up warms any CDN or edge cache), and point to cdn_cache')
    if rep['metadata']['machine'] != doc['machine'] or rep['metadata']['calibration'] != doc['calibration']:
        errs.append('metadata.machine / calibration do not match the measurements')
    browsers = sorted({r['browser'] for r in doc['runs']})
    if sorted(rep['scope']['browsers']) != browsers:
        errs.append(f'scope.browsers should be {browsers}')
    lock = load(HERE.parent / 'data' / 'assets.lock.json')['assets'][0]
    wv = next((t for t in rep['metadata']['tools'] if t['name'] == 'web-vitals'), None)
    if not wv or wv['version'] != lock['version'] or wv['sha256'] != lock['sha256']:
        errs.append('metadata.tools: web-vitals must be the pinned version and SHA-256 from data/assets.lock.json')
    if not any(t['name'].lower().startswith('lighthouse') for t in rep['metadata']['tools']):
        errs.append('metadata.tools: record Lighthouse as ran, not_run or errored')
    bpath = a.budget
    if bpath is None and cands_doc.get('budget', {}).get('path'):
        bpath = a.candidates.parent / cands_doc['budget']['path']
        if not bpath.is_file():
            errs.append(f'candidates.json names budget {cands_doc["budget"]["path"]}, which does not exist; pass --budget')
            bpath = None
    budget = at.Budget(bpath)
    if rep['scope']['budget']['source'] != budget.source:
        errs.append(f'scope.budget.source should be {budget.source!r}')

    # measurements
    meas = at.measurements(doc, budget)
    if rep['measurements'] != meas:
        mine = {(m['url'], m['profile'], m['cache'], m['browser'], m['metric']): m for m in rep['measurements']}
        for m in meas:
            k = (m['url'], m['profile'], m['cache'], m['browser'], m['metric'])
            got = mine.get(k)
            if got is None:
                errs.append(f'measurements: {k} is missing')
            elif got != m:
                diff = [f for f in m if got.get(f) != m[f]]
                errs.append(f'measurements: {k[0]} {k[1]} {k[4]}: {", ".join(diff)} do not follow from the runs '
                            f'(expected status {m["status"]}, median {m["median"]}, cv {m["cv"]})')
        if len(mine) != len(meas):
            errs.append('measurements: entries that are not in the runs')
    for m in rep['measurements']:
        if m['metric'] == 'INP' and m['status'] in ('within', 'over', 'unstable') and not sample.get(m['url'], {}).get('interaction'):
            errs.append(f'measurements: INP reported for {m["url"]}, which has no interaction')
        if m['browser'] != 'chromium' and m['metric'] in at.CHROMIUM_ONLY and m['status'] != 'not_supported':
            errs.append(f'measurements: {m["metric"]} in {m["browser"]} must be not_supported')
        if m['status'] == 'unstable' and m['runs_used'] >= m['run_cap'] and 'unstable at the cap' not in (m['note'] or ''):
            errs.append(f'measurements: {m["url"]} {m["metric"]} is unstable at the run cap; its note must say so')
        if m.get('kind') == 'field' or 'field' in str(m.get('source', '')).lower():
            errs.append('measurements: field data must not be mixed into lab measurements')
    tp = at.third_party(doc)
    if tp is not None or rep['scope'].get('third_party') is not None:
        if rep['scope'].get('third_party') != tp:
            errs.append(f'scope.third_party should be {json.dumps(tp)[:200]} (from the measurements config and blocked requests)')
    if tp and tp['blocked'] and not any(THIRD_PARTY.search(x) and NOT_MEASURED.search(x) for x in rep['limitations']):
        errs.append(f'limitations: {sum(b["requests"] for b in tp["blocked"])} third-party request(s) were blocked; say that '
                    'third-party cost and behaviour were not measured')
    bfp = at.blocked_first_party(doc)
    if rep['scope'].get('blocked_first_party', []) != bfp:
        errs.append(f'scope.blocked_first_party should be {json.dumps(bfp)[:200]} (the blocked non-third-party requests)')
    if bfp and not any(FIRST_PARTY.search(x) and BLOCKED.search(x) for x in rep['limitations']):
        errs.append(f'limitations: {sum(b["requests"] for b in bfp)} first-party request(s) were blocked (beacons, reports, '
                    'telemetry); say so and that their cost and the handling of the failures are not measured')
    for key, fn in (('cdn_cache', at.cdn_cache), ('request_summary', at.request_summary)):
        want = fn(doc)
        if rep.get(key, []) != want:
            errs.append(f'{key} does not follow from the runs (attribute.{key})')
    if cands_doc['measurements'] != meas:
        errs.append('candidates.json is stale; rerun attribute.py')
    if rep['server_timing'] != at.server_timing(doc):
        errs.append('server_timing does not follow from the runs')

    # candidates, triage, findings
    fresh = at.candidates(doc, meas)
    if json.dumps(fresh, sort_keys=True) != json.dumps(cands_doc['candidates'], sort_keys=True):
        errs.append('candidates.json does not match the runs; rerun attribute.py')
    cands = {c['id']: c for c in cands_doc['candidates']}
    seen = {}
    for t in rep['triage']:
        if t['candidate_id'] in seen:
            errs.append(f'triage: {t["candidate_id"]} is triaged twice')
        seen[t['candidate_id']] = t
        if t['candidate_id'] not in cands:
            errs.append(f'triage: {t["candidate_id"]} is not a candidate')
    for cid in cands:
        if cid not in seen:
            errs.append(f'triage: candidate {cid} is missing')
    findings = {f['id']: f for f in rep['findings']}
    review = {n['candidate_id'] for n in rep['needs_review']}
    for t in rep['triage']:
        c = cands.get(t['candidate_id'])
        if t['decision'] == 'promoted':
            f = findings.get(t['finding_id'] or '')
            if not f or f['candidate_id'] != t['candidate_id']:
                errs.append(f'triage: {t["candidate_id"]} is promoted to {t["finding_id"]}, which is not its finding')
            if c and c['confidence'] == 'low':
                errs.append(f'triage: {t["candidate_id"]} has low confidence (unstable or noisy): it belongs in needs_review')
        elif t['finding_id'] is not None:
            errs.append(f'triage: {t["candidate_id"]} is {t["decision"]} but names {t["finding_id"]}')
        if t['decision'] == 'needs_review' and t['candidate_id'] not in review:
            errs.append(f'triage: {t["candidate_id"]} is needs_review but not in needs_review[]')
    for cid in review:
        if seen.get(cid, {}).get('decision') != 'needs_review':
            errs.append(f'needs_review: {cid} is not triaged as needs_review')
    for f in rep['findings']:
        c = cands.get(f['candidate_id'])
        if not c:
            errs.append(f'{f["id"]}: candidate {f["candidate_id"]} does not exist')
            continue
        for k in FINDING_FIELDS:
            if f[k] != c[k]:
                errs.append(f'{f["id"]}: {k} does not match candidate {c["id"]} (the runs give {json.dumps(c[k])[:80]})')
        if not any(m['status'] == 'over' for m in f['measurements']):
            errs.append(f'{f["id"]}: no measurement in it is over budget (unstable metrics can\'t make a finding)')
        for ev in f['evidence']:
            for p in [ev.get('trace')] + list(ev.get('filmstrip') or []):
                if p and not (base / p).is_file():
                    errs.append(f'{f["id"]}: evidence file {p} is missing')
            for rid in ev['run_ids']:
                if not any(r['id'] == rid for r in doc['runs']):
                    errs.append(f'{f["id"]}: run {rid} is not in the measurements')
        if f['site_wide'] != (len(set(f['templates'])) > 1) or (f['template'] is None) != f['site_wide']:
            errs.append(f'{f["id"]}: site_wide must be true exactly when templates names more than one (and template is then null)')
        if PRAISE.search(f['title']):
            errs.append(f'{f["id"]}: the title uses praise words; describe the measurement instead')

    # regressions
    if a.regressions:
        reg = load(a.regressions)
        rows = [r for r in reg['comparisons'] if r['direction'] in ('regressed', 'improved')]
        bpath = a.regressions.parent / reg['baseline']
        if not bpath.is_file():
            errs.append(f'regressions.json names a baseline that does not exist: {bpath}')
        elif cmp.compare(load(bpath), cands_doc) != reg['comparisons']:
            errs.append('regressions.json does not follow from the two candidate files; rerun compare.py')
        if rep['regressions'] != rows:
            errs.append('regressions should list the regressed and improved comparisons from regressions.json')
    elif rep['regressions']:
        errs.append('regressions are reported without --regressions')

    # field data
    if rep['field_data'] is not None and rep['field_data'].get('kind') != 'field':
        errs.append('field_data must be labelled kind: field')

    # headline
    hl = rep['headline']
    exp = expected_headline(rep, meas, doc)
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if PRAISE.search(hl['summary']):
        errs.append('headline.summary uses praise words ("fast", "performant", ...); lab data can only be over or within a budget')
    if hl['verdict'] == 'within_budget_in_lab' and WITHIN_PHRASE not in hl['summary'].lower():
        errs.append(f'headline.summary must say "under the tested profiles; {WITHIN_PHRASE}"')
    if 'lab' not in hl['summary'].lower():
        errs.append('headline.summary must say the numbers are lab data')

    if a.write_summary and not errs:
        a.report.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'headline written to {a.report}')
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} error(s)')
        return 1
    print(f'VALID: {len(rep["measurements"])} measurement(s), {len(rep["findings"])} finding(s), {len(rep["triage"])} candidate(s) '
          f'triaged, verdict {hl["verdict"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
