#!/usr/bin/env python3
"""Apply a release policy to a collection of reports: required evidence, de-duplication, rules, waivers, verdict.

    python apply_policy.py collection.json [--waivers waivers.json] [--as-of 2026-11-12] --out gate_report.json

Reads the collection from collect_reports.py and the policy it names (its SHA-256 must still match).
The verdict is decided in this order: no_go when a blocking rule fires on an issue that no valid waiver
covers; not_established when any required evidence is absent, invalid, stale or unverified (or its
validator was not run and the policy doesn't accept that); go otherwise, worded as "no blocking findings
in the evidence provided". Ratings are copied from the sources, never re-scored.

Findings from every counted source are used: valid reports, and reports whose validator was not run.
An invalid report doesn't count. A stale or unverified report's findings still block, because a known
blocker blocks until a fresh report shows it fixed.
"""
from __future__ import annotations
import argparse, fnmatch, hashlib, json, sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adapters import SEV_RANK, CONF_RANK, TYPES  # noqa: E402

PRIO_RANK = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
COMPATIBLE = [{'crash'}, {'security', 'privacy'}, {'security', 'test_data'}, {'accessibility'}, {'api', 'crash'}, {'performance'}, {'functional', 'crash'}]
HOW = {'ci-junit': 'Export the CI JUnit XML for the candidate commit, with a provenance sidecar naming the commit and build.'}
PERSONAL = {'personal_data_to_third_party', 'sensitive_category_flow', 'session_replay_unmasked'}


def how_to_produce(skill: str) -> str:
    return HOW.get(skill, f'Run the {skill} skill against the candidate commit, validate it with its own validator, and save it with '
                          f'provenance (a subject block or a sidecar provenance.json naming the commit and build).')


def required_for(policy: dict, release_type: str) -> list[dict]:
    rt = policy['release_types'].get(release_type)
    if rt is None:
        raise SystemExit(f'release type {release_type!r} is not in the policy ({", ".join(policy["release_types"])})')
    out = required_for(policy, rt['extends']) if rt.get('extends') else []
    return out + rt['required']


def near(a: dict | None, b: dict | None) -> str | None:
    """Merge reason when two locations are the same place, else None."""
    if not a or not b:
        return None
    if a.get('file') and a.get('file') == b.get('file'):
        la, lb = a.get('line'), b.get('line')
        if la is None or lb is None or abs(int(la) - int(lb)) <= 3:
            return f'same file {a["file"]}' + (f' lines {la} and {lb}' if la is not None and lb is not None else '')
    if a.get('url') and a.get('url') == b.get('url') and a.get('selector') and a.get('selector') == b.get('selector'):
        return f'same element {a["selector"]} on {a["url"]}'
    return None


def compatible(c1, c2) -> bool:
    return any({c1, c2} <= g for g in COMPATIBLE)


def junit_findings(src: dict) -> list[dict]:
    """One finding per suite (classname) with failing or erroring cases; never rated."""
    by: dict = {}
    for c in src['facts'].get('cases', []):
        if c['status'] in ('failed', 'error'):
            by.setdefault(c['classname'] or '(no classname)', []).append(c)
    return [{'skill': 'ci-junit', 'finding_id': cls, 'title': f'{len(cs)} failing case(s) in {cls}', 'severity': None, 'tier': None,
             'priority': None, 'confidence': 'observed', 'evidence_type': 'ci', 'category': 'failing_tests', 'class': 'test',
             'waiver_category': TYPES['junit']['waiver_category'], 'location': {'ref': cls}, 'facts': {'cases': [c['id'] for c in cs]}}
            for cls, cs in sorted(by.items())]


def journey_findings(src: dict, priorities: list[str]) -> list[dict]:
    out = []
    res = src['facts'].get('results', [])
    for j in src['facts'].get('journeys', []):
        if j.get('priority') not in priorities or j.get('in_scope') is False:
            continue
        main = [r for r in res if r['journey_id'] == j['id'] and not r.get('variant')]
        if not any(r['status'] == 'pass' for r in main):
            out.append({'skill': 'journey-to-tests', 'finding_id': j['id'], 'title': f'Critical journey "{j.get("title")}" has no passing main-path test',
                        'severity': None, 'tier': None, 'priority': None, 'confidence': (main[0]['status'] if main else 'no test'),
                        'evidence_type': 'journey_tests', 'category': 'untested_critical_journey', 'class': 'functional',
                        'waiver_category': 'testing', 'location': {'ref': j['id']}, 'facts': {'main_path_results': [r['status'] for r in main]}})
    return out


