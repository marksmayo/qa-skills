#!/usr/bin/env python3
"""Validate a WCAG audit (structure and honesty rules).

    python validate_audit.py audit.json [--run <run-dir>] [--candidates candidates.json] [--write-summary]

Checks the report against schemas/audit.schema.json, then the rules a schema can't express:

- the verdict is never "conforms": `does_not_conform` needs a high-confidence A/AA failure, and
  anything else is `not_established`;
- every SC at the target version and level has exactly one result; tool passes alone never make
  an SC `passed`, and an SC no tool can check needs agent or manual evidence to pass; `untested`
  SCs are in the manual test plan and `cantTell` SCs have needs-review items;
- severity follows the rubric: the failure pattern's default, one level up on a critical path,
  and Critical for any non-interference SC (1.4.2, 2.1.2, 2.2.2, 2.3.1);
- confidence: agent judgment is never high; high needs two agreeing sources or an ACT rule;
- priority, ranking, severity counts, SC coverage, evidence completeness and overall confidence
  are recomputed.

--run re-checks tool statuses, the check matrix, the sample and screenshots against run.json, and
that an SC passed on probe evidence had its probes run on every planned check (a probe whose
injection failed measured nothing). --candidates checks every candidate was triaged exactly once,
that finding sources come from the candidates they cite, and that no contrast candidate whose
estimate is below the threshold, whose background is an image or video, or that is a group with no
estimate, is dismissed. --write-summary fills or recomputes the headline numbers in place (not
the verdict or its text) before anything is checked, so a draft may leave them empty. Exit code 0
when valid, 1 when invalid.
"""
from __future__ import annotations
import argparse, json, math, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / 'data'
SCHEMA = HERE.parent / 'schemas' / 'audit.schema.json'
SEVERITY = ['minor', 'moderate', 'serious', 'critical']
SEVERITY_W = {'critical': 4, 'serious': 3, 'moderate': 2, 'minor': 1}
CONF_ORDER = {'high': 0, 'medium': 1, 'low': 2}
LEVEL_ORDER = {'A': 0, 'AA': 1, 'AAA': 2}
VERSION_ORDER = {'2.0': 0, '2.1': 1, '2.2': 2}
REQUIRED_ENGINES = ['axe-core', 'ibm-equal-access', 'htmlcs']
BANNED_VERDICT = re.compile(r'(?i)(?<!not )(?<!n\'t )\b(conforms|compliant|conformant|fully accessible|is accessible|meets wcag|passes wcag)\b')
VAGUE_REASON = re.compile(r'(?i)^\W*(false positive|fp|not an issue|not a problem|n/?a|ok|fine|ignore|noise)\W*$')


# ---------------------------------------------------------------- minimal JSON Schema (the subset the schema uses)
def schema_errors(value, schema: dict, root: dict, path: str = '$') -> list[str]:
    if '$ref' in schema:
        node = root
        for part in schema['$ref'].lstrip('#/').split('/'):
            node = node[part]
        return schema_errors(value, node, root, path)
    errs = []
    if 'const' in schema and value != schema['const']:
        return [f'{path}: must be {schema["const"]!r}']
    if 'enum' in schema and value not in schema['enum']:
        return [f'{path}: {value!r} is not one of {schema["enum"]}']
    t = schema.get('type')
    if t:
        types = t if isinstance(t, list) else [t]
        ok = any((tt == 'object' and isinstance(value, dict)) or (tt == 'array' and isinstance(value, list)) or
                 (tt == 'string' and isinstance(value, str)) or (tt == 'boolean' and isinstance(value, bool)) or
                 (tt == 'integer' and isinstance(value, int) and not isinstance(value, bool)) or
                 (tt == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)) or
                 (tt == 'null' and value is None) for tt in types)
        if not ok:
            return [f'{path}: expected {"/".join(types)}, got {type(value).__name__}']
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            errs.append(f'{path}: shorter than {schema["minLength"]} characters')
        if schema.get('pattern') and not re.search(schema['pattern'], value):
            errs.append(f'{path}: {value!r} does not match {schema["pattern"]}')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errs.append(f'{path}: below {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            errs.append(f'{path}: above {schema["maximum"]}')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errs.append(f'{path}: needs at least {schema["minItems"]} item(s)')
        if 'items' in schema:
            for i, v in enumerate(value):
                errs += schema_errors(v, schema['items'], root, f'{path}[{i}]')
    if isinstance(value, dict):
        for k in schema.get('required', []):
            if k not in value:
                errs.append(f'{path}: missing {k!r}')
        props = schema.get('properties', {})
        extra = schema.get('additionalProperties', True)
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], root, f'{path}.{k}')
            elif extra is False:
                errs.append(f'{path}: unexpected field {k!r}')
    return errs


