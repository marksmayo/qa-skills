#!/usr/bin/env python3
"""Validate a test data audit report against the schema, the evidence and the rubric (stdlib only).

    python validate_test_data_report.py test_data_report.json --candidates candidates.json --repo <repo>
        [--runs runs.json] [--evidence <run dir>] [--write-summary]

--candidates checks every candidate is promoted to a finding, dismissed with a reason, or
handed off, and that each finding agrees with its candidates. --repo re-reads every flagged
personal-data and secret location and fails if any value (or a long part of one) appears in
the report or in the files under --evidence. --runs checks every experiment: its runs exist, an
affected test failed in them and passed in the baseline. --write-summary fills in each
finding's severity, reach, score and priority, and the summary; the headline stays yours.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import datafiles, scan_data  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'test_data_report.schema.json'
RULES = scan_data.RULES
WEIGHT = {'critical': 4, 'serious': 3, 'moderate': 2, 'minor': 1}
DOWN = {'critical': 'serious', 'serious': 'moderate', 'moderate': 'minor', 'minor': 'minor'}
BANDS = [(9, 'P0'), (6, 'P1'), (3, 'P2'), (0, 'P3')]
CONFIRMABLE = ('time_bomb', 'shared_mutable', 'locale_assumption')
GENERIC_LOCAL = {'admin', 'administrator', 'contact', 'demo', 'example', 'hello', 'info', 'noreply', 'no-reply', 'postmaster',
                 'sample', 'support', 'test', 'tester', 'testing', 'user', 'users', 'webmaster', 'no.reply', 'do-not-reply',
                 'donotreply', 'test.user', 'first.last', 'john.doe', 'jane.doe'}
SERVED = re.compile(r'(^|/)(public|static|wwwroot|www|htdocs|uploads)(/|$)')
BANNED = re.compile(r'\b(test )?data (is|are|looks?) (clean|safe|fine)\b|\bclean (test )?data\b|\bno (real |personal |sensitive )?'
                    r'(personal )?data (found|left|remains?)\b|\bfree of (personal|sensitive) data\b|\bprivacy[- ]safe\b|'
                    r'\b(GDPR|privacy)[- ]compliant\b|\bfixtures? (is|are) (clean|safe)\b|\bnothing sensitive\b', re.I)


# ---------------------------------------------------------------- schema (subset of JSON Schema)
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


# ---------------------------------------------------------------- computed fields
def days_until(d: str | None, today: date) -> int | None:
    return (date.fromisoformat(d) - today).days if d else None


def severity(f: dict, cands: dict, today: date) -> str:
    cat = f['category']
    if cat == 'personal_data':
        s = 'critical'
        return DOWN[s] if f.get('anonymised_by') else s
    if cat == 'time_bomb':
        when = f.get('fails_on_date') or next((cands[c]['details'].get('date') for c in f['candidates'] if c in cands), None)
        n = days_until(when, today)
        if n is None:
            return 'moderate'
        h = RULES['today_horizon_days']
        return 'critical' if n <= h['critical'] else 'serious' if n <= h['serious'] else 'moderate'
    if cat == 'environment_coupling':
        hosts = [cands[c]['details'].get('host') or '' for c in f['candidates'] if c in cands]
        return 'serious' if any(re.search(r'(^|[.-])(prod|production|live)([.-]|$)', h, re.I) for h in hosts) else 'moderate'
    if cat == 'shared_mutable':
        return 'serious' if f['confidence'] == 'confirmed' else 'moderate'
    if cat in ('snapshot_rot', 'unused_or_duplicate'):
        return 'minor'
    if cat == 'residue':
        # files a test run leaves in the working tree; Serious when they land in a directory the app serves
        return 'serious' if SERVED.search(f['location']['file']) else 'moderate'
    return 'moderate'


def reach(f: dict, critical: set[str]) -> int:
    n = len(f['affected_tests'])
    if critical & set(f['affected_tests']) or n >= 10:
        return 3
    return 2 if n >= 2 else 1


def priority(f: dict) -> str:
    if f['category'] == 'personal_data' and f.get('real_looking'):
        return 'P0'
    p = next(p for floor, p in BANDS if f['score'] >= floor)
    if f['confidence'] == 'suspected' and p in ('P0', 'P1'):
        return 'P2'
    return p


def fill(rep: dict, cands: dict) -> None:
    today = date.fromisoformat(rep['metadata']['today'])
    critical = set(rep['metadata'].get('critical_tests', []))
    for f in rep['findings']:
        f['severity'] = severity(f, cands, today)
        f['reach'] = reach(f, critical)
        f['score'] = WEIGHT[f['severity']] * f['reach']
        f['priority'] = priority(f)


def completeness(rep: dict) -> float:
    inv = rep['inventory']
    scanned = len([i for i in inv if i['status'] == 'scanned']) / len(inv) if inv else 1.0
    conf = [f for f in rep['findings'] if f['category'] in CONFIRMABLE]
    exp = len([f for f in conf if f.get('experiment', {}).get('result') in ('failed_as_predicted', 'passed')]) / len(conf) if conf else 1.0
    tools = [t for t in rep['tools'] if t['status'] != 'not_applicable']
    ran = len([t for t in tools if t['status'] == 'ran']) / len(tools) if tools else 1.0
    return round((scanned + exp + ran) / 3, 4)


def summary(rep: dict) -> dict:
    fs = rep['findings']
    bombs = [f['fails_on_date'] for f in fs if f['category'] == 'time_bomb' and f.get('fails_on_date')]
    count = lambda key: {k: len([f for f in fs if f[key] == k]) for k in sorted({f[key] for f in fs})}  # noqa: E731
    return {'files_scanned': len([i for i in rep['inventory'] if i['status'] == 'scanned']),
            'not_assessed': len([i for i in rep['inventory'] if i['status'] == 'not_assessed']),
            'findings_by_category': count('category'), 'by_priority': count('priority'), 'by_severity': count('severity'),
            'earliest_time_bomb_date': min(bombs) if bombs else None, 'evidence_completeness': completeness(rep),
            'dismissed': len(rep['dismissed']), 'handoffs': len(rep['handoffs'])}


# ---------------------------------------------------------------- values that must never appear
def flagged_values(c: dict, repo: Path) -> set[str]:
    p = repo / c['file']
    try:
        lines = p.read_text(encoding='utf-8', errors='replace').split('\n')
    except OSError:
        return set()
    line = lines[c['line'] - 1] if 0 < c['line'] <= len(lines) else ''
    vals: set[str] = set()
    if c['category'] == 'secret':
        rx = scan_data.SECRETS.get(c['rule'])
        for m in (rx.finditer(line) if rx else []):
            if scan_data.placeholder_secret(c['rule'], m.group(0)):
                continue                   # a placeholder on the same line is its own (dismissed) candidate
            tokens = [t for t in re.findall(r'[A-Za-z0-9._~+/=-]{12,}', m.group(0)) if re.search(r'\d', t)]
            vals |= set(tokens)
        return vals
    if c['rule'] in ('name_and_address', 'dob_with_name'):
        try:
            for r in datafiles.load_records(p, c['file']):
                if any(v['line'] == c['line'] for v in r['fields'].values()):
                    nm = c['details'].get('name_field')
                    if nm in r['fields']:
                        vals.add(str(r['fields'][nm]['value']))
                    if c['field'] in r['fields']:
                        vals.add(str(r['fields'][c['field']]['value']))
        except datafiles.NotAssessed:
            pass
        return {v for v in vals if len(v) >= 6}
    for m in scan_data.EMAIL.finditer(line):
        vals.add(m.group(0))
        # the whole address always counts. The local part alone counts only when it reads as a person's name (first.last,
        # j_smith): a single word such as staff, owner or bookings appears in ordinary test names and prose
        local = m.group(1)
        if len(local) >= 5 and re.search(r'[A-Za-z][._-][A-Za-z]', local) and local.lower() not in GENERIC_LOCAL:
            vals.add(local)
    for q in re.findall(r'"([^"]+)"|\'([^\']+)\'', line):
        s = q[0] or q[1]
        d = scan_data.digits(s)
        if c['rule'] != 'email' and len(d) >= 6:
            vals |= {s, d}
    return {v for v in vals if len(v) >= 4}


def leaks(values: set[str], text: str, flat: str | None = None) -> list[str]:
    hits = []
    flat = flat if flat is not None else re.sub(r'[\s-]', '', text)
    for v in values:
        # a value of letters only (a name, an email's local part) counts as a whole token, not inside a longer word or a
        # path (user -> mockServer/src/user.js); values with digits (secrets, numbers) still count anywhere
        if re.search(r'[A-Za-z]', v) and not re.search(r'\d', v):
            if re.search(rf'(?<![\w@./\\-]){re.escape(v)}(?![\w@/\\-]|\.\w)', text):
                hits.append('a full value')
            continue
        if v in text:
            hits.append('a full value')
            continue
        d = re.sub(r'\D', '', v)
        if len(d) >= 8 and d == re.sub(r'[\s-]', '', v) and d in flat:
            hits.append('a value with its separators removed')
            continue
        if len(d) >= 8 and d == re.sub(r'[\s-]', '', v):
            windows = six_digit_windows(flat)
            if any(d[i:i + 6] in windows for i in range(len(d) - 5)):
                hits.append('six or more consecutive digits of a value')
    return hits


_WINDOWS: dict[int, tuple[str, set[str]]] = {}


def six_digit_windows(flat: str) -> set[str]:
    """Every run of six digits in the flattened text (cached per text), so each value is a set lookup, not a scan."""
    hit = _WINDOWS.get(id(flat))
    if hit and hit[0] is flat:
        return hit[1]
    out = set()
    for m in re.finditer(r'\d{6,}', flat):
        s = m.group(0)
        out.update(s[i:i + 6] for i in range(len(s) - 5))
    _WINDOWS.clear()
    _WINDOWS[id(flat)] = (flat, out)
    return out


# ---------------------------------------------------------------- agent-found findings
def norm(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()


def excerpt_echoes_value(excerpt: str) -> str | None:
    """An excerpt must never carry personal data: no six-digit run and no address at a non-reserved domain."""
    if re.search(r'\d{6}', excerpt):
        return 'six or more consecutive digits'
    for q in re.findall(r"""'([^']*)'|"([^"]*)"|`([^`]*)`""", excerpt):
        s = next(x for x in q if x is not None) if any(q) else ''
        if len(re.sub(r'\D', '', s)) >= 6 and not (scan_data.DATE_TIME.match(s.strip()) or scan_data.DATE_SHAPE.match(s.strip())):
            return 'a quoted number of six or more digits'
    for m in scan_data.EMAIL.finditer(excerpt):
        if scan_data.email_class(m.group(2))[0] != 'reserved':
            return 'an email address'
    return None


