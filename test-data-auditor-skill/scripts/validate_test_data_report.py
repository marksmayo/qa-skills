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
        hosts = [cands[c]['details'].get('host') or '' for c in f['candidates'] if c in cands] + [f.get('host') or '']
        # a review finding whose basis shows tests reaching a live endpoint (a proxy log, the code that calls it) is as
        # serious as a production host by name; live_host_errors() checks the basis names the host
        live = f.get('source') == 'agent' and f.get('live_endpoint') is True
        return 'serious' if live or any(re.search(r'(^|[.-])(prod|production|live)([.-]|$)', h, re.I) for h in hosts) else 'moderate'
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
    tools = [t for t in rep['tools'] if t['status'] != 'not_applicable']
    ran = len([t for t in tools if t['status'] == 'ran']) / len(tools) if tools else 1.0
    if not conf:
        # nothing to confirm is not evidence that experiments ran: the share is left out, not counted as complete
        return round((scanned + ran) / 2, 4)
    exp = len([f for f in conf if (f.get('experiment') or {}).get('result') in ('failed_as_predicted', 'passed')]) / len(conf)
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
VALUE_SPANS = [scan_data.CARD, scan_data.PHONE_TEXT, scan_data.SSN, scan_data.IBAN, re.compile(r'(?<![\w])\d[\d -]{4,}\d(?![\w])')]


def spans_on(line: str) -> set[str]:
    """Every string on a line that a personal-data rule could have taken as its value: quoted strings, delimited
    fields, and the digit runs, cards, phones, SSNs, NINOs and IBANs the scanner matches."""
    out = {q[0] or q[1] for q in re.findall(r'"([^"]+)"|\'([^\']+)\'', line)}
    out |= {x.strip() for x in re.split(r'[,\t;|<>]', line) if x.strip()}
    for rx in VALUE_SPANS:
        out |= {m.group(0) for m in rx.finditer(line)}
    out |= {m.group(0) for m in scan_data.NINO.finditer(line.upper())}
    return out


def extract_values(c: dict, p: Path) -> set[str]:
    """The values of a flagged extract column that could identify someone: names of two or more words, or long ones."""
    vals = set()
    try:
        for r in datafiles.load_records(p, c['file']):
            v = r['fields'].get(c['field'], {}).get('value')
            if isinstance(v, str) and (len(v.strip()) >= 8 or (' ' in v.strip() and len(v.strip()) >= 6)):
                vals.add(v.strip())
            if len(vals) >= 5000:
                break
    except datafiles.NotAssessed:
        pass
    return vals


def flagged_values(c: dict, repo: Path) -> set[str]:
    p = repo / c['file']
    if c['rule'] == 'customer_extract':
        return extract_values(c, p)
    try:
        lines = datafiles.text_of(p).split('\n')
    except (OSError, datafiles.NotAssessed):
        return set()
    line = lines[c['line'] - 1] if 0 < c['line'] <= len(lines) else ''
    vals: set[str] = set()
    fpr = c.get('fingerprint')
    if c['category'] == 'personal_data' and fpr and c['rule'] not in ('name_and_address', 'dob_with_name'):
        # the candidate's own value: the span on its line whose fingerprint is the candidate's, not every number on the line
        for s in spans_on(line):
            d = scan_data.digits(s)
            if scan_data.fp(s) == fpr or (len(d) >= 6 and scan_data.fp(d) == fpr):
                if scan_data.fp(s) == fpr or not re.sub(r'[\d\s().+/-]', '', s):
                    vals.add(s)            # the value as written (a number with its separators), never the text around it
                if len(d) >= 6 and c['rule'] != 'email':
                    vals.add(d)
                if c['rule'] == 'email':
                    m = scan_data.EMAIL.search(s)
                    local = m.group(1) if m else ''
                    if len(local) >= 5 and re.search(r'[A-Za-z][._-][A-Za-z]', local) and local.lower() not in GENERIC_LOCAL:
                        vals.add(local)
        if vals:
            return {v for v in vals if len(v) >= 4}
        # no span carries the fingerprint (the line changed, or an older candidates file): fall back to every value on it
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