# ---------------------------------------------------------------- rubric
def load_scs() -> dict:
    return {s['sc']: s for s in json.loads((DATA / 'wcag-sc.json').read_text(encoding='utf-8'))['success_criteria']}


def target_scs(scs: dict, version: str, level: str) -> list[str]:
    out = [s for s, m in scs.items() if VERSION_ORDER[m['added_in']] <= VERSION_ORDER[version] and LEVEL_ORDER[m['level']] <= LEVEL_ORDER[level]]
    return sorted(out, key=lambda s: [int(x) for x in s.split('.')])


def expected_severity(f: dict) -> str:
    sev = f['base_severity']
    if f.get('critical_path'):
        sev = SEVERITY[min(SEVERITY.index(sev) + 1, 3)]
    if f['non_interference']:
        sev = 'critical'
    return sev


def reach(f: dict, n_pages: int) -> int:
    pages = len(set(f['pages']))
    if pages >= 2 and pages >= n_pages / 2:
        return 3
    return 2 if pages >= 2 else 1


def leverage(f: dict) -> int:
    return 2 if len(f['instances']) >= 2 else 1


def band(f: dict, score: int) -> str:
    b = 'P0' if score >= 24 else 'P1' if score >= 12 else 'P2' if score >= 6 else 'P3'
    if f['severity'] == 'critical' and b in ('P2', 'P3'):
        b = 'P1'
    if f['severity'] == 'serious' and b == 'P3':
        b = 'P2'
    if f.get('blocks_critical_path'):
        b = 'P0'
    if f['evidence_type'] == 'agent_judgment' and b == 'P0':
        b = 'P1'
    return b


def rank_key(f: dict):
    return (f['priority']['band'], -f['priority']['score'], -SEVERITY.index(f['severity']), CONF_ORDER[f['confidence']])


def completeness(checks: list[dict]) -> float:
    if not checks:
        return 0.0
    return round(sum(1 for c in checks if c['status'] == 'ran') / len(checks), 2)


def overall_confidence(d: dict, comp: float) -> str:
    engines = {t['name']: t['status'] for t in d['metadata']['tools']}
    all_engines = all(engines.get(e) == 'ran' for e in REQUIRED_ENGINES)
    # a probe cut short (partial: the keyboard probe stopped at its press limit) measured only part of the page
    partial = any(c['status'] == 'partial' for c in d['metadata']['checks'])
    if comp >= 0.9 and all_engines and d['assisted_review']['scs_reviewed'] and not partial:
        return 'high'
    return 'medium' if comp >= 0.6 else 'low'


def compute_summary(d: dict) -> dict:
    counts = {k: 0 for k in ('critical', 'serious', 'moderate', 'minor')}
    for f in d['findings']:
        counts[f['severity']] += 1
    cov = {'total': len(d['sc_results']), **{k: sum(1 for r in d['sc_results'] if r['outcome'] == k)
                                            for k in ('failed', 'passed', 'cantTell', 'inapplicable', 'untested')}}
    comp = completeness(d['metadata']['checks'])
    return {'severity_counts': counts, 'sc_coverage': cov, 'evidence_completeness': comp,
            'overall_confidence': overall_confidence(d, comp),
            'blocked_critical_paths': sorted({f['critical_path']['name'] for f in d['findings'] if f.get('blocks_critical_path') and f.get('critical_path')})}


# candidate kind (normalize.py) -> advisory kind (schema)
ADVISORY_KIND = {'best_practice': 'best_practice', 'experimental': 'best_practice', 'above_level': 'above_target_level',
                 'outside_version': 'outside_version', 'obsolete': 'obsolete'}
ECHO_PREFIX =re.compile(r'(?i)^\W*(needs (a )?(human|manual) (check|review)|needs review|manual check|to check|check)\W*')


