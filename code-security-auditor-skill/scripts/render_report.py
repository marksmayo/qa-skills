#!/usr/bin/env python3
"""Render a code security audit as a terminal/Markdown report with optional ANSI colour, and
optionally export the findings as SARIF for code-scanning dashboards.

    python render_report.py security_audit.json [--color auto|always|never] [--brief] [--sarif findings.sarif]

--brief prints the summary, the ranked table and dependencies without per-finding detail,
collapses tools that did not apply into one line, and counts dismissed candidates by reason
instead of listing each one. Findings found by review (source: agent) are labelled "found by
review". With --sarif only the SARIF file is written (and a one-line note printed); the report
itself is not printed.
The SARIF export gives each rule its CWE tags and a security-severity (the CVSS v4.0 score), which
GitHub code scanning uses to rank alerts. It contains titles, locations and fixes, not exploit
steps, so it is safer to upload than the report itself.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sarif  # noqa: E402

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO_STYLE = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
SEV_STYLE = {'critical': RED + BOLD, 'high': RED, 'medium': YELLOW, 'low': DIM}
CONF_STYLE = {'confirmed': GREEN, 'traced': CYAN, 'possible': DIM}
TOOL_STYLE = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}
REACH_STYLE = {'yes': RED, 'conditional': YELLOW, 'no': DIM, 'unknown': MAGENTA}
EXPOSURE_LABEL = {'public': 'public', 'authenticated': 'authenticated', 'restricted': 'restricted',
                  'local': 'local (attacker-supplied input: file/arg/stdin/env)'}

def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ

def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text

def primary(f: dict) -> dict:
    return next((l for l in f['locations'] if l['role'] in ('sink', 'config')), f['locations'][0])

def reason_group(reason: str) -> str:
    """A short key for grouping dismissals: the reason up to its first clause, without file:line references.

    A reason that opens with a file reference is grouped on what follows it, and a colon only ends the clause when
    at least three words come before it ("secure: false is a form-field option" stays whole)."""
    text = re.sub(r'[\w./-]+\.\w+(:\d+)?', '<file>', reason)
    text = re.sub(r'^(?:<file>[\s,:;-]*)+', '', text)
    head = re.split(r'\.\s|;|\(| - ', text, maxsplit=1)[0]
    before, colon, _ = head.partition(': ')
    if colon and len(before.split()) >= 3:
        head = before
    return ' '.join(head.split())[:90] or reason[:90]

def to_sarif(d: dict) -> dict:
    results = []
    for f in d['findings']:
        l = primary(f)
        level = 'error' if f['priority'] in ('P0', 'P1') else 'warning' if f['priority'] == 'P2' else 'note'
        results.append({
            'rule_id': f['cwe'][0], 'rule_name': f['title'], 'level': level, 'file': l['file'], 'line': l['line'],
            'message': f"[{f['id']} {f['priority']} {f['cvss']['severity']} {f['cvss']['score']} {f['confidence']}] {f['title']}",
            'help': f"{f['fix']['change']} Safer API: {f['fix']['safer_api']}",
            'properties': {'finding_id': f['id'], 'priority': f['priority'], 'confidence': f['confidence'], 'cvss_vector': f['cvss']['vector'],
                           'exposure': f['exposure'], 'cwe': f['cwe'], 'owasp': f['owasp']},
            'rule_properties': {'tags': ['security'] + [c.lower().replace('cwe-', 'external/cwe/cwe-') for c in f['cwe']],
                                'security-severity': str(f['cvss']['score'])},
        })
    return sarif.build('code-security-auditor', d['skill'].get('version'), results)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true', help='summary and tables only')
    ap.add_argument('--sarif', type=Path, help='also write the findings as SARIF 2.1.0')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr)
        return 2
    c = supports_color(args.color)
    P = lambda t, s: paint(t, s, c)  # noqa: E731
    s, cov, ver = d['summary'], d['coverage'], d['verification']
    out = []
    w = out.append
    w(P('# Code security audit', BOLD))
    w(P(f"> {d['notice']}", RED))
    w('')
    w(f"Repository: {d['target']['repository']}" + (f" @ {d['target']['commit']}" if d['target'].get('commit') else '') +
      f"  |  generated {d['generated_at']}  |  {d['skill']['name']} {d['skill']['version']}")
    w(f"Scope: {', '.join(d['target']['scope'])}" + (f"  |  excluded: {', '.join(d['target']['excluded'])}" if d['target']['excluded'] else ''))
    w('')
    w(P(s['headline'], BOLD))
    cnt = s['counts']
    w('Priority: ' + '  '.join(P(f"{k} {v}", PRIO_STYLE[k]) for k, v in cnt['by_priority'].items()) +
      '   Confidence: ' + '  '.join(P(f"{k} {v}", CONF_STYLE[k]) for k, v in cnt['by_confidence'].items()))
    w(f"Candidates triaged: {cnt['candidates']} ({cnt['dismissed']} dismissed)  |  reachable vulnerable dependencies: "
      f"{cnt['dependencies_reachable']} of {len(d['dependencies'])}  |  observations: {cnt['observations']}")
    vt = f"{ver['target_kind']} {ver['target']}" if ver['permitted'] else 'not permitted'
    w(f"Verification: {vt}")
    w('')
    w(P('## Tools', BOLD))
    na = [t for t in d['tools'] if t['status'] == 'not_applicable'] if args.brief else []
    for t in d['tools']:
        if t in na:
            continue
        extra = t.get('version') or ''
        why = f" - {t['reason']}" if t.get('reason') else ''
        w(f"- {t['name']} {extra} [{P(t['status'], TOOL_STYLE[t['status']])}]{why}")
    if na:
        w(P(f"- not applicable: {', '.join(t['name'] for t in na)}", DIM))
    w('')
    w(P('## Findings (ranked)', BOLD))
    w('| # | ID | Priority | Severity | Confidence | Exposure | CWE | Finding | Location |')
    w('|---|---|---|---|---|---|---|---|---|')
    for f in sorted(d['findings'], key=lambda f: f['rank']):
        l = primary(f)
        w(f"| {f['rank']} | {f['id']} | {P(f['priority'], PRIO_STYLE[f['priority']])} | "
          f"{P(f['cvss']['severity'] + ' ' + str(f['cvss']['score']), SEV_STYLE[f['cvss']['severity']])} | "
          f"{P(f['confidence'], CONF_STYLE[f['confidence']])} | {f['exposure']} | {', '.join(f['cwe'])} | {f['title']}"
          f"{' (found by review)' if f.get('source') == 'agent' else ''} | {l['file']}:{l['line']} |")
    if not args.brief:
        for f in sorted(d['findings'], key=lambda f: f['rank']):
            w('')
            w(P(f"### {f['id']} [{f['priority']}] {f['title']}", BOLD))
            w(f"{', '.join(f['cwe'])} | {f['owasp']} | {f['category']} | sources: {', '.join(f['sources'])}"
              + (' | found by review' if f.get('source') == 'agent' else ''))
            for b in f.get('basis') or []:
                w(f"  basis: {b['evidence']}" if b.get('evidence') else f"  basis: {b['file']}:{b['line']}  `{b['excerpt']}`")
            w(f"CVSS {f['cvss']['score']} {f['cvss']['severity']}: {f['cvss']['vector']}" + (f" - {f['cvss']['rationale']}" if f['cvss'].get('rationale') else ''))
            w(f"Confidence {P(f['confidence'], CONF_STYLE[f['confidence']])}, exposure {EXPOSURE_LABEL.get(f['exposure'], f['exposure'])}" +
              (f" ({f['exposure_note']})" if f.get('exposure_note') else '') + f", priority score {f['priority_score']}" +
              (f"; business impact {f['business_impact']['kind']}: {f['business_impact']['basis']}"
               if f.get('business_impact') and f['business_impact']['kind'] != 'none' else ''))
            w(f"Path: {f['path']['summary']}")
            if f['path'].get('gap'):
                w(P(f"Gap: {f['path']['gap']}", YELLOW))
            for l in f['locations']:
                w(f"  - {l['role']:<13} {l['file']}:{l['line']}  `{l['snippet']}`" + (f"  ({l['note']})" if l.get('note') else ''))
            w(f"Checked: {'; '.join(f['path']['sanitisers_checked'])}")
            r = f['repro']
            if r['preconditions']:
                w('Preconditions: ' + ' '.join(r['preconditions']))
            w('Steps:')
            for i, step in enumerate(r['steps'], 1):
                w(f"  {i}. {step}")
            w(f"Safe payload: {r['safe_payload']['payload']}  ->  {r['safe_payload']['proof']}")
            w(f"Expected (secure): {r['expected_secure']}")
            w(f"Actual (vulnerable): {r['actual_vulnerable']}")
            poc = f['poc']
            if poc['result'] == 'confirmed':
                fixed = f", against the fixed build: {poc['fixed_build_result']}" if poc.get('fixed_build_result') else ''
                w(P(f"PoC: confirmed ({poc['file']}, evidence {poc['evidence']}{fixed})", GREEN))
            else:
                w(f"PoC: {poc['result']} - {poc.get('reason')}")
            w(f"Impact: {f['impact']}")
            w(f"Fix: {f['fix']['change']} Safer API: {f['fix']['safer_api']}")
            rt = f['regression_test']
            status = {'fails_on_vulnerable_code': 'fails on the vulnerable code' + (' and passes after the fix' if rt.get('passes_after_fix') else ''),
                      'not_run': 'not run', 'passes_on_vulnerable_code': 'PASSES on the vulnerable code'}[rt['result']]
            w(f"Regression test ({rt['framework']}): {status}" + (f" - {rt['file']}::{rt['name']}" if rt.get('name') else '') +
              (f". {rt['note']}" if rt.get('note') and rt['result'] == 'not_run' else ''))
            if f.get('secret'):
                sc = f['secret']
                basis = {'path': ' (at this path)', 'full': ' (all scanned commits)'}.get(sc.get('history_basis'), '')
                status = f", file {sc['git_status']} in git" if sc.get('git_status') else ''
                w(f"Secret: {sc['type']}, {sc['fingerprint']}{status}, in git history: {sc['in_git_history']}{basis}, "
                  f"rotate: {'yes' if sc['rotate'] else 'no'}")
            if f.get('dependency'):
                dep = f['dependency']
                w(f"Dependency: {dep['package']} {dep['version']} -> {', '.join(dep['fixed_in'])} ({', '.join(dep['ids'])})")
            links = [f"chains with {', '.join(f['chains_with'])}"] if f.get('chains_with') else []
            links += [f"variant of {f['variant_of']}"] if f.get('variant_of') else []
            links += [f"variants: {', '.join(f['variants'])}"] if f.get('variants') else []
            if links:
                w('Related: ' + '; '.join(links))
    w('')
    w(P('## Dependencies', BOLD))
    adv = d.get('dependency_advisories')
    if adv:
        style = GREEN if adv['status'] == 'assessed' else YELLOW if adv['status'] == 'partial' else RED
        w(P(f"Advisories {adv['status'].replace('_', ' ')}", style) + (f" by {', '.join(adv['tools'])}" if adv['tools'] else '') +
          (f" ({adv['packages']} pinned package(s) in {len(adv.get('lockfiles') or [])} lockfile(s))" if adv.get('packages') is not None else '') +
          (f": {adv['reason']}" if adv.get('reason') else ''))
    by = {}
    for x in d['dependencies']:
        by.setdefault(x['reachable'], []).append(x)
    for reach in ('yes', 'conditional', 'unknown'):
        for x in by.get(reach, []):
            ali = next((a for a in x.get('aliases', []) if a.startswith('CVE-')), x['advisory'])
            w(f"- {P(reach, REACH_STYLE[reach])} {x['package']} {x['version']} {ali} (fixed in {', '.join(x['fixed_in']) or '?'})"
              f"{' -> ' + x['finding'] if x.get('finding') else ''}: {x['basis']}")
    if by.get('no'):
        pk = sorted({f"{x['package']} {x['version']}" for x in by['no']})
        w(P(f"- not reachable: {len(by['no'])} advisories in {', '.join(pk)} (each with its basis in the JSON)", DIM))
    if d['variant_rules']:
        w('')
        w(P('## Variant rules (CI guards)', BOLD))
        for r in d['variant_rules']:
            w(f"- {r['id']} {r['file']} for {', '.join(r['for_findings'])}: matches {', '.join(r['matches']) or 'nothing'}" +
              (f" - {r['test_note']}" if r.get('test_note') else ''))
    if d['observations']:
        w('')
        w(P('## Observations', BOLD))
        for o in d['observations']:
            w(f"- {o['id']} {o['title']} ({o['file']}:{o['line']}{', ' + ', '.join(o['cwe']) if o.get('cwe') else ''}): {o['note']}")
    dismissed = [t for t in d['triage'] if t['decision'] == 'dismissed']
    if dismissed:
        w('')
        w(P('## Dismissed candidates', BOLD))
        if args.brief:
            groups = Counter(reason_group(t['reason']) for t in dismissed)
            for reason, n in groups.most_common(12):
                ids = [t['candidate'] for t in dismissed if reason_group(t['reason']) == reason]
                w(f"- {n} x {reason}" + (f" ({', '.join(ids[:4])}{', ...' if len(ids) > 4 else ''})"))
            if len(groups) > 12:
                rest = sum(n for _, n in groups.most_common()[12:])
                w(P(f"- {rest} more in {len(groups) - 12} other reason(s); each reason is in the JSON", DIM))
        else:
            for t in dismissed:
                w(f"- {t['candidate']}: {t['reason']}")
    w('')
    w(P('## Coverage', BOLD))
    w(f"Languages: {', '.join(cov['languages_reviewed'])}  |  files read {cov['files_read']}/{cov['files_in_scope']}  |  "
      f"entry points reviewed {cov['entry_points_reviewed']}/{cov['entry_points_total']}")
    w(f"Deep review: {', '.join(cov['areas_deep_reviewed'])}")
    for n in cov['not_reviewed']:
        w(P(f"- Not reviewed: {n['area']} - {n['reason']}", YELLOW))
    w('')
    w(P('## Limitations', BOLD))
    for l in d['limitations']:
        w(f"- {l}")
    if args.sarif:
        args.sarif.write_text(json.dumps(to_sarif(d), indent=2) + '\n', encoding='utf-8')
        print(f"SARIF ({len(d['findings'])} result(s)) -> {args.sarif}")
        return 0
    print('\n'.join(out))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