def repo_paths(cand_doc: dict | None) -> list[str]:
    """Every repository path the scanner reported (the inventory and the candidates' files) and each parent directory,
    longest first. A path is a fact about the repository, not a value someone copied: a dated run directory in a
    fixture's path is not a phone or a card wherever the path is named."""
    if not cand_doc:
        return []
    paths = set(cand_doc.get('inventory_status', {})) | {c['file'] for c in cand_doc.get('candidates', [])}
    out = set()
    for p in paths:
        parts = p.split('/')
        out |= {'/'.join(parts[:i]) + '/' for i in range(1, len(parts))}
        out.add(p)
    out |= {x.replace('/', '\\') for x in out}
    return sorted(out, key=len, reverse=True)


_PATH_RX: dict[tuple, re.Pattern] = {}


def without_paths(text: str, paths: list[str]) -> str:
    """The text with every known repository path blanked out (so its digits are not read as a value)."""
    if not paths:
        return text
    key = tuple(paths)
    if key not in _PATH_RX:
        _PATH_RX.clear()
        _PATH_RX[key] = re.compile('|'.join(re.escape(x) for x in paths))
    return _PATH_RX[key].sub(' ', text)


def protected_values(cands: dict, repo: Path, rep: dict | None = None) -> dict[str, set[str]]:
    """{candidate id: values that must never appear}: every personal-data and secret candidate no rule dismissed,
    except a personal-data candidate the review dismissed as not personal data at all (not_personal_data: true)."""
    exempt = {d['candidate_id'] for d in (rep or {}).get('dismissed', []) if d.get('not_personal_data') and d.get('by') == 'review'}
    per = {c['id']: flagged_values(c, repo) for c in cands.values()
           if c['category'] in ('personal_data', 'secret') and not c['auto_dismiss'] and c['id'] not in exempt}
    # a personal-data or secret finding from review (source: agent) has no candidate: the values on its basis lines are
    # protected the same way, so the address it found cannot be pasted into why_flagged either
    for f in (rep or {}).get('findings', []):
        if f.get('source') != 'agent' or f.get('category') not in ('personal_data', 'secret'):
            continue
        vals: set[str] = set()
        for b in f.get('basis') or []:
            if b.get('file') and isinstance(b.get('line'), int):
                pseudo = {'category': f['category'], 'rule': 'agent', 'file': b['file'], 'line': b['line'],
                          'fingerprint': None, 'details': {}}
                vals |= {v for v in flagged_values(pseudo, repo) if not reserved_address(v)}
        if vals:
            per[f['id']] = vals
    return {k: v for k, v in per.items() if v}


def reserved_address(v: str) -> bool:
    """An address at a reserved domain (example.com, .test): never personal data, so naming it is no leak."""
    m = scan_data.EMAIL.fullmatch(v.strip())
    return bool(m) and scan_data.email_class(m.group(2))[0] == 'reserved'


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
        if scan_data.email_class(m.group(2))[0] != 'reserved' and not scan_data.not_mailbox(excerpt, m):
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
                lines = datafiles.text_of(p).split('\n')
            except (OSError, datafiles.NotAssessed):
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