def _flat(s: str) -> str:
    return re.sub(r'\W+', ' ', ECHO_PREFIX.sub('', s or '').lower()).strip()


def echoes(reason: str, texts: list[str]) -> bool:
    """The reason adds nothing: after a "needs a human check:" style prefix it is (a cut of) one of the texts."""
    r = _flat(reason)
    return len(r) < 25 or any(r and r in _flat(t) for t in texts if t)


def sources_key(s: dict) -> tuple:
    return (s['tool'], s['rule_id'])


# ---------------------------------------------------------------- checks
SOURCE_CITE = re.compile(r'(?<![\w/.-])([\w./-]*[A-Za-z_][\w-]*\.[A-Za-z]{1,6}):(\d+)\b')


def _norm_ws(s: str) -> str:
    return ' '.join(str(s).split())


def check_basis(f: dict, w: str, repo: Path | None) -> list[str]:
    """An agent finding that names source lines carries them as a basis ({file, line, excerpt}); with --repo every
    excerpt must be on its line, so the claim can be re-read."""
    e = []
    basis = f.get('basis') or []
    prose = ' '.join(str(f.get(k) or '') for k in ('resolution', 'remediation', 'severity_rationale'))
    cited = {(m.group(1).rsplit('/', 1)[-1], int(m.group(2))) for m in SOURCE_CITE.finditer(prose)}
    have = {(str(b.get('file', '')).rsplit('/', 1)[-1], b.get('line')) for b in basis}
    if f['evidence_type'] == 'agent_judgment' and cited - have:
        miss = sorted(cited - have)[0]
        e.append(f'{w}: cites {miss[0]}:{miss[1]} but has no basis for it: add basis [{{file, line, excerpt}}] so the '
                 'claim can be re-read (validate with --repo)')
    if repo is None:
        return e
    for b in basis:
        p = (repo / b['file']).resolve()
        if repo.resolve() not in p.parents:
            e.append(f'{w}.basis: {b["file"]} is outside --repo')
            continue
        try:
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            e.append(f'{w}.basis: {b["file"]} not found in --repo')
            continue
        if b['line'] > len(lines) or _norm_ws(b['excerpt']) not in _norm_ws(lines[b['line'] - 1]):
            e.append(f'{w}.basis: the excerpt is not on {b["file"]}:{b["line"]} (re-read with --repo)')
    return e