def basis_errors(f: dict, repo: Path | None, evidence: Path | None) -> list[str]:
    """A finding is from the tools (it cites candidates) or from review (source: agent, with a basis the validator re-reads)."""
    w, errs = f['id'], []
    source = f.get('source', 'tool')
    basis = f.get('basis') or []
    if source == 'tool':
        if not f['candidates']:
            errs.append(f'{w}: a finding from the tools cites at least one candidate; one you found yourself (by reading or by an '
                        'experiment) sets source: agent and gives its basis')
        if basis:
            errs.append(f'{w}: basis is for source: agent findings')
        return errs
    if not basis:
        return [f'{w}: source: agent needs a basis (file, line and excerpt, or an evidence file)']
    files = set()
    for i, b in enumerate(basis):
        where = f'{w}.basis[{i}]'
        if 'evidence' in b:
            if evidence is None:
                errs.append(f'{where}: pass --evidence to check the evidence file')
            elif not (evidence / b['evidence']).is_file():
                errs.append(f'{where}: evidence file {b["evidence"]} is not in {evidence.as_posix()}')
            continue
        if not all(k in b for k in ('file', 'line', 'excerpt')):
            errs.append(f'{where}: give file, line and excerpt, or an evidence file')
            continue
        files.add(b['file'])
        echo = excerpt_echoes_value(b['excerpt'])
        if echo:
            errs.append(f'{where}: the excerpt contains {echo}; quote the code around a value, never the value')
        if repo is not None:
            p = repo / b['file']
            try:
                lines = p.read_text(encoding='utf-8', errors='replace').split('\n')
            except OSError:
                errs.append(f'{where}: {b["file"]} is not in the repository')
                continue
            if not 0 < b['line'] <= len(lines):
                errs.append(f'{where}: {b["file"]} has no line {b["line"]}')
            elif norm(b['excerpt']) not in norm(lines[b['line'] - 1]):
                errs.append(f'{where}: the excerpt is not on {b["file"]}:{b["line"]}')
    if files and f['location']['file'] not in files:
        errs.append(f'{w}: location is not one of its basis files')
    if f['category'] == 'time_bomb' and f['confidence'] == 'confirmed' and not f.get('fails_on_date'):
        errs.append(f'{w}: a confirmed time bomb found by review names the date it fails on (bracket it with dated runs)')
    if f['category'] == 'residue' and not any('evidence' in b for b in basis):
        errs.append(f'{w}: residue is shown by a run: cite the residue.py diff (or the run log) as evidence')
    return errs


