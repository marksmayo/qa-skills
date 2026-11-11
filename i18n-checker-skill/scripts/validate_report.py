#!/usr/bin/env python3
"""Validate an i18n report against the schema, the candidates, the evidence and the rules.

    python validate_report.py report.json --candidates run/candidates.json --keys run/keys.json --resources run/resources.json
        --repo examples/bookclub [--runtime run/runtime/runtime.json] [--write-summary]

Fails when:
  * a finding's kind, status, locales, ratings, locations, keys or evidence differ from its candidate, or a
    candidate is missing from triage or triaged twice;
  * a static finding has no file and line, or the line is past the end of the file; a runtime finding has no
    screenshot (on disk) or no measurement;
  * a locale has no status, a not_run locale has no reason, or a shipped locale is missing from scope;
  * the summary says "fully localised", "fully translated" or similar, or translation_quality isn't untested
    without reviewer evidence; an untranslated finding has high confidence;
  * the proposed patch is marked applied, its file is missing, or its keys don't match the missing JSON keys;
  * key coverage or headline counts differ from the inputs.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_check  # noqa: E402

CLAIMS = re.compile(r'\b(fully|completely|100\s*%)\s+(locali[sz]ed|translated|internationali[sz]ed)\b|\btranslation (is )?complete\b|\ball strings (are )?translated\b', re.I)
FIELDS = ('kind', 'status', 'locales', 'severity', 'confidence', 'priority', 'score', 'reach', 'leverage', 'locations', 'keys')


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--keys', type=Path, required=True)
    ap.add_argument('--resources', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--runtime', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    rep, cands_doc, keys, res = load(a.report), load(a.candidates), load(a.keys), load(a.resources)
    errs: list[str] = []
    errs += [f'schema: {e}' for e in schema_check.errors(rep, load(HERE.parent / 'schemas' / 'i18n_report.schema.json'))]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1
    rt = load(a.runtime) if a.runtime else None
    shots_dir = a.runtime.parent if a.runtime else None
    cands = {c['id']: c for c in cands_doc['candidates']}

    # triage
    seen = {}
    for t in rep['triage']:
        if t['candidate_id'] in seen:
            errs.append(f'triage: {t["candidate_id"]} is triaged twice')
        seen[t['candidate_id']] = t
    for cid in cands:
        if cid not in seen:
            errs.append(f'triage: candidate {cid} is missing')
    fmap = {f['id']: f for f in rep['findings']}
    for t in rep['triage']:
        if t['decision'] == 'promoted':
            f = fmap.get(t['finding_id'] or '')
            if not f or f['candidate_id'] != t['candidate_id']:
                errs.append(f'triage: {t["candidate_id"]} is promoted to {t["finding_id"]}, which is not its finding')
        elif t['finding_id'] is not None:
            errs.append(f'triage: {t["candidate_id"]} is {t["decision"]} but names {t["finding_id"]}')
        if t['decision'] == 'needs_review' and t['candidate_id'] not in {n['candidate_id'] for n in rep['needs_review']}:
            errs.append(f'triage: {t["candidate_id"]} is needs_review but not in needs_review[]')

    # findings
    for f in rep['findings']:
        c = cands.get(f['candidate_id'])
        if not c:
            errs.append(f'{f["id"]}: candidate {f["candidate_id"]} does not exist')
            continue
        for k in FIELDS:
            if f[k] != c[k]:
                errs.append(f'{f["id"]}: {k} differs from candidate {c["id"]}')
        if [e.get('screenshot') for e in f['evidence']] != [e.get('screenshot') for e in c['evidence']]:
            errs.append(f'{f["id"]}: evidence differs from candidate {c["id"]}')
        for loc in f['locations']:
            p = a.repo / loc['file']
            if not p.is_file():
                errs.append(f'{f["id"]}: {loc["file"]} does not exist')
            elif loc['line'] is None or loc['line'] < 1 or loc['line'] > len(p.read_text(encoding='utf-8', errors='replace').splitlines()):
                errs.append(f'{f["id"]}: {loc["file"]} has no line {loc["line"]}')
        if not f['locations'] and not f['evidence']:
            errs.append(f'{f["id"]}: no file and line, and no runtime evidence')
        for e in f['evidence']:
            if not e.get('measure'):
                errs.append(f'{f["id"]}: runtime evidence on {e["page"]} has no measurement')
            if not e.get('screenshot') or (shots_dir and not (shots_dir / e['screenshot']).is_file()):
                errs.append(f'{f["id"]}: runtime evidence on {e["page"]} has no screenshot on disk')
        if f['kind'] == 'untranslated' and f['confidence'] == 'high':
            errs.append(f'{f["id"]}: an untranslated finding is never high confidence (some strings are the same in both languages)')
        if CLAIMS.search(f['title'] + ' ' + f['remediation']):
            errs.append(f'{f["id"]}: claims the product is fully localised')

    # locales
    shipped = set(cands_doc['shipped_locales']) | {cands_doc['source_locale']}
    codes = {l['code']: l for l in rep['scope']['locales']}
    for s in shipped:
        if s not in codes:
            errs.append(f'scope.locales: {s} ships but has no status')
    for l in rep['scope']['locales']:
        if l['status'] in ('not_run', 'excluded') and not l['reason']:
            errs.append(f'scope.locales: {l["code"]} is {l["status"]} with no reason')
    if rt:
        for nr in rt['not_run']:
            if codes.get(nr['locale'], {}).get('status') != 'not_run':
                errs.append(f'scope.locales: {nr["locale"]} was not run ({nr["reason"]}) but is not listed as not_run')

    # patch
    pp = rep['proposed_patch']
    if pp != cands_doc['proposed_patch']:
        errs.append('proposed_patch differs from candidates.json')
    if pp:
        if not (a.candidates.parent / pp['path']).is_file():
            errs.append(f'proposed_patch: {pp["path"]} does not exist')
        want = {}
        for f in keys['findings']:
            if f['kind'] == 'missing_key' and str(f.get('missing_in', '')).endswith('.json'):
                want.setdefault(f['missing_in'], []).append(f['key'])
        if {k: sorted(v) for k, v in pp['keys_added'].items()} != {k: sorted(v) for k, v in want.items()}:
            errs.append('proposed_patch: keys_added do not match the missing JSON keys')

    # headline
    hl = rep['headline']
    if hl['translation_quality'] != 'untested' and not any(re.match(r'Translations reviewed by \S', x) for x in rep['limitations']):
        errs.append('headline.translation_quality must be untested unless a reviewer checked the translations '
                    '(say who, as "Translations reviewed by ..." in limitations)')
    exp = {'verdict': 'issues_found' if rep['findings'] else 'none_detected',
           'severity_counts': dict(Counter(f['severity'] for f in rep['findings'])), 'key_coverage': keys['key_coverage']}
    partial = any(l['status'] != 'checked' for l in rep['scope']['locales']) or bool(rep['needs_review']) or \
        any(u['status'] == 'not_assessed' for u in keys['unused_check']) or bool(res.get('not_parsed'))
    exp['evidence_completeness'] = 'partial' if partial else 'complete'
    exp['overall_confidence'] = 'medium' if partial or any(f['confidence'] != 'high' for f in rep['findings']) else 'high'
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if CLAIMS.search(hl['summary']):
        errs.append('headline.summary claims the product is fully localised; the best verdict is none_detected, scoped')

    if a.write_summary and not errs:
        a.report.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'headline written to {a.report}')
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} error(s)')
        return 1
    print(f'VALID: {len(rep["findings"])} finding(s), {len(rep["triage"])} candidate(s) triaged, verdict {hl["verdict"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
