#!/usr/bin/env python3
"""Render a hypercare watch: current states, the tick timeline and the incident drafts.

    python render_report.py state/ [--brief] [--write-drafts] [--color auto|always|never]

--write-drafts rewrites state/incidents/<id>.md from the JSON drafts (after the agent added suggested
actions or notes). Drafts are for a person to raise; this script never sends anything.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from watch import draft_md  # noqa: E402

COL = {'confirmed': '31;1', 'suspected': '33', 'recovering': '36', 'recovered': '32', 'normal': '2', 'not_assessed': '35', 'bold': '1'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('state', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--write-drafts', action='store_true')
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    st = json.loads((a.state / 'watch.json').read_text(encoding='utf-8'))
    if a.write_drafts:
        for d in st['drafts']:
            (a.state / 'incidents' / f'{d["id"]}.md').write_text(draft_md(d), encoding='utf-8', newline='\n')
        print(f'rewrote {len(st["drafts"])} draft(s)')
        return 0
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))
    c = lambda t, k: f'\033[{COL[k]}m{t}\033[0m' if use and k in COL else str(t)  # noqa: E731
    cfg, r = st['config'], st['config']['release']
    last = st['ticks'][-1] if st['ticks'] else None
    labels = {f'datadog:{q["id"]}': q.get('label', q['id']) for q in cfg['sources'].get('datadog', {}).get('queries', [])}
    if st['ticks']:
        lt = json.loads((a.state / 'ticks' / f'{st["ticks"][-1]["n"]:03d}.json').read_text(encoding='utf-8'))
        labels.update({ev['key']: f'Sentry {ev["short_id"]}' for ev in lt['evaluations'] if ev['source'] == 'sentry' and ev.get('short_id')})
    out = [c(f'# Hypercare: {r["service"]} {r["version"]}', 'bold'), '',
           f'Deployed {r["deployed_at"]}; window to {st["window"]["to"]}; {len(st["ticks"])} tick(s) every {cfg["tick_minutes"]} minutes'
           + (f', last at {last["at"]}' if last else '') + f'. Schedule (installed by you, not the skill): `{st["schedule"]["cron"]} {st["schedule"]["command"]}`.', '']
    if st.get('summary', {}).get('headline'):
        out += [st['summary']['headline'], '']
    out += ['## What is being watched', '', '| Signal | State | Correlated with the release | Since tick |', '|---|---|---|---|']
    for k, it in st['items'].items():
        out.append(f'| {labels.get(k, k)} | {c(it["state"], it["state"])} | {it["correlated"] or "-"}{" (" + it["correlation_basis"] + ")" if it["correlation_basis"] else ""} | '
                   f'{it["first_anomalous_tick"] or "-"} |')
    out += ['', '## Incident drafts', '']
    out += [f'- **{d["id"]}** {d["severity"]}, {d["status"]} for {d["owner"]}: {d["title"]} (started {d["started_at"]}, `{(a.state / "incidents" / (d["id"] + ".md")).as_posix()}`)'
            for d in st['drafts']] or ['None.']
    if not a.brief:
        out += ['', '## Ticks', '', '| Tick | At | Normal | Anomalous | Not assessed | Changes |', '|---|---|---|---|---|---|']
        for t in st['ticks']:
            ch = '; '.join(f'{labels.get(x["key"], x["key"])} {x["from"]} to {x["to"]}' for x in t['changed']) or '-'
            out.append(f'| {t["n"]} | {t["at"]} | {t["counts"]["normal"]} | {t["counts"]["anomalous"]} | {t["counts"]["not_assessed"]} | {ch} |')
        for d in st['drafts']:
            out += ['', draft_md(d).rstrip()]
        if st.get('limitations'):
            out += ['', '## Limitations', ''] + [f'- {x}' for x in st['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
