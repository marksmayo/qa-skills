#!/usr/bin/env python3
"""Validate a UI crash hunter report (structure and honesty rules).

    python validate_crash_report.py crash_report.json [--repo <app>] [--candidates candidates.json] [--write-summary]

Evidence paths in the report (screenshots, evidence files, repro specs) are relative to the report's
directory and must exist. --repo re-checks every finding location against the source (file exists,
line exists, snippet is on that line) and that nothing the run wrote is inside the repository.
--candidates re-checks that every candidate was triaged. --write-summary recomputes the summary
counts in place (the headline is kept) before validating. Exit code 0 when valid, 1 when invalid.

A reproduced finding needs a stack trace, or a probe that reproduced it by its effect (evidence
effect_observed, every expectation held) plus repro.observed_effect. An effect is enough for a B3/B4
degradation; a B1/B2 crash without a stack trace also needs the probe to show the app blanked
(root_empty, or a screen fallback for B2). A baseline_clean probe ignores console errors caused by
the probe's own third-party blocking (ERR_BLOCKED_BY_CLIENT, kept in blocked_console_errors).
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

REQUIRED = ['metadata', 'recon', 'tools', 'triage', 'probes', 'crashes', 'degradations', 'coverage', 'limitations',
            'files_written', 'summary']
TIERS = {'B1', 'B2', 'B3', 'B4'}
CONFIDENCE = ['reproduced', 'traced', 'pattern']
EVIDENCE_TYPES = {'reproduced', 'trace', 'tool', 'agent_judgment'}
TRIGGER_KINDS = {'data', 'persisted_state', 'environment', 'user_action', 'lifecycle', 'navigation'}
EFFECT_TIER = {'app_terminated': 'B1', 'white_screen': 'B1', 'crash_loop': 'B1', 'screen_replaced': 'B2',
               'screen_frozen': 'B2', 'component_replaced': 'B3', 'component_broken': 'B3', 'console_only': 'B4'}
CAUGHT_EFFECTS = {'screen_replaced', 'component_replaced'}
TOOL_STATUS = {'ran', 'not_run', 'errored', 'not_applicable'}
PROBE_OUTCOMES = {'reproduced', 'not_reproduced', 'baseline_clean', 'errored'}
BLAST = {'B1': 3, 'B2': 2}
REACH = {'critical_path': 3, 'high_traffic': 2, 'unknown': 2, 'rare': 1}
LIKELIHOOD = {'normal_data': 4, 'edge_data': 3, 'rare_environment': 2, 'adversarial': 1}
RECOVERABILITY = {'crash_loop': 3, 'restart_or_navigate': 2, 'retry_fixes': 1}
BANNED = re.compile(r'\b(crash[- ]free|no crashes|never crash(es)?|bug[- ]free|(is|are) (stable|safe)|production[- ]ready|'
                    r'fully (tested|covered)|100% (covered|safe))\b', re.I)
BLOCKED = re.compile(r'ERR_BLOCKED_BY_CLIENT|blockedbyclient', re.I)
VAGUE = re.compile(r'^\W*(this|it|value|data|field)?\s*(could|might|may|can) be (null|undefined|nil|empty)\W*$', re.I)

def score(tier: str, r: dict) -> int:
    return BLAST.get(tier, 0) * REACH.get(r.get('reach'), 0) * LIKELIHOOD.get(r.get('likelihood'), 0) * \
        RECOVERABILITY.get(r.get('recoverability'), 0)

def priority(s: int, confidence: str) -> tuple[str, str | None]:
    p = 'P0' if s >= 48 else 'P1' if s >= 24 else 'P2' if s >= 8 else 'P3'
    if p == 'P0' and confidence == 'pattern':
        return 'P1', 'pattern-confidence findings are capped at P1'
    return p, None

def rank_key(f: dict):
    r = f.get('ranking') or {}
    conf = CONFIDENCE.index(f['confidence']) if f.get('confidence') in CONFIDENCE else 9
    return (-int(r.get('score') or 0), conf, f.get('id', ''))

def compute_summary(d: dict) -> dict:
    crashes, degr = d.get('crashes', []), d.get('degradations', [])
    tools = d.get('tools', [])
    applicable = [t for t in tools if t.get('status') != 'not_applicable']
    ran = [t for t in applicable if t.get('status') == 'ran']
    routes = (d.get('recon') or {}).get('routes_screens', [])
    triage = d.get('triage') or {}
    count = lambda xs, key, vals: {v: sum(1 for x in xs if x.get(key) == v) for v in vals}
    return {
        'crashes': len(crashes),
        'by_tier': count(crashes + degr, 'tier', ['B1', 'B2', 'B3', 'B4']),
        'by_priority': {p: sum(1 for c in crashes if (c.get('ranking') or {}).get('priority') == p) for p in ('P0', 'P1', 'P2', 'P3')},
        'by_confidence': count(crashes, 'confidence', CONFIDENCE),
        'degradations': len(degr),
        'candidates_total': int(triage.get('total') or 0),
        'candidates_promoted': len({p['candidate'] for p in triage.get('promoted', [])}),
        'candidates_dismissed': len(triage.get('dismissed', [])),
        'tools_ran': len(ran),
        'tools_applicable': len(applicable),
        'evidence_completeness': round(len(ran) / len(applicable), 2) if applicable else 0.0,
        'surface_coverage': round(sum(1 for r in routes if r.get('analysed')) / len(routes), 2) if routes else 0.0,
    }

def norm(s: str) -> str:
    return ' '.join(str(s or '').split())

def check(d: dict, base: Path, repo: Path | None, candidates: dict | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    E, W = errors.append, warnings.append
    for k in REQUIRED:
        if k not in d:
            E(f'missing top-level field: {k}')
    if errors:
        return errors, warnings
    meta, recon = d['metadata'], d['recon']
    for k in ('repository', 'generated_at', 'skill_version', 'platforms', 'app_run'):
        if not meta.get(k):
            E(f'metadata.{k} is required')
    app_run = meta.get('app_run') or {}
    boundaries = {b.get('id'): b for b in recon.get('boundaries', [])}
    routes = recon.get('routes_screens', [])
    if not routes:
        E('recon.routes_screens is empty: the report must state which routes and screens were analysed')

    # tools
    for t in d['tools']:
        if t.get('status') not in TOOL_STATUS:
            E(f"tool {t.get('name')}: status must be one of {sorted(TOOL_STATUS)}")
        if t.get('status') in ('not_run', 'errored') and not t.get('note'):
            E(f"tool {t.get('name')}: {t.get('status')} needs a note saying why")
    if not any(t.get('status') == 'ran' for t in d['tools']):
        E('no tool ran: there is no evidence base for the report')

    # probes
    probes = {p.get('id'): p for p in d['probes']}
    evidence: dict[str, dict] = {}
    for p in d['probes']:
        if p.get('outcome') not in PROBE_OUTCOMES:
            E(f"probe {p.get('id')}: outcome must be one of {sorted(PROBE_OUTCOMES)}")
        for k in ('scenario_file', 'evidence_file'):
            if p.get(k) and not (base / p[k]).exists():
                E(f"probe {p.get('id')}: {k} {p[k]} not found")
        if p.get('evidence_file') and (base / p['evidence_file']).exists():
            try:
                evidence[p.get('id')] = json.loads((base / p['evidence_file']).read_text(encoding='utf-8'))
            except ValueError:
                E(f"probe {p.get('id')}: evidence file {p['evidence_file']} is not valid JSON")
        ev = evidence.get(p.get('id'))
        if ev is not None and p.get('outcome') in ('reproduced', 'baseline_clean'):
            if p['outcome'] == 'reproduced' and not ev.get('reproduced'):
                E(f"probe {p['id']}: outcome 'reproduced' but its evidence file says it did not reproduce")
            if p['outcome'] == 'baseline_clean':
                # errors caused by the probe's own third-party blocking are not the app's errors
                own = [c for c in ev.get('console_errors') or [] if not BLOCKED.search(str(c.get('text', c)))]
                if ev.get('page_errors') or own:
                    E(f"probe {p['id']}: outcome 'baseline_clean' but its evidence file records errors")
                if ev.get('baseline_ok') is False or any(not x.get('passed') for x in ev.get('expectations') or []):
                    E(f"probe {p['id']}: outcome 'baseline_clean' but its healthy-behaviour expectations did not all hold")
    if d['probes'] and not app_run.get('attempted'):
        E('probes are listed but metadata.app_run.attempted is false')

    # findings
    all_ids: set[str] = set()
    findings = [('crashes', f) for f in d['crashes']] + [('degradations', f) for f in d['degradations']]
    for section, f in findings:
        fid = f.get('id', '?')
        if fid in all_ids:
            E(f'{fid}: duplicate finding id')
        all_ids.add(fid)
        tier, conf, ev = f.get('tier'), f.get('confidence'), f.get('evidence_type')
        if tier not in TIERS:
            E(f'{fid}: tier must be B1-B4')
        if section == 'crashes' and tier not in ('B1', 'B2'):
            E(f'{fid}: {tier} is a degradation and must not appear in the crash ranking')
        if section == 'degradations' and tier not in ('B3', 'B4'):
            E(f'{fid}: {tier} is a crash and must be ranked under crashes, not listed as a degradation')
        if conf not in CONFIDENCE:
            E(f'{fid}: confidence must be one of {CONFIDENCE}')
        if ev not in EVIDENCE_TYPES:
            E(f'{fid}: evidence_type must be one of {sorted(EVIDENCE_TYPES)}')
        if (conf == 'reproduced') != (ev == 'reproduced'):
            E(f'{fid}: confidence "reproduced" and evidence_type "reproduced" go together')
        if conf == 'pattern' and ev == 'trace':
            E(f'{fid}: a traced finding must have confidence "traced", not "pattern"')
        if not f.get('title'):
            E(f'{fid}: title is required')
        locs = f.get('locations') or []
        if not locs:
            E(f'{fid}: needs at least one file:line location')
        for loc in locs:
            if not loc.get('file') or not isinstance(loc.get('line'), int) or loc['line'] < 1:
                E(f'{fid}: every location needs a file and a line number')
            elif repo:
                src = repo / loc['file']
                if not src.exists():
                    E(f"{fid}: {loc['file']} not found in the repository")
                else:
                    lines = src.read_text(encoding='utf-8', errors='replace').splitlines()
                    if loc['line'] > len(lines):
                        E(f"{fid}: {loc['file']}:{loc['line']} is past the end of the file")
                    elif loc.get('snippet') and norm(loc['snippet']) not in norm(lines[loc['line'] - 1]):
                        E(f"{fid}: snippet not found at {loc['file']}:{loc['line']}")
        trig = f.get('trigger') or {}
        if trig.get('kind') not in TRIGGER_KINDS:
            E(f'{fid}: trigger.kind must be one of {sorted(TRIGGER_KINDS)}')
        desc = norm(trig.get('description'))
        if len(desc) < 20 or VAGUE.match(desc):
            E(f'{fid}: trigger.description must name a concrete data shape, environment or user action, not "could be null"')
        if not norm(trig.get('basis')):
            E(f'{fid}: trigger.basis must say why the trigger is realistic')
        if trig.get('kind') in ('data', 'persisted_state') and trig.get('data') in (None, '', {}, []):
            E(f'{fid}: a {trig.get("kind")} trigger needs the exact trigger data')
        bnd = f.get('boundary') or {}
        effect = bnd.get('effect')
        if effect not in EFFECT_TIER:
            E(f'{fid}: boundary.effect must be one of {sorted(EFFECT_TIER)}')
        elif tier in TIERS and EFFECT_TIER[effect] != tier:
            E(f'{fid}: effect "{effect}" is {EFFECT_TIER[effect]}, not {tier}')
        caught = bnd.get('caught_by')
        if caught is not None and caught not in boundaries:
            E(f'{fid}: boundary.caught_by {caught} is not in recon.boundaries')
        if effect in CAUGHT_EFFECTS and caught is None:
            E(f'{fid}: effect "{effect}" means a boundary caught the error; name it in boundary.caught_by')
        if effect in ('white_screen', 'crash_loop') and caught and (boundaries.get(caught) or {}).get('scope') not in ('app', 'global'):
            E(f'{fid}: a {effect} cannot be caught by a {boundaries[caught].get("scope")}-scoped boundary')
        if conf in ('reproduced', 'traced'):
            hops = f.get('trace') or []
            if len(hops) < 2 or any(not h.get('file') or not isinstance(h.get('line'), int) for h in hops):
                E(f'{fid}: a {conf} finding needs a trace of at least two file:line hops from the input to the throw')
        if conf == 'reproduced':
            rp = f.get('repro') or {}
            if not app_run.get('attempted'):
                E(f'{fid}: marked reproduced but the app was never run')
            if not norm(rp.get('stack_trace')):
                # without a stack trace, the probe must have shown the effect itself: any observed effect for a
                # degradation, but for a crash only an effect showing the app or screen gone
                pev = evidence.get(rp.get('probe_id')) or {}
                effect = bool(pev.get('effect_observed')) and bool(pev.get('expectations'))
                blanked = bool(pev.get('root_empty')) or (tier == 'B2' and bool(pev.get('fallback_visible')))
                if not effect:
                    E(f'{fid}: marked reproduced without a stack trace, and its probe has no observed effect '
                      '(evidence effect_observed with expectations that all held)')
                elif tier in ('B1', 'B2') and not blanked:
                    E(f'{fid}: a {tier} crash reproduced without a stack trace needs evidence that the app blanked '
                      '(root_empty, or a screen fallback for B2); otherwise give the stack trace or keep it traced')
                if not norm(rp.get('observed_effect')):
                    E(f'{fid}: reproduced by its effect: repro.observed_effect must say what the probe saw')
            if not rp.get('screenshot'):
                E(f'{fid}: marked reproduced without a screenshot')
            elif not (base / rp['screenshot']).exists():
                E(f"{fid}: screenshot {rp['screenshot']} not found")
            pr = probes.get(rp.get('probe_id'))
            if not pr:
                E(f'{fid}: marked reproduced but repro.probe_id does not name a probe')
            elif pr.get('outcome') != 'reproduced':
                E(f"{fid}: probe {rp.get('probe_id')} did not reproduce it (outcome {pr.get('outcome')})")
            elif fid not in (pr.get('findings') or []):
                E(f"{fid}: probe {pr['id']} does not list this finding")
            if rp.get('evidence_file') and not (base / rp['evidence_file']).exists():
                E(f"{fid}: evidence file {rp['evidence_file']} not found")
        rp = f.get('repro') or {}
        if section == 'crashes' and not rp.get('steps'):
            E(f'{fid}: crashes need repro steps')
        if rp.get('repro_spec') and not (base / rp['repro_spec']).exists():
            E(f"{fid}: repro spec {rp['repro_spec']} not found")
        if not f.get('sources'):
            E(f'{fid}: sources must name the tool or say "agent"')
        if not norm(f.get('remediation')) or not norm(f.get('verification_step')):
            E(f'{fid}: remediation and verification_step are required')
        if section == 'crashes' and (recon.get('blast_radius') or {}).get('model') == 'per_script' and \
                not ((f.get('repro') or {}).get('probe_id') and (evidence.get(f['repro']['probe_id']) or {}).get('root_empty')):
            W(f'{fid}: {tier} on a per_script site (static pages): an uncaught error stops only its own script, so this is '
              'a crash only if the page content is rendered by script or the script blanks it; otherwise it is B3/B4')
        if section == 'crashes':
            r = f.get('ranking') or {}
            for k, scale in (('reach', REACH), ('likelihood', LIKELIHOOD), ('recoverability', RECOVERABILITY)):
                if r.get(k) not in scale:
                    E(f'{fid}: ranking.{k} must be one of {list(scale)}')
            if r.get('recoverability') == 'crash_loop' and trig.get('kind') != 'persisted_state':
                E(f'{fid}: a crash loop needs a persisted_state trigger (it survives reloads because the bad state is stored)')
            if effect == 'crash_loop' and r.get('recoverability') != 'crash_loop':
                E(f'{fid}: effect crash_loop needs recoverability crash_loop')
            if r.get('reach') == 'unknown':
                W(f'{fid}: reach unknown (scored as high_traffic); give journey priorities or analytics to rank it properly')
            if not norm(r.get('likelihood_basis')):
                E(f'{fid}: ranking.likelihood_basis must say why this likelihood')
            s = score(tier, r)
            if r.get('score') != s:
                E(f"{fid}: score {r.get('score')} does not match blast x reach x likelihood x recoverability = {s}")
            p, cap = priority(s, conf)
            if r.get('priority') != p:
                E(f"{fid}: priority {r.get('priority')} does not match score {s} ({p}{'; ' + cap if cap else ''})")
        elif f.get('ranking') or f.get('rank'):
            E(f'{fid}: degradations are not ranked; remove ranking and rank')
    ordered = sorted(d['crashes'], key=rank_key)
    for n, f in enumerate(ordered, 1):
        if f.get('rank') != n:
            E(f"{f.get('id')}: rank {f.get('rank')} but its score puts it at {n}")
    if [f.get('id') for f in d['crashes']] != [f.get('id') for f in ordered]:
        E('crashes must be listed in rank order (score, then confidence)')

    # triage
    tri = d['triage']
    seen: dict[str, str] = {}
    for p in tri.get('promoted', []):
        if p.get('finding') not in all_ids:
            E(f"triage: {p.get('candidate')} promoted to unknown finding {p.get('finding')}")
        seen.setdefault(p.get('candidate'), 'promoted')
    for x in tri.get('dismissed', []):
        if x.get('candidate') in seen:
            E(f"triage: {x.get('candidate')} is both {seen[x['candidate']]} and dismissed")
        seen[x.get('candidate')] = 'dismissed'
        if len(norm(x.get('reason'))) < 10:
            E(f"triage: {x.get('candidate')} dismissed without a real reason")
    if len(seen) != tri.get('total'):
        E(f"triage: {len(seen)} candidate(s) accounted for, but total is {tri.get('total')}")
    for _, f in findings:
        for cid in f.get('candidate_ids') or []:
            if seen.get(cid) != 'promoted':
                E(f"{f.get('id')}: candidate {cid} is not listed as promoted in triage")
    if candidates is not None:
        ids = {c['id'] for c in candidates.get('candidates', [])}
        if ids != set(seen):
            missing, extra = sorted(ids - set(seen)), sorted(set(seen) - ids)
            E(f'triage does not match the candidates file: untriaged {missing[:8]}, unknown {extra[:8]}')

    # coverage
    cov = d['coverage']
    analysed = sum(1 for r in routes if r.get('analysed'))
    if cov.get('routes_total') != len(routes) or cov.get('routes_analysed') != analysed:
        E(f"coverage: routes_total/routes_analysed must be {len(routes)}/{analysed} (from recon.routes_screens)")
    listed = {x.get('item') for x in cov.get('not_analysed', [])}
    for r in routes:
        if not r.get('analysed') and r.get('id') not in listed and r.get('path') not in listed:
            E(f"coverage: route {r.get('id')} was not analysed and is not in coverage.not_analysed with a reason")
    if not d['limitations']:
        E('limitations must not be empty')

    # files written stay outside the repository
    if repo:
        root = repo.resolve()
        for p in d['files_written']:
            q = (base / p).resolve()
            if q == root or root in q.parents:
                E(f'files_written: {p} is inside the repository; the skill must not write to it')

    # summary
    s, want = d['summary'], compute_summary(d)
    for k, v in want.items():
        if s.get(k) != v:
            E(f'summary.{k} is {s.get(k)!r}, expected {v!r} (run with --write-summary)')
    headline = s.get('headline') or ''
    if BANNED.search(headline):
        E(f'summary.headline overclaims ("{BANNED.search(headline).group(0)}"): no findings is not evidence of no crashes')
    if not d['crashes'] and not d['probes']:
        W('no crashes and no dynamic confirmation: say clearly that this is a static result only')
    if want['evidence_completeness'] < 0.5:
        W(f"evidence completeness is {want['evidence_completeness']}: most applicable tools did not run")
    return errors, warnings

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--repo', type=Path, help='the analysed app, to re-check locations and that nothing was written to it')
    ap.add_argument('--candidates', type=Path, help='candidates.json from scan_pitfalls.py')
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args()
    try:
        d = json.loads(args.report.read_text(encoding='utf-8'))
        cands = json.loads(args.candidates.read_text(encoding='utf-8')) if args.candidates else None
    except (OSError, ValueError) as exc:
        print(f'Unable to read input: {exc}', file=sys.stderr)
        return 2
    if args.write_summary and isinstance(d.get('summary'), dict):
        d['summary'] = {**compute_summary(d), 'headline': d['summary'].get('headline', '')}
        args.report.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errors, warnings = check(d, args.report.resolve().parent, args.repo.resolve() if args.repo else None, cands)
    for w in warnings:
        print(f'WARN: {w}')
    if errors:
        for e in errors:
            print(f'ERROR: {e}')
        print(f'INVALID ({len(errors)} error(s))')
        return 1
    print('VALID')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