def evaluate(col: dict, policy: dict, waivers: dict | None, as_of: str) -> dict:
    cand = col['candidate']
    reqs = required_for(policy, cand.get('release_type', 'minor'))
    conds = col.get('conditions', {})
    sources = [s for s in col['sources']]
    live = [s for s in sources if s['type'] not in ('unrecognised', 'missing_file') and not s['superseded']]
    nr_ok = policy.get('validator_not_run_acceptable', False)
    counted = [s for s in live if s['validator']['status'] == 'valid' or (s['validator']['status'] == 'not_run')]

    # required evidence ------------------------------------------------------------------------------------
    missing, req_rows = [], []
    for r in reqs:
        skill = 'ci-junit' if r.get('kind') == 'junit' else r['skill']
        if r.get('if') and not conds.get(r['if'], {}).get('value', True):
            req_rows.append({'id': r['id'], 'skill': skill, 'status': 'not_required', 'reason': f'condition {r["if"]} is false ({conds[r["if"]]["basis"]})'})
            continue
        cands = [s for s in live if s['type'] == skill or s['skill'] == skill]
        if not cands:
            reason, src = 'absent', None
        else:
            src = cands[0]
            v = src['validator']['status']
            if v == 'invalid':
                reason = 'invalid'
            elif v == 'not_run' and not nr_ok:
                reason = 'validator_not_run'
            elif src['freshness'] != 'fresh':
                reason = src['freshness']
            else:
                reason = None
        row = {'id': r['id'], 'skill': skill, 'status': 'met' if reason is None else reason, 'source': src['path'] if src else None}
        if r.get('if'):
            row['condition'] = r['if']
        req_rows.append(row)
        if reason:
            m = {'requirement': r['id'], 'reason': reason, 'produced_by_skill': skill, 'how_to_produce': how_to_produce(skill)}
            if src:
                m['source'] = src['path']
                m['detail'] = (src['validator']['errors'][:3] if reason in ('invalid', 'validator_not_run')
                               else src.get('freshness_reasons', []) + ([f'{src["commits_behind"]} commit(s) behind the candidate'] if src.get('commits_behind') else []))
            missing.append(m)
    active = [r for r in req_rows if r['status'] != 'not_required']
    met = sum(1 for r in active if r['status'] == 'met')
    completeness = round(met / len(active), 2) if active else 1.0
    required_paths = {r['source'] for r in active if r.get('source')}

    # findings -> issues -------------------------------------------------------------------------------------
    rules = policy['rules']
    jr = next((x for x in rules if x['kind'] == 'untested_critical_journey'), None)
    found = []
    for s in counted:
        fs = list(s['facts']['findings'])
        if s['type'] == 'ci-junit':
            fs = junit_findings(s)
        if s['type'] == 'journey-to-tests' and jr:
            fs += journey_findings(s, jr.get('priorities', ['P0']))
        for f in fs:
            found.append(dict(f, source_path=s['path']))
    issues: list[dict] = []
    for f in found:
        home = None
        for i in issues:
            for g in i['_findings']:
                if g['skill'] != f['skill'] and compatible(g['class'], f['class']):
                    why = near(g['location'], f['location'])
                    if why:
                        home = (i, why); break
            if home:
                break
        if home:
            home[0]['_findings'].append(f); home[0]['merge_reason'] = home[1]
        else:
            issues.append({'_findings': [f], 'merge_reason': None})

    def best(i):
        fs = i['_findings']
        top = max(fs, key=lambda f: (SEV_RANK.get(f['severity'] or '', 0), -PRIO_RANK.get(f['priority'] or 'P9', 9)))
        pr = min((f['priority'] for f in fs if f['priority']), key=lambda p: PRIO_RANK[p], default=None)
        return top, pr

    for i in issues:
        top, pr = best(i)
        i.update(title=top['title'], severity=top['severity'], severity_from=top['skill'] if top['severity'] else None, tier=top.get('tier'),
                 priority=pr, confidence=top['confidence'], evidence_type=top.get('evidence_type'), location=top['location'])
    issues.sort(key=lambda i: (PRIO_RANK.get(i['priority'] or 'P9', 9), -SEV_RANK.get(i['severity'] or '', 0), i['_findings'][0]['skill'], str(i['_findings'][0]['finding_id'])))
    for n, i in enumerate(issues, 1):
        i['issue_id'] = f'I-{n:03d}'

    # rules ---------------------------------------------------------------------------------------------------
    by_path = {s['path']: s for s in sources}
    present = {s['skill'] for s in counted}
    rule_rows, warnings = [], []
    fired: dict = {}
    for r in rules:
        k = r['kind']
        hits = []
        need = {'cvss_at_least': [r.get('skill')], 'blocked_critical_path': r.get('skills', []), 'junit_failed': ['ci-junit'],
                'flaky_rate': ['flaky-test-hunter'], 'irreversible_migration': ['db-migration-reviewer'], 'untested_critical_journey': ['journey-to-tests'],
                'perf_over_budget': ['performance-budget-auditor'], 'privacy_pre_consent': ['privacy-flow-auditor']}.get(k)
        if need is not None and not (set(need) & present):
            rule_rows.append({'rule_id': r['id'], 'effect': r['effect'], 'status': 'not_evaluable', 'reason': f'no counted report from {", ".join(need)}'})
            continue
        if k == 'completeness':
            for s in counted:
                c = s['facts'].get('evidence_completeness')
                if s['path'] in required_paths and ((isinstance(c, (int, float)) and c < policy.get('min_completeness', 0.8)) or c in ('partial', 'incomplete')):
                    warnings.append({'rule_id': r['id'], 'source': s['path'], 'skill': s['skill'], 'measure': c, 'threshold': policy.get('min_completeness', 0.8)})
            rule_rows.append({'rule_id': r['id'], 'effect': r['effect'], 'status': 'fired' if any(w['rule_id'] == r['id'] for w in warnings) else 'not_fired'}); continue
        if k == 'flaky_rate':
            for s in counted:
                for t in s['facts'].get('test_rates', []):
                    gate = any(fnmatch.fnmatch(t['id'] or '', g) for g in r.get('gating', ['*']))
                    if gate and (t.get('normal_low') or 0) > r['threshold']:
                        warnings.append({'rule_id': r['id'], 'source': s['path'], 'skill': s['skill'], 'test': t['id'], 'measure': t['normal_low'], 'threshold': r['threshold'],
                                         'basis': f'{t["normal_fails"]} of {t["normal_runs"]} normal runs failed; lower bound of the 95% interval'})
            rule_rows.append({'rule_id': r['id'], 'effect': r['effect'], 'status': 'fired' if any(w['rule_id'] == r['id'] for w in warnings) else 'not_fired'}); continue
        for i in issues:
            for f in i['_findings']:
                src = by_path[f['source_path']]
                ok = False
                if k == 'priority':
                    ok = f['priority'] in r['priorities']
                elif k == 'cvss_at_least':
                    ok = f['skill'] == r['skill'] and isinstance(f['facts'].get('cvss'), (int, float)) and f['facts']['cvss'] >= r['score']
                elif k == 'blocked_critical_path':
                    ok = (f['skill'] in r['skills'] and src['facts'].get('verdict') == 'does_not_conform' and bool(src['facts'].get('blocked_critical_paths'))
                          and bool(f['facts'].get('blocks_critical_path')))
                elif k == 'junit_failed':
                    ok = f['skill'] == 'ci-junit' and any(fnmatch.fnmatch(c, g) or fnmatch.fnmatch(f['finding_id'], g) for c in f['facts']['cases'] for g in r.get('gating', ['*']))
                elif k == 'irreversible_migration':
                    ok = f['skill'] == 'db-migration-reviewer' and f['category'] == 'irreversible' and not f['facts'].get('rollback_plan')
                elif k == 'untested_critical_journey':
                    ok = f['category'] == 'untested_critical_journey'
                elif k == 'perf_over_budget':
                    m, b = f['facts'].get('median'), f['facts'].get('budget')
                    ok = f['skill'] == 'performance-budget-auditor' and isinstance(m, (int, float)) and isinstance(b, (int, float)) and m > b
                elif k == 'privacy_pre_consent':
                    st = f['facts'].get('consent_states') or []
                    ok = f['skill'] == 'privacy-flow-auditor' and (f['facts'].get('type') == 'tracking_before_consent' or ('before' in st and f['facts'].get('type') in PERSONAL))
                if ok:
                    hits.append(i['issue_id'])
                    fired.setdefault(i['issue_id'], {}).setdefault(r['id'], []).append({'skill': f['skill'], 'finding_id': f['finding_id']})
        rule_rows.append({'rule_id': r['id'], 'effect': r['effect'], 'status': 'fired' if hits else 'not_fired', 'issues': sorted(set(hits))})
    effect = {r['id']: r['effect'] for r in rules}

    # waivers -------------------------------------------------------------------------------------------------
    approvers = policy.get('waiver_approvers', {})
    by_ref = {f'{f["skill"]}:{f["finding_id"]}': (i, f) for i in issues for f in i['_findings']}
    valid_w, bad_w = {}, []
    for w in (waivers or {}).get('waivers', []):
        probs = []
        for k in ('id', 'approver', 'reason', 'expires', 'finding_ids'):
            if not w.get(k):
                probs.append(f'missing {k}')
        ids = w.get('finding_ids') or []
        if any('*' in x or ':' not in x for x in ids):
            probs.append('a waiver must name finding ids (skill:finding_id), never a whole skill or a wildcard')
        if w.get('expires') and str(w['expires']) < as_of:
            probs.append(f'expired on {w["expires"]} (as of {as_of})')
        for x in ids:
            if x not in by_ref:
                probs.append(f'{x} is not an open finding in the evidence')
                continue
            cat = by_ref[x][1]['waiver_category']
            if w.get('approver') and w['approver'] not in approvers.get(cat, []):
                probs.append(f'{w["approver"]} is not on the waiver_approvers list for {cat} ({x})')
        if probs:
            bad_w.append({'waiver_id': w.get('id'), 'finding_ids': ids, 'approver': w.get('approver'), 'problems': probs})
        else:
            valid_w[w['id']] = w

    covered = {x: w for w in valid_w.values() for x in w['finding_ids']}
    blockers, waived = [], []
    for i in issues:
        rs = {rid: v for rid, v in fired.get(i['issue_id'], {}).items() if effect[rid] == 'block'}
        if not rs:
            continue
        refs = [f'{f["skill"]}:{f["finding_id"]}' for f in i['_findings']]
        row = {'issue_id': i['issue_id'], 'rule_ids': sorted(rs), 'title': i['title'], 'severity': i['severity'], 'severity_from': i['severity_from'], 'tier': i['tier'],
               'priority': i['priority'], 'confidence': i['confidence'], 'evidence_type': i['evidence_type'],
               'sources': [{'skill': f['skill'], 'finding_id': f['finding_id'], 'path': f['source_path'], 'severity': f['severity'],
                            'priority': f['priority'], 'confidence': f['confidence']} for f in i['_findings']],
               'location': i['location'], 'source_freshness': sorted({by_path[f['source_path']]['freshness'] for f in i['_findings']}),
               'why_blocking': '; '.join(f'{rid}: ' + next(r['note'] for r in rules if r['id'] == rid) for rid in sorted(rs))}
        if all(x in covered for x in refs):
            w = covered[refs[0]]
            waived.append({'issue_id': i['issue_id'], 'rule_ids': row['rule_ids'], 'title': i['title'], 'waiver_id': w['id'], 'approver': w['approver'],
                           'approver_category': i['_findings'][0]['waiver_category'], 'reason': w['reason'], 'expires': w['expires'], 'finding_ids': refs})
        else:
            blockers.append(row)
    unused = sorted({w['id'] for w in valid_w.values()} - {x['waiver_id'] for x in waived})

    # risks accepted, untested ---------------------------------------------------------------------------------
    shown = {b['issue_id'] for b in blockers} | {w['issue_id'] for w in waived}
    th_sev, th_pr = SEV_RANK[policy['show_threshold']['severity']], PRIO_RANK[policy['show_threshold']['priority']]
    risks: dict = {}
    for i in issues:
        if i['issue_id'] in shown:
            continue
        if (i['priority'] and PRIO_RANK[i['priority']] <= th_pr) or SEV_RANK.get(i['severity'] or '', 0) >= th_sev:
            sk = i['_findings'][0]['skill']
            risks.setdefault(sk, []).append({'issue_id': i['issue_id'], 'finding_ids': [f['finding_id'] for f in i['_findings']], 'title': i['title'],
                                             'severity': i['severity'], 'priority': i['priority'], 'confidence': i['confidence']})
    untested = []
    for s in counted:
        groups: dict = {}
        for u in s['facts']['untested']:
            groups.setdefault(u['label'], []).append(u['item'])
        for label, items in groups.items():
            untested.append({'source': s['path'], 'skill': s['skill'], 'category': label, 'items': items})
        if s['facts'].get('not_run_tools'):
            untested.append({'source': s['path'], 'skill': s['skill'], 'category': 'tools not run', 'items': s['facts']['not_run_tools']})

    # confidence and verdict -----------------------------------------------------------------------------------
    confs = [by_path[r['source']]['facts'].get('overall_confidence') for r in active if r.get('source') and r['source'] in by_path and by_path[r['source']]['facts']]
    confs = [c for c in confs if c in CONF_RANK]
    conf = min(confs, key=lambda c: CONF_RANK[c]) if confs else 'medium'
    if completeness < 1 or any(by_path[r['source']]['validator']['status'] == 'not_run' for r in active if r.get('source')):
        conf = min(conf, 'medium', key=lambda c: CONF_RANK[c])
    if completeness < 0.5:
        conf = 'low'
    verdict = 'no_go' if blockers else 'not_established' if missing else 'go'
    return {'requirements': req_rows, 'missing_evidence': missing, 'issues': issues, 'rules': rule_rows, 'warnings': warnings,
            'blockers': blockers, 'waived': waived, 'invalid_waivers': bad_w, 'unused_waivers': unused, 'risks': risks,
            'untested': untested, 'completeness': completeness, 'confidence': conf, 'verdict': verdict, 'counted': [s['path'] for s in counted]}