def check(d: dict, run_dir: Path | None, cands: dict | None, repo: Path | None = None) -> list[str]:
    e: list[str] = []
    scs = load_scs()
    scope, meta, head = d['scope'], d['metadata'], d['headline']
    version, level = scope['wcag_version'], scope['level']
    targets = target_scs(scs, version, level)
    en_map = json.loads((DATA / 'en301549-map.json').read_text(encoding='utf-8'))['map']

    # metadata -------------------------------------------------------------
    tools = {t['name']: t for t in meta['tools']}
    for eng in REQUIRED_ENGINES:
        if eng not in tools:
            e.append(f'metadata.tools: required engine {eng} missing (record it as not_run with a reason)')
        elif tools[eng]['status'] != 'ran':
            if not tools[eng]['reason'].strip():
                e.append(f'metadata.tools: {eng} is {tools[eng]["status"]} without a reason')
            if not any(eng.split('-')[0].lower() in l.lower() for l in d['limitations']):
                e.append(f'limitations: must say what is lost because {eng} did not run')
    for t in meta['tools']:
        if t['status'] != 'ran' and not t['reason'].strip():
            e.append(f'metadata.tools: {t["name"]} is {t["status"]} without a reason')
    cut_short = sorted({c['name'] for c in meta['checks'] if c['status'] == 'partial'})
    for name in cut_short:
        words = (name.split('_')[0],) + (('keyboard',) if name == 'focus_visible' else ())  # measured during the keyboard probe
        if not any(any(w in l.lower() for w in words) and 'partial' in l.lower() for l in d['limitations']):
            rows = [c for c in meta['checks'] if c['name'] == name and c['status'] == 'partial']
            e.append(f'limitations: the {name} probe was partial on {len(rows)} check(s) ({rows[0]["page"]} {rows[0]["viewport"]}); '
                     'say it was partial and what it did not measure')
    pages = {p['id']: p for p in meta['sample']}
    if len(pages) != len(meta['sample']):
        e.append('metadata.sample: duplicate page ids')
    for c in meta['checks']:
        if c['page'] not in pages:
            e.append(f'metadata.checks: unknown page {c["page"]}')
        if c['viewport'] not in meta['viewports']:
            e.append(f'metadata.checks: unknown viewport {c["viewport"]}')
    n_pages = sum(1 for p in meta['sample'] if p['reason'] not in ('unreachable', 'blocked'))

    # sc_results -------------------------------------------------------------
    got = [r['sc'] for r in d['sc_results']]
    if sorted(got, key=lambda s: [int(x) for x in s.split('.')]) != targets:
        missing = sorted(set(targets) - set(got)); extra = sorted(set(got) - set(targets))
        e.append(f'sc_results: must list every SC at WCAG {version} {level} exactly once (missing {missing}, unexpected {extra}, '
                 f'{len(got) - len(set(got))} duplicate(s))')
    findings = {f['id']: f for f in d['findings']}
    nrs = {n['id']: n for n in d['needs_review']}
    plan = {m['sc'] for m in d['manual_test_plan']}
    for r in d['sc_results']:
        m = scs.get(r['sc'])
        where = f'sc_results[{r["sc"]}]'
        if not m:
            continue
        for k in ('title', 'level', 'principle', 'automatable'):
            if r[k] != m[k]:
                e.append(f'{where}.{k}: {r[k]!r} does not match data/wcag-sc.json ({m[k]!r})')
        if m.get('obsolete_in') and VERSION_ORDER[version] >= VERSION_ORDER[m['obsolete_in']] and r['outcome'] != 'inapplicable':
            e.append(f'{where}: obsolete in WCAG {m["obsolete_in"]}, so it must be inapplicable')
        cited = [f for f in d['findings'] if r['sc'] in f['sc']]
        if r['outcome'] == 'failed':
            if not r['finding_ids']:
                e.append(f'{where}: failed without finding_ids')
        elif cited:
            e.append(f'{where}: outcome {r["outcome"]} but findings {[f["id"] for f in cited]} fail it')
        for fid in r['finding_ids']:
            if fid not in findings:
                e.append(f'{where}: unknown finding {fid}')
            elif r['sc'] not in findings[fid]['sc']:
                e.append(f'{where}: {fid} does not list {r["sc"]}')
        for f in cited:
            if f['id'] not in r['finding_ids']:
                e.append(f'{where}: finding_ids must include {f["id"]}')
        if r['outcome'] == 'passed':
            basis = set(r['basis'])
            if not basis - {'tool'}:
                e.append(f'{where}: passed on tool evidence alone; a passing tool rule never proves the SC (add probe, agent_review or manual evidence, or use cantTell/untested)')
            if m['automatable'] == 'none' and not basis & {'agent_review', 'manual'}:
                e.append(f'{where}: passed with automatable none needs agent_review or manual evidence')
        if r['outcome'] == 'cantTell':
            ids = r.get('needs_review_ids') or []
            if not ids:
                e.append(f'{where}: cantTell needs needs_review_ids')
            for i in ids:
                if i not in nrs or r['sc'] not in nrs[i]['sc']:
                    e.append(f'{where}: needs-review item {i} missing or does not list {r["sc"]}')
        if r['outcome'] in ('passed', 'failed') and 'agent_review' in r['basis'] and r['sc'] not in d['assisted_review']['scs_reviewed']:
            e.append(f'{where}: basis agent_review, but assisted_review.scs_reviewed does not list {r["sc"]}')
        if r['outcome'] == 'failed' and cited:
            best = min((CONF_ORDER[f['confidence']] for f in cited), default=CONF_ORDER['low'])
            if CONF_ORDER[r['confidence']] < best:
                e.append(f'{where}.confidence: {r["confidence"]} is above every finding that fails it '
                         f'(best is {[k for k, v in CONF_ORDER.items() if v == best][0]})')
        if r['outcome'] == 'untested' and r['sc'] not in plan:
            e.append(f'{where}: untested SCs must be in the manual_test_plan')
        if scope['standards_map'] == 'en301549' and r.get('en301549_clause') != en_map.get(r['sc']):
            e.append(f'{where}.en301549_clause: expected {en_map.get(r["sc"])!r}')

    # findings ---------------------------------------------------------------
    cp_names = {p['name']: p for p in scope['critical_paths']}
    ids = [f['id'] for f in d['findings']]
    if len(set(ids)) != len(ids):
        e.append('findings: duplicate ids')
    reviewed = set(d['assisted_review']['scs_reviewed'])
    cand_by_id = {c['id']: c for c in (cands or {}).get('candidates', [])}
    for f in d['findings']:
        w = f'findings[{f["id"]}]'
        for s in f['sc']:
            if s not in targets:
                e.append(f'{w}: {s} is outside WCAG {version} {level}; report it as an advisory')
        lv = max((scs[s]['level'] for s in f['sc'] if s in scs), key=lambda x: LEVEL_ORDER[x], default='A')
        if f['level'] != lv:
            e.append(f'{w}.level: {f["level"]} but its highest SC level is {lv}')
        pats = {p['id']: p for s in f['sc'] if s in scs for p in scs[s]['failure_patterns']}
        if f['pattern'] not in pats:
            e.append(f'{w}.pattern: {f["pattern"]!r} is not a failure pattern of {f["sc"]} (see data/wcag-sc.json)')
        elif f['base_severity'] != pats[f['pattern']]['default_severity'] and not f.get('severity_override_reason'):
            e.append(f'{w}.base_severity: rubric default for {f["pattern"]} is {pats[f["pattern"]]["default_severity"]}; give severity_override_reason to deviate')
        ni = any(scs[s]['non_interference'] for s in f['sc'] if s in scs)
        if f['non_interference'] != ni:
            e.append(f'{w}.non_interference: must be {ni}')
        if f['severity'] != expected_severity(f):
            e.append(f'{w}.severity: expected {expected_severity(f)} (base {f["base_severity"]}'
                     f'{", +1 for a critical path" if f.get("critical_path") else ""}{", Critical for non-interference" if f["non_interference"] else ""})')
        e.extend(check_basis(f, w, repo))
        if f['evidence_type'] == 'agent_judgment':
            if f['confidence'] == 'high':
                e.append(f'{w}: agent_judgment findings can never have high confidence')
            for s in f['sc']:
                if s not in reviewed:
                    e.append(f'{w}: agent_judgment on {s}, which assisted_review.scs_reviewed does not list')
        else:
            if not f['sources']:
                e.append(f'{w}: {f["evidence_type"]} findings need sources')
            if not f['candidate_ids']:
                e.append(f'{w}: {f["evidence_type"]} findings must cite the candidates they come from')
            if f['evidence_type'] == 'probe' and any(s['tool'] != 'probe' for s in f['sources']):
                e.append(f'{w}: evidence_type probe but an engine is a source; use tool')
        if f['confidence'] == 'high':
            failing = [s for s in f['sources'] if s.get('outcome', 'failed') == 'failed']
            distinct = {s['tool'] if s['tool'] != 'probe' else 'probe:' + s['rule_id'].split('.')[0] for s in failing}
            if len(distinct) < 2 and not any(s['act_rules'] for s in failing):
                e.append(f'{w}: high confidence needs two agreeing failing sources or an ACT-rule implementation')
        if f.get('critical_path'):
            cp = cp_names.get(f['critical_path']['name'])
            if not cp:
                e.append(f'{w}.critical_path: unknown path {f["critical_path"]["name"]!r}')
            else:
                step = next((s for s in cp['steps'] if s['label'] == f['critical_path']['step']), None)
                if not step:
                    e.append(f'{w}.critical_path.step: not a step of {cp["name"]}')
                elif not any(pages.get(p, {}).get('url') == step['url'] for p in f['pages']):
                    e.append(f'{w}.critical_path: step {step["label"]!r} is {step["url"]}, which is not one of the finding\'s pages')
        if f['blocks_critical_path'] and (not f.get('critical_path') or f['severity'] != 'critical'):
            e.append(f'{w}: blocks_critical_path needs a critical_path and Critical severity')
        sc_w = SEVERITY_W[f['severity']]
        rr, ll = reach(f, n_pages), leverage(f)
        score = sc_w * rr * ll
        if (f['priority']['reach'], f['priority']['leverage'], f['priority']['score']) != (rr, ll, score):
            e.append(f'{w}.priority: expected reach {rr}, leverage {ll}, score {score}')
        if f['priority']['band'] != band(f, score):
            e.append(f'{w}.priority.band: expected {band(f, score)}')
        for i, inst in enumerate(f['instances']):
            if inst['page'] not in pages:
                e.append(f'{w}.instances[{i}]: unknown page {inst["page"]}')
            elif inst['url'] != pages[inst['page']]['url']:
                e.append(f'{w}.instances[{i}]: url does not match sample page {inst["page"]}')
            if inst['viewport'] not in meta['viewports']:
                e.append(f'{w}.instances[{i}]: unknown viewport {inst["viewport"]}')
            if inst['page'] not in f['pages']:
                e.append(f'{w}.instances[{i}]: page {inst["page"]} not in pages')
            if run_dir and inst['screenshot'] and not (run_dir / 'screenshots' / inst['screenshot']).exists():
                e.append(f'{w}.instances[{i}]: screenshot {inst["screenshot"]} not in the run directory')
        if scope['standards_map'] == 'en301549':
            want = sorted({en_map[s] for s in f['sc'] if en_map.get(s)})
            if sorted(f.get('en301549', [])) != want:
                e.append(f'{w}.en301549: expected {want}')
        if cands is not None:
            union = set()
            for cid in f['candidate_ids']:
                c = cand_by_id.get(cid)
                if not c:
                    e.append(f'{w}: unknown candidate {cid}'); continue
                if c['kind'] != 'wcag':
                    e.append(f'{w}: candidate {cid} is {c["kind"]}; it belongs in advisories')
                union |= {(s['tool'], s['rule_id']) for s in c['sources']}
            for s in f['sources']:
                if sources_key(s) not in union:
                    e.append(f'{w}: source {s["tool"]}:{s["rule_id"]} is not in its candidates')
            cited = [cand_by_id[c] for c in f['candidate_ids'] if c in cand_by_id]
            if cited and all(c['outcome'] == 'cantTell' for c in cited):
                if f['evidence_type'] != 'agent_judgment' or not f.get('resolution'):
                    e.append(f'{w}: built only from cantTell candidates, so it is agent_judgment and needs a resolution saying what was checked')
    if [f['id'] for f in sorted(d['findings'], key=rank_key)] != ids:
        e.append(f'findings: not in priority order (expected {[f["id"] for f in sorted(d["findings"], key=rank_key)]})')

    # headline -----------------------------------------------------------------
    s = compute_summary(d)
    for k in ('severity_counts', 'sc_coverage', 'overall_confidence', 'blocked_critical_paths'):
        if head[k] != s[k]:
            e.append(f'headline.{k}: expected {s[k]}')
    if abs(head['evidence_completeness'] - s['evidence_completeness']) > 0.005:
        e.append(f'headline.evidence_completeness: expected {s["evidence_completeness"]} (ran / planned checks)')
    strong = [f for f in d['findings'] if f['confidence'] == 'high' and f['level'] in ('A', 'AA')]
    want = 'does_not_conform' if strong else 'not_established'
    if head['conformance_verdict'] != want:
        e.append(f'headline.conformance_verdict: expected {want}' +
                 ('' if strong else ' (does_not_conform needs at least one high-confidence A or AA failure)'))
    if BANNED_VERDICT.search(head['verdict_text']):
        e.append('headline.verdict_text: never claim conformance or compliance')
    if want == 'not_established' and 'not established' not in head['verdict_text'].lower():
        e.append('headline.verdict_text: must say conformance is not established')

    # needs review / advisories / manual plan -----------------------------------
    for n in d['needs_review']:
        for sc in n['sc']:
            if sc not in targets:
                e.append(f'needs_review[{n["id"]}]: {sc} outside the target; use an advisory')
    for a in d['advisories']:
        if a['kind'] == 'above_target_level' and a['sc'] and all(sc in targets for sc in a['sc']):
            e.append(f'advisories[{a["id"]}]: above_target_level but its SCs are in the target')
        if a['kind'] == 'obsolete' and a['sc'] and not all(scs.get(sc, {}).get('obsolete_in') and
                                                          VERSION_ORDER[version] >= VERSION_ORDER[scs[sc]['obsolete_in']] for sc in a['sc']):
            e.append(f'advisories[{a["id"]}]: obsolete, but not every SC it lists is obsolete in WCAG {version}')
        for cid in a['candidate_ids']:
            c = cand_by_id.get(cid)
            if c and c['kind'] in ADVISORY_KIND and a['kind'] != ADVISORY_KIND[c['kind']] and c['kind'] in ('obsolete', 'outside_version'):
                e.append(f'advisories[{a["id"]}]: candidate {cid} is {c["kind"]}, so the advisory kind is {ADVISORY_KIND[c["kind"]]}')
    for m in d['manual_test_plan']:
        if m['sc'] not in targets:
            e.append(f'manual_test_plan[{m["sc"]}]: outside the target')
    for sc in reviewed:
        if sc not in targets:
            e.append(f'assisted_review.scs_reviewed: {sc} outside the target')

    # triage -------------------------------------------------------------------
    refs = {'finding': findings, 'needs_review': nrs, 'advisory': {a['id']: a for a in d['advisories']}}
    seen: dict[str, int] = {}
    for t in d['triage']:
        seen[t['candidate_id']] = seen.get(t['candidate_id'], 0) + 1
        w = f'triage[{t["candidate_id"]}]'
        if t['decision'] == 'dismissed':
            if t['ref'] is not None:
                e.append(f'{w}: dismissed candidates have no ref')
            if VAGUE_REASON.match(t['reason']) or len(t['reason']) < 25:
                e.append(f'{w}: dismissal reason must be checkable (what was looked at and why it is not a failure)')
        else:
            target = refs[t['decision']].get(t['ref'])
            if not target:
                e.append(f'{w}: {t["decision"]} {t["ref"]!r} does not exist')
            elif t['candidate_id'] not in target['candidate_ids']:
                e.append(f'{w}: {t["ref"]} does not list {t["candidate_id"]}')
        c = cand_by_id.get(t['candidate_id'])
        if cands is not None:
            if not c:
                e.append(f'{w}: not a candidate in the candidates file')
            elif c['kind'] != 'wcag' and t['decision'] in ('finding', 'needs_review'):
                e.append(f'{w}: {c["kind"]} candidates are advisories or dismissed, never WCAG findings')
            elif t['decision'] == 'needs_review' and c['outcome'] == 'failed' and t['ref'] in nrs and \
                    echoes(t['reason'], [nrs[t['ref']]['what_to_check']] + [s.get('message') or '' for s in c['sources']]):
                e.append(f'{w}: a failed candidate kept as needs review needs a reason saying why the failure is not '
                         'confirmed (what was looked at); this one only repeats the needs-review text or the tool message')
            elif t['decision'] == 'dismissed' and c['kind'] == 'wcag' and c['outcome'] == 'cantTell':
                est = c.get('contrast_estimate')
                if est and (est.get('min_ratio') is None or est.get('below')):
                    why = ('an image or video background' if est.get('min_ratio') is None
                           else f'an estimate of {est["min_ratio"]}:1, below {est["required"]}:1')
                    e.append(f'{w}: a contrast candidate with {why} is needs review or a finding, never dismissed '
                             '(check the screenshot pixels, then promote it or put it in needs_review)')
                elif c.get('group') and not est:
                    e.append(f'{w}: a grouped contrast candidate with no estimate is needs review, never dismissed')
    for cid, n in seen.items():
        if n > 1:
            e.append(f'triage: {cid} triaged {n} times')
    for coll, name in ((d['findings'], 'finding'), (d['needs_review'], 'needs_review'), (d['advisories'], 'advisory')):
        for x in coll:
            for cid in x['candidate_ids']:
                if not any(t['candidate_id'] == cid and t['ref'] == x['id'] for t in d['triage']):
                    e.append(f'{name} {x["id"]}: candidate {cid} has no triage entry pointing here')
    if cands is not None:
        missing = sorted(set(cand_by_id) - set(seen), key=lambda x: int(x[2:]))
        if missing:
            e.append(f'triage: {len(missing)} candidate(s) not triaged: {missing[:10]}')

    # run directory ------------------------------------------------------------
    if run_dir:
        run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
        rs = {x['name']: x for x in run['engines']}
        for t in meta['tools']:
            if t['name'] in rs and t['status'] != rs[t['name']]['status']:
                e.append(f'metadata.tools[{t["name"]}]: status {t["status"]} but run.json says {rs[t["name"]]["status"]}')
            if t['name'] in rs and t['version'] != rs[t['name']]['version']:
                e.append(f'metadata.tools[{t["name"]}]: version differs from run.json')
        if meta['checks'] != run['checks']:
            e.append('metadata.checks: must be copied unchanged from run.json')
        # blocked_requests: the list run.json records, or its length
        rb = run.get('blocked_requests') or []
        n_run = len(rb) if isinstance(rb, list) else int(rb)
        mb = meta['blocked_requests']
        n_meta = len(mb) if isinstance(mb, list) else mb
        if n_meta != n_run:
            e.append(f'metadata.blocked_requests: {n_meta} but run.json blocked {n_run} request(s); copy the list or its count')
        run_pages = {s['id']: s['url'] for s in run['sample']}
        if {p['id']: p['url'] for p in meta['sample']} != run_pages:
            e.append('metadata.sample: ids and URLs must match run.json')
        if meta['browser']['version'] != run['browser']['version']:
            e.append('metadata.browser.version differs from run.json')
        # a pass resting on probe evidence needs that probe to have run everywhere it was planned: a probe whose
        # injection failed (a CSP refusing its style) measured nothing, so it can't support "passed"
        probe_sc: dict[str, set] = {}
        for code, m in json.loads((DATA / 'rule-map.json').read_text(encoding='utf-8'))['probe'].items():
            for s in m['sc']:
                probe_sc.setdefault(s, set()).add(code.split('.')[0].replace('focus', 'keyboard'))
        for r in d['sc_results']:
            if r['outcome'] != 'passed' or 'probe' not in r['basis']:
                continue
            for probe in sorted(probe_sc.get(r['sc'], ())):
                rows = [c for c in run['checks'] if c['kind'] == 'probe' and c['name'] == probe]
                bad = [c for c in rows if c['status'] != 'ran']
                if rows and bad:
                    e.append(f'sc_results[{r["sc"]}]: passed on probe evidence, but the {probe} probe did not run on '
                             f'{len(bad)} of {len(rows)} planned checks ({bad[0]["page"]} {bad[0]["viewport"]}: {bad[0]["status"]})')
    return e


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('audit', type=Path)
    ap.add_argument('--run', type=Path, help='run_audit.py output directory')
    ap.add_argument('--candidates', type=Path, help='merge_findings.py output')
    ap.add_argument('--write-summary', action='store_true', help='recompute headline numbers in place')
    ap.add_argument('--repo', type=Path, help='the site source, to re-read every finding basis {file, line, excerpt}')
    args = ap.parse_args()
    try:
        d = json.loads(args.audit.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'INVALID: cannot read {args.audit}: {exc}')
        return 1
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    if args.write_summary:
        # fill the computed headline fields first: the schema requires them, and a draft report
        # leaves them empty for this step to fill (the verdict and its text stay the author's)
        try:
            summary = compute_summary(d)
        except (KeyError, TypeError, AttributeError) as exc:
            summary = None
            print(f'cannot compute the summary yet ({type(exc).__name__}: {exc}); the schema errors below say what is missing')
        if summary is not None:
            head = d.get('headline') if isinstance(d.get('headline'), dict) else {}
            d['headline'] = {k: head[k] for k in ('conformance_verdict', 'verdict_text') if k in head} | summary | \
                {k: v for k, v in head.items() if k not in summary and k not in ('conformance_verdict', 'verdict_text')}
            args.audit.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errs = schema_errors(d, schema, schema)
    if errs:
        print(f'INVALID: {len(errs)} schema error(s)')
        for x in errs[:50]:
            print(f'  - {x}')
        return 1
    cands = json.loads(args.candidates.read_text(encoding='utf-8')) if args.candidates else None
    errs = check(d, args.run, cands, args.repo)
    if errs:
        print(f'INVALID: {len(errs)} problem(s)')
        for x in errs:
            print(f'  - {x}')
        return 1
    h = d['headline']
    print(f"VALID: {len(d['findings'])} finding(s) {h['severity_counts']}, verdict {h['conformance_verdict']}, "
          f"SCs {h['sc_coverage']}, completeness {h['evidence_completeness']}, confidence {h['overall_confidence']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
