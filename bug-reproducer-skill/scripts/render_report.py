#!/usr/bin/env python3
"""Render a bug reproduction report (or a batch) as a terminal/Markdown report with optional ANSI colour.

    python render_report.py repro_report.json [--color auto|always|never] [--brief] [--comment KEY]

--brief prints one line per report. --comment KEY prints a plain comment for that ticket, to paste
into Jira or GitHub by hand; nothing is posted.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
OUTCOME = {'reproduced': ('REPRODUCED', RED + BOLD), 'reproduced_differently': ('REPRODUCED DIFFERENTLY', YELLOW + BOLD),
           'not_reproduced': ('NOT REPRODUCED', YELLOW), 'cannot_attempt': ('CANNOT ATTEMPT', DIM)}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def step_text(s) -> str:
    if not isinstance(s, dict):
        return str(s)
    (k, v), *rest = s.items()
    extra = ' '.join(f'{rk}={rv!r}' for rk, rv in rest)
    return f'{k} {v}' + (f' {extra}' if extra else '')


def env_text(a: dict) -> str:
    e = {k: v for k, v in (a.get('environment') or {}).items() if v}
    return ', '.join([a.get('browser') or a['channel']] + [f'{k} {v}' for k, v in e.items()])


def one_line(r: dict, P) -> str:
    o = r['outcome']
    label, style = OUTCOME[o['value']]
    rt = r['rating']
    pr = f'{rt["priority"]} {rt["severity"]}{" (as reported)" if rt["basis"] == "as_reported" else ""}' if rt else 'unrated'
    frac = f' {o["matched_attempts"]}/{o["total_attempts"]}' if o['total_attempts'] else ''
    dup = [d['key'] for d in r['duplicates'] if d['verdict'] == 'likely_duplicate']
    return f'{P(r["key"], BOLD)}  {P(label + frac, style)}  {pr}' + (f'  dup? {", ".join(dup)}' if dup else '')


def report(r: dict, P) -> list[str]:
    m, o, t = r['metadata'], r['outcome'], r['triage']
    label, style = OUTCOME[o['value']]
    out = [P(f'## {r["key"]}: {m["report"].get("summary", "")}', BOLD), '', P(label, style) +
           (f' ({o["confidence"]} confidence)' if o['confidence'] != 'n/a' else ''), '', r['headline'], '']
    tgt = m['target']
    out.append(f'Version stated: {tgt["version_stated"] or "none"}; observed: {", ".join(tgt["versions_observed"]) or "none"}. '
               f'Target: {tgt["url"] or "none"}' + (f' (authorised by {tgt["authorised_by"]})' if tgt.get('authorised_by') else ''))
    if t['fields_missing']:
        out += ['', P('Missing from the report', BOLD) + f': {", ".join(t["fields_missing"])}']
        out += [f'- {q}' for q in t['questions']]
    if t['suspicious_instructions_ignored'] or r['skipped_steps']:
        out += ['', P('Not followed', YELLOW + BOLD)]
        out += [f'- instruction ignored: "{s}"' for s in t['suspicious_instructions_ignored']]
        out += [f'- step skipped: "{s["text"]}" ({s["reason"]})' + (f' Instead: {s["instead"]}' if s.get('instead') else '') for s in r['skipped_steps']]
    if r['attempts']:
        out += ['', P('Attempts', BOLD), '', '| Candidate | Runs | Matched | Environment | Version |', '|---|---|---|---|---|']
        cands: dict[str, list] = {}
        for a in r['attempts']:
            cands.setdefault(a['candidate'], []).append(a)
        for c, xs in cands.items():
            n = sum(a['result'] == 'matched' for a in xs)
            other = sorted({a['result'] for a in xs} - {'matched'})
            lab = xs[0].get('label') or c
            out.append(f'| {c}: {lab} | {len(xs)} | {n}' + (f' ({", ".join(other)} otherwise)' if other else '') +
                       f' | {env_text(xs[0])} | {", ".join(sorted({a["version"] or "?" for a in xs}))} |')
        ex = next((a for a in r['attempts'] if a['result'] == 'matched'), r['attempts'][0])
        out += ['', f'Steps ({ex["candidate"]}): ' + ' -> '.join(step_text(s) for s in ex['steps'])]
        if ex.get('observed_signature'):
            out.append(f'Observed: {ex["observed_signature"]}')
        assumptions = sorted({x for a in r['attempts'] for x in a['assumptions']})
        if assumptions:
            out += ['', P('Assumptions', BOLD)] + [f'- {x}' for x in assumptions]
        out += ['', f'Evidence: {", ".join(ex["artifacts"])}']
    mr = r.get('minimal_repro')
    if mr:
        out += ['', P('Minimal reproduction', BOLD) + f' ({len(mr["steps"])} steps, confirmed {mr["confirmed"]}, {mr.get("trials", "?")} ddmin trials): '
                + ' -> '.join(step_text(s) for s in mr['steps']),
                f'Removed: {", ".join(step_text(s["step"]) for s in mr["removed_steps"]) or "nothing"}. Re-run: {mr["script_path"]}']
    if r['duplicates']:
        out += ['', P('Possible duplicates', BOLD) + f' (searched {r.get("duplicates_searched", "?")}; nothing closed or linked)']
        out += [f'- {d["key"]} {d["similarity"]:.2f} {d["verdict"].replace("_", " ")}: {d.get("summary") or ""}' for d in r['duplicates']]
    if r['localisation']:
        out += ['', P('Where it probably is', BOLD)]
        for loc in r['localisation']:
            out.append(f'- {loc["file"]}' + (f':{loc["line"]}' if loc.get('line') else '') + f' ({loc["confidence"]}): {loc["evidence"]}')
            if loc.get('code'):
                out.append(f'  `{loc["code"]}`')
    rt = r['rating']
    if rt:
        out += ['', P('Rating', BOLD) + f': {rt["severity"]} x reach {rt["reach"]} x frequency {rt["frequency"]} = {rt["score"]} -> '
                + P(rt['priority'], BOLD) + f' (basis {rt["basis"].replace("_", " ")}, {rt["confidence"]} confidence'
                + (f'; reporter said {rt["reporter_label"]}' if rt.get('reporter_label') else '') + ')',
                f'- Severity: {rt["severity_reason"]}', f'- Reach: {rt["reach_reason"]}']
    rg = r.get('regression_test')
    if rg:
        out += ['', P('Regression test', BOLD) + f': {rg["status"].replace("_", " ")}']
        if rg['status'] == 'written':
            red = rg['red']
            out.append(f'- {rg["path"]} ({rg["framework"]}): red by {red["status"].replace("_", " ")} in run {red["run_id"]}: "{red["failure_excerpt"]}"')
            if rg.get('fix_check'):
                out.append(f'- {rg["fix_check"]}')
        elif rg.get('reason'):
            out.append(f'- {rg["reason"]}')
    for title, items in (('Files written', r['files_written']), ('Limitations', r['limitations']), ('Next steps', r.get('next_steps', []))):
        if items:
            out += ['', P(title, BOLD)] + [f'- {x}' for x in items]
    return out + ['']


def comment(r: dict) -> str:
    o, rt = r['outcome'], r['rating']
    lines = [r['headline'], '']
    if r['triage']['questions'] and o['value'] in ('cannot_attempt', 'not_reproduced'):
        lines += ['Could you tell us:'] + [f'- {q}' for q in r['triage']['questions']] + ['']
    mr = r.get('minimal_repro')
    if mr:
        lines += ['Minimal steps: ' + ' -> '.join(step_text(s) for s in mr['steps']), '']
    if rt:
        lines.append(f'Suggested rating: {rt["severity"]}, {rt["priority"]} ({rt["basis"].replace("_", " ")}).')
    dup = [d['key'] for d in r['duplicates'] if d['verdict'] == 'likely_duplicate']
    if dup:
        lines.append(f'Possibly the same as {", ".join(dup)}.')
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--comment', metavar='KEY')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read file: {exc}", file=sys.stderr)
        return 2
    reports = d['reports'] if 'reports' in d else [d]
    c = supports_color(args.color)
    P = lambda t, s: f'{s}{t}{RESET}' if c and s else t  # noqa: E731
    if args.comment:
        r = next((x for x in reports if x['key'] == args.comment), None)
        if r is None:
            print(f'{args.comment} is not in this report', file=sys.stderr)
            return 2
        print(comment(r))
        return 0
    out = []
    if 'reports' in d:
        s = d['summary']
        out += [P('# Bug reproduction', BOLD), '',
                f'{len(reports)} report(s): ' + ', '.join(f'{v} {k.replace("_", " ")}' for k, v in s['by_outcome'].items()) +
                f'; {s["regression_tests_written"]} regression test(s) written.', '']
    out += [one_line(r, P) for r in reports]
    if not args.brief:
        out.append('')
        for r in reports:
            out += report(r, P)
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
