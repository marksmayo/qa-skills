#!/usr/bin/env python3
"""Render a privacy audit as a terminal or Markdown report.

    python render_report.py audit.json [--brief] [--color auto|always|never]

--brief prints the headline, the consent matrix and the findings table only.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

COL = {'P0': '31;1', 'P1': '31', 'P2': '33', 'P3': '36', 'dim': '2', 'bold': '1'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('audit', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))

    def c(text, key):
        return f'\033[{COL[key]}m{text}\033[0m' if use else str(text)

    r = json.loads(a.audit.read_text(encoding='utf-8'))
    s, h = r['scope'], r['headline']
    out = [c('# Privacy flow audit', 'bold'), '',
           f'Targets {", ".join(s["targets"]) or "-"}; repository `{s.get("repo") or "-"}`; regions {", ".join(s["regions"])}; '
           f'consent states tested: {", ".join(s["consent_states_tested"])}.', '',
           ' '.join(f'{c(p, p)} {n}' for p, n in h['counts'].items()), '', h['statement'], '',
           f'Third parties before consent: {", ".join(h["third_parties_before_consent"]) or "none"}. '
           f'After "Reject all": {", ".join(h["third_parties_after_reject"]) or "none"}. '
           f'Special-category flows: {", ".join(h["special_category_flows"]) or "none"}.', '',
           c(h['disclaimer'], 'dim'), '', '## Consent matrix', '', '| State | Third-party domains | Cookies | Canaries sent to third parties |', '|---|---|---|---|']
    for m in r['consent_matrix']:
        if m['status'] != 'tested':
            out.append(f'| {m["state"]} | {m["status"]}: {m.get("reason") or ""} | | |')
        else:
            out.append(f'| {m["state"]} | {", ".join(m["third_party_domains"]) or "-"} | {", ".join(m["cookies"]) or "-"} | {", ".join(m["canaries_to_third_parties"]) or "-"} |')
    out += ['', '## Findings', '', '| ID | Priority | Type | Recipient | Categories | Score | Confidence |', '|---|---|---|---|---|---|---|']
    for f in sorted(r['findings'], key=lambda f: (f['priority'], f['id'])):
        out.append(f'| {f["id"]} | {c(f["priority"], f["priority"])} | {f["type"]} | {f.get("vendor") or f.get("domain") or "-"} '
                   f'| {", ".join(f["categories"])} | {f["sensitivity"]}x{f["flow"]}x{f["reach"]}={f["score"]} | {f["confidence"]} |')
    out.append('')
    if not a.brief:
        for f in sorted(r['findings'], key=lambda f: (f['priority'], f['id'])):
            ev = f['evidence']
            out += [f'### {f["id"]} {c(f["priority"], f["priority"])} {f["title"]}', '', ev.get('summary', ''), '']
            if f.get('consent_states'):
                out.append(f'- Consent states: {", ".join(f["consent_states"])}')
            if f.get('floor') or f.get('cap'):
                out.append(f'- Priority adjusted: {f.get("floor") or f.get("cap")}')
            for q in ev.get('requests', [])[:3]:
                out.append(f'- Request ({q["state"]} #{q["n"]}, {q.get("phase", "")}): `{q.get("url", "")[:140]}`')
            for cp in ev.get('code_path', []):
                out.append(f'- Code: `{cp["file"]}:{cp["line"]}` {cp.get("what", "")}')
            refs = '; '.join(f'{k}: {", ".join(v)}' for k, v in f['references'].items())
            out += [f'- References (for a privacy professional): {refs}', f'- Fix: {f["fix"]}', f'- Verify: {f["verification"]}', '']
        out += ['## Recipients', '', '| Domain | Vendor | Kind | Region | States | Categories received |', '|---|---|---|---|---|---|']
        for x in r['recipients']:
            out.append(f'| {x["domain"]} | {x["vendor"] or "-"} | {x["kind"]} | {x["region"] or "-"} | {", ".join(x["states"])} | {", ".join(x["categories"]) or "-"} |')
        out += ['', '## Data inventory', '']
        for i in r['inventory']:
            rec = ', '.join(f'{x["domain"]} ({"/".join(x["encodings"])})' for x in i['recipients']) or 'none seen'
            out.append(f'- **{i["field"]}** ({i["category"]}): collected at {", ".join(i["collected_at"])}; stored {", ".join(i["stored"]) or "-"}; '
                       f'logged {", ".join(i["logged"]) or "-"}; sent to {rec}')
        out += ['', '## Triage', '']
        for t in r['triage']:
            if t['decision'] == 'promoted':
                out.append(f'- {t["candidate"]}: promoted to {t["finding"]}')
            else:
                out.append(f'- {t["candidate"]}: ' + c(f'{t["decision"]}{" (" + t["reason_code"] + ")" if t.get("reason_code") else ""}', 'dim') + f': {t["reason"]}')
        out += ['', '## Covered', ''] + [f'- {x}' for x in s['covered']] + ['', '## Not covered', ''] + [f'- {x}' for x in s['not_covered']]
        out += ['', '## Limitations', ''] + [f'- {x}' for x in r['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