# ---------------------------------------------------------------- checks
def run_tests_of(runs: dict, rid: str) -> dict[str, str]:
    out = {}
    for c in runs['cases']:
        if c['run_id'] == rid:
            f = (c.get('file') or '').replace('\\', '/')
            out[f'{f}::{c["name"]}'] = c['status']
    return out


def check(rep: dict, cands: dict | None, runs: dict | None, repo: Path | None, evidence: Path | None, report_text: str) -> list[str]:
    errs = []
    fs = rep['findings']
    ids = [f['id'] for f in fs]
    if len(set(ids)) != len(ids):
        errs.append('findings: duplicate ids')
    if cands is not None:
        cand_ids = set(cands)
        promoted = [c for f in fs for c in f['candidates']]
        dismissed = [d['candidate_id'] for d in rep['dismissed']]
        handed = [h['candidate_id'] for h in rep['handoffs'] if h.get('candidate_id')]
        seen = promoted + dismissed + handed
        for c in sorted(cand_ids - set(seen), key=lambda x: int(x[1:])):
            errs.append(f'{c}: neither promoted, dismissed nor handed off')
        for c in sorted({x for x in seen if seen.count(x) > 1}):
            errs.append(f'{c}: appears in more than one of findings, dismissed and handoffs')
        for c in sorted(set(seen) - cand_ids):
            errs.append(f'{c}: not a candidate in the candidates file')
        for d in rep['dismissed']:
            c = cands.get(d['candidate_id'])
            if c and c['auto_dismiss'] and (d['by'] != 'rule' or c['auto_dismiss'] not in d['reason']):
                errs.append(f'{d["candidate_id"]}: dismissed by a reserved-value rule; say so (by: rule) and quote the rule')
            if c and not c['auto_dismiss'] and d['by'] == 'rule':
                errs.append(f'{d["candidate_id"]}: no rule dismisses this candidate; a review dismissal needs your reason')
            if c and c['category'] == 'secret' and not c['auto_dismiss']:
                errs.append(f'{d["candidate_id"]}: secrets are handed to code-security-auditor, not dismissed (only a value '
                            'scan_data.py marks as an obvious placeholder may be dismissed, by rule)')
        for f in fs:
            linked = [cands[c] for c in f['candidates'] if c in cands]
            if any(c['category'] != f['category'] for c in linked):
                errs.append(f'{f["id"]}: its candidates are a different category')
            if linked and f['location']['file'] not in {c['file'] for c in linked}:
                errs.append(f'{f["id"]}: location is not one of its candidates\' files')
            if any(c['auto_dismiss'] for c in linked):
                errs.append(f'{f["id"]}: promotes a candidate a reserved-value rule dismisses')
            if f['category'] == 'time_bomb':
                want = next((c['details'].get('fails_on_date') for c in linked if c['details'].get('fails_on_date')), None)
                if want and f.get('fails_on_date') != want:
                    errs.append(f'{f["id"]}: fails_on_date must be {want}, from the date arithmetic')
        for c in cands.values():
            if c['category'] == 'secret' and not c['auto_dismiss'] and not any(h.get('candidate_id') == c['id'] and h['to'] == 'code-security-auditor'
                                                    for h in rep['handoffs']):
                errs.append(f'{c["id"]}: a secret must be handed to code-security-auditor (location and type only)')
    for f in fs:
        errs += basis_errors(f, repo, evidence)
    for f in fs:
        w = f['id']
        if f['category'] == 'personal_data':
            if f.get('real_looking') is not True:
                errs.append(f'{w}: personal data is real_looking: true (the repository cannot tell otherwise); dismiss it with a reason instead')
            if f['priority'] != 'P0':
                errs.append(f'{w}: real-looking personal data is always P0')
        exp = f.get('experiment')
        if f['confidence'] == 'confirmed':
            # date arithmetic counts only where scan_data.py itself computed the fail date (a fixed expiry compared with now)
            arithmetic = f['category'] == 'time_bomb' and f.get('fails_on_date') and cands is not None and any(
                cands[c]['details'].get('compared_with_now') and cands[c]['details'].get('fails_on_date') == f['fails_on_date']
                for c in f['candidates'] if c in cands)
            # residue is shown by the tree diff itself (an evidence file the validator checks exists)
            residue = f['category'] == 'residue' and any('evidence' in b for b in f.get('basis') or [])
            if not (exp and exp['result'] == 'failed_as_predicted') and not arithmetic and not residue:
                errs.append(f'{w}: confirmed needs a failing experiment' + (' or a fails_on_date from date arithmetic'
                                                                             if f['category'] == 'time_bomb' else ''))
        if exp and exp['result'] == 'failed_as_predicted':
            if runs is None:
                errs.append(f'{w}: pass --runs to check the experiment')
            else:
                all_ids = {r['id'] for r in runs['runs']}
                missing = [r for r in exp['run_ids'] + exp.get('baseline_run_ids', []) if r not in all_ids]
                if missing:
                    errs.append(f'{w}: experiment runs {missing} are not in the runs file')
                else:
                    failed = any(run_tests_of(runs, r).get(t) in ('failed', 'error') for r in exp['run_ids'] for t in f['affected_tests'])
                    if not failed:
                        errs.append(f'{w}: no affected test failed in the experiment runs')
                    for r in exp.get('baseline_run_ids', []):
                        res = run_tests_of(runs, r)
                        bad = [t for t in f['affected_tests'] if res.get(t) in ('failed', 'error')]
                        if bad:
                            errs.append(f'{w}: {bad[0]} also fails in baseline run {r}, so the variation is not what broke it')
                    if not exp.get('baseline_run_ids'):
                        errs.append(f'{w}: name the baseline runs the experiment is compared with')
        if f['category'] == 'shared_mutable' and f['confidence'] == 'confirmed' and not any(
                h['to'] == 'flaky-test-hunter' and h.get('finding_id') == f['id'] for h in rep['handoffs']):
            errs.append(f'{w}: hand confirmed shared state to flaky-test-hunter')
    if not rep['metadata']['apply_fixes'] and rep['files_changed']:
        errs.append('files_changed: fixes are proposed, not applied, unless apply_fixes is true')
    # text rules
    texts = [('summary.headline', rep['summary'].get('headline', ''))] + \
        [(f'limitations[{i}]', t) for i, t in enumerate(rep['limitations'])] + \
        [(f'{f["id"]}.{k}', f.get(k) or '') for f in fs for k in ('title', 'why_flagged', 'fix')] + \
        [(f'dismissed[{d["candidate_id"]}]', d['reason']) for d in rep['dismissed']]
    for where, t in texts:
        m = BANNED.search(t)
        if m:
            errs.append(f'{where}: "{m.group(0)}" — say which files and formats were scanned, never that the data is clean')
    if any(f['priority'] == 'P0' for f in fs) and 'P0' not in rep['summary'].get('headline', ''):
        errs.append('summary.headline: name the P0 findings')
    # no personal data or secret value anywhere
    if repo is not None and cands is not None:
        per = {c['id']: flagged_values(c, repo) for c in cands.values()
               if c['category'] in ('personal_data', 'secret') and not c['auto_dismiss']}
        per = {k: v for k, v in per.items() if v}

        def leak_errors(where: str, text: str) -> list[str]:
            # the candidate id is safe to name and saves hunting for the leak; the value never is
            flat, out = re.sub(r'[\s-]', '', text), []
            for cid in sorted(per, key=lambda x: int(x[1:])):
                for kind in sorted(set(leaks(per[cid], text, flat))):
                    out.append(f'{where} contains {kind} flagged by {cid}; give the file, line, field and data type only '
                               '(redact evidence with redact_evidence.py)')
            return out
        errs += leak_errors('the report', report_text)
        if evidence is not None:
            for p in sorted(evidence.rglob('*')):
                if p.is_file() and p.suffix.lower() in ('.json', '.xml', '.txt', '.md', '.log', '.html', '.sh'):
                    errs += leak_errors(p.relative_to(evidence).as_posix(), p.read_text(encoding='utf-8', errors='replace'))
    # computed
    if cands is not None:
        want = json.loads(json.dumps(rep))
        fill(want, cands)
        for a, b in zip(rep['findings'], want['findings']):
            for k in ('severity', 'reach', 'score', 'priority'):
                if a[k] != b[k]:
                    errs.append(f'{a["id"]}.{k}: {a[k]!r} but computed {b[k]!r} (run --write-summary)')
    want_s = summary(rep)
    for k, v in want_s.items():
        if rep['summary'].get(k) != v:
            errs.append(f'summary.{k}: {rep["summary"].get(k)!r} but computed {v!r} (run --write-summary)')
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--candidates', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--runs', type=Path)
    ap.add_argument('--evidence', type=Path, help='run directory whose files must not contain flagged values')
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    rep = json.loads(args.report.read_text(encoding='utf-8'))
    cands = {c['id']: c for c in json.loads(args.candidates.read_text(encoding='utf-8'))['candidates']} if args.candidates else None
    if args.write_summary:
        if cands is None:
            print('INVALID: --write-summary needs --candidates')
            return 1
        try:
            fill(rep, cands)
            rep['summary'] = {'headline': (rep.get('summary') or {}).get('headline', ''), **summary(rep)}
        except (KeyError, TypeError, ValueError) as exc:
            print(f'INVALID: cannot compute the summary ({exc!r}); fix the report structure first')
            return 1
        args.report.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errs = schema_errors(rep, schema, schema)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    runs = json.loads(args.runs.read_text(encoding='utf-8')) if args.runs else None
    errs = check(rep, cands, runs, args.repo.resolve() if args.repo else None, args.evidence, args.report.read_text(encoding='utf-8'))
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    s = rep['summary']
    print(f'VALID: {len(rep["findings"])} findings ({", ".join(f"{v} {k}" for k, v in s["by_priority"].items())}), '
          f'{s["dismissed"]} dismissed, {s["handoffs"]} handed off, {s["files_scanned"]} files scanned')
    return 0


if __name__ == '__main__':
    sys.exit(main())
