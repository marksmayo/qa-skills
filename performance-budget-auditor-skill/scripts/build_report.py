#!/usr/bin/env python3
"""Seed perf_report.json from the run, the candidates and the agent's triage (stdlib).

    python build_report.py --measurements run/measurements.json --candidates run/candidates.json --triage triage.json \
        --out run/perf_report.json [--regressions run/regressions.json] [--plan plan.json]

Everything the validator recomputes is copied or computed here, so only the agent's judgement is written by hand.
triage.json:

    {"summary": "Lab data: ... field performance not established.",
     "decisions": {"K1": {"decision": "promoted", "reason": "...", "title": "...", "remediation": "...",
                          "verification_step": "..."},
                   "K2": {"decision": "needs_review", "reason": "..."},
                   "K3": {"decision": "dismissed", "reason": "..."}},
     "limitations": ["..."], "next_steps": ["..."], "field_data": null, "authorised_by": null,
     "tools": [{"name": "lighthouse", "version": null, "sha256": null, "status": "not_run", "note": "..."}]}

Copied or computed: scope (targets, variants and setup from the sample; profiles, runs, min_runs, max_runs, cache
modes and browsers from the config and runs; the budget source; third_party and blocked_first_party), metadata
(machine, calibration, tools, the harness record), measurements, server_timing, cdn_cache, request_summary,
warm_cache, delivery, service_workers, findings (every candidate field; F1, F2, ... in candidate order), triage,
needs_review, regressions (from --regressions) and the computed headline fields. Each limitation the runs require
(validate_report.limitation_rules: third parties blocked, warm runs not cached, a service worker active, a loaded
machine, DPR 1 on a mobile profile, INP untested everywhere, ...) that no limitation of the triage already states is
added in a default wording, and listed on stderr: read them and reword where the run needs more than that.

Refuses (exit 2) when a candidate has no decision, a decision names no candidate, a promoted candidate has low
confidence (it belongs in needs_review) or lacks a title, remediation or verification step, or the summary is missing.
Then run validate_report.py on the result.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import attribute as at  # noqa: E402
import validate_report as vr  # noqa: E402
from browser_harness import now  # noqa: E402

DECISIONS = ('promoted', 'needs_review', 'dismissed')


def skill_version() -> str:
    for line in (HERE.parent / 'SKILL.md').read_text(encoding='utf-8').splitlines():
        if line.startswith('version:'):
            return line.split(':', 1)[1].strip()
    return 'unknown'


def rel(p: Path, out: Path) -> str:
    try:
        return Path(os.path.relpath(p.resolve(), out.resolve().parent)).as_posix()
    except ValueError:   # another drive on Windows
        return p.resolve().as_posix()


def targets(doc: dict) -> list[dict]:
    out = []
    for s in doc['sample']:
        t = {'url': s['url'], 'template': s['template'], 'reason': s.get('reason') or '', 'critical_path': bool(s.get('critical_path')),
             'interaction': bool(s.get('interaction'))}
        if s.get('variants'):
            t['variants'] = [v['name'] for v in s['variants']]
        if s.get('setup'):
            t['setup'] = s['setup']
        out.append(t)
    return out


def build(doc: dict, cands_doc: dict, triage: dict, measurements_file: str, regressions: dict | None,
          plan: dict | None) -> tuple[dict, list[str], list[str]]:
    """(report, errors, limitations added)."""
    errs = []
    cands = cands_doc['candidates']
    dec = triage.get('decisions') or {}
    ids = [c['id'] for c in cands]
    for cid in ids:
        if cid not in dec:
            errs.append(f'{cid}: no decision in the triage')
    for cid, d in dec.items():
        if cid not in ids:
            errs.append(f'{cid}: the triage decides a candidate that does not exist')
        elif d.get('decision') not in DECISIONS:
            errs.append(f'{cid}: decision must be one of {", ".join(DECISIONS)}')
        elif len(d.get('reason') or '') < 10:
            errs.append(f'{cid}: give a reason (10 characters or more)')
    if len(triage.get('summary') or '') < 20:
        errs.append('summary: write the headline summary (lab data; never "fast")')
    findings, tri, review = [], [], []
    for c in cands:
        d = dec.get(c['id']) or {}
        fid = None
        if d.get('decision') == 'promoted':
            if c['confidence'] == 'low':
                errs.append(f'{c["id"]}: low confidence (unstable, noisy, or within the machine\'s slowdown): it belongs in needs_review')
            missing = [k for k in ('title', 'remediation', 'verification_step') if len(d.get(k) or '') < 10]
            if missing:
                errs.append(f'{c["id"]}: a promoted candidate needs {", ".join(missing)}')
            fid = f'F{len(findings) + 1}'
            findings.append({'id': fid, 'candidate_id': c['id'], 'title': d.get('title') or '',
                             **{k: c[k] for k in vr.FINDING_FIELDS},
                             'remediation': d.get('remediation') or '', 'verification_step': d.get('verification_step') or ''})
        elif d.get('decision') == 'needs_review':
            review.append({'candidate_id': c['id'], 'reason': d.get('reason') or ''})
        tri.append({'candidate_id': c['id'], 'decision': d.get('decision'), 'finding_id': fid, 'reason': d.get('reason') or ''})
    meas = cands_doc['measurements']
    cfg = doc['config']
    runs, min_runs, cap = at.run_limits(doc)
    tools = list(doc.get('tools') or []) + list(triage.get('tools') or [])
    if not any(t['name'].lower().startswith('lighthouse') for t in tools):
        tools.append({'name': 'lighthouse', 'version': None, 'sha256': None, 'status': 'not_run',
                      'note': 'not run; the repeated runs are the evidence'})
    scope = {'targets': targets(doc), 'profiles': cfg.get('profiles') or {},
             'budget': {'source': cands_doc['budget']['source'], 'note': cands_doc['budget'].get('note')},
             'runs': runs, 'min_runs': min_runs, 'max_runs': cap, 'cache': cfg.get('cache') or ['cold'],
             'browsers': sorted({r['browser'] for r in doc['runs']}),
             'authorised_by': triage.get('authorised_by', (plan or {}).get('authorised_by'))}
    tp = at.third_party(doc)
    if tp is not None:
        scope['third_party'] = tp
    bfp = at.blocked_first_party(doc)
    if bfp:
        scope['blocked_first_party'] = bfp
    meta = {'generated_at': now(), 'machine': doc['machine'], 'calibration': doc['calibration'], 'tools': tools,
            'measurements_file': measurements_file}
    if doc.get('harness') is not None:
        meta['harness'] = doc['harness']
    rep = {'skill': {'name': 'performance-budget-auditor', 'version': skill_version()}, 'scope': scope, 'metadata': meta,
           'headline': {'verdict': 'over_budget', 'data_kind': 'lab', 'summary': triage.get('summary') or ''},
           'measurements': meas, 'server_timing': at.server_timing(doc)}
    for key, fn in (('cdn_cache', at.cdn_cache), ('request_summary', at.request_summary), ('warm_cache', at.warm_cache),
                    ('delivery', at.delivery), ('service_workers', at.service_workers)):
        rows = fn(doc)
        if rows:
            rep[key] = rows
    rep.update(findings=findings,
               regressions=[r for r in (regressions or {}).get('comparisons') or [] if r['direction'] in ('regressed', 'improved')],
               field_data=triage.get('field_data'), needs_review=review, triage=tri,
               limitations=list(triage.get('limitations') or []), next_steps=list(triage.get('next_steps') or []))
    rep['headline'].update(vr.expected_headline(rep, meas, doc))
    added = []
    for r in vr.limitation_rules(rep, doc, meas):
        if r['needed'] and not r['satisfied']:
            rep['limitations'].append(r['text'])
            added.append(f'{r["key"]}: {r["text"]}')
    return rep, errs, added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--measurements', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--triage', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--regressions', type=Path)
    ap.add_argument('--plan', type=Path)
    a = ap.parse_args()
    load = lambda p: json.loads(p.read_text(encoding='utf-8'))  # noqa: E731
    doc, cands_doc, triage = load(a.measurements), load(a.candidates), load(a.triage)
    rep, errs, added = build(doc, cands_doc, triage, rel(a.measurements, a.out),
                             load(a.regressions) if a.regressions else None, load(a.plan) if a.plan else None)
    if errs:
        print('\n'.join(errs), file=sys.stderr)
        print(f'NOT BUILT: {len(errs)} problem(s) in the triage', file=sys.stderr)
        return 2
    for x in added:
        print(f'limitation added: {x}', file=sys.stderr)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(rep["findings"])} finding(s), {len(rep["needs_review"])} in needs_review, {len(added)} limitation(s) added '
          f'-> {a.out}; now run validate_report.py', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
