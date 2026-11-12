#!/usr/bin/env python3
"""Validate a code security audit (structure and honesty rules).

    python validate_security_audit.py security_audit.json [--repo <repo>] [--candidates candidates.json] [--write-summary]

Checks the report against schemas/security_audit.schema.json, then the rules a schema can't
express: CVSS v4.0 scores are recomputed from their vectors, priorities and ranks are recomputed,
Critical needs at least traced confidence and possible findings stop at P1, confirmed findings need
permitted verification against a safe target with a PoC that passes the safety lint and evidence
that shows the proof passed, regression tests must fail on the vulnerable code, reachable
dependencies need findings, and no secret value appears anywhere in the report.

Paths to PoCs, evidence, regression tests and variant rules are relative to the report's
directory. --repo re-checks every location (file, line, snippet) against the source, re-reads each
secret to make sure its value isn't in the report or the evidence, and checks that nothing the run
wrote is inside the repository. --candidates checks every candidate and dependency advisory was
triaged. --write-summary recomputes the summary counts in place (the headline is kept).
Exit code 0 when valid, 1 when invalid.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cvss4  # noqa: E402
import run_poc  # noqa: E402
import scan_secrets  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'security_audit.schema.json'
SEVERITY_W = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}
CONFIDENCE_W = {'confirmed': 4, 'traced': 3, 'possible': 2}
EXPOSURE_W = {'public': 3, 'authenticated': 2, 'restricted': 1}
CONF_ORDER = {'confirmed': 0, 'traced': 1, 'possible': 2}
RAISING_IMPACT = {'direct_financial_loss', 'fraud', 'regulated_data_breach'}
SEED_TODO = 'TODO(seed)'  # left by seed_report.py for the agent to replace
SECRET_CWES = {'CWE-798', 'CWE-259', 'CWE-321', 'CWE-547', 'CWE-1394'}
# Verdicts about the code are banned; "secure" in a technical phrase ("the Secure cookie flag") is not.
BANNED_HEADLINE = re.compile(r'(?i)\b(is|are|looks|appears|seems|now|remains?|considered|deemed|be) (now |fully |reasonably |)(secure|safe)\b|'
                             r'\bno (vulnerabilit|security issue|security problem|risk)|vulnerability[- ]free|\bfully secure|'
                             r'\bsafe to (deploy|ship|release)|passed (the )?security|\bsecure (code|codebase|app|application|system|overall)\b|'
                             r'\b(code|codebase|app|application|system|repository|service)\b[^.;]{0,30}\bsecure\b(?!\s*(cookie|flag|attribute|context|'
                             r'channel|transport|connection|header|storage|random|hash|compare|comparison|-?coding|defaults?))')
VAGUE_REASON = re.compile(r'(?i)^\W*(false positive|fp|not exploitable|not an issue|not a problem|n/?a|safe|ok|fine|ignore)\W*$')
LOC_REF = re.compile(r'[\w./-]+\.\w+:\d+')

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
            elif isinstance(extra, dict):
                errs += schema_errors(v, extra, root, f'{path}.{k}')
    return errs

# ---------------------------------------------------------------- scoring
def priority_score(f: dict) -> int:
    return SEVERITY_W.get(f['cvss']['severity'], 0) * CONFIDENCE_W.get(f['confidence'], 0) * EXPOSURE_W.get(f['exposure'], 0)

PRIO_ORDER = ['P0', 'P1', 'P2', 'P3']

def priority(score: int, confidence: str, business_impact: dict | None = None) -> str:
    p = 'P0' if score >= 32 else 'P1' if score >= 18 else 'P2' if score >= 8 else 'P3'
    if business_impact and business_impact.get('kind') in RAISING_IMPACT and p in ('P2', 'P3'):
        p = PRIO_ORDER[PRIO_ORDER.index(p) - 1]  # direct money or fraud loss raises one level, never to P0
    return 'P1' if confidence == 'possible' and p == 'P0' else p

def rank_key(f: dict):
    return (PRIO_ORDER.index(f['priority']) if f.get('priority') in PRIO_ORDER else 9, -f['priority_score'],
            -float(f['cvss']['score']), CONF_ORDER.get(f['confidence'], 9))

def compute_summary(d: dict) -> dict:
    fs = d.get('findings') or []
    count = lambda key, values: {v: sum(1 for f in fs if key(f) == v) for v in values}  # noqa: E731
    return {
        'findings': len(fs),
        'by_priority': count(lambda f: f.get('priority'), ['P0', 'P1', 'P2', 'P3']),
        'by_severity': count(lambda f: (f.get('cvss') or {}).get('severity'), ['critical', 'high', 'medium', 'low']),
        'by_confidence': count(lambda f: f.get('confidence'), ['confirmed', 'traced', 'possible']),
        'candidates': len(d.get('triage') or []),
        'dismissed': sum(1 for t in d.get('triage') or [] if t.get('decision') == 'dismissed'),
        'dependencies_reachable': sum(1 for x in d.get('dependencies') or [] if x.get('reachable') in ('yes', 'conditional')),
        'observations': len(d.get('observations') or []),
    }

def norm(s: str) -> str:
    return ' '.join((s or '').split())

def inside(path: Path, repo: Path) -> bool:
    try:
        path.resolve().relative_to(repo.resolve())
        return True
    except ValueError:
        return False

def git_ignored(path: Path, repo: Path) -> bool:
    try:
        p = subprocess.run(['git', '-C', str(repo), 'check-ignore', '-q', str(path)], capture_output=True, timeout=30)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def json_strings(value, path: str = '$'):
    """(JSON path, string) for every string value; keys are field names, never secrets."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from json_strings(v, f'{path}[{i}]')
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from json_strings(v, f'{path}.{k}')

