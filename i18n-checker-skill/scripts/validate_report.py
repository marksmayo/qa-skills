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
  * key coverage or headline counts differ from the inputs, or the summary's counts ("13 issues", "2 High",
    "55 need review", "4 dismissed") differ from the report;
  * the summary or a finding title claims readiness the evidence can't show: "RTL-ready" (or "supports RTL") with no
    RTL locale checked, and "ready for localisation", "localisation-ready", "ready for any language" at all (tools
    show problems, never readiness);
  * a dismissal's reason is vague (shorter than 25 characters, or "false positive" / "not needed" / "not relevant"
    with nothing checkable: no quoted text, file or line);
  * the limitations leave out what wasn't checked: a not_run or excluded locale (by code or name), resource files
    that were not parsed, a text-walk pseudo-locale, or the absence of any translation resource / second locale;
  * a single-locale product (candidates.json `single_locale`: no locale ships besides the source) has a verdict
    other than `readiness_only` (or the more cautious `not_assessed`), or a summary that doesn't say only one
    locale ships.

Evidence is `partial` (and confidence at most medium) when a locale wasn't checked, something needs review, unused
keys weren't assessed, a resource file wasn't parsed, no resource set was found, the code scan read no file, or the
pseudo-locale was a text walk (which can't tell hard-coded text from translated text), or the product is
single-locale. For a single-locale product the verdict is `readiness_only`. When neither resources nor
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
NEG = re.compile(r"(?:\bnot|n't|\bnever|\bno longer|\bwithout)\W+(?:\w+\W+){0,2}$", re.I)
RTL_CLAIM = re.compile(r'\bRTL[- ](?:ready|safe|compatible|proof)\b|\bready for RTL\b|\bsupports? (?:full )?RTL\b|'
                       r'\bRTL (?:is |was )?(?:fully )?(?:supported|handled|fine|correct|ok)\b|\bmirrors? correctly\b', re.I)
READY_CLAIM = re.compile(r'\bready (?:for|to be) (?:locali[sz]\w*|translat\w*|internationali[sz]\w*|any language|all languages|'
                         r'other languages|every language|new languages)|\b(?:locali[sz]ation|translation|i18n|l10n|'
                         r'internationali[sz]ation)[- ]ready\b|\b(?:supports?|works in|handles) (?:any|all|every) languages?\b|'
                         r'\bfully internationali[sz]ed\b', re.I)
VAGUE = re.compile(r"^\W*(?:false[- ]positive|not (?:needed|relevant|applicable|an issue|a problem|important|required|"
                   r"necessary|a bug)|irrelevant|n/?a|ok|fine|ignored?|won'?t fix|out of scope|by design|expected|noise|"
                   r"unimportant|not i18n)\b", re.I)
CHECKABLE = re.compile(r'''['"“‘`]\S|\b[\w./-]+\.\w{1,5}(?::\d+)?\b|\bline \d+|\b(?:brand|product|proper) (?:name|noun)''', re.I)
NO_RES = re.compile(r'\bno (?:translation |locale |message |i18n |language )?(?:resources?|catalog(?:ue)?s?|translation files?|'
                    r'message files?|locale files?|i18n library)\b|\bwithout (?:any )?(?:translation )?resources\b|'
                    r'\bsingle[- ]locale\b|\bno (?:second|other|further) (?:locale|language)\b', re.I)
LANG_NAMES = {'ar': 'Arabic', 'he': 'Hebrew', 'fa': 'Persian', 'ur': 'Urdu', 'de': 'German', 'fr': 'French', 'es': 'Spanish',
              'it': 'Italian', 'pt': 'Portuguese', 'nl': 'Dutch', 'sv': 'Swedish', 'da': 'Danish', 'nb': 'Norwegian',
              'no': 'Norwegian', 'fi': 'Finnish', 'pl': 'Polish', 'cs': 'Czech', 'ru': 'Russian', 'uk': 'Ukrainian',
              'el': 'Greek', 'tr': 'Turkish', 'ja': 'Japanese', 'zh': 'Chinese', 'ko': 'Korean', 'hi': 'Hindi', 'th': 'Thai',
              'vi': 'Vietnamese', 'id': 'Indonesian', 'ms': 'Malay', 'hu': 'Hungarian', 'ro': 'Romanian', 'bg': 'Bulgarian',
              'sw': 'Swahili', 'en': 'English', 'ca': 'Catalan', 'hr': 'Croatian', 'sr': 'Serbian', 'sk': 'Slovak'}
_NAMES = '|'.join(sorted(set(LANG_NAMES.values())))
SINGLE = re.compile(r'\bsingle[- ]locale\b|\b(?:one|a single) (?:locale|language)\b|\bno (?:second|other|further) '
                    r'(?:locale|language)\b|\b(?:' + _NAMES + r')[- ]only\b|\bonly (?:in )?(?:' + _NAMES + r')\b', re.I)
COUNT_NOUN = r'(issues?|findings?|problems?)'


def single_locale(cands_doc: dict) -> bool:
    """No locale ships besides the source (older candidates.json without the flag: no shipped locale)."""
    if 'single_locale' in cands_doc:
        return bool(cands_doc['single_locale'])
    return not cands_doc.get('shipped_locales')


def claim_errors(text: str, where: str, rtl_checked: bool) -> list[str]:
    """Readiness the evidence can't show. A negated claim ("not RTL-ready") is fine."""
    errs = []
    for m in RTL_CLAIM.finditer(text):
        if not rtl_checked and not NEG.search(text[max(0, m.start() - 30):m.start()]):
            errs.append(f'{where} claims {m.group(0)!r}, but no RTL locale was checked: say RTL was not assessed')
    for m in READY_CLAIM.finditer(text):
        if not NEG.search(text[max(0, m.start() - 30):m.start()]):
            errs.append(f'{where} claims {m.group(0)!r}: tools show localisation problems, never that a product is ready for '
                        'localisation; scope the claim to what was checked')
    return errs


def summary_count_errors(summary: str, rep: dict) -> list[str]:
    """Counts the summary states must equal the report's."""
    errs = []
    sev = lambda level: sum(1 for f in rep['findings'] if f['severity'] == level)  # noqa: E731
    for m in re.finditer(r'\b(\d+)\s+(?:(Critical|High|Medium|Low)\s+)?(?:([\w-]+)\s+)?' + COUNT_NOUN + r'\b', summary):
        n, level, qual = int(m.group(1)), m.group(2), (m.group(3) or '').lower()
        if level:
            if n != sev(level):
                errs.append(f'headline.summary says {n} {level} {m.group(4)}, but the report has {sev(level)} {level} finding(s)')
        elif qual in ('readiness', 'agent', 'static', 'runtime', 'confirmed', 'possible', 'more', 'other', 'android', 'web'):
            continue
        elif n != len(rep['findings']):
            errs.append(f'headline.summary says {n} {m.group(4)}, but the report has {len(rep["findings"])} finding(s)')
    for m in re.finditer(r'\b(\d+)\s+(Critical|High|Medium|Low)\b(?!\s+(?:[\w-]+\s+)?' + COUNT_NOUN + r'\b)', summary):
        if int(m.group(1)) != sev(m.group(2)):
            errs.append(f'headline.summary says {m.group(1)} {m.group(2)}, but the report has {sev(m.group(2))} {m.group(2)} '
                        'finding(s)')
    for m in re.finditer(r'\b(\d+)\s+(?:[\w-]+\s+){0,2}?(?:need|needs|needing|awaiting|left for)\s+(?:a\s+)?review\b', summary):
        if int(m.group(1)) != len(rep['needs_review']):
            errs.append(f'headline.summary says {m.group(1)} need review, but needs_review has {len(rep["needs_review"])}')
    dismissed = sum(1 for t in rep['triage'] if t['decision'] == 'dismissed')
    for m in re.finditer(r'\b(\d+)\s+(?:[\w-]+\s+){0,2}?(?:were\s+|are\s+)?dismissed\b', summary):
        if int(m.group(1)) != dismissed:
            errs.append(f'headline.summary says {m.group(1)} dismissed, but triage dismissed {dismissed}')
    return errs


def limitation_errors(rep: dict, res: dict, rt: dict | None, single: bool) -> list[str]:
    """SKILL.md rule 9: what wasn't checked must be in limitations."""
    errs, lim = [], ' '.join(rep['limitations'])
    for l in rep['scope']['locales']:
        if l['status'] in ('not_run', 'excluded'):
            code = l['code']
            name = LANG_NAMES.get(code.split('-')[0].lower())
            if not re.search(r'(?<![\w-])' + re.escape(code) + r'(?![\w-])', lim, re.I) and not (name and name.lower() in lim.lower()):
                errs.append(f'limitations: {code} is {l["status"]} but no limitation names it')
    np = [x['file'] for x in res.get('not_parsed') or []]
    if np and not re.search(r'not (?:been )?(?:parsed|read)|unparsed|could not (?:be )?(?:parsed|read)', lim, re.I) and \
            not all(f in lim for f in np):
        errs.append(f'limitations: {len(np)} file(s) that look like resources were not parsed ({", ".join(np[:3])}) and no '
                    'limitation says so')
    text_walk = (rt or {}).get('pseudo_method') == 'text_walk' or ((rep['scope'].get('pseudo') or {}).get('method') == 'text_walk')
    if text_walk and not re.search(r'text[- ]walk', lim, re.I):
        errs.append('limitations: the pseudo-locale was a text walk (untranslated text not assessed, attributes and later '
                    'text not pseudo-localised) and no limitation says so')
    if (not res.get('sets') or single) and not (NO_RES.search(lim) or SINGLE.search(lim)):
        errs.append('limitations: no translation resource set or second locale exists, and no limitation says so (key, '
                    'placeholder and plural checks were not assessed)')
    return errs


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def expected_headline(rep: dict, cands_doc: dict, keys: dict, res: dict, rt: dict | None) -> dict:
    inputs = cands_doc.get('inputs') or {}
    no_sets = not res.get('sets')
    no_code = inputs.get('code_files_scanned') == 0
    rendered = bool(rt and any(r.get('ok') for r in rt.get('runs', [])))
    text_walk = (rt or {}).get('pseudo_method') == 'text_walk' or ((rep['scope'].get('pseudo') or {}).get('method') == 'text_walk')
    nothing = no_sets and no_code and not rendered
    single = single_locale(cands_doc)
    exp = {'verdict': 'not_assessed' if nothing else 'readiness_only' if single else
           'issues_found' if rep['findings'] else 'none_detected',
           'severity_counts': dict(Counter(f['severity'] for f in rep['findings'])), 'key_coverage': keys['key_coverage']}
    partial = any(l['status'] != 'checked' for l in rep['scope']['locales']) or bool(rep['needs_review']) or \
        any(u['status'] == 'not_assessed' for u in keys['unused_check']) or bool(res.get('not_parsed')) or \
        no_sets or no_code or text_walk or single
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
        return want in ('none_detected', 'readiness_only') and have == 'not_assessed'
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
        if t['decision'] == 'dismissed':
            why = t['reason'].strip()
            if len(why) < 25 or (VAGUE.search(why) and not CHECKABLE.search(why)):
                errs.append(f'triage: {t["candidate_id"]} is dismissed with a vague reason ({why!r}); say what makes it '
                            'safe to drop (the text, the file or line, why it is never translated)')
        if t['decision'] == 'needs_review' and t['candidate_id'] not in {n['candidate_id'] for n in rep['needs_review']}:
            errs.append(f'triage: {t["candidate_id"]} is needs_review but not in needs_review[]')

    rtl_codes = {c.split('-')[0] for c in rep['scope'].get('rtl_locales') or []} | {'ar', 'he', 'fa', 'ur', 'yi', 'ps', 'sd', 'ug', 'dv'}
    rtl_checked = any(l['status'] == 'checked' and l['code'].split('-')[0] in rtl_codes for l in rep['scope']['locales'])
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
        errs += claim_errors(f['title'], f'{f["id"]}.title', rtl_checked)

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
    errs += claim_errors(hl['summary'], 'headline.summary', rtl_checked)
    errs += summary_count_errors(hl['summary'], rep)
    single = single_locale(cands_doc)
    if hl['verdict'] == 'readiness_only' and not SINGLE.search(hl['summary']):
        errs.append('headline.summary: a readiness_only verdict must say the product ships a single locale '
                    '("single-locale", "English only", "no second locale")')
    errs += limitation_errors(rep, res, rt, single)
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
