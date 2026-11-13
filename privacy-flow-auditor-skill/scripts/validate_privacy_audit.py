#!/usr/bin/env python3
"""Validate a privacy audit against its contract and its run, and recompute every rating and count.

    python validate_privacy_audit.py audit.json --candidates run/candidates.json --capture run/capture.json
        --canaries run/canaries.json [--evidence run] [--repo app] [--recompute] [--write-summary]
    python validate_privacy_audit.py audit.json --candidates run/candidates.json [--repo app] [--recompute]   # static only

Fails when: any output or evidence file holds something that looks like personal data (an email address, a
phone or card number, a canary value outside canaries.json) or a secret; a transfer finding has neither a
captured request nor a code path; a vendor is named without a list entry or code import; a category isn't in
data/categories.json; a sensitivity, flow, reach, score, priority, floor or cap is wrong; agent judgment is
above pattern, or a pattern finding is P0; a consent-state claim names a state that wasn't tested, or the
capture doesn't show it; a legal reference list differs from data/provisions.json; a candidate isn't triaged
exactly once, or a dismissal isn't supported by its facts; the headline counts differ; a consent state that
wasn't tested is summarised as a list (it must be null, listed in states_not_tested, and the statement must say
it wasn't tested rather than "none"); requests were blocked in the browser and the limitations don't say so; the
consent choice's own record was blocked and no limitation says so; screenshots are in the evidence and no
limitation says what they may show; or any text says the app is compliant.

run_status (complete, partial or not_run, with run_status_reason) must equal the candidates file's. A not_run
audit (merge_candidates.py without --capture) needs no capture or canaries; it must test no consent state, and
it fails on anything that depends on the browser run: an observed finding, a captured request as evidence,
consent states on a finding, live candidates, recipients, or a statement that the run found nothing or that
consent is respected.

Findings found by reading the code rather than from a candidate set "source": "agent" and carry "basis": a list of
{"file", "line", "excerpt"} (re-read with --repo: the excerpt must be on that line) or {"evidence": path} (a file
in --evidence). They are at most traced, and never observed without runtime evidence.

--write-summary fills the computed fields (headline counts and states; each finding's sensitivity, flow, score,
priority, floor and cap when they are missing) and writes the audit back; --fix-ratings also rewrites computed
rating fields that are wrong, printing each change (the categories, flow basis, reach and confidence stay yours).
--recompute re-runs merge_candidates.py from the recorded inputs and compares.

An app with no consent banner (consent.banner null in flows.json) has one state, no_banner, and before, reject and
accept are not_applicable: the headline carries consent_banner false and states_not_applicable, its third parties
"before consent" are the no_banner state's, and the statement must say there is no consent banner. In a not_run
audit, a sentence of the statement or a title that says where data goes ("sent only to", "never leaves") must say
it is what the code does ("by the code", "the static scan"). A browser storage-state file (session cookies) in the
evidence fails; a signed-in capture needs metadata.session and a limitation about the session. File upload and
parsing points from the static scan must be followed up in the inventory, scope or limitations.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from match_canaries import EMAIL, PHONE_RX, CARD, card_like, encodings  # noqa: E402
from domains import registrable  # noqa: E402

DATA = HERE.parent / 'data'
CATS = json.loads((DATA / 'categories.json').read_text(encoding='utf-8'))
PROV = json.loads((DATA / 'provisions.json').read_text(encoding='utf-8'))
VENDORS = json.loads((DATA / 'vendors.json').read_text(encoding='utf-8'))
HOST_RX = re.compile(r'(?<![\w.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])', re.I)


def vendors_named_in(texts) -> set[str]:
    """Vendors data/vendors.json lists for a host that appears in texts (a basis excerpt or a code path line),
    with their aliases and parent company: an agent finding traced to a known vendor's endpoint may name it."""
    out = set()
    for t in texts:
        for m in HOST_RX.finditer(t or ''):
            host = m.group(1).lower()
            entry = VENDORS['domains'].get(host) or VENDORS['domains'].get(registrable(host))
            if entry:
                v = entry['vendor']
                info = VENDORS.get('vendors', {}).get(v, {})
                out |= {v, *info.get('aliases', []), *([info['company']] if info.get('company') else [])}
    return out