def _line(repo: Path, file: str | None, line: int | None) -> tuple[str, str] | None:
    if not file or not line:
        return None
    src = repo / file
    if not src.is_file():
        return None
    lines = src.read_text(encoding='utf-8', errors='replace').splitlines()
    return (lines[line - 1], src.name) if 0 < line <= len(lines) else None

def secret_values(findings: list[dict], cand_secrets: dict, triaged: dict, repo: Path) -> list[tuple[str, str]]:
    """(value, where it came from) for every value the report must not contain. Never print the value.

    Values come from findings with a secret block (their locations) and from secret candidates that
    were not dismissed. A dismissed candidate (a placeholder or a fixture) still counts when its
    value looks like a real key: 16 or more characters with at least 3.5 bits of entropy per
    character. Placeholder words and values under 8 characters are skipped: they are words, not keys."""
    out: dict[str, str] = {}
    def add(text_name, origin, strong_only=False):
        if not text_name:
            return
        text, name = text_name
        for s in scan_secrets.find_in_line(text, name):
            v = s['value']
            if len(v) < 8 or scan_secrets.is_placeholder(v, name, s.get('fallback', False)) or v in out:
                continue
            if strong_only and not (len(v) >= 16 and scan_secrets.entropy(v) >= 3.5):
                continue
            out[v] = origin
    for f in findings:
        if f.get('secret'):
            for l in f['locations']:
                add(_line(repo, l['file'], l['line']), f"{f['id']} ({l['file']}:{l['line']})")
    for cid, c in cand_secrets.items():
        if c.get('history_only'):
            continue  # not in the working tree; its value can't be re-read without the history
        dismissed = (triaged.get(cid) or {}).get('decision') == 'dismissed'
        add(_line(repo, c.get('file'), c.get('line')), f"{cid} ({c.get('file')}:{c.get('line')})", strong_only=dismissed)
    return [(v, o) for v, o in out.items()]

