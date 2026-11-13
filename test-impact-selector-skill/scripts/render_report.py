#!/usr/bin/env python3
"""Render a test impact report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py impact_report.json [--color auto|always|never] [--brief]

--brief prints the headline, the commands (more than 20 test files shown as a count), the first 20
selected files, the blind spots and the recall line, without each
test's reasons or the evidence sources.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
SOURCE_STYLE = {'ran': GREEN, 'stale': YELLOW, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}
BRIEF_ITEMS = 20     # --brief lists at most this many selected files and test arguments per command
KIND_LABEL = {'global_config': 'global config', 'shared_fixture': 'shared fixture', 'dynamic_reference': 'dynamic reference',
              'data_or_schema': 'data or schema', 'unmapped_source': 'unmapped source', 'deleted_or_renamed': 'deleted or renamed',
              'non_code': 'non-code', 'huge_change': 'huge change'}
SCOPE_LABEL = {'full_suite': 'full suite', 'package': 'whole package', 'fixture_reach': "fixture's reach",
               'package_and_contract': 'package + contract tests', 'importers': 'former importers',
               'e2e_and_visual': 'e2e and visual tests', 'labelled': 'labelled only (not widened)'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
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
    md, s = d['metadata'], d['summary']
    ch = md['change']
    out = []
    title = ch.get('title') or ch.get('diff') or f'{ch.get("base")}...{ch.get("head")}'
    out.append(P(f'# Test impact: {md["repo"]}, {title}', BOLD))
    out.append(f'Mode {md["mode"]} | {s["selected"]} of {s["total_tests"]} test files selected | '
               f'{s["blind_spots"]} blind spot(s) | evidence completeness '
               + ('n/a' if s['evidence_completeness'] is None else f'{100 * s["evidence_completeness"]:.0f}%'))
    out.append('')
    out.append(P(s['headline'], BOLD))
    out.append('')

    if not args.brief:
        out.append(P('## Evidence sources', BOLD))
        for src in d['sources']:
            out.append(f'- {src["name"]}: {P(src["status"], SOURCE_STYLE.get(src["status"], ""))}. {src["reason"]}')
        out.append('')
        out.append(P('## Changed files', BOLD))
        for x in d['changes']:
            rng = ', '.join(f'{a}-{b}' if a != b else str(a) for a, b in x['line_ranges'][:6])
            out.append(f'- `{x["path"]}` ({x["status"]}, {x["kind"]})' + (f' lines {rng}' if rng else ''))
        out.append('')

    out.append(P('## Run', BOLD))
    if not d['command']['commands']:
        out.append('- Nothing to run' + (' (docs-only change).' if d.get('docs_only') else '.'))
    for b in d['command'].get('build_first', []):
        out.append(f'    (cd {b["cwd"]} && {" ".join(b["argv"])})    # build first: {b["why"]}')
    for cmd in d['command']['commands']:
        part = f' (part {cmd["part"]} of {cmd["parts"]})' if cmd.get('parts', 1) > 1 else ''
        argv = cmd['argv']
        # the trailing arguments: test files, or (go test) package directories, counted in `args`
        n_args = cmd.get('args', 0 if cmd.get('full_suite') else cmd.get('tests', 0))
        if args.brief and n_args > BRIEF_ITEMS and n_args < len(argv):
            what = 'packages' if cmd.get('framework') == 'go-testing' else 'test files'
            argv = argv[:len(argv) - n_args] + [f'<{n_args} {what}>']
        out.append(f'    (cd {cmd["cwd"]} && {" ".join(argv)}){part}')
    for pkg, why in d['command'].get('no_runner_reasons', {}).items():
        out.append(f'    # {pkg}: no command, {why}')
    out.append('')

    out.append(P(f'## Selected ({len(d["selected"])})', BOLD))
    shown = d['selected'][:BRIEF_ITEMS] if args.brief else d['selected']
    if len(shown) < len(d['selected']):
        out.append(f'(the first {len(shown)} in run order; the report lists all {len(d["selected"])})')
    for t in shown:
        tag = P('widened', YELLOW) if t['strength'] == 0 else f'strength {t["strength"]}'
        extra = ''.join([f', journey {t["journey_priority"]}' if t.get('journey_priority') else '',
                         f', failed in {t["past_failures"].replace("/", " of ")} past runs' if t.get('past_failures') and not t['past_failures'].startswith('0/') else ''])
        out.append(f'{t["order"]:>3}. `{t["file"]}` ({tag}{extra})')
        if not args.brief:
            for r in t['reasons']:
                out.append(P(f'       {r["source"]}: {r["evidence"]}', DIM))
    if d.get('not_selected_count'):
        out.append(f'Not selected by this evidence: {d["not_selected_count"]} test file(s).')
    out.append('')

    out.append(P(f'## Blind spots ({len(d["blind_spots"])})', BOLD))
    if not d['blind_spots']:
        out.append('- None: every changed file is linked to a selected test by evidence.')
    for b in d['blind_spots']:
        colour = YELLOW if b['action'] == 'labelled' else MAGENTA
        out.append(f'- {b["id"]} {P(KIND_LABEL[b["kind"]], colour)} `{b["path"]}` -> {SCOPE_LABEL[b["action"]]}'
                   + (f' ({len(b["widened_to"])} test files)' if b['widened_to'] else
                      f' (would add {len(b["would_widen_to"])} in strict mode)' if b['action'] == 'labelled' else ''))
        out.append(P(f'    {b["note"]}', DIM))
        if b.get('comment'):
            out.append(f'    {b["comment"]}')
    out.append('')

    out.append(P('## Recall', BOLD))
    r = d.get('replay')
    if s['recall_status'] == 'measured':
        out.append(f'- {s["recall_text"]}.')
        if r.get('mean_selected_share') is not None:
            out.append(f'- On average {100 * r["mean_selected_share"]:.0f}% of test files were selected per replayed change.')
        for m in r['misses']:
            out.append(f'- {P("Missed", RED)} `{m["test_id"]}` in {m["change"]} ({m["why_missed"].replace("_", " ")}): {m["explanation"]}')
            if m.get('fix'):
                out.append(f'    Fix: {m["fix"]}')
        if r.get('flaky_failures'):
            out.append(f'- {len(r["flaky_failures"])} failure(s) of known-flaky tests not counted.')
    else:
        out.append('- Not assessed: no history to replay.')
    v = d.get('verify')
    if v:
        outside = v['failures_outside_selection']
        out.append(f'- Verify run: {len(v["failures_inside_selection"])} failing file(s) inside the selection, '
                   + (P(f'{len(outside)} outside: {", ".join(outside)}', RED) if outside else '0 outside') + '.')
        if v.get('note'):
            out.append(P(f'    {v["note"]}', DIM))
    out.append('')

    out.append(P('## Recommendations', BOLD))
    out.append(f'- Full suite: {d["recommendations"]["full_suite"]}')
    for o in d['recommendations'].get('other', []):
        out.append(f'- {o}')
    if d.get('ci_snippet'):
        out.append(f'- CI snippet ({d["ci_snippet"]["format"]}) for you to add: `{d["ci_snippet"]["path"]}`')
    if not args.brief and d.get('limitations'):
        out.append('')
        out.append(P('## Limitations', BOLD))
        out += [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
