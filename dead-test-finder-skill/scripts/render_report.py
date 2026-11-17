#!/usr/bin/env python3
"""Render a dead test report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py dead_test_report.json [--color auto|always|never] [--brief]

--brief prints the headline, the ranked findings without their evidence details, the dismissals
grouped by reason (a count and the first ids of each group) and the limitations; it leaves out
the test list, the collection details and the tools.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
from term_safe import load_json  # noqa: E402  (report strings lose terminal control characters)

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
PRIO = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
TOOL = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}
LABEL = {'never_runs': 'never runs', 'cant_fail': "can't fail", 'mock_only': 'mock only', 'async_not_awaited': 'async not awaited',
         'swallowed_failure': 'swallowed failure', 'unreached_assertion': 'unreached assertion', 'stale_skip': 'stale skip',
         'duplicate': 'duplicate', 'overlap': 'overlap', 'weak': 'weak'}
GROUP_SHOWN = 5


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


def target_name(t) -> str:
    """A target as written: the function of `module.function`, or `file.py:function` whole (a file-form target);
    a bare string (an older report) as it is."""
    name = t.get('target', '?') if isinstance(t, dict) else str(t)
    if ':' in name and name.split(':', 1)[0].endswith('.py'):
        return name
    return name.split('.')[-1]


def evidence_line(e: dict) -> str | None:
    if e.get('neutralisation'):
        n = e['neutralisation']
        if n.get('outcome') == 'errored':
            return 'Neutralisation errored: ' + (n.get('error_reason') or 'the test did not run')
        parts = []
        for t in n.get('targets') or []:
            variants = t.get('variants') if isinstance(t, dict) else None
            note = t.get('note') if isinstance(t, dict) else None
            parts.append(f'{target_name(t)}: ' + (', '.join(f'{v.get("variant")} -> {v.get("test_status")}' for v in variants)
                                                   if variants else (note or 'not run')))
        return 'Neutralised: ' + ('; '.join(parts) if parts else 'nothing to neutralise')
    if e.get('collection'):
        return f'Collect output ({e["collection"].get("raw_output")}): not collected' if e['collection'].get('not_collected') else None
    if e.get('similarity'):
        s = e['similarity']
        if 'coverage_identical' not in s:
            return f'Similarity {100 * s["similarity"]:.0f}%, coverage not compared'
        return (f'Similarity {100 * s["similarity"]:.0f}%, coverage {"identical" if s["coverage_identical"] else "differs"}, '
                f'values {"identical" if s.get("values_identical") else "differ"}')
    if e.get('skip'):
        s = e['skip']
        t = s.get('ticket') or {}
        return f'Skip {s["verdict"]}' + (f': {t.get("key")} is {t.get("status")}' if t else '') + f'; age {s.get("age_source")}'
    return None


def dismissed_lines(dismissed: list[dict], brief: bool) -> list[str]:
    """Every dismissal on its own line, or (brief) one line per shared reason with the count and the first ids."""
    if not brief:
        return [f'- {x["candidate_id"]}' + (' (scanner error)' if x.get('scanner_error') else '') + f': {x["reason"]}'
                for x in dismissed]
    groups: dict[tuple, list[str]] = {}
    for x in dismissed:
        groups.setdefault((x['reason'], bool(x.get('scanner_error'))), []).append(x['candidate_id'])
    out = []
    for (reason, wrong), ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        shown = ', '.join(ids[:GROUP_SHOWN]) + (f' and {len(ids) - GROUP_SHOWN} more' if len(ids) > GROUP_SHOWN else '')
        out.append(f'- {len(ids)} x' + (' (scanner error)' if wrong else '') + f' {shown}: {reason}')
    return out


def render(d: dict, brief: bool = False, color: bool = False) -> str:
    P = lambda t, s: paint(t, s, color)  # noqa: E731
    md, s = d['metadata'], d['summary']
    out = [P(f'# Dead tests: {md["repo"]}', BOLD),
           f'{s["tests_defined"]} tests defined, {s["collected"]} collected, {s["neutralised"]} neutralised | {len(d["findings"])} '
           f'findings | evidence completeness {100 * s["evidence_completeness"]:.0f}%', '', P(s['headline'], BOLD), '',
           P('## Findings', BOLD)]
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    for f in sorted(d['findings'], key=lambda f: (order[f['priority']], -f['score'], f['id'])):
        out.append(f'- {P(f["priority"], PRIO[f["priority"]])} {f["id"]} [{LABEL[f["category"]]}, {f["confidence"]}'
                   + (', found by review' if f.get('source') == 'agent' else '') + f'] '
                   f'{f["location"]["file"]}:{f["location"]["line"]}')
        out.append(f'    {f["why"]}')
        p = f['proposal']
        proved = (p.get('proof') or {}).get('result')
        out.append(f'    Proposal: {p["action"]}' + (f' ({p["patch_path"]})' if p.get('patch_path') else '') +
                   (P(f', {proved}', GREEN if proved == 'proved' else YELLOW) if proved else '') +
                   (P(', applied', CYAN) if f['applied'] else ''))
        if not brief:
            ev = evidence_line(f['evidence'])
            if ev:
                out.append(P(f'    {ev}', DIM))
            for b in f.get('basis') or []:
                out.append(P('    Basis: ' + (f'{b["file"]}:{b["line"]}' if b.get('file') else b.get('evidence', '')), DIM))
            if f['evidence'].get('note'):
                out.append(P(f'    {f["evidence"]["note"]}', DIM))
            out.append(P(f'    {p["rationale"]}', DIM))
    if d['dismissed']:
        out += ['', P(f'## Dismissed ({len(d["dismissed"])})', BOLD)] + dismissed_lines(d['dismissed'], brief)
    if not brief:
        col = d['collection']
        out += ['', P('## Collection', BOLD), f'- {col["collected"]} of {col["defined"]} defined tests collected']
        out += [f'- not collected: `{t}`' for t in col['not_collected']]
        out += [f'- not assessed: `{t}`' for t in col['not_assessed']]
        out += ['', P('## Tools', BOLD)] + [f'- {t["name"]}: {P(t["status"], TOOL.get(t["status"], ""))}. {t["reason"]}' for t in d['tools']]
    # the limitations say what was not read, listed or neutralised: kept in --brief too
    out += ['', P('## Limitations', BOLD)] + [f'- {x}' for x in d['limitations']]
    return '\n'.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = load_json(args.json_file)
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr)
        return 2
    print(render(d, args.brief, supports_color(args.color)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
