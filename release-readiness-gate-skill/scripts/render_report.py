#!/usr/bin/env python3
"""Render a gate report as a one-page summary.

    python render_report.py gate_report.json [--brief] [--markdown page.md] [--color auto|always|never]

The page: verdict and statement, blockers, missing evidence, waivers (applied and rejected), warnings,
risks accepted, and what the sources report as untested. --brief prints the verdict, counts and blockers
only. --markdown writes the same page without colour, for a release ticket or channel.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

COL = {'no_go': '31;1', 'not_established': '33;1', 'go': '32;1', 'P0': '31;1', 'P1': '31', 'dim': '2', 'bold': '1'}


def page(d: dict, brief: bool, c) -> list[str]:
    h, cand = d['headline'], d['candidate']
    out = [c('# Release readiness gate', 'bold'), '',
           f'Candidate `{cand["commit"][:12]}` {cand.get("version") or ""} (build {cand.get("build_id") or "-"}), release type {d["policy"]["release_type"]}; '
           f'policy `{d["policy"]["path"]}`{" (the starter policy, an example)" if d["policy"]["starter"] else ""}, as of {d["as_of"]}.', '',
           c(f'Verdict: {h["verdict"]}', h['verdict']), '', h['statement'], '',
           f'{h["blocker_count"]} blocker(s), {h["waived_count"]} waived, {h["missing_evidence_count"]} missing evidence, {h["warning_count"]} warning(s). '
           f'Evidence completeness {h["evidence_completeness"]}, overall confidence {h["overall_confidence"]}.', '',
           '## Blockers', '']
    if d['blockers']:
        out += ['| Issue | Rules | Source | Priority | Severity | Confidence | Title |', '|---|---|---|---|---|---|---|']
        for b in d['blockers']:
            src = ', '.join(f'{s["skill"]} {s["finding_id"]}' for s in b['sources'])
            stale = ' (stale report)' if 'stale' in b.get('source_freshness', []) else ''
            out.append(f'| {b["issue_id"]} | {", ".join(b["rule_ids"])} | {src}{stale} | {c(b["priority"] or "-", b["priority"]) if b["priority"] in COL else b["priority"] or "-"} | '
                       f'{b["severity"] or (("tier " + b["tier"]) if b.get("tier") else "-")} | {b["confidence"] or "-"} | {b["title"]} |')
    else:
        out.append('None in the evidence provided.')
    if brief:
        return out
    out += ['', '## Missing evidence', '']
    out += [f'- **{m["requirement"]}** ({m["produced_by_skill"]}): {m["reason"]}' + (f' - {"; ".join(map(str, m.get("detail") or []))}' if m.get('detail') else '')
            + f'. {m["how_to_produce"]}' for m in d['missing_evidence']] or ['None.']
    out += ['', '## Waivers', '']
    out += [f'- {w["issue_id"]} waived by {w["waiver_id"]} ({w["approver"]}, {w["approver_category"]}), expires {w["expires"]}: {w["title"]}. Reason: {w["reason"]}' for w in d['waived']]
    out += [f'- {c("Not applied", "dim")} {x["waiver_id"]} ({x.get("approver")}): {"; ".join(x["problems"])}' for x in d['expired_or_invalid_waivers']]
    if not d['waived'] and not d['expired_or_invalid_waivers']:
        out.append('None.')
    out += ['', '## Warnings', '']
    out += [f'- {w["rule_id"]}: {" ".join(x for x in (w["skill"], w.get("test")) if x)} measured {w["measure"]} against {w["threshold"]}'
            + (f' ({w["basis"]})' if w.get('basis') else '') for w in d['warnings']] or ['None.']
    out += ['', f'## Risks accepted (non-blocking, at or above {d["policy"]["show_threshold"]["severity"]} or {d["policy"]["show_threshold"]["priority"]})', '']
    for g in d['risks_accepted']:
        out.append(f'- **{g["skill"]}**: ' + '; '.join(f'{i["finding_ids"][0]} {i["priority"] or ""} {i["severity"] or ""} {i["title"][:70]}'.replace('  ', ' ') for i in g['issues']))
    if not d['risks_accepted']:
        out.append('None.')
    out += ['', '## Untested or not run (as each source reported it)', '']
    out += [f'- {u["skill"]}, {u["category"]}: {", ".join(u["items"][:12])}{" ..." if len(u["items"]) > 12 else ""} ({len(u["items"])})' for u in d['untested']] or ['None reported.']
    out += ['', '## Sources', '', '| Source | Skill | Validator | Freshness | Required |', '|---|---|---|---|---|']
    for s in d['sources']:
        fr = s['freshness'] + (f' ({", ".join(s["freshness_reasons"])}' + (f'; {s["commits_behind"]} commit(s) behind' if s.get('commits_behind') else '') + ')' if s['freshness_reasons'] else '')
        out.append(f'| `{s["path"]}` | {s["skill"]} | {s["validator"]["status"]} | {fr}{" superseded" if s["superseded"] else ""} | {"yes" if s["required"] else "no"} |')
    out += ['', '## Limitations', ''] + [f'- {x}' for x in d['limitations']]
    if d['open_questions']:
        out += ['', '## Open questions', ''] + [f'- {q}' for q in d['open_questions']]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--markdown', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    d = json.loads(a.report.read_text(encoding='utf-8'))
    if a.markdown:
        a.markdown.write_text('\n'.join(page(d, a.brief, lambda t, k: str(t))) + '\n', encoding='utf-8', newline='\n')
        print(f'wrote {a.markdown}')
        return 0
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))
    print('\n'.join(page(d, a.brief, lambda t, k: f'\033[{COL[k]}m{t}\033[0m' if use and k in COL else str(t))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
