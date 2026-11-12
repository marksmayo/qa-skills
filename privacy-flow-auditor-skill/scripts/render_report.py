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
    matrix = {m['state']: m for m in r['consent_matrix']}

    def seen(key: str, state: str) -> str:
        """The third parties seen in a state, "none" only when the state was tested and showed none."""
        row = matrix.get(state) or {'status': 'not_run'}
        if h.get(key) is None or row.get('status') != 'tested':
            why = (h.get('states_not_tested') or {}).get(state) or row.get('reason') or row.get('status')
            return f'not tested ({why})'
        return ', '.join(h[key]) or 'none'
    blocked = [x['domain'] for x in r['recipients'] if x.get('blocked_requests') and x.get('kind') != 'first_party']
    fp_writes = [x for x in r['recipients'] if x.get('blocked_requests') and x.get('kind') == 'first_party']
    csp = [x['domain'] for x in r['recipients'] if x.get('csp_blocked_attempts')]
    status = r.get('run_status') or 'complete'
    out = [c('# Privacy flow audit', 'bold'), '',
           f'Targets {", ".join(s["targets"]) or "-"}; repository `{s.get("repo") or "-"}`; regions {", ".join(s["regions"])}; '
           f'consent states tested: {", ".join(s["consent_states_tested"]) or "none"}.', '']
    if status != 'complete':
        out += [c(f'Browser capture {"NOT RUN" if status == "not_run" else "PARTIAL"}: {(r.get("run_status_reason") or "-").rstrip(".")}.', 'bold')
                + (' Static findings only; no consent state, request or canary was tested.' if status == 'not_run' else ''), '']
    out += [
           ' '.join(f'{c(p, p)} {n}' for p, n in h['counts'].items()), '', h['statement'], '',
           f'Third parties before consent: {seen("third_parties_before_consent", "before")}. '
           f'After "Reject all": {seen("third_parties_after_reject", "reject")}. '
           f'Special-category flows: {", ".join(h["special_category_flows"]) or "none"}.', '']
    if blocked:
        out += [f'Blocked in the browser, so they never reached the vendor and could set no cookies or send follow-up hits: '
                f'{", ".join(blocked)}.', '']
    if fp_writes:
        out += ['First-party writes blocked by the read-only run (telemetry, CSP reports, consent records; their effect on the site is '
                'not seen): ' + ', '.join(f'{x["domain"]} x{x["blocked_requests"]}' for x in fp_writes) + '.', '']
    if csp:
        out += [f'Stopped by the site\'s own Content Security Policy before leaving the browser (attempted, from CSP reports and console): '
                f'{", ".join(csp)}.', '']
    out += [c(h['disclaimer'], 'dim'), '', '## Consent matrix', '','| State | Third-party domains | Cookies | Canaries sent to third parties |', '|---|---|---|---|']
    for m in r['consent_matrix']:
        if m['status'] != 'tested':
            out.append(f'| {m["state"]} | {m["status"]}: {m.get("reason") or ""} | | |')
        else:
            extra = (f' (CSP-blocked: {", ".join(m["csp_blocked_domains"])})' if m.get('csp_blocked_domains') else '') + \
                (f' (consent record blocked x{m["consent_writes_blocked"]})' if m.get('consent_writes_blocked') else '')
            out.append(f'| {m["state"]} | {", ".join(m["third_party_domains"]) or "-"}{extra} | {", ".join(m["cookies"]) or "-"} | {", ".join(m["canaries_to_third_parties"]) or "-"} |')
    out += ['', '## Findings', '', '| ID | Priority | Type | Recipient | Categories | Score | Confidence |', '|---|---|---|---|---|---|---|']
    for f in sorted(r['findings'], key=lambda f: (f['priority'], f['id'])):
        out.append(f'| {f["id"]}{" (found by review)" if f.get("source") == "agent" else ""} | {c(f["priority"], f["priority"])} | {f["type"]} | {f.get("vendor") or f.get("domain") or "-"} '
                   f'| {", ".join(f["categories"])} | {f["sensitivity"]}x{f["flow"]}x{f["reach"]}={f["score"]} | {f["confidence"]} |')
    out.append('')
    if not a.brief:
        for f in sorted(r['findings'], key=lambda f: (f['priority'], f['id'])):
            ev = f['evidence']
            out += [f'### {f["id"]} {c(f["priority"], f["priority"])} {f["title"]}' + (' (found by review)' if f.get('source') == 'agent' else ''),
                    '', ev.get('summary', ''), '']
            for b in f.get('basis') or []:
                out.append(f'- Basis: `{b["file"]}:{b["line"]}` {b.get("excerpt", "")}' if 'file' in b else f'- Basis: evidence {b.get("evidence")}')
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
        out += ['## Recipients', '', '| Domain | Vendor | Kind | Declared as | Region | States | Categories received | Requests (blocked) |',
                '|---|---|---|---|---|---|---|---|']
        for x in r['recipients']:
            dec = f'{x["declared_as"]["name"]} ({x["declared_as"]["match"]})' if x.get('declared_as') else '-'
            reqs = (f'{x["requests"]} ({x.get("blocked_requests", 0)})' if 'requests' in x else '-') + \
                (f', CSP-blocked {x["csp_blocked_attempts"]}' if x.get('csp_blocked_attempts') else '')
            out.append(f'| {x["domain"]} | {x["vendor"] or "-"} | {x["kind"]} | {dec} | {x["region"] or "-"} | {", ".join(x["states"])} '
                       f'| {", ".join(x["categories"]) or "-"} | {reqs} |')
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