ORDER = ['P0', 'P1', 'P2', 'P3']
FLOW = {'undeclared_third_party': 3, 'before_consent': 3, 'after_reject': 3, 'declared_unexpected': 2, 'logs': 2,
        'insecure_storage': 2, 'url': 2, 'no_use': 1}
TRANSFER = {'personal_data_to_third_party', 'session_replay_unmasked', 'undeclared_processor', 'cross_border_transfer',
            'tracking_before_consent', 'tracking_after_reject'}
JUDGMENT = {'excess_collection'}
DISMISS = {'strictly_necessary', 'functional_no_identifier', 'constant_in_source', 'pii_disabled', 'duplicate', 'false_match',
           'not_personal_data', 'random_per_load_id'}
BANNED = re.compile(r'\b(is|are|fully|now|be)\s+compliant\b|\bGDPR[- ]ready\b|\bmeets the APPs\b|\bcompliant with\b|\bprivacy[- ]safe\b', re.I)
SECRET = re.compile(r'AKIA[0-9A-Z]{16}|sk_live_[0-9a-zA-Z]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[bp]-[0-9A-Za-z-]{10,}|ghp_[0-9A-Za-z]{30,}')
CSRF_COOKIE = re.compile(r'csrf|xsrf', re.I)
# a statement that only a browser run could support, in an audit whose browser run did not happen
RUNTIME_CLAIM = re.compile(r'\b(no|none of the|zero)\s+(third[- ]part(y|ies)|trackers?|tracking|requests?|vendors?|canar(y|ies)|cookies?)\b'
                           r'|\bconsent (is|was) (respected|honou?red)\b|\bnothing (is|was) sent\b|\b(capture|live run|browser run) (showed|found|confirmed)\b', re.I)