# ---------------------------------------------------------------- checks
def check(d: dict, base: Path, repo: Path | None, candidates: dict | None) -> tuple[list[str], list[str]]:
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errors = schema_errors(d, schema, schema)
    warnings: list[str] = []
    if errors:
        return errors, warnings  # the rule checks below assume the structure is right
    E, W = errors.append, warnings.append
    findings = d['findings']
    fids = [f['id'] for f in findings]
    obs_ids = {o['id'] for o in d['observations']}
    eps = {e['id']: e for e in d['recon']['entry_points']}
    tools = {t['name']: t for t in d['tools']}
    ran = {n for n, t in tools.items() if t['status'] == 'ran'}
    ver = d['verification']
    report_text = json.dumps(d, ensure_ascii=False)

    if 'exploit' not in d['notice'].lower():
        E('notice: must warn that the report contains exploit details')
    for dup in {x for x in fids if fids.count(x) > 1}:
        E(f'findings: duplicate id {dup}')

    # tools
    for t in d['tools']:
        if t['status'] != 'ran' and not t.get('reason'):
            E(f"tools[{t['name']}]: status {t['status']} needs a reason")
        if t['status'] == 'ran' and t['area'] not in ('agent', 'patterns', 'recon') and not t.get('version'):
            W(f"tools[{t['name']}]: ran without a recorded version")

    # verification
    if ver['permitted']:
        if not ver.get('permission'):
            E('verification: permitted without recording who allowed it')
        if not ver.get('target') or ver['target_kind'] == 'none':
            E('verification: permitted without a target')
        if ver['target_kind'] == 'production' and not ver.get('production_written_permission'):
            E('verification: production targets need production_written_permission')
        if ver['target_kind'] == 'localhost' and ver.get('target'):
            host = urllib.parse.urlsplit(ver['target']).hostname or ''
            if not run_poc.is_loopback(host):
                E(f'verification: target_kind is localhost but {host!r} is not a loopback address')

    # triage
    triaged: dict[str, dict] = {}
    for t in d['triage']:
        if t['candidate'] in triaged:
            E(f"triage: {t['candidate']} triaged twice")
        triaged[t['candidate']] = t
        if t['decision'] == 'promoted':
            if not t.get('finding') or t['finding'] not in fids:
                E(f"triage[{t['candidate']}]: promoted to {t.get('finding')!r}, which is not a finding")
            else:
                f = findings[fids.index(t['finding'])]
                if t['candidate'] not in f['candidates']:
                    E(f"triage[{t['candidate']}]: {t['finding']} does not list this candidate")
        elif t['decision'] == 'observation':
            if t.get('finding') not in obs_ids:
                E(f"triage[{t['candidate']}]: observation {t.get('finding')!r} does not exist")
        else:
            if VAGUE_REASON.match(t['reason']):
                E(f"triage[{t['candidate']}]: dismissal reason {t['reason']!r} can't be checked; say which guard, invariant or fact makes it safe")
            elif not LOC_REF.search(t['reason']) and len(t['reason'].split()) < 8:
                W(f"triage[{t['candidate']}]: dismissal reason has no file:line and is short; make it checkable")
    if candidates is not None:
        want = {c['id'] for c in candidates.get('candidates', [])} | {s['id'] for s in candidates.get('secrets', [])}
        for missing in sorted(want - set(triaged)):
            E(f'triage: candidate {missing} from the candidates file was not triaged')
        for extra in sorted(set(triaged) - want):
            E(f'triage: {extra} is not in the candidates file')
        deps_want = {x['id'] for x in candidates.get('dependencies', [])}
        deps_have = {x['id'] for x in d['dependencies']}
        for missing in sorted(deps_want - deps_have):
            E(f'dependencies: advisory {missing} from the candidates file is not assessed')
        for extra in sorted(deps_have - deps_want):
            E(f'dependencies: {extra} is not in the candidates file')
        cand_secrets = {s['id']: s for s in candidates.get('secrets', [])}
    else:
        cand_secrets = {}

    # dependencies
    for x in d['dependencies']:
        if x['reachable'] in ('yes', 'conditional'):
            if not x.get('finding') or x['finding'] not in fids:
                E(f"dependencies[{x['id']}]: reachable={x['reachable']} needs a finding")
            else:
                f = findings[fids.index(x['finding'])]
                if x['id'] not in ((f.get('dependency') or {}).get('ids') or []):
                    E(f"dependencies[{x['id']}]: {x['finding']} does not list it")
        elif x.get('finding'):
            E(f"dependencies[{x['id']}]: reachable={x['reachable']} but linked to {x['finding']}")
        if x['reachable'] == 'unknown':
            W(f"dependencies[{x['id']}]: reachability unknown; say what would settle it")

    # findings
    for f in findings:
        fid = f['id']
        P = lambda msg: E(f'{fid}: {msg}')  # noqa: E731
        try:
            score = cvss4.score(f['cvss']['vector'])
        except (ValueError, KeyError) as exc:
            P(f'invalid CVSS v4.0 vector: {exc}')
            continue
        if abs(score - float(f['cvss']['score'])) > 1e-9:
            P(f"CVSS score {f['cvss']['score']} does not match the vector (computed {score})")
        sev = cvss4.severity(score)
        if sev == 'none':
            P('CVSS score 0.0 (no impact): record it as an observation, not a finding')
        elif sev != f['cvss']['severity']:
            P(f"severity {f['cvss']['severity']} does not match score {score} ({sev})")
        if f['cvss']['severity'] == 'critical' and f['confidence'] == 'possible':
            P('a Critical finding needs at least traced confidence')
        ps = priority_score(f)
        if ps != f['priority_score']:
            P(f"priority_score {f['priority_score']} should be {ps} (severity x confidence x exposure)")
        bi = f.get('business_impact')
        pr = priority(ps, f['confidence'], bi)
        if pr != f['priority']:
            P(f"priority {f['priority']} should be {pr}" + (f" (business_impact {bi['kind']} raises P2/P3 one level)" if bi else ''))
        if bi and bi['kind'] in RAISING_IMPACT and not LOC_REF.search(bi['basis']) and len(bi['basis'].split()) < 10:
            P('business_impact.basis must say where the loss happens (file:line) or how, in a sentence someone can check')
        # agent-found findings (no tool candidate): the basis is re-read from the source
        if f.get('source') == 'agent':
            if 'agent' not in f['sources']:
                P('source is agent, so sources must include "agent"')
            if not f.get('basis'):
                P('a finding with source agent needs a basis: file, line and excerpt (or runtime evidence)')
            for b in f.get('basis') or []:
                if b.get('evidence'):
                    if not (base / b['evidence']).exists():
                        P(f"basis evidence {b['evidence']} not found")
                    continue
                if not (b.get('file') and b.get('line') and b.get('excerpt')):
                    P('each basis entry needs file, line and excerpt, or evidence')
                elif repo:
                    got = _line(repo, b['file'], b['line'])
                    if got is None:
                        P(f"basis {b['file']}:{b['line']} does not exist in the repository")
                    elif norm(b['excerpt']) not in norm(got[0]):
                        P(f"basis excerpt is not on {b['file']}:{b['line']} (line reads {got[0].strip()[:80]!r})")
            if f['candidates']:
                P('source agent means no tool candidate led to it; list candidates only on tool findings')
        if f['entry_point'] and f['entry_point'] not in eps:
            P(f"entry_point {f['entry_point']} is not in recon.entry_points")
        for c in f['candidates']:
            t = triaged.get(c)
            if not t:
                P(f'candidate {c} is not in triage')
            elif t['decision'] != 'promoted' or t.get('finding') != fid:
                P(f'candidate {c} is not triaged as promoted to {fid}')
        for s in f['sources']:
            if s != 'agent' and s not in ran:
                P(f"source {s!r} is not a tool that ran")
        roles = {l['role'] for l in f['locations']}
        if not roles & {'sink', 'config', 'guard_missing'}:
            P('locations need the sink (or the config line / missing guard)')
        if f['confidence'] in ('confirmed', 'traced'):
            if not f['path']['sanitisers_checked']:
                P('traced and confirmed findings must list the sanitisers/guards checked (or why none apply)')
            if len(f['locations']) < 2 and 'config' not in roles:
                P('traced and confirmed findings need the path: at least a source and a sink location')
        else:
            if not f['path'].get('gap'):
                P('possible findings must say what is missing from the path (path.gap)')
        # PoC and evidence
        poc = f['poc']
        if f['confidence'] == 'confirmed':
            if poc['result'] != 'confirmed':
                P('confidence confirmed needs poc.result confirmed')
            if not ver['permitted']:
                P('confirmed, but verification was not permitted')
            if ver['target_kind'] not in ('localhost', 'test_environment') and not (ver['target_kind'] == 'production' and ver.get('production_written_permission')):
                P(f"confirmed against target_kind {ver['target_kind']!r}")
        elif poc['result'] == 'confirmed':
            P(f"poc.result confirmed but confidence is {f['confidence']}")
        if poc['result'] in ('not_run', 'not_confirmed') and not poc.get('reason'):
            P(f"poc.result {poc['result']} needs a reason")
        if poc['result'] in ('confirmed', 'not_confirmed'):
            if not poc.get('file') or not poc.get('evidence'):
                P('a PoC that ran needs poc.file and poc.evidence')
            else:
                pf, ev = base / poc['file'], base / poc['evidence']
                if not pf.exists():
                    P(f"PoC file {poc['file']} not found")
                else:
                    spec = json.loads(pf.read_text(encoding='utf-8'))
                    host = urllib.parse.urlsplit(ver.get('target') or '').hostname
                    issues = run_poc.lint(spec, ver.get('target'), [host] if host and ver['target_kind'] == 'test_environment' else [])
                    for i in issues:
                        P(f'PoC fails the safety lint: {i}')
                    if spec.get('finding') != fid:
                        P(f"PoC file is for {spec.get('finding')!r}")
                if not ev.exists():
                    P(f"evidence {poc['evidence']} not found")
                else:
                    e = json.loads(ev.read_text(encoding='utf-8'))
                    if e.get('finding') != fid:
                        P(f"evidence is for {e.get('finding')!r}")
                    if e.get('result') != poc['result']:
                        P(f"evidence result {e.get('result')!r} differs from poc.result {poc['result']!r}")
                    if poc['result'] == 'confirmed' and not (e.get('proof') or {}).get('passed'):
                        P('evidence does not show the proof passing')
                    if not e.get('permission'):
                        P('evidence does not record the permission')
                    if ver.get('target') and e.get('target') and urllib.parse.urlsplit(e['target']).hostname != urllib.parse.urlsplit(ver['target']).hostname:
                        P(f"evidence target {e['target']} is not the verification target")
                    if not e.get('steps'):
                        P('evidence has no request/response steps')
        for label, rx in run_poc.BANNED:
            if re.search(rx, f['repro']['safe_payload']['payload']):
                P(f'repro.safe_payload is not a canary ({label})')
        # regression test
        rt = f['regression_test']
        if rt['result'] == 'passes_on_vulnerable_code':
            P('the regression test passes on the vulnerable code, so it does not test the bug')
        if rt['result'] == 'fails_on_vulnerable_code':
            if not rt.get('file') or not rt.get('name'):
                P('a regression test that ran needs file and name')
            elif not (base / rt['file']).exists():
                P(f"regression test file {rt['file']} not found")
            elif not re.search(rf"def {re.escape(rt['name'])}\s*\(|['\"]{re.escape(rt['name'])}['\"]", (base / rt['file']).read_text(encoding='utf-8')):
                P(f"test {rt['name']} is not in {rt['file']}")
        elif rt['result'] == 'not_run' and not rt.get('note'):
            P('regression_test not_run needs a note saying why')
        # secrets
        if set(f['cwe']) & SECRET_CWES and not f.get('secret'):
            P('a hard-coded secret finding needs the secret block (type, fingerprint, history, rotate)')
        if f.get('secret'):
            fp = f['secret']['fingerprint']
            linked = [c for c in f['candidates'] if c in cand_secrets]
            if linked and all(cand_secrets[c].get('fingerprint') != fp for c in linked):
                P('secret fingerprint does not match the secret candidate')
            sc = f['secret']
            for c in (cand_secrets[c] for c in linked):
                if c.get('in_git_history') == 'yes' and sc['in_git_history'] != 'yes':
                    P(f"the scanner found this secret in git history ({', '.join(c.get('history_commits') or []) or 'in_git_history: yes'}) "
                      f"but the report says {sc['in_git_history']!r}")
                if sc.get('git_status') and c.get('git_status') not in (None, sc['git_status']):
                    P(f"git_status {sc['git_status']!r} but the scanner recorded {c['git_status']!r}")
                if sc['in_git_history'] == 'no' and c.get('in_git_history') == 'unknown':
                    P("in_git_history 'no' but the scanner could not tell; say 'unknown' or rerun scan_secrets.py in the git checkout")
            if not f['secret']['rotate'] and f['secret']['in_git_history'] != 'no':
                W(f'{fid}: secret may be in history but rotate is false')
        if f.get('dependency'):
            for dep in f['dependency']['ids']:
                x = next((y for y in d['dependencies'] if y['id'] == dep), None)
                if not x:
                    P(f'dependency {dep} is not in dependencies')
                elif x.get('finding') != fid:
                    P(f'dependency {dep} is not linked back to {fid}')
                elif x['package'].lower() != f['dependency']['package'].lower():
                    P(f"dependency {dep} is {x['package']}, not {f['dependency']['package']}: group advisories by package")
        for ref in (f.get('chains_with') or []) + (f.get('variants') or []) + ([f['variant_of']] if f.get('variant_of') else []):
            if ref not in fids or ref == fid:
                P(f'refers to {ref}, which is not another finding')
        if f.get('variant_of'):
            parent = findings[fids.index(f['variant_of'])] if f['variant_of'] in fids else None
            if parent and fid not in (parent.get('variants') or []):
                P(f"{f['variant_of']} does not list {fid} as a variant")
        # locations against the source
        if repo:
            for l in f['locations']:
                src = repo / l['file']
                if not src.exists():
                    P(f"location {l['file']} does not exist in the repository")
                    continue
                lines = src.read_text(encoding='utf-8', errors='replace').splitlines()
                if l['line'] > len(lines):
                    P(f"location {l['file']}:{l['line']} is past the end of the file")
                elif norm(l['snippet']) not in norm(lines[l['line'] - 1]):
                    P(f"snippet is not on {l['file']}:{l['line']} (line reads {lines[l['line'] - 1].strip()[:80]!r})")

    # ranks
    ranked = sorted(findings, key=lambda f: f['rank'])
    if [f['rank'] for f in ranked] != list(range(1, len(findings) + 1)):
        E('findings: ranks must be 1..n with no gaps or repeats')
    for a, b in zip(ranked, ranked[1:]):
        if rank_key(a) > rank_key(b):
            E(f"findings: {a['id']} (rank {a['rank']}) should rank below {b['id']} (priority score, then CVSS score, then confidence)")

    # variant rules and observations
    for r in d['variant_rules']:
        for ref in r['for_findings']:
            if ref not in fids:
                E(f"variant_rules[{r['id']}]: {ref} is not a finding")
        if not (base / r['file']).exists():
            E(f"variant_rules[{r['id']}]: rule file {r['file']} not found")
        if r['tested'] and not r.get('test_note'):
            E(f"variant_rules[{r['id']}]: tested rules need a test_note saying what it matched")
    if repo:
        for o in d['observations']:
            src = repo / o['file']
            if not src.exists() or o['line'] > len(src.read_text(encoding='utf-8', errors='replace').splitlines()):
                E(f"observations[{o['id']}]: {o['file']}:{o['line']} does not exist")

    # coverage
    cov = d['coverage']
    if cov['entry_points_total'] != len(eps):
        E(f"coverage: entry_points_total {cov['entry_points_total']} but recon lists {len(eps)}")
    reviewed = sum(1 for e in eps.values() if e['reviewed'])
    if cov['entry_points_reviewed'] != reviewed:
        E(f"coverage: entry_points_reviewed {cov['entry_points_reviewed']} but {reviewed} are marked reviewed")
    if reviewed < len(eps) and not any('entry' in n['area'].lower() for n in cov['not_reviewed']):
        E('coverage: some entry points were not reviewed; say which and why in not_reviewed')
    if cov['files_read'] > cov['files_in_scope']:
        E('coverage: files_read exceeds files_in_scope')
    if not cov['not_reviewed']:
        W('coverage: nothing listed as not reviewed; is that really so?')
    not_ran = [t['name'] for t in d['tools'] if t['status'] in ('not_run', 'errored')]
    if not_ran and not any(n in ' '.join(d['limitations']) for n in not_ran):
        W(f"limitations: tools that did not run ({', '.join(not_ran)}) are not mentioned")
    if candidates is not None:
        hist = next((t.get('history') for t in candidates.get('tools', []) if t.get('tool') == 'scan_secrets'), None)
        if hist in ('partial', 'errored') and not re.search(r'(?i)\bhistory\b', ' '.join(d['limitations'])):
            E(f'limitations: the git history secret scan was {hist}; say how much of the history was not scanned')

    # secrets must never appear
    strings = list(json_strings(d))
    for where, text in strings:
        for s in scan_secrets.find_in_line(text):
            if s['type'] != 'generic_secret':
                E(f"report contains what looks like a {s['type']} value at {where}; secrets must be redacted")
    if repo:
        texts = list(strings)
        for f in findings:
            for k in ('file', 'evidence'):
                p = f['poc'].get(k)
                if p and (base / p).exists():
                    texts.append((p, (base / p).read_text(encoding='utf-8', errors='replace')))
        for value, origin in secret_values(findings, cand_secrets, triaged, repo):
            rx = None if len(value) >= 16 else re.compile(r'(?<![A-Za-z0-9_])' + re.escape(value) + r'(?![A-Za-z0-9_])')
            for where, text in texts:
                if (value in text) if rx is None else bool(rx.search(text)):
                    E(f'the secret value of {origin} appears in {where}; report only its fingerprint')
                    break

    # files written
    for w in d['files_written']:
        if w.startswith('<scratch>'):
            continue
        p = Path(w) if Path(w).is_absolute() else base / w
        if repo and inside(p, repo) and not git_ignored(p, repo):
            E(f'files_written: {w} is inside the repository and not gitignored')

    # placeholders left by seed_report.py
    for where, text in strings:
        if SEED_TODO in text:
            E(f'{where}: still holds a {SEED_TODO} placeholder from seed_report.py; replace it with the real assessment')

    # summary
    want = compute_summary(d)
    if d['summary']['counts'] != want:
        E(f"summary.counts differ from the report: expected {json.dumps(want)}")
    if BANNED_HEADLINE.search(d['summary']['headline']):
        E('summary.headline claims the code is secure or free of issues; state what was found and covered instead')
    return errors, warnings

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--candidates', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    try:
        d = json.loads(args.report.read_text(encoding='utf-8'))
        cands = json.loads(args.candidates.read_text(encoding='utf-8')) if args.candidates else None
    except (OSError, ValueError) as exc:
        print(f'Unable to read input: {exc}', file=sys.stderr)
        return 1
    if args.write_summary and isinstance(d.get('summary'), dict):
        d['summary']['counts'] = compute_summary(d)
        args.report.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errors, warnings = check(d, args.report.resolve().parent, args.repo.resolve() if args.repo else None, cands)
    for w in warnings:
        print(f'WARNING: {w}')
    for e in errors:
        print(f'ERROR: {e}')
    if errors:
        print(f'INVALID: {len(errors)} error(s)')
        return 1
    print(f"VALID: {len(d['findings'])} finding(s), {len(d['triage'])} candidate(s) triaged, {len(d['dependencies'])} advisory(ies) assessed")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