def statement(e: dict) -> str:
    b, m, w = len(e['blockers']), len(e['missing_evidence']), len(e['waived'])
    miss = '; '.join(f'{x["produced_by_skill"]} {x["reason"].replace("_", " ")}' for x in e['missing_evidence'])
    if e['verdict'] == 'no_go':
        s = f'No go: {b} blocking issue(s) in the evidence provided'
        s += f', {w} waived.' if w else '.'
        return s + (f' Required evidence is also incomplete ({miss}).' if m else '')
    if e['verdict'] == 'not_established':
        return f'Not established: no blocking findings in the evidence provided, but required evidence is incomplete ({miss}).'
    return 'Go: no blocking findings in the evidence provided' + (f', {w} waived.' if w else '.')


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


KINDS = {'priority', 'cvss_at_least', 'blocked_critical_path', 'junit_failed', 'flaky_rate', 'completeness', 'irreversible_migration',
         'untested_critical_journey', 'perf_over_budget', 'privacy_pre_consent'}


def check_inputs(policy: dict, waivers: dict | None) -> list[str]:
    """The structural checks of schemas/release_policy.schema.json and waivers.schema.json (stdlib, no jsonschema)."""
    errs = [f'policy: missing {k}' for k in ('schema_version', 'name', 'starter', 'release_types', 'rules', 'show_threshold', 'waiver_approvers') if k not in policy]
    for name, rt in policy.get('release_types', {}).items():
        if rt.get('extends') and rt['extends'] not in policy['release_types']:
            errs.append(f'policy: release type {name} extends unknown {rt["extends"]}')
        for r in rt.get('required', []):
            if not r.get('id') or not (r.get('skill') or r.get('kind') == 'junit'):
                errs.append(f'policy: release type {name} has a requirement without an id and a skill (or kind junit)')
            if r.get('if') and r['if'] not in policy.get('conditions', {}):
                errs.append(f'policy: requirement {r.get("id")} uses unknown condition {r["if"]}')
    for r in policy.get('rules', []):
        if r.get('effect') not in ('block', 'warn') or r.get('kind') not in KINDS or not r.get('id'):
            errs.append(f'policy: rule {r.get("id")} needs an id, effect block or warn, and a known kind')
    for w in (waivers or {}).get('waivers', []) if waivers is not None else []:
        if not isinstance(w.get('finding_ids'), list):
            errs.append(f'waivers: {w.get("id")} finding_ids must be a list')
    return errs


