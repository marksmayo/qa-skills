#!/usr/bin/env python3
"""Validate a privacy audit against its contract and its run, and recompute every rating and count.

    python validate_privacy_audit.py audit.json --candidates run/candidates.json --capture run/capture.json
        --canaries run/canaries.json [--evidence run] [--repo app] [--recompute] [--write-summary]

Fails when: any output or evidence file holds something that looks like personal data (an email address, a
phone or card number, a canary value outside canaries.json) or a secret; a transfer finding has neither a
captured request nor a code path; a vendor is named without a list entry or code import; a category isn't in
data/categories.json; a sensitivity, flow, reach, score, priority, floor or cap is wrong; agent judgment is
above pattern, or a pattern finding is P0; a consent-state claim names a state that wasn't tested, or the
capture doesn't show it; a legal reference list differs from data/provisions.json; a candidate isn't triaged
exactly once, or a dismissal isn't supported by its facts; the headline counts differ; or any text says the
app is compliant. --recompute re-runs merge_candidates.py from the recorded inputs and compares.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from match_canaries import EMAIL, PHONE_RX, CARD, luhn, encodings  # noqa: E402

DATA = HERE.parent / 'data'
CATS = json.loads((DATA / 'categories.json').read_text(encoding='utf-8'))
PROV = json.loads((DATA / 'provisions.json').read_text(encoding='utf-8'))
ORDER = ['P0', 'P1', 'P2', 'P3']
FLOW = {'undeclared_third_party': 3, 'before_consent': 3, 'after_reject': 3, 'declared_unexpected': 2, 'logs': 2,
        'insecure_storage': 2, 'url': 2, 'no_use': 1}
TRANSFER = {'personal_data_to_third_party', 'session_replay_unmasked', 'undeclared_processor', 'cross_border_transfer',
            'tracking_before_consent', 'tracking_after_reject'}
JUDGMENT = {'excess_collection'}
DISMISS = {'strictly_necessary', 'functional_no_identifier', 'constant_in_source', 'pii_disabled', 'duplicate', 'false_match', 'not_personal_data'}
BANNED = re.compile(r'\b(is|are|fully|now|be)\s+compliant\b|\bGDPR[- ]ready\b|\bmeets the APPs\b|\bcompliant with\b|\bprivacy[- ]safe\b', re.I)
SECRET = re.compile(r'AKIA[0-9A-Z]{16}|sk_live_[0-9a-zA-Z]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[bp]-[0-9A-Za-z-]{10,}|ghp_[0-9A-Za-z]{30,}')


def rate(f: dict) -> tuple[int, int, int, int]:
    sens = max((CATS['categories'][c]['sensitivity'] for c in f['categories']), default=1)
    if f.get('children'):
        sens = min(4, sens + 1)
    flow = FLOW[f['flow_basis']]
    return sens, flow, f['reach'], sens * flow * f['reach']


def priority(score: int) -> str:
    return 'P0' if score >= 27 else 'P1' if score >= 18 else 'P2' if score >= 8 else 'P3'


def expected(f: dict) -> tuple[str, str | None, str | None]:
    p = priority(rate(f)[3])
    floor = cap = None
    special = any(c in CATS['special'] for c in f['categories'])
    if special and f.get('recipient_kind') in ('third_party', 'declared_processor'):
        floor, p = 'special_category_to_third_party', 'P0'
    elif f['type'] == 'tracking_after_reject' and ORDER.index(p) > 1:
        floor, p = 'tracking_after_reject', 'P1'
    if f['confidence'] == 'pattern' and p == 'P0':
        cap, p = 'pattern_not_P0', 'P1'
    return p, floor, cap


def pii_problems(text: str, where: str, canary_values: list[str]) -> list[str]:
    out = []
    for m in EMAIL.finditer(text):
        out.append(f'{where}: email-like value {m.group(0)[:3]}...')
    for m in PHONE_RX.finditer(text):
        out.append(f'{where}: phone-like value')
    for m in CARD.finditer(text):
        if luhn(re.sub(r'\D', '', m.group(0))):
            out.append(f'{where}: card-number-like value')
    low = text.lower()
    for v in canary_values:
        if v.lower() in low:
            out.append(f'{where}: an unredacted canary value')
    if SECRET.search(text):
        out.append(f'{where}: a secret-like value')
    return out


def summary(rep: dict, cands: dict) -> dict:
    counts = {p: sum(1 for f in rep['findings'] if f['priority'] == p) for p in ORDER}
    m = {r['state']: r for r in cands['consent_matrix']}
    third = lambda s: [d for d in m.get(s, {}).get('third_party_domains', [])]  # noqa: E731
    return {'counts': counts, 'third_parties_before_consent': third('before'), 'third_parties_after_reject': third('reject'),
            'special_category_flows': sorted(f['id'] for f in rep['findings'] if any(c in CATS['special'] for c in f['categories']))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('audit', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--capture', type=Path, required=True)
    ap.add_argument('--canaries', type=Path, required=True)
    ap.add_argument('--evidence', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--recompute', action='store_true')
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    rep = json.loads(a.audit.read_text(encoding='utf-8'))
    cfile = json.loads(a.candidates.read_text(encoding='utf-8'))
    cap = json.loads(a.capture.read_text(encoding='utf-8'))
    canaries = json.loads(a.canaries.read_text(encoding='utf-8'))
    cands = {c['id']: c for c in cfile['candidates']}
    errs: list[str] = []
    e = errs.append

    for k in ('schema_version', 'skill', 'scope', 'metadata', 'inventory', 'recipients', 'consent_matrix', 'findings', 'triage', 'headline', 'limitations'):
        if k not in rep:
            e(f'missing top-level field {k}')
    if errs:
        print('\n'.join(errs)); return 1

    # --- personal data and secrets anywhere in the outputs
    values = sorted({v for f, c in canaries['fields'].items() for k, v in encodings(f, c['value']).items()
                     if k in ('raw', 'url', 'url_plus', 'url_double', 'base64', 'e164', 'digits')}, key=len, reverse=True)
    files = [a.audit, a.candidates, a.capture]
    if a.evidence:
        files += [p for p in a.evidence.rglob('*') if p.is_file() and p.suffix in ('.json', '.txt', '.md', '.log', '.har', '.csv')
                  and p.resolve() != a.canaries.resolve()]
    for p in dict.fromkeys(files):
        errs += pii_problems(p.read_text(encoding='utf-8', errors='replace'), p.name, values)
    if not canaries.get('synthetic'):
        e('the canary set is not marked synthetic')
    if cap.get('canary_set') != canaries['id'] or rep['metadata'].get('canary_set') != canaries['id']:
        e('canary set id differs between the audit, the capture and canaries.json')

    # --- scope and states
    tested = {s['state'] for s in cap['states'] if s['status'] == 'tested'}
    if sorted(rep['scope'].get('consent_states_tested', [])) != sorted(tested):
        e(f'scope.consent_states_tested {rep["scope"].get("consent_states_tested")} differs from the capture ({sorted(tested)})')
    if rep['consent_matrix'] != cfile['consent_matrix']:
        e('consent_matrix differs from the candidates file')
    if rep['recipients'] != cfile['recipients']:
        e('recipients differ from the candidates file')
    regions = rep['scope'].get('regions') or []
    if sorted(regions) != sorted(cfile['regions']):
        e(f'scope.regions {regions} differ from the regions the candidates were built with {cfile["regions"]}')

    lists_vendors = {r['vendor'] for r in cfile['recipients'] if r.get('vendor')}
    static_vendors = {c.get('vendor') for c in cfile['candidates'] if c.get('source') == 'static' and c.get('vendor')}

    # --- findings
    seen_ids = set()
    for f in rep['findings']:
        fid = f.get('id', '?')
        if fid in seen_ids:
            e(f'{fid}: duplicate id')
        seen_ids.add(fid)
        for k in ('type', 'title', 'categories', 'flow_basis', 'reach', 'reach_source', 'confidence', 'evidence', 'references', 'fix', 'verification', 'candidates'):
            if k not in f or f[k] in (None, '', []):
                if not (k == 'categories' and f.get('type', '').startswith('tracking_')):
                    e(f'{fid}: missing {k}')
        if errs and fid in errs[-1]:
            continue
        for c in f['categories']:
            if c not in CATS['categories']:
                e(f'{fid}: category {c!r} is not in data/categories.json')
        if f['type'] not in PROV['types']:
            e(f'{fid}: unknown type {f["type"]}')
            continue
        if f['flow_basis'] not in FLOW:
            e(f'{fid}: flow_basis {f["flow_basis"]!r}')
            continue
        if f['reach'] not in (1, 2, 3):
            e(f'{fid}: reach {f["reach"]}')
            continue
        sens, flow, reach, score = rate(f)
        if (f.get('sensitivity'), f.get('flow'), f.get('score')) != (sens, flow, score):
            e(f'{fid}: sensitivity/flow/score {f.get("sensitivity")}/{f.get("flow")}/{f.get("score")} should be {sens}/{flow}/{score}')
        p, floor, cap_ = expected(f)
        if f.get('priority') != p or f.get('floor') != floor or f.get('cap') != cap_:
            e(f'{fid}: priority/floor/cap {f.get("priority")}/{f.get("floor")}/{f.get("cap")} should be {p}/{floor}/{cap_}')
        if f['confidence'] not in ('observed', 'traced', 'pattern'):
            e(f'{fid}: confidence {f["confidence"]!r}')
        if f['type'] in JUDGMENT and f['confidence'] != 'pattern':
            e(f'{fid}: agent judgment ({f["type"]}) must be pattern confidence')
        own = [cands.get(c) for c in f['candidates']]
        if None in own:
            e(f'{fid}: names a candidate that is not in the candidates file'); continue
        if f['type'] not in {c['type'] for c in own}:
            e(f'{fid}: type {f["type"]} is not the type of any of its candidates')
        live = [c for c in own if c['source'] == 'live']
        if f['confidence'] == 'observed' and not live:
            e(f'{fid}: observed without a live candidate')
        if f['confidence'] == 'traced' and not f['evidence'].get('code_path'):
            e(f'{fid}: traced without a code path')
        if f['type'] in TRANSFER and not live and not f['evidence'].get('code_path'):
            e(f'{fid}: a transfer finding needs a captured request or a code path')
        cand_fields = sorted({x for c in own for x in c.get('fields', [])})
        cand_cats = sorted({x for c in own for x in c.get('categories', [])})
        if cand_cats and not set(cand_cats) <= set(f['categories']):
            e(f'{fid}: categories {f["categories"]} leave out {sorted(set(cand_cats) - set(f["categories"]))} from its candidates')
        if f.get('fields') is not None and cand_fields and sorted(f['fields']) != cand_fields:
            e(f'{fid}: fields {f.get("fields")} differ from its candidates ({cand_fields})')
        if f.get('vendor') and f['vendor'] not in lists_vendors | static_vendors:
            e(f'{fid}: vendor {f["vendor"]!r} is not named by the domain list or a code import')
        if f.get('recipient_kind'):
            kinds = {c.get('recipient') for c in live if c.get('recipient')}
            if kinds and f['recipient_kind'] not in kinds:
                e(f'{fid}: recipient_kind {f["recipient_kind"]} differs from its candidates ({sorted(kinds)})')
        states = f.get('consent_states') or []
        for s in states:
            if s not in tested:
                e(f'{fid}: consent state {s!r} was not tested')
        live_states = {s for c in live for s in (c.get('states') or ([c['state']] if c.get('state') else []))}
        if states and live and not set(states) <= live_states:
            e(f'{fid}: consent states {states} are not all shown by its candidates ({sorted(live_states)})')
        if f['flow_basis'] == 'before_consent' and not any(c['type'] == 'tracking_before_consent' or 'before' in (c.get('states') or []) for c in live):
            e(f'{fid}: flow_basis before_consent, but no candidate shows the before state')
        if f['flow_basis'] == 'after_reject' and not any(c['type'] == 'tracking_after_reject' or 'reject' in (c.get('states') or []) for c in live):
            e(f'{fid}: flow_basis after_reject, but no candidate shows the reject state')
        if f['flow_basis'] == 'undeclared_third_party' and not any(c.get('recipient') == 'third_party' for c in live):
            e(f'{fid}: flow_basis undeclared_third_party, but no candidate has an undeclared recipient')
        want_refs = {r: PROV['types'][f['type']][r] for r in ('EU', 'UK', 'AU') if r in regions or (r == 'UK' and 'EU' in regions and 'UK' in regions)}
        if f['references'] != want_refs:
            e(f'{fid}: references differ from data/provisions.json for regions {regions}')
        if a.repo:
            for cp in f['evidence'].get('code_path', []):
                p_ = a.repo / cp['file']
                if not p_.exists() or cp['line'] > len(p_.read_text(encoding='utf-8').splitlines()):
                    e(f'{fid}: code path {cp["file"]}:{cp["line"]} not found')
        for r in f['evidence'].get('requests', []):
            st = next((s for s in cap['states'] if s['state'] == r['state']), None)
            req = next((x for x in (st or {}).get('requests', []) if x['n'] == r['n']), None)
            if not req:
                e(f'{fid}: evidence request {r["state"]}#{r["n"]} is not in the capture')
            elif r.get('url') and r['url'] != req['url'][:len(r['url'])]:
                e(f'{fid}: evidence request {r["state"]}#{r["n"]} URL differs from the capture')

    # --- triage
    seen: dict = {}
    for t in rep['triage']:
        cid = t.get('candidate')
        if cid in seen:
            e(f'candidate {cid} triaged twice')
        seen[cid] = t
        c = cands.get(cid)
        if not c:
            e(f'triage names {cid}, not in the candidates file'); continue
        d = t.get('decision')
        if d == 'promoted':
            f = next((x for x in rep['findings'] if x['id'] == t.get('finding')), None)
            if not f or cid not in f['candidates']:
                e(f'{cid}: promoted to {t.get("finding")}, which does not list it')
        elif d == 'inventory':
            if not t.get('reason'):
                e(f'{cid}: inventory without a reason')
            if c['type'] in ('tracking_after_reject', 'sensitive_category_flow') or any(x in CATS['special'] for x in c.get('categories', [])):
                e(f'{cid}: a {c["type"]} candidate or a special category cannot be recorded as inventory only')
        elif d == 'dismissed':
            code = t.get('reason_code')
            if code not in DISMISS or not t.get('reason'):
                e(f'{cid}: dismissal needs a reason_code from {sorted(DISMISS)} and a reason')
            if code == 'strictly_necessary' and not (c.get('recipient') == 'first_party' and (c.get('cookie_flags') or {}).get('http_only')):
                e(f'{cid}: strictly_necessary needs a first-party HttpOnly cookie')
            if code == 'functional_no_identifier':
                st = next((s for s in cap['states'] if s['state'] == c.get('state')), {'requests': []})
                hit = any(r['matches'] for r in st['requests'] if (r['host'] or '').endswith(c.get('domain', '?')))
                if c.get('cookies') or c.get('vendor_category') in ('analytics', 'advertising', 'session_replay') or hit:
                    e(f'{cid}: functional_no_identifier, but the candidate has cookies, a tracking category or carried a canary')
            if code == 'constant_in_source':
                found = a.repo and any(c.get('value', '\0') in p.read_text(encoding='utf-8', errors='replace')
                                       for p in a.repo.rglob('*') if p.is_file() and p.suffix in ('.js', '.html', '.py', '.ts'))
                if not found:
                    e(f'{cid}: constant_in_source, but the value was not found in the repo (pass --repo)')
            if code == 'pii_disabled' and (c.get('facts') or {}).get('default_pii') is not False:
                e(f'{cid}: pii_disabled, but the candidate does not show default PII off')
            if any(x in CATS['special'] for x in c.get('categories', [])) and code != 'false_match':
                e(f'{cid}: a special-category candidate can only be dismissed as a false match')
        else:
            e(f'{cid}: decision must be promoted, inventory or dismissed')
    missing = sorted(set(cands) - set(seen))
    if missing:
        e(f'candidates not triaged: {missing}')
    for f in rep['findings']:
        for cid in f['candidates']:
            if (seen.get(cid) or {}).get('finding') != f['id']:
                e(f'{f["id"]}: lists {cid}, but triage does not promote it there')

    # --- inventory
    for item in rep['inventory']:
        if item.get('category') not in CATS['categories']:
            e(f'inventory {item.get("field")}: category {item.get("category")!r} unknown')
        for r in item.get('recipients', []):
            ok = any(item['field'] in (c.get('fields') or []) and c.get('domain') == r['domain'] for c in cfile['candidates'] if c['source'] == 'live')
            if not ok:
                e(f'inventory {item["field"]}: recipient {r["domain"]} has no live candidate carrying that field')

    # --- language and headline
    blob = json.dumps(rep, ensure_ascii=False)
    m = BANNED.search(blob)
    if m:
        e(f'the audit says {m.group(0)!r}: never call an app compliant or private')
    if PROV['disclaimer'] not in rep['headline'].get('disclaimer', ''):
        e('headline.disclaimer must carry the disclaimer from data/provisions.json')
    want = summary(rep, cfile)
    if a.write_summary:
        rep['headline'].update(want)
        a.audit.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    for k, v in want.items():
        if rep['headline'].get(k) != v:
            e(f'headline.{k} {rep["headline"].get(k)!r} should be {v!r}')
    untested = [r['state'] for r in cfile['consent_matrix'] if r['status'] != 'tested']
    if untested and not any(s in ' '.join(rep['limitations']) for s in untested):
        e(f'consent states {untested} were not tested, but the limitations do not say so')

    if a.recompute:
        inp = cfile['inputs']
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'c.json'
            cmd = [sys.executable, str(HERE / 'merge_candidates.py'), '--capture', cfile['capture'], '--flows', inp['flows'], '--lists', inp['lists'],
                   '--regions', inp['regions'], '--out', str(out)]
            if cfile.get('static'):
                cmd += ['--static', cfile['static']]
            if inp.get('declared'):
                cmd += ['--declared', inp['declared']]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                e(f'recompute failed: {r.stderr.strip()[-300:]}')
            else:
                again = json.loads(out.read_text(encoding='utf-8'))
                for k in ('candidates', 'recipients', 'consent_matrix'):
                    if again[k] != cfile[k]:
                        e(f'recompute: {k} differ from the candidates file')

    if errs:
        print(f'{a.audit}: {len(errs)} problem(s)')
        for x in errs:
            print('  -', x)
        return 1
    c = want['counts']
    print(f'{a.audit}: valid. {len(rep["findings"])} finding(s) (P0 {c["P0"]}, P1 {c["P1"]}, P2 {c["P2"]}, P3 {c["P3"]}); {len(seen)} candidate(s) triaged; '
          f'no personal data or secrets in {len(dict.fromkeys(files))} file(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
