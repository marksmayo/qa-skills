#!/usr/bin/env python3
"""Validate a gate report against its contract, its sources, its policy and its waivers.

    python validate_gate.py gate_report.json [--reports] [--recompute collection.json]

Always: the verdict follows the order no_go > not_established > go (go only with no blockers, no missing
evidence and every required source valid and fresh; no_go only with blockers; not_established only with
missing evidence and no blockers); every waived issue has a waiver in the waivers file (its SHA-256 must
match) that names its findings, has an approver on the policy's allow-list for the finding's category, a
reason, and an expiry on or after as_of; a waiver can't name a whole skill or a wildcard; evidence
completeness and overall confidence equal the computation from requirements and sources; counts equal the
lists; no invalid report is counted; no summary text claims safety or readiness; an agent_judgment source
is never high confidence.
--reports: re-reads every source (its SHA-256 must match), checks every file in the reports directory or
manifest is listed, and compares each blocker's and waived issue's copied severity, priority and
confidence with the source report's own values.
--recompute: re-applies the policy to the collection and compares the verdict, blockers, waivers,
missing evidence and warnings.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adapters import CONF_RANK, facts_for  # noqa: E402

BANNED = re.compile(r'(?i)\b(safe to (release|ship|deploy)|(?<!not )safe\b|ready to (ship|release|deploy)|release[- ]ready|production[- ]ready|no risks?\b|risk[- ]free|fully tested|nothing (left )?to worry|good to go|all clear)')
REQUIRED = ['schema_version', 'skill', 'generated_at', 'as_of', 'candidate', 'policy', 'headline', 'requirements', 'sources', 'missing_evidence',
            'blockers', 'warnings', 'waived', 'expired_or_invalid_waivers', 'risks_accepted', 'rules', 'issues', 'untested', 'limitations', 'open_questions']


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() if p.exists() else None  # line endings normalised


def expected_confidence(d: dict) -> str:
    by = {s['path']: s for s in d['sources']}
    active = [r for r in d['requirements'] if r['status'] != 'not_required']
    comp = d['headline']['evidence_completeness']
    confs = [by[r['source']].get('overall_confidence') for r in active if r.get('source') in by]
    confs = [c for c in confs if c in CONF_RANK]
    conf = min(confs, key=lambda c: CONF_RANK[c]) if confs else 'medium'
    if comp < 1 or any(by[r['source']]['validator']['status'] == 'not_run' for r in active if r.get('source') in by):
        conf = min(conf, 'medium', key=lambda c: CONF_RANK[c])
    return 'low' if comp < 0.5 else conf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--reports', action='store_true', help='re-read the sources and the reports directory or manifest')
    ap.add_argument('--recompute', type=Path, metavar='COLLECTION')
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    for k in REQUIRED:
        if k not in d:
            e(f'missing {k}')
    if E:
        print('\n'.join(E)); return 1
    h = d['headline']
    by = {s['path']: s for s in d['sources']}
    pol_path = Path(d['policy']['path'])
    policy = json.loads(pol_path.read_text(encoding='utf-8')) if pol_path.exists() else None
    if policy is None:
        e(f'policy {pol_path} not found')
    elif sha(pol_path) != d['policy']['sha256']:
        e(f'policy {pol_path} does not match its recorded SHA-256')
    waivers = {}
    if d.get('waivers'):
        wp = Path(d['waivers']['path'])
        if not wp.exists():
            e(f'waivers {wp} not found')
        else:
            if sha(wp) != d['waivers']['sha256']:
                e(f'waivers {wp} do not match the recorded SHA-256')
            waivers = {w.get('id'): w for w in json.loads(wp.read_text(encoding='utf-8')).get('waivers', [])}

    # verdict ------------------------------------------------------------------------------------------
    v = h['verdict']
    req_bad = [r for r in d['requirements'] if r['status'] not in ('met', 'not_required')]
    if v == 'go' and (d['blockers'] or d['missing_evidence'] or req_bad):
        e('verdict go with blockers, missing evidence or a required source that is not valid and fresh')
    if v == 'no_go' and not d['blockers']:
        e('verdict no_go without blockers')
    if v == 'not_established' and (d['blockers'] or not d['missing_evidence']):
        e('verdict not_established needs missing evidence and no blockers (a known blocker makes it no_go)')
    if v not in ('go', 'no_go', 'not_established'):
        e(f'unknown verdict {v}')
    if sorted(r['id'] for r in req_bad) != sorted(m['requirement'] for m in d['missing_evidence']):
        e('missing_evidence must list exactly the requirements that are not met')
    for r in d['requirements']:
        s = by.get(r.get('source'))
        if r['status'] == 'met' and (not s or s['validator']['status'] not in ('valid',) + (('not_run',) if (policy or {}).get('validator_not_run_acceptable') else ()) or s['freshness'] != 'fresh'):
            e(f'requirement {r["id"]} is met but its source is not valid and fresh')
    for k, n in (('blocker_count', 'blockers'), ('waived_count', 'waived'), ('missing_evidence_count', 'missing_evidence'), ('warning_count', 'warnings')):
        if h.get(k) != len(d[n]):
            e(f'headline.{k} {h.get(k)} should be {len(d[n])}')
    active = [r for r in d['requirements'] if r['status'] != 'not_required']
    comp = round(sum(1 for r in active if r['status'] == 'met') / len(active), 2) if active else 1.0
    if h['evidence_completeness'] != comp:
        e(f'headline.evidence_completeness {h["evidence_completeness"]} should be {comp}')
    if h['overall_confidence'] != expected_confidence(d):
        e(f'headline.overall_confidence {h["overall_confidence"]} should be {expected_confidence(d)}')

    # sources, blockers -------------------------------------------------------------------------------
    for s in d['sources']:
        if s['counted'] and s['validator']['status'] == 'invalid':
            e(f'{s["path"]}: an invalid report is counted')
    for b in d['blockers'] + d['issues']:
        for src in b['sources']:
            if by.get(src['path']) and not by[src['path']]['counted']:
                e(f'{b["issue_id"]}: source {src["path"]} is not counted (invalid or superseded)')
            if src.get('evidence_type') == 'agent_judgment' and src.get('confidence') == 'high':
                e(f'{b["issue_id"]}: an agent_judgment finding is shown with high confidence')
    ids = {i['issue_id'] for i in d['issues']}
    for b in d['blockers'] + d['waived']:
        if b['issue_id'] not in ids:
            e(f'{b["issue_id"]} is not in issues')
    if {b['issue_id'] for b in d['blockers']} & {w['issue_id'] for w in d['waived']}:
        e('an issue is both blocking and waived')

    # waivers -----------------------------------------------------------------------------------------
    approvers = (policy or {}).get('waiver_approvers', {})
    issue_of = {i['issue_id']: i for i in d['issues']}
    for w in d['waived']:
        wid = w['waiver_id']
        src = waivers.get(wid)
        if not src:
            e(f'{w["issue_id"]}: waiver {wid} is not in the waivers file'); continue
        for k in ('approver', 'reason', 'expires', 'finding_ids'):
            if not src.get(k):
                e(f'{wid}: missing {k}')
        if src.get('expires') and str(src['expires']) < d['as_of']:
            e(f'{wid}: expired on {src["expires"]} (as of {d["as_of"]})')
        if any('*' in x or ':' not in x for x in src.get('finding_ids', [])):
            e(f'{wid}: a waiver must name finding ids, never a whole skill or a wildcard')
        refs = [f'{s["skill"]}:{s["finding_id"]}' for s in issue_of.get(w['issue_id'], {}).get('sources', [])]
        if not refs or any(r not in src.get('finding_ids', []) for r in refs):
            e(f'{wid}: does not name every finding of {w["issue_id"]} ({refs})')
        if src.get('approver') not in approvers.get(w['approver_category'], []):
            e(f'{wid}: {src.get("approver")} is not on the waiver_approvers list for {w["approver_category"]}')
        if w.get('approver') != src.get('approver') or w.get('expires') != src.get('expires'):
            e(f'{wid}: approver or expiry differ from the waivers file')

    # language ------------------------------------------------------------------------------------------
    texts = [h['statement']] + d['limitations'] + [str(q) for q in d['open_questions']] + [b.get('why_blocking', '') for b in d['blockers']]
    for t in texts:
        m = BANNED.search(t)
        if m:
            e(f'text says {m.group(0)!r}: the strongest wording allowed is "no blocking findings in the evidence provided"')
    if v == 'go' and 'no blocking findings in the evidence provided' not in h['statement']:
        e('a go statement must say "no blocking findings in the evidence provided"')

    # --reports -----------------------------------------------------------------------------------------
    if a.reports:
        inp = d.get('inputs') or {}
        listed = {s['path'] for s in d['sources']}
        rp = Path(inp.get('reports') or '')
        if inp.get('reports_kind') == 'directory' and rp.is_dir():
            for x in rp.rglob('*'):
                if x.is_file() and not x.name.endswith('.provenance.json') and x.as_posix() not in listed:
                    e(f'{x.as_posix()} is in the reports directory but not in sources')
        elif inp.get('reports_kind') == 'manifest' and rp.is_file():
            import os
            m = json.loads(rp.read_text(encoding='utf-8'))
            for r in m['reports']:
                q = Path(os.path.relpath(os.path.normpath(rp.parent / r['path']))).as_posix()
                if q not in listed:
                    e(f'{q} is in the manifest but not in sources')
        facts = {}
        for s in d['sources']:
            p = Path(s['path'])
            if s['sha256'] and sha(p) != s['sha256']:
                e(f'{s["path"]}: SHA-256 does not match the file')
            elif s['type'] not in ('unrecognised', 'missing_file') and p.exists():
                facts[s['path']] = facts_for(p)
        for b in d['blockers'] + [dict(issue_of[w['issue_id']], issue_id=w['issue_id']) for w in d['waived'] if w['issue_id'] in issue_of]:
            for src in b['sources']:
                f = facts.get(src['path'])
                if not f or src['skill'] == 'ci-junit' or f['type'] == 'journey-to-tests' and src['finding_id'].startswith('JRN-'):
                    continue
                orig = next((x for x in f['findings'] if x['finding_id'] == src['finding_id']), None)
                if not orig:
                    e(f'{b["issue_id"]}: {src["skill"]}:{src["finding_id"]} is not in {src["path"]}'); continue
                for k in ('severity', 'priority', 'confidence'):
                    if orig[k] != src.get(k):
                        e(f'{b["issue_id"]}: {k} {src.get(k)!r} differs from the source report ({orig[k]!r}); the gate never re-scores')

    # --recompute ---------------------------------------------------------------------------------------
    if a.recompute:
        from apply_policy import evaluate
        col = json.loads(a.recompute.read_text(encoding='utf-8'))
        if col['policy']['sha256'] != d['policy']['sha256']:
            e('the collection was made with a different policy')
        wl = json.loads(Path(d['waivers']['path']).read_text(encoding='utf-8')) if d.get('waivers') else None
        r = evaluate(col, policy, wl, d['as_of'])
        if r['verdict'] != v:
            e(f'recomputed verdict {r["verdict"]} differs from {v}')
        key = lambda xs: sorted((x['issue_id'], tuple(x['rule_ids'])) for x in xs)  # noqa: E731
        if key(r['blockers']) != key(d['blockers']):
            e(f'recomputed blockers {key(r["blockers"])} differ from {key(d["blockers"])}')
        if key(r['waived']) != key(d['waived']):
            e('recomputed waived issues differ')
        if sorted((m['requirement'], m['reason']) for m in r['missing_evidence']) != sorted((m['requirement'], m['reason']) for m in d['missing_evidence']):
            e('recomputed missing evidence differs')
        if sorted(json.dumps(w, sort_keys=True) for w in r['warnings']) != sorted(json.dumps(w, sort_keys=True) for w in d['warnings']):
            e('recomputed warnings differ')
        if sorted(x['waiver_id'] for x in r['invalid_waivers']) != sorted(x['waiver_id'] for x in d['expired_or_invalid_waivers']):
            e('recomputed expired or invalid waivers differ')

    if E:
        print(f'{a.report}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    print(f'{a.report}: valid. verdict {v}; {len(d["blockers"])} blocker(s), {len(d["waived"])} waived, {len(d["missing_evidence"])} missing, '
          f'{len(d["warnings"])} warning(s); completeness {h["evidence_completeness"]}, confidence {h["overall_confidence"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