def live_host_errors(f: dict, evidence: Path | None, repo: Path | None = None) -> list[str]:
    """A finding's `host` and `live_endpoint: true` are claims about where tests reach: only a review finding may make
    them, and one of its basis items (an excerpt, its re-read line, or an evidence file such as a proxy log) must name
    the host."""
    w, errs = f['id'], []
    if not f.get('host') and not f.get('live_endpoint'):
        return errs
    if f.get('live_endpoint') and f['category'] != 'environment_coupling':
        errs.append(f'{w}: live_endpoint is for environment_coupling findings')
    if f.get('source') != 'agent' or not f.get('basis'):
        return errs + [f'{w}: host and live_endpoint are for source: agent findings, with a basis that names the host']
    if f.get('live_endpoint') and not f.get('host'):
        return errs + [f'{w}: live_endpoint needs the host the tests reach']
    host = f['host'].lower()
    texts = []
    for b in f['basis']:
        if 'evidence' in b and evidence is not None and (evidence / b['evidence']).is_file():
            texts.append((evidence / b['evidence']).read_text(encoding='utf-8', errors='replace'))
        if 'excerpt' in b:
            texts.append(b['excerpt'])
        if repo is not None and 'file' in b and isinstance(b.get('line'), int):
            try:
                lines = datafiles.text_of(repo / b['file']).split('\n')
                texts.append(lines[b['line'] - 1] if 0 < b['line'] <= len(lines) else '')
            except (OSError, datafiles.NotAssessed):
                pass
    if not any(host in t.lower() for t in texts):
        errs.append(f'{w}: no basis item names the host {f["host"]} (quote the line that calls it, or cite the proxy or sink log '
                    'that recorded the tests reaching it; pass --evidence to check an evidence file)')
    return errs


# ---------------------------------------------------------------- checks
def run_tests_of(runs: dict, rid: str, cwd: str | None = None) -> dict[str, str]:
    """{test id: status} for one run. Case files are relative to the directory the run started in; with the
    experiment's cwd (repo-relative) they are made repo-relative, like the inventory's and the findings' ids."""
    out = {}
    prefix = (cwd or '').replace('\\', '/').strip('/')
    for c in runs['cases']:
        if c['run_id'] == rid:
            f = (c.get('file') or '').replace('\\', '/')
            if prefix and prefix != '.' and f:
                f = f'{prefix}/{f}'
            out[f'{f}::{c["name"]}'] = c['status']
    return out


def run_errored(run: dict) -> str | None:
    """Why a run is not a result (a crash, a timeout, a usage error, no tests), or None. A crashed interpreter can
    leave exit code 3221225725 or -11 and no cases, which a runner may record as 0 passed, 0 failed."""
    t = run.get('totals') or {}
    if run.get('timed_out'):
        return 'timed out'
    if not t.get('tests'):
        return f'ran no tests (0 cases parsed, exit code {run.get("exit_code")})'
    code = run.get('exit_code')
    if code is None or code in (0, 1):
        return None
    if run.get('preset') != 'custom' or code < 0 or code > 255 or 'pytest' in (run.get('command') or ''):
        return f'exit code {code} (not a test result: a crash, an interrupted run or a usage error)'
    return None


def _var(r: dict) -> dict:
    return r.get('variation') or {}


VARIATION_CHECKS = [
    (re.compile(r'revers', re.I), lambda r: _var(r).get('order') == 'reverse', 'order reverse'),
    (re.compile(r'shuffl|random order', re.I), lambda r: _var(r).get('order') == 'shuffle', 'order shuffle'),
    (re.compile(r'\blocale\b|\bLANG\b|\bLC_ALL\b', re.I),
     lambda r: bool(_var(r).get('locale')) or bool({'LANG', 'LC_ALL'} & set(_var(r).get('env') or {})), 'a locale'),
    (re.compile(r'time ?zone|\bTZ\b'), lambda r: bool(_var(r).get('tz')) or 'TZ' in (_var(r).get('env') or {}), 'a time zone'),
    (re.compile(r'isolat', re.I), lambda r: bool(_var(r).get('isolate')) or len(r.get('tests_selected') or []) == 1, 'isolation'),
]


