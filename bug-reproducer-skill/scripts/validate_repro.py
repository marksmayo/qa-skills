#!/usr/bin/env python3
"""Validate a bug reproduction report, or a batch of them, against its evidence and the rubric (stdlib only).

    python validate_repro.py repro_report.json [--tickets tickets.json] [--run RUN_DIR] [--write-summary]

Paths in the report (artifacts, script_path, runs_file) are relative to the report's directory.

Checks:
  outcome      recomputed from the attempts: reproduced needs a matched attempt with an artifact on
               disk; the basis candidate is the as-reported one when it matched, else the first
               variation that did; confidence is high only for >= 2/3 on the as-reported candidate
               and the stated version, otherwise medium; versions_tried equals the versions the
               attempts saw; not_reproduced needs at least `attempts` attempts and a variation;
               cannot_attempt has no attempts that ran
  rating       score = weight x reach x frequency, frequency recomputed (3 every attempt, 2 at
               least two, 1 otherwise), priority thresholds P0 >= 24, P1 >= 12, P2 >= 6, S1 at least
               P1, not_reproduced is as_reported, low confidence and never P0
  regression   a written test's red is an assertion failure in its run (runs_file), never an error
  duplicates   similarity = 0.5 sig + 0.3 summary + 0.2 component, verdict by threshold
  triage       with --tickets: content_sha256 matches, every suspicious instruction is listed as
               ignored, every harmful step (triage.py) is in skipped_steps, and no attempt step
               quotes it; questions are asked for every missing field
  run          with --run: every attempt equals the record in RUN_DIR/attempts/<key>.json
  safety       non-loopback targets need authorised_by; no secret, email or card number anywhere
  honesty      a bug that isn't reproduced is never called "not a bug", "invalid", "fixed" or
               "works as designed", and its headline doesn't say it reproduced
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_check, triage as triage_mod  # noqa: E402
from browser_harness import is_loopback  # noqa: E402

SCHEMA = json.loads((HERE.parent / 'schemas' / 'repro_report.schema.json').read_text(encoding='utf-8'))
WEIGHT = {'S1': 4, 'S2': 3, 'S3': 2, 'S4': 1}
SECRETS = [
    (re.compile(r'[\w.+-]+@[\w-]+\.[a-z]{2,}(?:\.[a-z]{2,})?', re.I), 'an email address'),
    (re.compile(r'(?i)\bbearer\s+(?!\[secret\])[\w.~+/-]{12,}'), 'a bearer token'),
    (re.compile(r'\b(AKIA|ASIA)[0-9A-Z]{16}\b'), 'an AWS access key'),
    (re.compile(r'(?i)\b(password|passwd|api[_-]?key|secret)\s*[=:]\s*(?!\[secret\])[^\s"\[]{6,}'), 'a credential'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'a private key'),
]
CARD = re.compile(r'\b(?:\d[ -]?){13,19}\b')
DISMISS = re.compile(r"\b(not a bug|invalid|works as (designed|intended|expected)|(is|was|been|already|now) fixed|user error|"
                     r"cannot happen|can't happen)\b", re.I)


def luhn_ok(digits: str) -> bool:
    d = [int(c) for c in digits][::-1]
    total = sum(d[0::2]) + sum(sum(divmod(2 * x, 10)) for x in d[1::2])
    return len(d) >= 13 and total % 10 == 0


def priority(score: int) -> str:
    return 'P0' if score >= 24 else 'P1' if score >= 12 else 'P2' if score >= 6 else 'P3'


def expected_outcome(r: dict, cfg_attempts: int) -> dict:
    atts = r['attempts']
    ran = [a for a in atts if a['result'] != 'errored']
    stated = r['metadata']['target']['version_stated']
    versions = sorted({a['version'] for a in atts if a.get('version')})
    cands: dict[str, list] = {}
    for a in atts:
        cands.setdefault(a['candidate'], []).append(a)
    matched_c = [c for c, xs in cands.items() if any(a['result'] == 'matched' for a in xs)]
    base = {'versions_tried': versions}
    if not ran:
        return {**base, 'value': 'cannot_attempt', 'confidence': 'n/a', 'matched_attempts': 0, 'total_attempts': len(atts),
                'basis_candidate': None, 'only_after_variation': False}
    if matched_c:
        as_rep = [c for c in matched_c if cands[c][0].get('variation') is None]
        basis = as_rep[0] if as_rep else matched_c[0]
        xs = cands[basis]
        m, n = sum(a['result'] == 'matched' for a in xs), len(xs)
        after_var = not as_rep
        on_stated = stated is None or all(a.get('version') == stated for a in xs if a['result'] == 'matched')
        conf = 'high' if (not after_var and n and m / n >= 2 / 3 and on_stated) else 'medium'
        return {**base, 'value': 'reproduced', 'confidence': conf, 'matched_attempts': m, 'total_attempts': n,
                'basis_candidate': basis, 'only_after_variation': after_var}
    diff = [a for a in atts if a['result'] == 'different']
    if diff:
        return {**base, 'value': 'reproduced_differently', 'confidence': 'medium', 'matched_attempts': 0, 'total_attempts': len(atts),
                'basis_candidate': diff[0]['candidate'], 'only_after_variation': diff[0].get('variation') is not None}
    return {**base, 'value': 'not_reproduced', 'confidence': 'n/a', 'matched_attempts': 0, 'total_attempts': len(atts),
            'basis_candidate': None, 'only_after_variation': False}


def texts(obj, path='$'):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from texts(v, f'{path}.{k}')
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from texts(v, f'{path}[{i}]')


def check_report(r: dict, base: Path, tickets: dict | None, run_dir: Path | None) -> list[str]:
    k = r['key']
    errs = []
    E = lambda m: errs.append(f'{k}: {m}')  # noqa: E731
    cfg = r['metadata']['config']
    out = r['outcome']
    exp = expected_outcome(r, cfg['attempts'])
    for f in ('value', 'confidence', 'matched_attempts', 'total_attempts', 'versions_tried', 'basis_candidate', 'only_after_variation'):
        if out.get(f) != exp[f]:
            E(f'outcome.{f} is {out.get(f)!r}; the attempts give {exp[f]!r}')
    if sorted(r['metadata']['target']['versions_observed']) != exp['versions_tried']:
        E(f'metadata.target.versions_observed is {r["metadata"]["target"]["versions_observed"]}; the attempts saw {exp["versions_tried"]}')
    allm = {'matched': sum(a['result'] == 'matched' for a in r['attempts']), 'total': len(r['attempts'])}
    if out.get('all_attempts') not in (None, allm):
        E(f'outcome.all_attempts is {out["all_attempts"]}; the attempts give {allm}')
    # artifacts
    for a in r['attempts']:
        if not a['artifacts'] and a['result'] != 'errored':
            E(f'attempt {a["id"]} has no artifact')
        for p in a['artifacts']:
            if not (base / p).is_file():
                E(f'attempt {a["id"]}: artifact {p} is not on disk')
        if a['result'] == 'matched' and not a.get('observed_signature'):
            E(f'attempt {a["id"]} matched but records no observed_signature')
    if out['value'] == 'reproduced':
        if not any(a['result'] == 'matched' and a['artifacts'] for a in r['attempts']):
            E('reproduced, but no matched attempt has an artifact')
        if r['signature'].get('failure_checks') is None:
            E('reproduced, but signature.failure_checks (what counts as the reported failure) is empty')
    if out['value'] == 'not_reproduced':
        if len(r['attempts']) < cfg['attempts']:
            E(f'not_reproduced with {len(r["attempts"])} attempt(s); at least {cfg["attempts"]} are configured')
        if not any(a.get('variation') for a in r['attempts']):
            E('not_reproduced without any variation (browser, data, locale, time zone, network or version)')
    stated = r['metadata']['target']['version_stated']
    if stated and out['value'] in ('reproduced', 'not_reproduced') and stated not in exp['versions_tried']:
        if not any(stated in lim for lim in r['limitations']):
            E(f'the report names version {stated} but no attempt ran on it, and no limitation says so')
    if out['value'] == 'cannot_attempt' and not r['triage']['questions'] and not r['limitations']:
        E('cannot_attempt needs the questions to ask or the limitation that stopped it')
    # minimal repro
    mr = r.get('minimal_repro')
    if mr:
        if out['value'] != 'reproduced':
            E('minimal_repro on a report that did not reproduce')
        if not (base / mr['script_path']).is_file():
            E(f'minimal_repro.script_path {mr["script_path"]} is not on disk')
        for p in mr['artifacts']:
            if not (base / p).is_file():
                E(f'minimal_repro artifact {p} is not on disk')
        m, n = map(int, mr['confirmed'].split(' of '))
        if m == 0:
            E('minimal_repro was not confirmed by any run')
    # rating
    rt = r['rating']
    if out['value'] == 'cannot_attempt':
        if rt is not None and rt['basis'] != 'as_reported':
            E('cannot_attempt can only carry an as_reported rating')
    elif rt is None:
        E('rating is missing')
    if rt:
        if out['value'] == 'reproduced':
            m, n = out['matched_attempts'], out['total_attempts']
            freq = 3 if m == n else 2 if m >= 2 else 1
        else:
            freq = 1
        if rt['frequency'] != freq:
            E(f'rating.frequency is {rt["frequency"]}; the attempts give {freq} (3 every attempt, 2 at least two, 1 otherwise)')
        score = WEIGHT[rt['severity']] * rt['reach'] * rt['frequency']
        if rt['score'] != score:
            E(f'rating.score is {rt["score"]}; {rt["severity"]} x reach {rt["reach"]} x frequency {rt["frequency"]} = {score}')
        pr = priority(score)
        if rt['severity'] == 'S1' and pr in ('P2', 'P3'):
            pr = 'P1'
        if out['value'] != 'reproduced':
            if pr == 'P0':
                pr = 'P1'
            if rt['basis'] != 'as_reported' or rt['confidence'] != 'low':
                E('a bug that did not reproduce keeps its reported severity: basis as_reported, confidence low')
        elif rt['basis'] != 'reproduced':
            E('a reproduced bug is rated on basis reproduced')
        if rt['priority'] != pr:
            E(f'rating.priority is {rt["priority"]}; score {score} gives {pr}')
    # regression test
    rg = r.get('regression_test')
    if rg and rg['status'] == 'written':
        red = rg.get('red')
        if out['value'] != 'reproduced':
            E('a regression test was written for a bug that did not reproduce')
        if not rg.get('path') or not red:
            E('a written regression test needs its path and its red run')
        else:
            rf = base / red['runs_file']
            if not rf.is_file():
                E(f'regression_test.red.runs_file {red["runs_file"]} is not on disk')
            else:
                runs = json.loads(rf.read_text(encoding='utf-8'))
                cases = [c for c in runs.get('cases', []) if c['run_id'] == red['run_id']]
                if not any(x['id'] == red['run_id'] for x in runs.get('runs', [])):
                    E(f'regression run {red["run_id"]} is not in {red["runs_file"]}')
                elif not cases:
                    E(f'regression run {red["run_id"]} has no test cases')
                else:
                    bad = [c for c in cases if c['status'] != 'failed' or c.get('failure_type') != 'assertion']
                    if bad:
                        E(f'regression test is not red by assertion: {bad[0]["name"]} is {bad[0]["status"]}/{bad[0].get("failure_type")}')
                    if red['status'] != 'assertion_failure':
                        E('regression_test.red.status must be assertion_failure')
                    if red['failure_excerpt'] and not any(red['failure_excerpt'] in (c.get('message_excerpt') or '') for c in cases):
                        E('regression_test.red.failure_excerpt is not in the run\'s failure messages')
            if not (base / rg['path']).is_file():
                E(f'regression test {rg["path"]} is not on disk')
            if rg['path'] not in r['files_written']:
                E(f'regression test {rg["path"]} is not listed in files_written')
        if cfg['write_regression_test'] == 'propose' and not rg.get('approval'):
            E('write_regression_test is propose, but the written test records no approval')
        if cfg['write_regression_test'] == 'off':
            E('write_regression_test is off, but a test was written')
    # duplicates
    for d in r['duplicates']:
        c = d['components']
        sim = round(0.5 * c['signature'] + 0.3 * c['summary'] + 0.2 * c['component'], 4)
        if abs(sim - d['similarity']) > 1e-4:
            E(f'duplicate {d["key"]}: similarity {d["similarity"]}; components give {sim}')
        v = 'likely_duplicate' if sim >= 0.7 else 'related' if sim >= 0.4 else None
        if v != d['verdict']:
            E(f'duplicate {d["key"]}: verdict {d["verdict"]}; similarity {sim} gives {v}')
    # triage against the ticket
    if tickets is not None:
        t = tickets.get(k)
        if t is None:
            E('not in --tickets')
        else:
            tri = triage_mod.triage(t)
            if r['metadata']['report']['content_sha256'] != t.get('content_sha256'):
                E('metadata.report.content_sha256 does not match the ticket')
            if sorted(r['triage']['fields_missing']) != sorted(tri['fields_missing']):
                E(f'triage.fields_missing is {r["triage"]["fields_missing"]}; the ticket gives {tri["fields_missing"]}')
            if len(r['triage']['questions']) < len(tri['fields_missing']):
                E('fewer questions than missing fields')
            for s in tri['suspicious_instructions']:
                txt = s if isinstance(s, str) else s.get('excerpt') or s.get('text', '')
                if not any(txt[:60] in x or x[:60] in txt for x in r['triage']['suspicious_instructions_ignored']):
                    E(f'suspicious instruction not listed as ignored: "{txt[:60]}"')
            for h in tri['harmful_steps']:
                if not any(h['match'].lower() in s['text'].lower() for s in r['skipped_steps']):
                    E(f'harmful step "{h["match"]}" is not in skipped_steps')
                for a in r['attempts']:
                    if h['match'].lower() in json.dumps(a['steps']).lower():
                        E(f'attempt {a["id"]} runs the harmful step "{h["match"]}"')
            if not tri['can_attempt'] and out['value'] != 'cannot_attempt':
                E(f'the report has no steps and no failure signature, but the outcome is {out["value"]}')
    # run records
    if run_dir is not None and r['attempts']:
        f = run_dir / 'attempts' / f'{k}.json'
        if not f.is_file():
            E(f'{f.as_posix()} is missing')
        else:
            recs = {a['id']: a for a in json.loads(f.read_text(encoding='utf-8'))['attempts']}
            if sorted(recs) != sorted(a['id'] for a in r['attempts']):
                E(f'the report\'s attempts are not the ones in {f.name}')
            for a in r['attempts']:
                ra = recs.get(a['id'])
                if ra and any(ra.get(x) != a.get(x) for x in ('result', 'version', 'browser', 'steps', 'environment', 'artifacts', 'observed_signature')):
                    E(f'attempt {a["id"]} differs from its record in {f.name}')
    # safety
    target = r['metadata']['target']
    urls = [target['url']] if target.get('url') else []
    urls += [s['goto'] for a in r['attempts'] for s in a['steps'] if isinstance(s, dict) and 'goto' in s]
    if any(not is_loopback(u) for u in urls) and not target.get('authorised_by'):
        E('a non-loopback target with no authorised_by')
    if out.get('fixed_in_candidate') and out['value'] != 'reproduced':
        E('fixed_in_candidate needs a reproduction on the reported version')
    # honesty
    if out['value'] in ('not_reproduced', 'cannot_attempt', 'reproduced_differently'):
        for where, s in texts({'headline': r['headline'], 'outcome': out, 'limitations': r['limitations'], 'next_steps': r.get('next_steps', []),
                               'rating': r['rating'] or {}}):
            m = DISMISS.search(s)
            if m:
                E(f'{where}: "{m.group(0)}" - a bug that did not reproduce is unconfirmed, not dismissed')
        if re.search(r'\breproduced\b', r['headline'], re.I) and not re.search(r"\b(not|n't|never|differently)\b", r['headline'], re.I):
            E('the headline says it reproduced, but the outcome is ' + out['value'])
    return errs


def scan_secrets(doc) -> list[str]:
    errs = []
    for where, s in texts(doc):
        for rx, what in SECRETS:
            if rx.search(s):
                errs.append(f'{where}: looks like {what} - redact it')
        for m in CARD.finditer(s):
            digits = re.sub(r'\D', '', m.group(0))
            if luhn_ok(digits):
                errs.append(f'{where}: looks like a card number - redact it')
    return errs


def summary(reports: list[dict]) -> dict:
    by = {}
    for r in reports:
        by[r['outcome']['value']] = by.get(r['outcome']['value'], 0) + 1
    dups = sorted(f'{r["key"]} ~ {d["key"]}' for r in reports for d in r['duplicates'] if d['verdict'] == 'likely_duplicate')
    written = sum(1 for r in reports if (r.get('regression_test') or {}).get('status') == 'written')
    return {'by_outcome': dict(sorted(by.items())), 'likely_duplicates': dups, 'regression_tests_written': written}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--tickets', type=Path)
    ap.add_argument('--run', type=Path)
    ap.add_argument('--write-summary', action='store_true', help='recompute the batch summary and write it back')
    args = ap.parse_args(argv)
    doc = json.loads(args.report.read_text(encoding='utf-8'))
    errs = schema_check.errors(doc, SCHEMA)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    reports = doc['reports'] if 'reports' in doc else [doc]
    tickets = None
    if args.tickets:
        data = json.loads(args.tickets.read_text(encoding='utf-8'))
        tickets = {t['key']: t for t in data.get('tickets', [data])}
    base = args.report.resolve().parent
    errs = []
    for r in reports:
        errs += check_report(r, base, tickets, args.run)
    errs += scan_secrets(doc)
    if 'reports' in doc:
        s = summary(reports)
        if args.write_summary:
            doc['summary'] = s
            args.report.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
        elif doc['summary'] != s:
            errs.append(f'summary is {doc["summary"]}; the reports give {s}')
        if doc['batch']['reports_reviewed'] != len(reports):
            errs.append(f'batch.reports_reviewed is {doc["batch"]["reports_reviewed"]}; there are {len(reports)} reports')
        keys = [r['key'] for r in reports]
        if len(set(keys)) != len(keys):
            errs.append('a report key appears twice')
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    parts = ', '.join(f'{r["key"]} {r["outcome"]["value"]}' for r in reports)
    print(f'VALID: {len(reports)} report(s): {parts}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