def build(col: dict, policy: dict, waivers: dict | None, waivers_path: Path | None, as_of: str) -> dict:
    e = evaluate(col, policy, waivers, as_of)
    req_of = {}
    for r in e['requirements']:
        if r.get('source'):
            req_of.setdefault(r['source'], []).append(r['id'])
    srcs = []
    for s in col['sources']:
        f = s.get('facts') or {}
        srcs.append({'path': s['path'], 'sha256': s['sha256'], 'type': s['type'], 'skill': s['skill'], 'skill_version': s.get('skill_version'),
                     'required': s['path'] in req_of, 'requirement_ids': req_of.get(s['path'], []), 'counted': s['path'] in e['counted'],
                     'superseded': s.get('superseded', False), 'validator': s['validator'], 'freshness': s['freshness'],
                     'freshness_reasons': s.get('freshness_reasons', []), 'commit': s['provenance'].get('commit'),
                     'provenance_source': s['provenance'].get('source'), 'generated_at': s.get('generated_at'),
                     'commits_behind': s.get('commits_behind'), 'changed_since': s.get('changed_since'),
                     'verdict': f.get('verdict'), 'evidence_completeness': f.get('evidence_completeness'),
                     'overall_confidence': f.get('overall_confidence'), 'not_run_tools': f.get('not_run_tools', []),
                     'finding_count': len(f.get('findings', []))})
    issues = [{'issue_id': i['issue_id'], 'title': i['title'], 'severity': i['severity'], 'severity_from': i['severity_from'], 'priority': i['priority'],
               'confidence': i['confidence'], 'location': i['location'], 'merge_reason': i['merge_reason'],
               'sources': [{'skill': f['skill'], 'finding_id': f['finding_id'], 'path': f['source_path'], 'severity': f['severity'], 'priority': f['priority'],
                            'confidence': f['confidence'], 'evidence_type': f.get('evidence_type')} for f in i['_findings']]} for i in e['issues']]
    st = statement(e)
    lim = ['The gate only reads reports: it ran no scanner or test, and every rating is the source skill\'s.']
    if policy.get('starter'):
        lim.append('The policy is the starter policy shipped with the skill (an example), not one the team wrote.')
    if not col.get('repo_used'):
        lim.append('No git repository was given: commits between a stale report and the candidate are not listed, and conditional requirements were treated as true.')
    return {
        'schema_version': '1.0.0', 'skill': {'name': 'release-readiness-gate', 'version': '1.0.0'},
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'as_of': as_of,
        'candidate': col['candidate'],
        'inputs': {'reports': col.get('reports'), 'reports_kind': col.get('reports_kind'), 'repo': col.get('repo'), 'repo_used': col.get('repo_used'),
                   'candidate_commit_time': col.get('candidate_commit_time'), 'collected_at': col.get('collected_at')},
        'policy': {'path': col['policy']['path'], 'sha256': col['policy']['sha256'], 'starter': col['policy']['starter'], 'name': policy.get('name'),
                   'release_type': col['candidate'].get('release_type', 'minor'), 'waiver_approvers': policy.get('waiver_approvers', {}),
                   'show_threshold': policy['show_threshold'], 'min_completeness': policy.get('min_completeness')},
        'waivers': {'path': waivers_path.as_posix(), 'sha256': sha256(waivers_path)} if waivers_path else None,
        'headline': {'verdict': e['verdict'], 'statement': st, 'blocker_count': len(e['blockers']), 'waived_count': len(e['waived']),
                     'missing_evidence_count': len(e['missing_evidence']), 'warning_count': len(e['warnings']),
                     'evidence_completeness': e['completeness'], 'overall_confidence': e['confidence']},
        'requirements': e['requirements'], 'conditions': col.get('conditions', {}),
        'sources': srcs, 'missing_evidence': e['missing_evidence'], 'blockers': e['blockers'], 'warnings': e['warnings'], 'waived': e['waived'],
        'expired_or_invalid_waivers': e['invalid_waivers'], 'unused_waivers': e['unused_waivers'],
        'risks_accepted': [{'skill': k, 'issues': v} for k, v in e['risks'].items()], 'rules': e['rules'], 'issues': issues,
        'untested': e['untested'], 'limitations': lim, 'open_questions': [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('collection', type=Path)
    ap.add_argument('--waivers', type=Path)
    ap.add_argument('--as-of', default=date.today().isoformat())
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    col = json.loads(a.collection.read_text(encoding='utf-8'))
    pp = Path(col['policy']['path'])
    if sha256(pp) != col['policy']['sha256']:
        raise SystemExit(f'{pp} changed since the reports were collected: collect again')
    policy = json.loads(pp.read_text(encoding='utf-8'))
    wp = a.waivers or (Path(col['waivers']['path']) if col.get('waivers') else None)
    waivers = json.loads(wp.read_text(encoding='utf-8')) if wp else None
    errs = check_inputs(policy, waivers)
    if errs:
        raise SystemExit('\n'.join(errs))
    rep = build(col, policy, waivers, wp, a.as_of)
    a.out.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    h = rep['headline']
    print(f'{h["verdict"]}: {h["statement"]}')
    print(f'{h["blocker_count"]} blocker(s), {h["waived_count"]} waived, {h["missing_evidence_count"]} missing, {h["warning_count"]} warning(s); '
          f'completeness {h["evidence_completeness"]}, confidence {h["overall_confidence"]} -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
