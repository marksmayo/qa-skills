#!/usr/bin/env python3
"""Render a visual review as a terminal/Markdown report with optional ANSI colour.

    python render_report.py review.json [--color auto|always|never] [--brief]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
IMPACT = {'high': RED + BOLD, 'medium': YELLOW, 'low': DIM}
INTENT = {'likely_unintended': 'likely unintended', 'unknown': 'intent unknown', 'likely_intended': 'likely intended'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def measure_text(g: dict) -> str:
    m = g['measure']
    if g['cause'] == 'layout_shift':
        parts = [f'{k} {v:+d} px' for k, v in m.items() if k in ('dx', 'dy', 'dw', 'dh') and v]
        return ', '.join(parts)
    if 'text_before' in m or 'text_after' in m:
        return f'"{m.get("text_before") or ""}" -> "{m.get("text_after") or ""}"'
    if 'colour_before' in m:
        return f'{m["property"]} {m["colour_before"]} -> {m["colour_after"]}'
    if 'aa_share' in m:
        return f'{int(m["aa_share"] * 100)}% anti-aliasing pixels'
    return ''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('review', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.review.read_text(encoding='utf-8'))
    P = (lambda s, st: f'{st}{s}{RESET}') if supports_color(a.color) else (lambda s, st: s)
    hl, sc = d['headline'], d['scope']
    impact = ', '.join(f'{n} {k}' for k, n in sorted(hl['groups_by_impact'].items(), key=lambda kv: ['high', 'medium', 'low'].index(kv[0])))
    out = [P('# Visual review', BOLD), '', hl['summary'], '',
           f'{len(d["groups"])} group(s) to review ({impact or "none"}); nothing is approved until you decide.',
           f'Compared {hl["pairs_compared"]} pair(s): pages {", ".join(hl["pages_compared"])} at {", ".join(sc["viewports"])} in '
           f'{", ".join(sc["browsers"])}. Flaky pixels excluded: {hl["flaky_pixels_excluded"]}. Evidence {hl["evidence_completeness"]}, '
           f'confidence {hl["overall_confidence"]}.']
    if hl['pages_untested']:
        out.append(P(f'Untested, added or removed: {", ".join(hl["pages_untested"])}', YELLOW))
    groups = sorted(d['groups'], key=lambda g: g['review_order'])
    if a.brief:
        out.append('')
        for g in groups:
            out.append(f'{g["review_order"]:>2}. {P(g["id"], BOLD)} {P(g["impact"], IMPACT[g["impact"]])} {g["cause"]} '
                       f'{", ".join(g["element_sigs"]) or "region"} on {", ".join(g["pages"])} ({INTENT[g["intent"]]})')
        print('\n'.join(out))
        return 0
    out += ['', P('## Groups, in review order', BOLD)]
    for g in groups:
        tag = ' (noise)' if g['noise'] else ''
        out += ['', P(f'### {g["review_order"]}. {g["id"]} {g["cause"]}{tag}: {g["impact"]} impact, {g["confidence"]} confidence', IMPACT[g['impact']]),
                '', f'{", ".join(g["element_sigs"]) or "region without DOM data"} on {", ".join(g["pages"])} at {", ".join(g["viewports"])}'
                    f'{" (" + measure_text(g) + ")" if measure_text(g) else ""}.',
                f'Pixels: {g["pixels"]} changed, {g["aa_pixels"]} anti-aliasing, {g["flaky_pixels"]} flaky. Reach {g["reach"]}, score {g["review_score"]}.',
                f'Intent: {INTENT[g["intent"]]}' + (f' ({g["intent_basis"].replace("_", " ")}, {g["intent_confidence"]} confidence)'
                                                    if g['intent'] != 'unknown' else '') + '.',
                '', g['explanation'], '',
                'Crops: ' + '; '.join(f'{c["baseline"]}, {c["current"]}, {c["diff"]} ({c["pair"]})' for c in g['crops'])]
    if d['unchanged']:
        out += ['', P('## Unchanged', BOLD), ''] + [f'- {u["pair"]}' + (f' ({u["flaky_pixels"]} flaky pixels excluded)' if u['flaky_pixels'] else '')
                                                   for u in d['unchanged']]
    for k in ('added', 'removed'):
        if d[k]:
            out += ['', P(f'## {k.capitalize()} (in one set only)', BOLD), ''] + [f'- {x}' for x in d[k]]
    if d['untested']:
        out += ['', P('## Untested', BOLD), ''] + [f'- {u["pair"]}: {u["reason"]}' for u in d['untested']]
    if d['files_written']:
        out += ['', P('## Baselines written (at your request)', BOLD), ''] + [f'- {f}' for f in d['files_written']]
    out += ['', P('## Tools', BOLD), ''] + [f'- {t["name"]}{" " + t["version"] if t["version"] else ""}: {t["status"]}' +
                                            (f' ({t["note"]})' if t['note'] else '') for t in d['metadata']['tools']]
    if d['limitations']:
        out += ['', P('## Limitations', BOLD), ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