def check(rep: dict, cands: dict | None, runs: dict | None, repo: Path | None, evidence: Path | None, report_text: str,
          paths: list[str] | None = None, candidates_path: Path | None = None) -> list[str]:
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
            if d.get('not_personal_data') and (d['by'] != 'review' or (c and c['category'] != 'personal_data')):
                errs.append(f'{d["candidate_id"]}: not_personal_data is a review judgement on a personal_data candidate '
                            '(a dated run id, an order number); it lifts the leak check for that candidate only')
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
        errs += live_host_errors(f, evidence, repo)
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
        if exp and exp['result'] in ('failed_as_predicted', 'passed') and runs is not None:
            by_id = {r['id']: r for r in runs['runs']}
            for r in exp['run_ids'] + exp.get('baseline_run_ids', []):
                why = run_errored(by_id[r]) if r in by_id else None
                if why:
                    errs.append(f'{w}: run {r} {why}; a crashed or empty run is not a result: set the experiment result to '
                                'errored, or fix the run and rerun it')
            for rx, ok, what in VARIATION_CHECKS:
                if rx.search(exp['variation']) and exp['run_ids'] and not any(ok(by_id[r]) for r in exp['run_ids'] if r in by_id):
                    errs.append(f'{w}: the variation says {what}, but no experiment run records it in runs.json '
                                '(check the option reached run_tests.py)')
        if exp and exp['result'] == 'failed_as_predicted':
            if runs is None:
                errs.append(f'{w}: pass --runs to check the experiment')
            else:
                all_ids = {r['id'] for r in runs['runs']}
                cwd = exp.get('cwd')
                missing = [r for r in exp['run_ids'] + exp.get('baseline_run_ids', []) if r not in all_ids]
                if missing:
                    errs.append(f'{w}: experiment runs {missing} are not in the runs file')
                else:
                    failed = any(run_tests_of(runs, r, cwd).get(t) in ('failed', 'error') for r in exp['run_ids'] for t in f['affected_tests'])
                    if not failed:
                        errs.append(f'{w}: no affected test failed in the experiment runs' +
                                    ('' if cwd else ' (if the runs started in a subdirectory, give the experiment its cwd)'))
                    for r in exp.get('baseline_run_ids', []):
                        res = run_tests_of(runs, r, cwd)
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
    headline = rep['summary'].get('headline', '')
    if any(f['priority'] == 'P0' for f in fs) and 'P0' not in headline:
        errs.append('summary.headline: name the P0 findings')
    m = re.search(r'\b(\d+) findings?\b', headline)
    if m and int(m.group(1)) != len(fs):
        errs.append(f'summary.headline: says {m.group(1)} finding(s), but the report has {len(fs)}')
    if fs and re.search(r'\bno findings\b', headline, re.I):
        errs.append(f'summary.headline: says no findings, but the report has {len(fs)}')
    # no personal data or secret value anywhere
    if repo is not None:
        per = protected_values(cands or {}, repo, rep)

        def leak_errors(where: str, text: str) -> list[str]:
            # the candidate id is safe to name and saves hunting for the leak; the value never is
            text = without_paths(text, paths or [])
            flat, out = re.sub(r'[\s-]', '', text), []
            for cid in sorted(per, key=lambda x: int(x[1:])):
                for kind in sorted(set(leaks(per[cid], text, flat))):
                    out.append(f'{where} contains {kind} flagged by {cid}; give the file, line, field and data type only '
                               '(redact evidence with redact_evidence.py)')
            return out
        errs += leak_errors('the report', report_text)
        if evidence is not None:
            skip = candidates_path.resolve() if candidates_path else None
            for p in sorted(evidence.rglob('*')):
                if skip is not None and p.resolve() == skip:
                    continue               # the scanner's own output: fingerprints and paths, never values (as redact_evidence.py)
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
    cand_doc = json.loads(args.candidates.read_text(encoding='utf-8')) if args.candidates else None
    cands = {c['id']: c for c in cand_doc['candidates']} if cand_doc else None
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
    errs = check(rep, cands, runs, args.repo.resolve() if args.repo else None, args.evidence, args.report.read_text(encoding='utf-8'),
                 repo_paths(cand_doc), args.candidates)
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
