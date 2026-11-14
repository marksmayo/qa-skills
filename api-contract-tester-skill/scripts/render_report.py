#!/usr/bin/env python3
"""Render an API contract test report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py report.json [--color auto|always|never] [--brief] [--sarif findings.sarif]

--brief prints the headline and one line per finding. --sarif also writes the findings as SARIF 2.1.0,
located at the operation's line in the contract file, for code-scanning dashboards.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sarif  # noqa: E402

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
SEV = {'Critical': RED + BOLD, 'High': RED, 'Medium': YELLOW, 'Low': CYAN}
CHECK_STYLE = {'failed': RED, 'cantTell': YELLOW, 'passed': GREEN, 'not_applicable': DIM, 'untested': DIM}
CHECK_ABBR = {'status_documented': 'status', 'server_error': '5xx', 'content_type': 'ctype', 'schema_conformance': 'schema',
              'required_headers': 'hdrs', 'negative_accepted': 'neg', 'positive_rejected': 'pos', 'auth_enforced': 'auth',
              'graphql_errors': 'gql'}
MARK = {'failed': 'FAIL', 'cantTell': '?', 'passed': 'ok', 'not_applicable': '-', 'untested': '.'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def to_sarif(d: dict, contract_lines: dict) -> dict:
    results = []
    src = d['scope']['contract']['source']
    for f in d['findings']:
        level = 'error' if f['priority'] in ('P0', 'P1') else 'warning' if f['priority'] == 'P2' else 'note'
        results.append({
            'rule_id': f['check'], 'rule_name': f['check'].replace('_', ' '), 'level': level,
            'file': src, 'line': contract_lines.get(f['operation_ids'][0]) or 1,
            'message': f"[{f['id']} {f['priority']} {f['severity']} {f['confidence']}] {f['title']} ({', '.join(f['operation_ids'])})",
            'help': f"{f['remediation']} Verify: {f['verification_step']}",
            'properties': {'finding_id': f['id'], 'priority': f['priority'], 'confidence': f['confidence'],
                           'repeatable': f['repeatable'], 'fix_direction': f['fix_direction'],
                           'exchanges': [e['path'] for e in f['exchanges']]},
            'rule_properties': {'tags': ['api-contract', 'security'] if f['security_relevant'] else ['api-contract']},
        })
    return sarif.build('api-contract-tester', d['skill'].get('version'), results)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--sarif', type=Path)
    ap.add_argument('--contract', type=Path, help='contract.json, to place SARIF results on operation lines')
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    color = supports_color(a.color)
    P = (lambda s, st: f'{st}{s}{RESET}') if color else (lambda s, st: s)
    sc, md, hl = d['scope'], d['metadata'], d['headline']
    c = sc['contract']
    cov = hl['operation_coverage']
    verdict = P('VIOLATIONS FOUND', RED + BOLD) if hl['verdict'] == 'violations_found' else P('CONFORMANCE NOT ESTABLISHED', YELLOW + BOLD)
    sev = ', '.join(f'{n} {k}' for k, n in hl['severity_counts'].items() if n) or 'no findings'
    kind = f'{c["kind"]} {c["version"]}' if c.get('version') else c['kind']
    name = c.get('title') or c['source']
    version = f' {c["api_version"]}' if c.get('api_version') else ''
    out = [P(f'# API contract test: {name}{version} ({kind})', BOLD), '',
           f'{verdict}  {sev}' + (f'; {hl["flaky_findings"]} flaky' if hl['flaky_findings'] else ''), '',
           hl['summary'], '',
           f'Operations: {cov["exercised"]}/{cov["total"]} exercised' +
           ''.join(f', {cov[k]} {k.replace("_", " ")}' for k in ('excluded', 'skipped_write', 'skipped_auth', 'untested') if cov[k]) +
           f'. Evidence {hl["evidence_completeness"]}, confidence {hl["overall_confidence"]}.']
    if c.get('provenance') in ('generated', 'derived'):
        out.append(P(f'Contract {c["provenance"]} from the code: the run checks the code against itself, so most checks '
                     'could not fail.', YELLOW))
    run_status = d.get('run_status', 'complete')
    if run_status == 'not_run':
        out.append(P('Not run: no request was sent; every check is untested.', YELLOW + BOLD) +
                   (f' {d["run_status_reason"]}' if d.get('run_status_reason') else ''))
    else:
        out.append(f'{md["requests_sent"]} requests to {sc["base_url"]} ({md["run_status"]}); seed {sc["seed"]}; generator {md["generator"]}; '
                   f'writes {"allowed" if sc["writes_allowed"] else "not allowed"}' +
                   (f' (authorised by {sc["authorised_by"]})' if sc['authorised_by'] else '') + '.')
        if run_status == 'partial':
            out.append(P(f'Run partial: {d.get("run_status_reason") or "see limitations"}', YELLOW))
    for x in md.get('aborted_attempts') or []:
        out.append(f'Earlier attempt: {x["requests_sent"]} request(s), {x["reason"]}' + (f' ({x["record"]})' if x['record'] else '') + '.')
    if md['rate_limited']['events']:
        out.append(f'Rate limited {md["rate_limited"]["events"]} time(s), {md["rate_limited"]["backoff_seconds"]} s of backoff.')
    bc = hl['breaking_change_counts']
    if sc['previous_contract']:
        out.append(f'Against {sc["previous_contract"]["api_version"]}: {bc["breaking"]} breaking, {bc["potentially_breaking"]} potentially '
                   f'breaking, {bc["compatible"]} compatible change(s).')
    hc = d.get('handler_check')
    if hc:
        counts = ', '.join(f'{n} {k.replace("_", " ")}' for k, n in hc['counts'].items() if n) or 'no differences'
        out.append(f'Handlers ({", ".join(hc["frameworks"]) or "none recognised"}): {hc["handlers_found"]} found; {counts}.')
    if a.brief:
        out.append('')
        for f in d['findings']:
            out.append(f'{P(f["id"], BOLD)} {P(f["priority"], SEV[f["severity"]])} {P(f["severity"], SEV[f["severity"]])} '
                       f'{f["check"]} {", ".join(f["operation_ids"])}: {f["title"]}' + ('' if f['repeatable'] else P(' (flaky)', YELLOW)))
        print('\n'.join(out))
        return write_sarif(a, d)
    out += ['', P('## Operations', BOLD), '',
            '| Operation | Status | Cases | ' + ' | '.join(CHECK_ABBR.values()) + ' |',
            '|---|---|---|' + '---|' * len(CHECK_ABBR)]
    for o in d['operations']:
        cells = [P(MARK[o['checks'][k]], CHECK_STYLE[o['checks'][k]]) for k in CHECK_ABBR]
        out.append(f'| {o["method"]} {o["path"]} ({o["operation_id"]}) | {o["status"]} | {o["cases_sent"]} | ' + ' | '.join(cells) + ' |')
    out += ['', 'ok passed, FAIL failed, ? could not be evaluated (cantTell), - not applicable, . untested.']
    if d['findings']:
        out += ['', P('## Findings', BOLD)]
    for f in d['findings']:
        out += ['', P(f'### {f["id"]} {f["priority"]} {f["severity"]}: {f["title"]}', SEV[f['severity']]), '',
                f'{f["check"]} on {", ".join(f["operation_ids"])}; confidence {f["confidence"]}' +
                ('' if f['repeatable'] else ' (did not repeat on a second send: flaky)') +
                f'; reach {f["reach"]}, score {f["score"]}' + ('; security relevant' if f['security_relevant'] else '') + '.', '',
                f'- Expected: {f["expected"]}', f'- Actual: {f["actual"]}',
                f'- Fix: {f["fix_direction"].replace("_", " ")} ({f["fix_direction_basis"].replace("_", " ")}). {f["remediation"]}']
        if f['handler']:
            out.append(f'- Handler: {f["handler"]["file"]}:{f["handler"]["line"]}')
        out.append(f'- Verify: {f["verification_step"]}')
        out.append('- Evidence: ' + ', '.join(f'{e["path"]} ({e["status"]})' for e in f['exchanges']))
        out += ['', '```python', f['regression_test_sketch'], '```']
    if d['needs_review']:
        out += ['', P('## Needs review', BOLD), ''] + [f'- {n["candidate_id"]}: {n["reason"]}' for n in d['needs_review']]
    dismissed = [t for t in d['triage'] if t['decision'] == 'dismissed']
    if dismissed:
        out += ['', P('## Dismissed candidates', BOLD), ''] + [f'- {t["candidate_id"]}: {t["reason"]}' for t in dismissed]
    if hc and hc['issues']:
        out += ['', P('## Contract vs handlers', BOLD), '',
                'From reading the route handlers (handler_map.py); leads to review, not run results.', '']
        shown = hc['issues'] if len(hc['issues']) <= 60 else hc['issues'][:60]
        out += [f'- {i["id"]} {i["kind"].replace("_", " ")}: {i["message"]}' for i in shown]
        if len(hc['issues']) > len(shown):
            out.append(f'- and {len(hc["issues"]) - len(shown)} more (see the report JSON)')
    if d['contract_issues']:
        out += ['', P('## Contract issues', BOLD), '', 'Problems in the contract itself, not the API.', '']
        for i in d['contract_issues']:
            if i.get('source') == 'agent':
                b = i['basis']
                out.append(f'- {i["id"]} {i["kind"]} at {i["location"]} (found by review, {b["file"]}:{b["line"]}): {i["message"]}')
            else:
                out.append(f'- {i["id"]} {i["kind"]} at {i["location"]}: {i["message"]}')
    rated = [b for b in d['breaking_changes'] if b['rating'] != 'compatible']
    if rated:
        out += ['', P('## Breaking changes', BOLD), '']
        out += [f'- {b["id"]} {P(b["rating"].replace("_", " "), RED if b["rating"] == "breaking" else YELLOW)}'
                f'{" (" + b["severity"] + ")" if b["severity"] else ""} {b["location"]}: {b["detail"]} [{", ".join(b["operation_ids"])}]'
                for b in rated]
        n = len(d['breaking_changes']) - len(rated)
        if n:
            out.append(f'- and {n} compatible change(s)')
    out += ['', P('## Tools', BOLD), ''] + [f'- {t["name"]}{" " + t["version"] if t["version"] else ""}: {t["status"]}' + (f' ({t["note"]})' if t['note'] else '')
                                            for t in md['tools']]
    if d['limitations']:
        out += ['', P('## Limitations', BOLD), ''] + [f'- {x}' for x in d['limitations']]
    if d['next_steps']:
        out += ['', P('## Next steps', BOLD), ''] + [f'- {x}' for x in d['next_steps']]
    print('\n'.join(out))
    return write_sarif(a, d)


def write_sarif(a, d) -> int:
    if a.sarif:
        lines = {}
        if a.contract:
            lines = {o['operation_id']: o.get('line') for o in json.loads(a.contract.read_text(encoding='utf-8'))['operations']}
        a.sarif.write_text(json.dumps(to_sarif(d, lines), indent=2) + '\n', encoding='utf-8')
        print(f"\nSARIF ({len(d['findings'])} result(s)) -> {a.sarif}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
