#!/usr/bin/env python3
"""Validate an i18n report against the schema, the candidates, the evidence and the rules.

    python validate_report.py report.json --candidates run/candidates.json --keys run/keys.json --resources run/resources.json
        --repo examples/bookclub [--runtime run/runtime/runtime.json] [--write-summary]

Fails when:
  * a finding's kind, status, locales, ratings, locations, keys or evidence differ from its candidate, or a
    candidate is missing from triage or triaged twice;
  * an agent finding (`source: "agent"`, no candidate) has no `basis`, a basis excerpt that is not on its line in
    --repo, or high confidence;
  * a static finding has no file and line, or the line is past the end of the file; a runtime finding has no
    screenshot (on disk) or no measurement;
  * a locale has no status, a not_run locale has no reason, or a shipped locale is missing from scope;
  * the summary says "fully localised", "fully translated" or similar, or translation_quality isn't untested
    without reviewer evidence; an untranslated finding has high confidence;
  * the proposed patch is marked applied, its file is missing, or its keys don't match the missing JSON keys;
  * key coverage or headline counts differ from the inputs.

Evidence is `partial` (and confidence at most medium) when a locale wasn't checked, something needs review, unused
keys weren't assessed, a resource file wasn't parsed, no resource set was found, the code scan read no file, or the
pseudo-locale was a text walk (which can't tell hard-coded text from translated text). When neither resources nor
code were analysed and nothing rendered, the verdict is `not_assessed` and confidence low. A headline more cautious
than the computed one (partial for complete, a lower confidence, not_assessed for none_detected) is accepted.
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
CONF = {'high': 2, 'medium': 1, 'low': 0}


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def expected_headline(rep: dict, cands_doc: dict, keys: dict, res: dict, rt: dict | None) -> dict:
    inputs = cands_doc.get('inputs') or {}
    no_sets = not res.get('sets')
    no_code = inputs.get('code_files_scanned') == 0
    rendered = bool(rt and any(r.get('ok') for r in rt.get('runs', [])))
    text_walk = (rt or {}).get('pseudo_method') == 'text_walk' or ((rep['scope'].get('pseudo') or {}).get('method') == 'text_walk')
    nothing = no_sets and no_code and not rendered
    exp = {'verdict': 'issues_found' if rep['findings'] else 'not_assessed' if nothing else 'none_detected',
           'severity_counts': dict(Counter(f['severity'] for f in rep['findings'])), 'key_coverage': keys['key_coverage']}
    partial = any(l['status'] != 'checked' for l in rep['scope']['locales']) or bool(rep['needs_review']) or \
        any(u['status'] == 'not_assessed' for u in keys['unused_check']) or bool(res.get('not_parsed')) or \
        no_sets or no_code or text_walk
    exp['evidence_completeness'] = 'partial' if partial else 'complete'
    exp['overall_confidence'] = 'low' if nothing else \
        'medium' if partial or any(f['confidence'] != 'high' for f in rep['findings']) else 'high'
    return exp


def cautious_enough(k: str, have, want) -> bool:
    """A headline value more cautious than the computed one is honest and accepted."""
    if k == 'evidence_completeness':
        return have == 'partial'
    if k == 'overall_confidence':
        return CONF.get(have, 9) <= CONF[want]
    if k == 'verdict':
        return want == 'none_detected' and have == 'not_assessed'
    return False


def check_basis(f: dict, repo: Path) -> list[str]:
    errs = []
    if not f.get('basis'):
        return [f'{f["id"]}: an agent finding needs a basis [{{file, line, excerpt}}] that --repo can re-read']
    for b in f['basis']:
        p = (repo / b['file'])
        try:
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            errs.append(f'{f["id"]}: basis file {b["file"]} not found in --repo')
            continue
        norm = lambda s: ' '.join(str(s).split())  # noqa: E731
        if not (1 <= b['line'] <= len(lines)) or norm(b['excerpt']) not in norm(lines[b['line'] - 1]):
            errs.append(f'{f["id"]}: basis excerpt is not on {b["file"]}:{b["line"]} (re-read with --repo)')
    return errs


def validate(rep: dict, cands_doc: dict, keys: dict, res: dict, repo: Path, rt: dict | None,
             shots_dir: Path | None = None, cand_dir: Path | None = None, write_summary: bool = False) -> list[str]:
    errs: list[str] = []
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
        if f.get('source') == 'agent':
            if f['candidate_id'] is not None:
                errs.append(f'{f["id"]}: an agent finding has no candidate (candidate_id null)')
            errs += check_basis(f, repo)
            if f['confidence'] == 'high':
                errs.append(f'{f["id"]}: an agent finding is never high confidence')
        else:
            c = cands.get(f['candidate_id'])
            if not c:
                errs.append(f'{f["id"]}: candidate {f["candidate_id"]} does not exist (a finding from review is source "agent" with a basis)')
                continue
            for k in FIELDS:
                if f[k] != c[k]:
                    errs.append(f'{f["id"]}: {k} differs from candidate {c["id"]}')
            if [e.get('screenshot') for e in f['evidence']] != [e.get('screenshot') for e in c['evidence']]:
                errs.append(f'{f["id"]}: evidence differs from candidate {c["id"]}')
        for loc in f['locations']:
            p = repo / loc['file']
            if not p.is_file():
                errs.append(f'{f["id"]}: {loc["file"]} does not exist')
            elif loc['line'] is None or loc['line'] < 1 or loc['line'] > len(p.read_text(encoding='utf-8', errors='replace').splitlines()):
                errs.append(f'{f["id"]}: {loc["file"]} has no line {loc["line"]}')
        if not f['locations'] and not f['evidence'] and not f.get('basis'):
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
        if cand_dir and not (cand_dir / pp['path']).is_file():
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
    for k, v in expected_headline(rep, cands_doc, keys, res, rt).items():
        if hl[k] != v and not cautious_enough(k, hl[k], v):
            if write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if CLAIMS.search(hl['summary']):
        errs.append('headline.summary claims the product is fully localised; the best verdict is none_detected, scoped')
    return errs


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
    errs = [f'schema: {e}' for e in schema_check.errors(rep, load(HERE.parent / 'schemas' / 'i18n_report.schema.json'))]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1
    rt = load(a.runtime) if a.runtime else None
    errs = validate(rep, cands_doc, keys, res, a.repo, rt, shots_dir=a.runtime.parent if a.runtime else None,
                    cand_dir=a.candidates.parent, write_summary=a.write_summary)
    hl = rep['headline']
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
