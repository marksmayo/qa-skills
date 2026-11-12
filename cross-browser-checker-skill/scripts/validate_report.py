#!/usr/bin/env python3
"""Validate a cross-browser report against the schema, the recorded runs and the rubric.

    python validate_report.py report.json --runs run/capture/runs.json --candidates run/candidates.json
        [--features run/features.json] [--targets targets.json] [--write-summary]

Recomputed and compared: the candidates (compare_engines.py run again on the recorded runs, with the
same feature scan), each difference's ratings and runs, the feature findings and their links, the
flaky list, and the headline (verdict, counts, checks planned and run, completeness, confidence).

Also fails when:
  * a difference's outcomes are not consistent across its reruns (every run of each engine the same),
    or it lacks evidence (screenshots that exist and match runs.json) from the reference and every
    affected engine;
  * "Safari" appears as an engine or in a difference title, or out_of_scope omits Safari and real devices;
  * the verdict isn't the one the runs support: differences_found with a promoted difference, else inconclusive
    when any planned engine comparison wasn't made (an engine not run or errored, a run errored or skipped; see
    compare_engines.comparisons), else no_differences_detected; in particular no_differences_detected with no
    comparison made;
  * an inconclusive summary doesn't say so (errored, skipped, not compared) or claims no differences; comparisons
    weren't made and no limitation says so;
  * any text says "compatible", "works in all browsers" or similar;
  * overall confidence isn't low when no comparison was made, or fewer than half of the comparisons or runs;
  * the capture recorded its settings (bypass_csp, reduced motion, rate limit, request budget) or request counts and
    scope.capture / scope.requests don't equal them; motion was forced or frozen and out_of_scope doesn't say motion
    wasn't compared; the feature scan listed limitations (utility-class CSS not scanned) the report doesn't carry;
  * a feature finding is confirmed_at_runtime with no linked difference, or possible with one;
  * a candidate is missing from triage or triaged twice;
  * a candidate that matches a known Playwright engine-build limitation (data/engine-limitations.json) is promoted:
    it goes to needs_review until a real browser confirms it;
  * the capture recorded its third-party mode and scope.third_party doesn't match it, or third-party requests were
    blocked and no limitation says their behaviour wasn't compared.

Checks planned are the (page or state, viewport) pairs runs.json records as "planned" (capture.py writes them,
honouring per-page and per-state viewports), or the same pairs from --targets (which must then agree), or, for an
older capture with neither, the pairs that ran; times engines and reruns. Sentences are split at a full stop followed
by a space, so "Safari 16.4" stays one sentence.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare_engines as ce  # noqa: E402
import schema_check  # noqa: E402

COMPAT = re.compile(r'\b(compatible|works (fine )?in all browsers|cross-browser (safe|ready)|fully supported everywhere|no browser issues)\b', re.I)
INCONCLUSIVE_SAYS = re.compile(r'inconclusive|errored|not compared|could not|couldn.t|did not (run|capture|load)|skipped|'
                               r'not captured|no comparison', re.I)
NO_DIFF_CLAIM = re.compile(r'\b(no (\w+ )?differences (were |was )?(found|detected|seen)|no differences detected|'
                           r'all (engines|browsers) (match|agree|behave the same))\b', re.I)
FIELDS = ('kind', 'subkind', 'page', 'states', 'element', 'severity', 'confidence', 'priority', 'score', 'usage_share', 'runs')


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def sentences(text: str) -> list[str]:
    """Split at ., ! or ? followed by whitespace (and at line breaks), so decimals and versions ("Safari 16.4", "1.5x") stay
    inside their sentence."""
    return [x for x in re.split(r'(?<=[.!?])\s+|\n+', text) if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--runs', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--features', type=Path)
    ap.add_argument('--targets', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    ap.add_argument('--jobs', type=int, default=ce.default_jobs(), help='screenshots decoded in parallel when recomputing')
    a = ap.parse_args()
    rep, runs, cands_doc = load(a.report), load(a.runs), load(a.candidates)
    errs: list[str] = []
    errs += [f'schema: {e}' for e in schema_check.errors(rep, load(HERE.parent / 'schemas' / 'cross_browser_report.schema.json'))]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1
    sc, hl = rep['scope'], rep['headline']
    run_dir = a.runs.parent

    # recompute candidates
    features = load(a.features) if a.features else None
    try:
        from fetch_data import asset_path
        caniuse = load(asset_path('caniuse-db'))
    except FileNotFoundError:
        caniuse = None
        print('warning: caniuse data not in the cache; usage shares are not rechecked')
    critical = {r['page'] for r in runs['runs'] if r.get('critical_path')}
    diffs, flaky, dismissed = ce.compare(runs, run_dir, cands_doc['reference'], a.jobs)
    fresh = ce.candidates(diffs, dismissed, runs, caniuse, features, critical)
    if caniuse is not None and json.dumps(fresh, sort_keys=True) != json.dumps(cands_doc['candidates'], sort_keys=True):
        errs.append('candidates.json does not follow from runs.json (and the feature scan); rerun compare_engines.py')
    if flaky != cands_doc['flaky']:
        errs.append('the flaky list does not follow from runs.json')
    if rep['flaky'] != cands_doc['flaky']:
        errs.append('flaky differs from candidates.json')
    if rep['dismissed_automatically'] != len(cands_doc['dismissed_automatically']):
        errs.append('dismissed_automatically should be the number of automatic dismissals in candidates.json')

    # scope
    names = [e['name'] for e in sc['engines']]
    if any('safari' in n.lower() for n in names):
        errs.append('scope.engines: Playwright WebKit is not Safari; Safari must not be listed as an engine')
    oos = ' '.join(sc['out_of_scope']).lower()
    if 'safari' not in oos or 'device' not in oos:
        errs.append('scope.out_of_scope must include Safari and real devices')
    if sc['engines'] != runs['engines']:
        errs.append('scope.engines does not match the engines in runs.json')
    if sc['reruns'] != runs['reruns'] or sc['reference_engine'] != cands_doc['reference']:
        errs.append('scope.reruns / reference_engine do not match the run')

    # triage and differences
    cands = {c['id']: c for c in cands_doc['candidates']}
    seen = {}
    for t in rep['triage']:
        if t['candidate_id'] in seen:
            errs.append(f'triage: {t["candidate_id"]} is triaged twice')
        seen[t['candidate_id']] = t
    for cid in cands:
        if cid not in seen:
            errs.append(f'triage: candidate {cid} is missing')
    diffs_by_id = {d['id']: d for d in rep['differences']}
    for t in rep['triage']:
        lim = (cands.get(t['candidate_id']) or {}).get('build_limitations')
        if t['decision'] == 'promoted' and lim:
            errs.append(f'triage: {t["candidate_id"]} matches a known Playwright build limitation ({", ".join(lim)}): '
                        'put it in needs_review and confirm it in the real browser')
        if t['decision'] == 'promoted':
            d = diffs_by_id.get(t['difference_id'] or '')
            if not d or d['candidate_id'] != t['candidate_id']:
                errs.append(f'triage: {t["candidate_id"]} is promoted to {t["difference_id"]}, which is not its difference')
        elif t['difference_id'] is not None:
            errs.append(f'triage: {t["candidate_id"]} is {t["decision"]} but names {t["difference_id"]}')
        if t['decision'] == 'needs_review' and t['candidate_id'] not in {n['candidate_id'] for n in rep['needs_review']}:
            errs.append(f'triage: {t["candidate_id"]} is needs_review but not in needs_review[]')
    shots = {r['screenshot']: r.get('screenshot_sha256') for r in runs['runs'] if r.get('screenshot')}
    for d in rep['differences']:
        c = cands.get(d['candidate_id'])
        if not c:
            errs.append(f'{d["id"]}: candidate {d["candidate_id"]} does not exist')
            continue
        for k in FIELDS:
            if d[k] != c[k]:
                errs.append(f'{d["id"]}: {k} differs from candidate {c["id"]}')
        if sorted(d['engines_affected']) != sorted(c['engines']) or d['reference'] != c['reference']:
            errs.append(f'{d["id"]}: engines_affected / reference differ from candidate {c["id"]}')
        for key, outcomes in d['runs'].items():
            if len(set(outcomes)) != 1:
                errs.append(f'{d["id"]}: outcomes for {key} are not the same in every rerun {outcomes}; that is flaky, not a difference')
        for state in d['states']:
            ref_o = d['runs'].get(f'{state}|{d["reference"]}')
            for eng in d['engines_affected']:
                eo = d['runs'].get(f'{state}|{eng}')
                if ref_o is not None and eo is not None and ref_o == eo:
                    errs.append(f'{d["id"]}: {eng} and {d["reference"]} have the same outcomes in {state}')
        ev_engines = {e['engine'] for e in d['evidence']}
        for need in set(d['engines_affected']) | {d['reference']}:
            if need not in ev_engines:
                errs.append(f'{d["id"]}: no evidence from {need}')
        for e in d['evidence']:
            for s in e['screenshots']:
                f = run_dir / s
                if not f.is_file():
                    errs.append(f'{d["id"]}: screenshot {s} is missing')
                elif shots.get(s) != hashlib.sha256(f.read_bytes()).hexdigest():
                    errs.append(f'{d["id"]}: screenshot {s} does not match runs.json')
        if re.search(r'\bsafari\b', d['title'], re.I):
            errs.append(f'{d["id"]}: the title names Safari; the engine tested is Playwright\'s WebKit')
        for text in (d['title'], d['remediation']):
            if COMPAT.search(text):
                errs.append(f'{d["id"]}: claims compatibility')

    # features
    if features:
        fmap = {f['feature']: f for f in cands_doc['features']['features']} if cands_doc.get('features') else {}
        if [f['feature'] for f in rep['feature_findings']] != [f['feature'] for f in fmap.values()]:
            errs.append('feature_findings do not match the feature scan')
        for f in rep['feature_findings']:
            src = fmap.get(f['feature'])
            if src and (f['status'] != src['status'] or f['linked_differences'] != src['linked_differences']):
                errs.append(f'feature {f["feature"]}: status or links differ from candidates.json')
            if f['status'] == 'confirmed_at_runtime' and not f['linked_differences']:
                errs.append(f'feature {f["feature"]}: confirmed_at_runtime with no linked difference')
            if f['status'] == 'possible' and f['linked_differences']:
                errs.append(f'feature {f["feature"]}: possible but linked to a difference')

    # third parties
    tp = ce.third_party(runs)
    if tp is not None or sc.get('third_party') is not None:
        if sc.get('third_party') != tp:
            errs.append(f'scope.third_party should be {json.dumps(tp)[:200]} (from runs.json capture settings and blocked requests)')
    if tp and tp['blocked'] and not any(re.search(r'third[- ]part', x, re.I) and re.search(r'\bnot\b', x, re.I) for x in rep['limitations']):
        errs.append(f'limitations: {sum(b["requests"] for b in tp["blocked"])} third-party request(s) were blocked; say that '
                    'third-party behaviour was not compared')

    # headline
    cmp = ce.comparisons(runs, cands_doc['reference'])
    if 'comparisons' in cands_doc and cands_doc['comparisons'] != cmp:
        errs.append('candidates.json comparisons do not follow from runs.json; rerun compare_engines.py')
    planned_engines = [e for e in sc['engines']]
    plan = runs.get('planned')
    if a.targets:
        t_plan = ce.planned_checks(load(a.targets))
        if plan is not None and t_plan != plan:
            errs.append('targets.json plans other checks than runs.json records as planned')
        plan = t_plan
    if plan is None:   # a capture that didn't record its plan: count what ran
        plan = [{'page': p, 'viewport': v} for p, v in sorted({(r['page'], r['viewport']) for r in runs['runs']})]
    n_states = len(plan)
    incomplete = cmp['made'] < cmp['planned']
    exp = {'verdict': 'differences_found' if rep['differences'] else 'inconclusive' if incomplete else 'no_differences_detected',
           'severity_counts': dict(Counter(d['severity'] for d in rep['differences'])), 'flaky_count': len(rep['flaky']),
           'checks': {'planned': n_states * len(planned_engines) * sc['reruns'], 'ran': sum(1 for r in runs['runs'] if r['ok']),
                      'comparisons_planned': cmp['planned'], 'comparisons_made': cmp['made']}}
    not_run = [e['name'] for e in sc['engines'] if e['status'] != 'ran']
    partial = bool(not_run or incomplete or rep['flaky'] or rep['needs_review'] or exp['checks']['ran'] < exp['checks']['planned']
                   or (features and any(f['status'] == 'not_assessed' for f in rep['feature_findings'])))
    exp['evidence_completeness'] = 'partial' if partial else 'complete'
    chk = exp['checks']
    thin = cmp['made'] == 0 or cmp['made'] * 2 < cmp['planned'] or chk['ran'] * 2 < chk['planned']
    exp['overall_confidence'] = ('low' if thin else
                                 'medium' if partial or any(d['confidence'] != 'high' for d in rep['differences']) else 'high')
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if hl['verdict'] == 'no_differences_detected' and cmp['made'] == 0:
        errs.append('no_differences_detected with no engine comparison made: nothing was compared, so the verdict is inconclusive')
    elif hl['verdict'] == 'no_differences_detected' and (not_run or incomplete):
        errs.append(f'no_differences_detected while {", ".join(not_run) or "some runs"} did not run or capture: the verdict is inconclusive')
    if hl['verdict'] == 'inconclusive' or exp['verdict'] == 'inconclusive':
        if not INCONCLUSIVE_SAYS.search(hl['summary']):
            errs.append('headline.summary must say the check was inconclusive (runs errored, were skipped or not compared)')
        if NO_DIFF_CLAIM.search(hl['summary']):
            errs.append('headline.summary claims no differences, but the comparison was inconclusive')
    if cmp['not_made'] and not any(INCONCLUSIVE_SAYS.search(x) for x in rep['limitations']):
        errs.append(f'limitations: {len(cmp["not_made"])} engine comparison(s) were not made (runs errored or skipped); '
                    'say which and why')
    cap = runs.get('capture') or {}
    if 'bypass_csp' in cap and sc.get('capture') != cap:
        errs.append('scope.capture should equal runs.json capture settings')
    if 'requests' in runs and sc.get('requests') != runs['requests']:
        errs.append('scope.requests should equal runs.json requests (sent, by engine, budget, back-offs)')
    if (cap.get('reduced_motion') == 'reduce' or cap.get('freeze_animations')) and not re.search(r'motion|animation', oos, re.I):
        errs.append('scope.out_of_scope must say motion was not compared (the capture forced reduced motion or froze animations)')
    for lim in (features or {}).get('limitations') or []:
        if lim not in rep['limitations']:
            errs.append(f'limitations: the feature scan says "{lim[:80]}..."; carry it into the report')
    if COMPAT.search(hl['summary']):
        errs.append('headline.summary claims compatibility; the best verdict is "no differences detected" for these pages and engines')
    for sentence in sentences(hl['summary']):
        if re.search(r'\bsafari\b', sentence, re.I) and not re.search(r'\bnot\b|\bisn.t\b|\bwasn.t\b|untested', sentence, re.I):
            errs.append(f'headline.summary mentions Safari as if it was tested: "{sentence[:120]}"')

    if a.write_summary and not errs:
        a.report.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'headline written to {a.report}')
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} error(s)')
        return 1
    print(f'VALID: {len(rep["differences"])} difference(s), {len(rep["flaky"])} flaky, {len(rep["feature_findings"])} feature finding(s), '
          f'verdict {hl["verdict"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