SKIP_DIRS = {'node_modules', '.git', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv', '__pycache__', '.turbo', '.cache'}
SOURCE_SUFFIXES = ('.js', '.mjs', '.cjs', '.jsx', '.ts', '.mts', '.cts', '.tsx', '.html', '.htm', '.py', '.vue', '.svelte', '.rb', '.json')
IMAGES = ('.png', '.jpg', '.jpeg', '.webp')
RATING = ('sensitivity', 'flow', 'score', 'priority', 'floor', 'cap')
# a positive claim about where data goes or doesn't go: in a not_run audit only the code can support it, so the sentence
# must say it is what the code does ("by the code", "statically", "the scan")
RECIPIENT_CLAIM = re.compile(r"\b(sent|send|sends|goes|go|flows?|reach(es)?|leaves?|shared|transmitted|disclosed)\b[^.;]{0,30}\b(only|solely|exclusively|just)\b"
                             r"|\b(only|solely|exclusively)\b[^.;]{0,30}\b(sent|goes|flows?|reach(es)?|shared|transmitted|disclosed)\b"
                             r"|\b(never|does not|doesn't|do not|don't|is not|isn't|are not|aren't)\s+(leave|leaves|reach|reaches|sent|shared|transmitted)\b"
                             r"|\bstays? (on|in|within) (the )?(app|first[- ]party|browser|device|own)", re.I)
CODE_QUALIFIER = re.compile(r'\b(code|static(ally)?|source|scan(ned)?|by reading|as written|in the repository)\b', re.I)
NO_BANNER_TEXT = re.compile(r'\bno (consent|cookie) (banner|prompt|dialog)|\bwithout a (consent|cookie) (banner|prompt)|\bhas no banner\b', re.I)
FILE_KINDS = ('file_upload', 'file_parse', 'form_data_upload')


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


def ratable(f: dict) -> bool:
    return (f.get('flow_basis') in FLOW and f.get('reach') in (1, 2, 3) and isinstance(f.get('categories'), list)
            and all(c in CATS['categories'] for c in f['categories']) and f.get('confidence') in ('observed', 'traced', 'pattern'))


def fill_ratings(f: dict) -> None:
    """--write-summary: the computed rating fields, where the finding leaves them out."""
    if not ratable(f) or all(k in f for k in RATING):
        return
    sens, flow, _, score = rate(f)
    p, floor, cap = expected(f)
    for k, v in zip(RATING, (sens, flow, score, p, floor, cap)):
        f.setdefault(k, v)


def pii_problems(text: str, where: str, canary_values: list[str]) -> list[str]:
    out = []
    for m in EMAIL.finditer(text):
        out.append(f'{where}: email-like value {m.group(0)[:3]}...')
    for m in PHONE_RX.finditer(text):
        out.append(f'{where}: phone-like value')
    for m in CARD.finditer(text):
        if card_like(text, m):
            out.append(f'{where}: card-number-like value')
    low = text.lower()
    for v in canary_values:
        if v.lower() in low:
            out.append(f'{where}: an unredacted canary value')
    if SECRET.search(text):
        out.append(f'{where}: a secret-like value')
    return out


def summary(rep: dict, cands: dict) -> dict:
    """Headline facts recomputed from the findings and the consent matrix. A state that wasn't tested has no list
    of third parties (null), never an empty one, and is named in states_not_tested with its reason."""
    counts = {p: sum(1 for f in rep['findings'] if f.get('priority') == p) for p in ORDER}
    m = {r['state']: r for r in cands['consent_matrix']}

    def third(s):
        row = m.get(s) or {}
        return list(row.get('third_party_domains', [])) if row.get('status') == 'tested' else None
    not_tested = {r['state']: (r.get('reason') or r['status']) for r in cands['consent_matrix'] if r['status'] not in ('tested', 'not_applicable')}
    no_banner = 'no_banner' in m
    out = {'counts': counts, 'third_parties_before_consent': third('no_banner' if no_banner else 'before'),
           'third_parties_after_reject': third('reject'), 'states_not_tested': not_tested,
           'special_category_flows': sorted(f['id'] for f in rep['findings'] if any(c in CATS['special'] for c in f.get('categories') or []))}
    if no_banner:  # no banner: everything happens "before consent", and there is no choice to reject or accept
        out['consent_banner'] = False
        out['states_not_applicable'] = {r['state']: r.get('reason') or 'no consent banner' for r in cands['consent_matrix']
                                        if r['status'] == 'not_applicable'}
    return out


NOT_TESTED = re.compile(r"\b(not tested|untested|could ?n[o']t be tested|was ?n[o']t tested|were ?n[o']t tested|not run|did ?n[o']t run|was ?n[o']t run)\b", re.I)
NONE_CLAIM = {'reject': re.compile(r'(after|following|once)\W+(the\W+)?(user\W+)?(rejects?|rejection|reject all|"reject all")[^.;]{0,40}?\b(none|nothing|no (third|tracking|request|vendor))', re.I),
              'before': re.compile(r'before\W+(any\W+|a\W+)?(consent|choice)[^.;]{0,40}?\b(none|nothing|no (third|tracking|request|vendor))', re.I),
              'accept': re.compile(r'(after|following|once)\W+(the\W+)?(user\W+)?(accepts?|acceptance|accept all|"accept all")[^.;]{0,40}?\b(none|nothing|no (third|tracking|request|vendor))', re.I)}


def walk_sources(repo: Path):
    """Source files under repo, pruning dependency and build directories before descending."""
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if n.endswith(SOURCE_SUFFIXES):
                yield Path(root) / n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('audit', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--capture', type=Path, help='required unless the candidates file is static only (run_status not_run)')
    ap.add_argument('--canaries', type=Path, help='required with --capture')
    ap.add_argument('--evidence', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--recompute', action='store_true')
    ap.add_argument('--write-summary', action='store_true')
    ap.add_argument('--fix-ratings', action='store_true',
                    help="rewrite each finding's computed rating fields (sensitivity, flow, score, priority, floor, cap) from its "
                         'categories, flow_basis, reach and confidence, listing every change; the inputs stay yours')
    a = ap.parse_args()
    rep = json.loads(a.audit.read_text(encoding='utf-8'))
    cfile = json.loads(a.candidates.read_text(encoding='utf-8'))
    run_status = cfile.get('run_status') or 'complete'
    static_only = run_status == 'not_run'
    if not static_only and not (a.capture and a.canaries):
        ap.error('this run has a live capture: pass --capture and --canaries')
    cap = json.loads(a.capture.read_text(encoding='utf-8')) if a.capture else {'states': []}
    canaries = json.loads(a.canaries.read_text(encoding='utf-8')) if a.canaries else None
    cands = {c['id']: c for c in cfile['candidates']}
    errs: list[str] = []
    e = errs.append

    for k in ('schema_version', 'skill', 'scope', 'metadata', 'inventory', 'recipients', 'consent_matrix', 'findings', 'triage', 'headline', 'limitations'):
        if k not in rep:
            e(f'missing top-level field {k}')
    if errs:
        print('\n'.join(errs)); return 1
    if a.write_summary or a.fix_ratings:
        for f in rep['findings']:
            if a.fix_ratings and ratable(f):
                sens, flow, _, score = rate(f)
                p_, floor_, cap_r = expected(f)
                for k, v in zip(RATING, (sens, flow, score, p_, floor_, cap_r)):
                    if k in f and f[k] != v:
                        print(f'{f.get("id")}: {k} {f[k]!r} -> {v!r}')
                    f[k] = v
            fill_ratings(f)
        if a.fix_ratings and not a.write_summary:
            a.audit.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')

    # --- run status
    if rep.get('run_status') != run_status:
        e(f'run_status {rep.get("run_status")!r} should be {run_status!r} (from the candidates file)')
    if run_status != 'complete' and not (rep.get('run_status_reason') or '').strip():
        e(f'run_status {run_status}: run_status_reason must say why')

    # --- personal data and secrets anywhere in the outputs
    values = sorted({v for f, c in (canaries or {'fields': {}})['fields'].items() for k, v in encodings(f, c['value']).items()
                     if k in ('raw', 'url', 'url_plus', 'url_double', 'base64', 'e164', 'digits')}, key=len, reverse=True)
    files = [a.audit, a.candidates] + ([a.capture] if a.capture else [])
    images = []
    if a.evidence:
        for p in a.evidence.rglob('*'):
            if not p.is_file() or (a.canaries and p.resolve() == a.canaries.resolve()):
                continue
            if p.suffix in ('.json', '.txt', '.md', '.log', '.har', '.csv'):
                files.append(p)
            elif p.suffix.lower() in IMAGES:
                images.append(p)
    for p in dict.fromkeys(files):
        text_ = p.read_text(encoding='utf-8', errors='replace')
        errs += pii_problems(text_, p.name, values)
        if p.suffix == '.json' and '"origins"' in text_ and '"cookies"' in text_:
            try:
                d_ = json.loads(text_)
            except ValueError:
                d_ = None
            if isinstance(d_, dict) and 'origins' in d_ and 'cookies' in d_:
                e(f'{p.name} looks like a browser storage-state file (session cookies and tokens): it must never be in the evidence')
    if cap.get('auth') and not any(re.search(r'sign(ed)?[- ]in|session|log(ged)?[- ]in|storage[- ]state', x, re.I) for x in rep['limitations']):
        e('the capture ran in a signed-in session (storage state or login recipe), but no limitation says whose session and what it covers')
    if cap.get('auth') and not rep['metadata'].get('session'):
        e('the capture ran in a signed-in session: metadata.session must say whose (a test account), never the session itself')
    if canaries is not None:
        if not canaries.get('synthetic'):
            e('the canary set is not marked synthetic')
        if cap.get('canary_set') != canaries['id'] or rep['metadata'].get('canary_set') != canaries['id']:
            e('canary set id differs between the audit, the capture and canaries.json')
    if images and not any(re.search(r'screenshot', x, re.I) and re.search(r'canar|typed|personal', x, re.I) for x in rep['limitations']):
        e(f'{len(images)} screenshot(s) are in the evidence, but no limitation says what they may show (masked canary fields; a canary '
          'the page echoes elsewhere can still appear) - images are not scanned for personal data')

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
    if static_only:
        if any(c.get('source') == 'live' for c in cfile['candidates']) or cfile['recipients']:
            e('a not_run candidates file has live candidates or recipients')
        if any(r['status'] not in ('not_run', 'not_applicable') for r in cfile['consent_matrix']):
            e('a not_run audit must have every consent state not_run (or not_applicable when the app has no consent banner)')

    lists_vendors = {r['vendor'] for r in cfile['recipients'] if r.get('vendor')}
    static_vendors = {c.get('vendor') for c in cfile['candidates'] if c.get('source') == 'static' and c.get('vendor')}
    static_vendors |= {c.get('vendor') for c in cfile['candidates'] if c.get('subtype') == 'first_party_proxy' and c.get('vendor')}

    # --- findings
    seen_ids = set()
    for f in rep['findings']:
        fid = f.get('id', '?')
        agent = f.get('source') == 'agent'
        if fid in seen_ids:
            e(f'{fid}: duplicate id')
        seen_ids.add(fid)
        n_err = len(errs)
        for k in ('type', 'title', 'categories', 'flow_basis', 'reach', 'reach_source', 'confidence', 'evidence', 'references', 'fix', 'verification', 'candidates'):
            if k not in f or f[k] in (None, '', []):
                if not (k == 'categories' and f.get('type', '').startswith(('tracking_', 'first_party_beacon'))) and not (k == 'candidates' and agent and 'candidates' in f):
                    e(f'{fid}: missing {k}')
        if len(errs) > n_err:
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
        if f['confidence'] not in ('observed', 'traced', 'pattern'):
            e(f'{fid}: confidence {f["confidence"]!r}')
            continue
        sens, flow, reach, score = rate(f)
        if (f.get('sensitivity'), f.get('flow'), f.get('score')) != (sens, flow, score):
            e(f'{fid}: sensitivity/flow/score {f.get("sensitivity")}/{f.get("flow")}/{f.get("score")} should be {sens}/{flow}/{score} '
              f'({sens} x {flow} x reach {reach}; --fix-ratings rewrites the computed fields, --write-summary fills them when left out)')
        p, floor, cap_ = expected(f)
        if f.get('priority') != p or f.get('floor') != floor or f.get('cap') != cap_:
            e(f'{fid}: priority/floor/cap {f.get("priority")}/{f.get("floor")}/{f.get("cap")} should be {p}/{floor}/{cap_}')
        if f['type'] in JUDGMENT and f['confidence'] != 'pattern':
            e(f'{fid}: agent judgment ({f["type"]}) must be pattern confidence')
        if static_only:
            if f['confidence'] == 'observed':
                e(f'{fid}: observed, but the browser run did not happen (run_status not_run)')
            if f['evidence'].get('requests'):
                e(f'{fid}: cites captured requests, but the browser run did not happen')
            if f.get('consent_states'):
                e(f'{fid}: names consent states {f["consent_states"]}, but none was tested (run_status not_run)')
        own = [cands.get(c) for c in f['candidates']]
        if None in own:
            e(f'{fid}: names a candidate that is not in the candidates file'); continue
        if agent:
            basis = f.get('basis')
            if not isinstance(basis, list) or not basis:
                e(f'{fid}: found by review (source agent) without a basis'); continue
            runtime = False
            for b in basis:
                if 'evidence' in b:
                    runtime = True
                    root = a.evidence or a.audit.parent
                    if not (root / b['evidence']).is_file() and not (a.audit.parent / b['evidence']).is_file():
                        e(f'{fid}: basis evidence {b["evidence"]} does not exist')
                elif {'file', 'line', 'excerpt'} <= set(b):
                    if a.repo:
                        p_ = a.repo / b['file']
                        lines = p_.read_text(encoding='utf-8', errors='replace').splitlines() if p_.is_file() else []
                        if not isinstance(b['line'], int) or not 1 <= b['line'] <= len(lines):
                            e(f'{fid}: basis {b["file"]}:{b["line"]} not found')
                        elif ' '.join(b['excerpt'].split()) not in ' '.join(lines[b['line'] - 1].split()):
                            e(f'{fid}: basis excerpt is not on {b["file"]}:{b["line"]}')
                else:
                    e(f'{fid}: each basis item needs file, line and excerpt, or evidence')
            if f['confidence'] == 'observed' and not (runtime and not static_only):
                e(f'{fid}: found by review, observed only with runtime evidence from a browser run')
            if f['type'] in TRANSFER and not own and not f['evidence'].get('code_path') and not runtime:
                e(f'{fid}: a transfer finding needs a captured request or a code path')
        else:
            if f['type'] not in {c['type'] for c in own}:
                e(f'{fid}: type {f["type"]} is not the type of any of its candidates')
        live = [c for c in own if c['source'] == 'live']
        static_own = [c for c in own if c['source'] == 'static']
        if f['confidence'] == 'observed' and not live and not agent:
            e(f'{fid}: observed without a live candidate')
        if f['confidence'] == 'traced' and not f['evidence'].get('code_path') and not (agent and any('file' in b for b in f.get('basis') or [])):
            e(f'{fid}: traced without a code path')
        if f['type'] in TRANSFER and not agent and not live and not f['evidence'].get('code_path'):
            e(f'{fid}: a transfer finding needs a captured request or a code path')
        cand_fields = sorted({x for c in own for x in c.get('fields', [])})
        cand_cats = sorted({x for c in own for x in c.get('categories', [])})
        if cand_cats and not set(cand_cats) <= set(f['categories']):
            e(f'{fid}: categories {f["categories"]} leave out {sorted(set(cand_cats) - set(f["categories"]))} from its candidates')
        if f.get('fields') is not None and cand_fields and sorted(f['fields']) != cand_fields:
            e(f'{fid}: fields {f.get("fields")} differ from its candidates ({cand_fields})')
        traced_vendors = set()
        if agent and f.get('vendor'):
            texts = [b.get('excerpt') for b in f.get('basis') or [] if isinstance(b, dict)]
            if a.repo:  # the code path lines themselves, re-read, so a host named on them counts too
                for cp in f['evidence'].get('code_path', []) or []:
                    p_ = a.repo / str(cp.get('file', ''))
                    ls = p_.read_text(encoding='utf-8', errors='replace').splitlines() if p_.is_file() else []
                    if isinstance(cp.get('line'), int) and 1 <= cp['line'] <= len(ls):
                        texts.append(ls[cp['line'] - 1])
            traced_vendors = vendors_named_in(texts)
        if f.get('vendor') and f['vendor'] not in lists_vendors | static_vendors | traced_vendors:
            e(f'{fid}: vendor {f["vendor"]!r} is not named by the domain list, a code import, or (for a finding found by review) '
              'a data/vendors.json host on its basis or code path')
        if f['flow_basis'] == 'declared_unexpected' and not (cfile.get('declared') or (rep.get('metadata') or {}).get('declared_processors')):
            e(f'{fid}: flow_basis declared_unexpected, but no declared processors were given (metadata.declared_processors '
              'is null and the candidates were merged without --declared)')
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
        static_before = f['confidence'] == 'pattern' and any(c['type'] == 'tracking_before_consent' for c in static_own)
        if f['flow_basis'] == 'before_consent' and not static_before and not any(
                c['type'] == 'tracking_before_consent' or {'before', 'no_banner'} & set(c.get('states') or []) or c.get('before_choice') for c in live):
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
                if not p_.exists() or cp['line'] > len(p_.read_text(encoding='utf-8', errors='replace').splitlines()):
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
            facts = c.get('facts') or {}
            if code not in DISMISS or not t.get('reason'):
                e(f'{cid}: dismissal needs a reason_code from {sorted(DISMISS)} and a reason')
            if code == 'strictly_necessary':
                flags = c.get('cookie_flags') or {}
                csrf = bool(CSRF_COOKIE.search(c.get('cookie') or '')) and re.search(r'csrf|xsrf|double[- ]submit', t.get('reason', ''), re.I)
                if not (c.get('recipient') == 'first_party' and (flags.get('http_only') or csrf)):
                    e(f'{cid}: strictly_necessary needs a first-party HttpOnly cookie, or a first-party CSRF cookie (name with csrf/xsrf, '
                      'readable by design for a double-submit check) whose reason says so')
            if code == 'functional_no_identifier':
                st = next((s for s in cap['states'] if s['state'] == c.get('state')), {'requests': []})
                hit = any(r['matches'] for r in st['requests'] if (r['host'] or '').endswith(c.get('domain', '?')))
                if c.get('cookies') or c.get('vendor_category') in ('analytics', 'advertising', 'session_replay') or hit:
                    e(f'{cid}: functional_no_identifier, but the candidate has cookies, a tracking category or carried a canary')
            if code == 'constant_in_source':
                found = a.repo and any(c.get('value', '\0') in p.read_text(encoding='utf-8', errors='replace') for p in walk_sources(a.repo))
                if not found:
                    e(f'{cid}: constant_in_source, but the value was not found in the repo (pass --repo)')
            if code == 'random_per_load_id':
                per_load = c.get('subtype') == 'possible_hashed_identifier' and not facts.get('value_in_several_states') and (
                    facts.get('differs_every_request') or (facts.get('known_sdk_id_key') and facts.get('distinct_values', 0) > 1))
                if not per_load:
                    e(f'{cid}: random_per_load_id needs hex values that differ from request to request and never recur in another '
                      'consent state (a fresh browser context): the candidate\'s facts do not show that')
            if code == 'pii_disabled' and facts.get('default_pii') is not False and facts.get('masked_by_default') is not True:
                e(f'{cid}: pii_disabled, but the candidate does not show default PII off or SDK-default masking')
            if any(x in CATS['special'] for x in c.get('categories', [])) and code != 'false_match':
                e(f'{cid}: a special-category candidate can only be dismissed as a false match')
        else:
            e(f'{cid}: decision must be promoted, inventory or dismissed')
    missing = sorted(set(cands) - set(seen))
    if missing:
        e(f'candidates not triaged: {missing}')
    for f in rep['findings']:
        for cid in f.get('candidates') or []:
            if (seen.get(cid) or {}).get('finding') != f['id']:
                e(f'{f["id"]}: lists {cid}, but triage does not promote it there')

    # --- file uploads the static scan found: what the files hold is unknown until someone says
    static_path = Path(cfile['static']) if cfile.get('static') else None
    if static_path and static_path.is_file():
        st_ = json.loads(static_path.read_text(encoding='utf-8'))
        ups = [c for c in st_.get('collection', []) if c.get('kind') in FILE_KINDS]
        said = json.dumps([rep['inventory'], rep['limitations'], rep['scope'].get('not_covered'), rep['scope'].get('covered')], ensure_ascii=False)
        # a bare "file" ("417 files scanned") does not count: the follow-up must speak of uploads (or attachments,
        # spreadsheets, a dropzone or file input), and name each file that holds an upload point when there are several
        if ups and not re.search(r'\b(upload(s|ed|ing)?|spreadsheets?|attachments?|dropzones?|file (inputs?|pickers?|parsing))\b', said, re.I):
            e(f'the static scan found {len(ups)} file upload or parsing point(s) ({ups[0]["file"]}:{ups[0]["line"]}), but the inventory, scope '
              'and limitations say nothing about uploads and what the files hold')
        elif ups:
            unnamed = sorted({c['file'] for c in ups if c['file'] not in said and c['file'].rsplit('/', 1)[-1] not in said})
            if len({c['file'] for c in ups}) > 1 and unnamed:
                e(f'the static scan found file upload or parsing points in {len({c["file"] for c in ups})} files, but the inventory, scope and '
                  f'limitations do not name {unnamed[:4]}: say what each upload point takes')

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
    untested = [r['state'] for r in cfile['consent_matrix'] if r['status'] not in ('tested', 'not_applicable')]
    if untested and not static_only and not any(s in ' '.join(rep['limitations']) for s in untested):
        e(f'consent states {untested} were not tested, but the limitations do not say so')
    statement = rep['headline'].get('statement', '')
    if untested and not NOT_TESTED.search(statement):
        e(f'headline.statement must say that {untested} {"was" if len(untested) == 1 else "were"} not tested')
    for s_ in untested:
        mm = NONE_CLAIM.get(s_) and NONE_CLAIM[s_].search(statement)
        if mm:
            e(f'headline.statement summarises the untested {s_} state as {mm.group(0)!r}: say it was not tested')
    if want.get('consent_banner') is False and not NO_BANNER_TEXT.search(statement):
        e('the app has no consent banner (consent.banner null): the headline statement must say so ("no consent banner")')
    if static_only:
        for where, text in [('headline.statement', statement)] + [(f'{f["id"]} title', f.get('title', '')) for f in rep['findings']]:
            for sent in re.split(r'(?<=[.;:!?])\s+', text):  # a clause: "Static review: X is sent only to Y" still needs its own qualifier
                mm = RECIPIENT_CLAIM.search(sent)
                if mm and not CODE_QUALIFIER.search(sent):
                    e(f'{where} says {mm.group(0)!r} as a fact about the running app, but the browser run did not happen (not_run); '
                      'say it is what the code does ("by the code", "the static scan")')
        if not any(re.search(r'not run|did ?n[o\']t run|static only|no (live|browser) (capture|run)', x, re.I) for x in rep['limitations']):
            e('run_status not_run: a limitation must say the browser capture was not run (consent states, live requests and canaries untested)')
        for where, text in [('headline.statement', statement)] + [(f'{f["id"]} title', f.get('title', '')) for f in rep['findings']]:
            mm = RUNTIME_CLAIM.search(text)
            if mm:
                e(f'{where} says {mm.group(0)!r}, which only a browser run could show; the run did not happen (not_run)')
    # requests the browser blocked never reached the vendor: the report must say what that hides
    blocked = cfile.get('blocked_domains') or sorted({r['host'] for s_ in cap['states'] for r in s_.get('requests', []) if r.get('blocked') == 'third_party'})
    if blocked and not any(re.search(r'\bblock', x, re.I) and re.search(r'third[- ]part|vendor', x, re.I) for x in rep['limitations']):
        e(f'requests to {blocked[:5]} were blocked in the browser, but no limitation says so (blocked vendors set no cookies and send no follow-up hits)')
    if any(r.get('consent_writes_blocked') for r in cfile['consent_matrix']) and not any(
            re.search(r'consent', x, re.I) and re.search(r'block|not (recorded|sent|stored)', x, re.I) for x in rep['limitations']):
        e('the consent choice triggered a write the harness blocked (the site\'s own record of the choice was never made), '
          'but no limitation says so')
    if cfile.get('blocked_first_party_writes') and not any(re.search(r'first[- ]party', x, re.I) and re.search(r'block|write|POST', x, re.I)
                                                           for x in rep['limitations']):
        e('first-party writes were blocked (telemetry, CSP reports, consent records), but no limitation says what that hides')
    if (cap.get('harness') or {}).get('bypass_csp') and not any(re.search(r'\bCSP\b|Content[- ]Security[- ]Policy', x, re.I) for x in rep['limitations']):
        e('the capture ran with the site\'s Content Security Policy bypassed (bypass_csp), but no limitation says so: a vendor call '
          'the CSP would stop could appear sent, and CSP-blocked attempts were not seen')
    for r_ in rep['recipients']:
        if r_.get('blocked_requests') and r_['blocked_requests'] > r_.get('requests', r_['blocked_requests']):
            e(f'recipient {r_["domain"]}: more blocked requests than requests')

    if a.recompute:
        inp = cfile['inputs']
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'c.json'
            cmd = [sys.executable, str(HERE / 'merge_candidates.py'), '--regions', inp['regions'], '--out', str(out)]
            if cfile.get('capture'):
                cmd += ['--capture', cfile['capture'], '--flows', inp['flows'], '--lists', inp['lists']]
            else:
                cmd += ['--run-status-reason', cfile.get('run_status_reason') or '']
                if inp.get('lists'):
                    cmd += ['--lists', inp['lists']]
                if inp.get('flows'):
                    cmd += ['--flows', inp['flows']]
            if cfile.get('static'):
                cmd += ['--static', cfile['static']]
            if inp.get('declared'):
                cmd += ['--declared', inp['declared']]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                e(f'recompute failed: {r.stderr.strip()[-300:]}')
            else:
                again = json.loads(out.read_text(encoding='utf-8'))
                for k in ('candidates', 'recipients', 'consent_matrix', 'run_status'):
                    if again.get(k) != cfile.get(k):
                        e(f'recompute: {k} differ from the candidates file')

    if errs:
        print(f'{a.audit}: {len(errs)} problem(s)')
        for x in errs:
            print('  -', x)
        return 1
    c = want['counts']
    print(f'{a.audit}: valid ({run_status}). {len(rep["findings"])} finding(s) (P0 {c["P0"]}, P1 {c["P1"]}, P2 {c["P2"]}, P3 {c["P3"]}); '
          f'{len(seen)} candidate(s) triaged; no personal data or secrets in {len(dict.fromkeys(files))} file(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
