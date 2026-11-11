#!/usr/bin/env python3
"""Render an i18n report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py report.json [--color auto|always|never] [--brief]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
SEV = {'Critical': RED + BOLD, 'High': RED, 'Medium': YELLOW, 'Low': CYAN}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    P = (lambda s, st: f'{st}{s}{RESET}') if supports_color(a.color) else (lambda s, st: s)
    sc, hl = d['scope'], d['headline']
    verdict = P('ISSUES FOUND', RED + BOLD) if hl['verdict'] == 'issues_found' else P('NONE DETECTED', GREEN + BOLD) + ' (for the locales, files and pages checked)'
    locs = ', '.join(f'{l["code"]} ({l["status"]}{": " + l["reason"] if l["reason"] else ""})' for l in sc['locales'])
    out = [P('# Internationalisation check', BOLD), '', verdict, '', hl['summary'], '',
           f'Locales: {locs}. Source {sc["source_locale"]}; RTL {", ".join(sc["rtl_locales"])}.',
           f'Key coverage: ' + ', '.join(f'{k} {round(100 * v)}%' for k, v in hl['key_coverage'].items()) +
           f'. Translation quality: {hl["translation_quality"]}. Evidence {hl["evidence_completeness"]}, confidence {hl["overall_confidence"]}.']
    if sc['pseudo']:
        out.append(f'Pseudo-locale: {sc["pseudo"]["expansion"]} expansion, by {sc["pseudo"]["method"].replace("_", " ")}.')
    if a.brief:
        out.append('')
        out += [f'{P(f["id"], BOLD)} {P(f["priority"] + " " + f["severity"], SEV[f["severity"]])} {f["kind"]} [{", ".join(f["locales"])}]: {f["title"]}'
                for f in d['findings']]
        print('\n'.join(out))
        return 0
    if d['findings']:
        out += ['', P('## Findings', BOLD)]
    for f in d['findings']:
        out += ['', P(f'### {f["id"]} {f["priority"]} {f["severity"]}: {f["title"]}', SEV[f['severity']]), '',
                f'{f["kind"]} ({f["status"]}), locales {", ".join(f["locales"])}; confidence {f["confidence"]}; reach {f["reach"]}, '
                f'leverage {f["leverage"]}, score {f["score"]}.']
        if f['keys']:
            out.append(f'- Keys: {", ".join(f["keys"])}')
        if f['locations']:
            out.append('- Code: ' + ', '.join(f'{l["file"]}:{l["line"]}' for l in f['locations']))
        for e in f['evidence'][:4]:
            out.append(f'- Seen: {e["page"]} in {e["locale"]} ({e["mode"]}), {e["element"]}: {json.dumps(e["measure"], ensure_ascii=False)[:120]} '
                       f'[{e["screenshot"]}]')
        out += [f'- Fix: {f["remediation"]}', f'- Verify: {f["verification_step"]}']
    if d['proposed_patch']:
        pp = d['proposed_patch']
        out += ['', P('## Proposed patch (not applied)', BOLD), '',
                f'{pp["path"]}: ' + '; '.join(f'{k}: {", ".join(v)}' for k, v in pp['keys_added'].items())]
    if d['needs_review']:
        out += ['', P('## Needs review', BOLD), ''] + [f'- {n["candidate_id"]}: {n["reason"]}' for n in d['needs_review']]
    dismissed = [t for t in d['triage'] if t['decision'] == 'dismissed']
    if dismissed:
        out += ['', P('## Dismissed', BOLD), ''] + [f'- {t["candidate_id"]}: {t["reason"]}' for t in dismissed]
    out += ['', P('## Tools', BOLD), ''] + [f'- {t["name"]}{" " + t["version"] if t.get("version") else ""}: {t["status"]}' +
                                            (f' ({t["note"]})' if t.get('note') else '') for t in d['metadata']['tools']]
    if d['limitations']:
        out += ['', P('## Limitations', BOLD), ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
