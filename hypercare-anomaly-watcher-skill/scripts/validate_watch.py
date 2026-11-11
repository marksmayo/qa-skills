#!/usr/bin/env python3
"""Validate a hypercare watch state: recompute every tick, replay the states, and check the drafts.

    python validate_watch.py state/ [--recorded recorded/]

Checks: each evaluation's score and decision are recomputed from the tick's stored value and baseline
(detect.py); a metric with too few baseline samples is not_assessed; replaying the decisions through the
state machine gives each item's history and state; an anomaly is correlated with the release only when its
first anomalous interval began after the deploy and the hour before the deploy was normal (or, for a
Sentry issue, it was first seen in the release); every confirmed, correlated item is in a draft for its
service and every draft item is one; drafts have status "draft" and the severity the rules give; every
request was a GET; no API key, token or event payload appears; no text says all clear, stable or healthy.
--recorded re-derives each tick's values and baselines from the recorded responses.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detect import evaluate, step  # noqa: E402
from sources import Sources, parse  # noqa: E402
from watch import metric_eval, sentry_eval  # noqa: E402

BANNED = re.compile(r'(?i)\b(all clear|all good|healthy|stable release|release is stable|nothing (is )?wrong|no issues|resolved itself)\b')
SECRET = re.compile(r'(?i)(dd-api-key|dd-application-key|api[_-]?key|app[_-]?key|authorization|bearer)\s*["\']?\s*[:=]\s*["\']?[A-Za-z0-9_-]{16,}|\b[0-9a-f]{32,40}\b|sntrys_[A-Za-z0-9_]{20,}')
SENTRY_FIELDS = {'key', 'source', 'id', 'short_id', 'title', 'culprit', 'first_seen', 'first_release', 'new_in_release', 'count', 'users',
                 'events_in_window', 'permalink', 'method', 'decision', 'reason', 'baseline', 'expected', 'score', 'threshold', 'change', 'anomalous', 'value'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('state', type=Path)
    ap.add_argument('--recorded', type=Path)
    a = ap.parse_args()
    st = json.loads((a.state / 'watch.json').read_text(encoding='utf-8'))
    cfg = st['config']
    dep = parse(cfg['release']['deployed_at'])
    qmap = {f'datadog:{q["id"]}': q for q in cfg['sources'].get('datadog', {}).get('queries', [])}
    sc = cfg['sources'].get('sentry', {})
    E: list[str] = []
    e = E.append
    replay: dict = {}
    corr: dict = {}
    src = Sources(cfg, a.recorded) if a.recorded else None
    tm, days = timedelta(minutes=cfg['tick_minutes']), int(cfg.get('baseline_days', 14))
    for t in st['ticks']:
        tk = json.loads((a.state / 'ticks' / f'{t["n"]:03d}.json').read_text(encoding='utf-8'))
        for r in tk['requests']:
            if r['method'] != 'GET':
                e(f'tick {t["n"]}: a {r["method"]} request to {r["host"]}{r["path"]}: the watch is read-only')
        start, end = parse(tk['window'][0]), parse(tk['window'][1])
        for ev in tk['evaluations']:
            key = ev['key']
            w = f'tick {t["n"]} {key}'
            if ev['source'] == 'datadog' and 'value' in ev:
                q = qmap[key]
                want = evaluate(ev['value'], ev['baseline'], q)
                if want['decision'] != ev['decision'] or want.get('score') != ev.get('score'):
                    e(f'{w}: {ev["decision"]} (score {ev.get("score")}) should be {want["decision"]} (score {want.get("score")})')
                if src:
                    pts = src.datadog(q['id'], q['query'], start - timedelta(days=days) - tm, end)
                    re_ev = metric_eval(pts, q, start, end, dep, days)
                    if re_ev['value'] != ev['value'] or re_ev['baseline'] != ev['baseline']:
                        e(f'{w}: value or baseline differ from the recorded data')
            if ev['source'] == 'sentry' and ev.get('method') == 'new_issue':
                hit = ev['users'] >= sc.get('min_users', 5) or ev['count'] >= sc.get('min_events', 20)
                if ev['decision'] != ('anomalous' if hit else 'normal'):
                    e(f'{w}: new-issue decision should be {"anomalous" if hit else "normal"}')
            elif ev['source'] == 'sentry' and 'baseline' in ev:
                q = {'kind': 'count', 'direction': 'up', 'p_value': sc.get('regression_p_value', 0.001), 'min_abs_change': sc.get('regression_min_abs', 10)}
                want = evaluate(float(ev['events_in_window']), ev['baseline'], q)
                if want['decision'] != ev['decision']:
                    e(f'{w}: regression decision should be {want["decision"]}')
            if ev['source'] == 'sentry' and set(ev) - SENTRY_FIELDS:
                e(f'{w}: Sentry fields beyond ids, titles, counts and links are copied: {sorted(set(ev) - SENTRY_FIELDS)}')
            if src and ev['source'] == 'sentry' and ev['key'] != 'sentry:*':
                issue = next((i for i in src.sentry_issues(end) if f'sentry:{i["id"]}' == key), None)
                if not issue or sentry_eval(issue, cfg, start, end, dep)['decision'] != ev['decision']:
                    e(f'{w}: differs from the recorded Sentry snapshot')
            # replay the state machine
            prev = replay.get(key, 'normal')
            if ev.get('method') == 'new_issue':
                nxt = 'confirmed' if ev['decision'] == 'anomalous' else ('recovering' if prev == 'confirmed' else prev if prev != 'suspected' else 'normal')
            else:
                nxt = step(prev, ev['decision'])
            replay[key] = nxt
            if ev['decision'] == 'anomalous' and key not in corr:
                if ev.get('method') == 'new_issue':
                    corr[key] = 'yes' if ev['new_in_release'] else 'no'
                else:
                    pre = (st['items'].get(key) or {}).get('pre_release')
                    corr[key] = 'no' if start < dep else 'unknown' if not pre or pre['decision'] == 'not_assessed' else 'no' if pre['decision'] == 'anomalous' else 'yes'
            hist = next((h for h in st['items'].get(key, {}).get('history', []) if h['tick'] == t['n']), None)
            if not hist or hist['state'] != nxt or hist['decision'] != ev['decision']:
                e(f'{w}: history should record {ev["decision"]} -> {nxt}')
    for key, it in st['items'].items():
        if replay.get(key, 'normal') != it['state']:
            e(f'{key}: state {it["state"]} should be {replay.get(key, "normal")}')
        if (corr.get(key) or None) != it['correlated']:
            e(f'{key}: correlated {it["correlated"]} should be {corr.get(key)}')
        if it['state'] == 'confirmed' and it['confirmed_tick'] is None:
            e(f'{key}: confirmed without a confirmed tick')
        hs = it['history']
        if it['confirmed_tick'] is not None and not next((h for h in hs if h['tick'] == it['confirmed_tick'] and h['state'] == 'confirmed'), None):
            e(f'{key}: confirmed_tick {it["confirmed_tick"]} is not when it became confirmed')
    # drafts -------------------------------------------------------------------------------------------------
    want_items: dict = {}
    for key, it in st['items'].items():
        if it['confirmed_tick'] is not None and it['correlated'] == 'yes':
            svc = qmap[key]['service'] if key in qmap else sc.get('service', 'unknown')
            want_items.setdefault(f'INC-DRAFT-{svc}', set()).add(key)
    have = {d['id']: {x['key'] for x in d['items']} for d in st['drafts']}
    if have != want_items:
        e(f'drafts {have} should be {want_items}')
    for d in st['drafts']:
        if d['status'] != 'draft':
            e(f'{d["id"]}: status {d["status"]}: the skill drafts incidents only')
        last = json.loads((a.state / 'ticks' / f'{st["ticks"][-1]["n"]:03d}.json').read_text(encoding='utf-8'))['evaluations']
        sev2 = any((qmap.get(x['key']) or {}).get('is_error_rate') and d['service'] in cfg.get('critical_services', []) for x in d['items']) or any(
            x['kind'] == 'sentry_issue' and next((ev for ev in last if ev['key'] == x['key']), {}).get('users', 0) >= sc.get('sev2_users', 25) for x in d['items'])
        if d['severity'] != ('SEV2' if sev2 else 'SEV3'):
            e(f'{d["id"]}: severity {d["severity"]} should be {"SEV2" if sev2 else "SEV3"}')
        mdp = a.state / 'incidents' / f'{d["id"]}.md'
        if not mdp.exists() or 'DRAFT' not in mdp.read_text(encoding='utf-8').split('\n')[0]:
            e(f'{d["id"]}: the Markdown draft is missing or not marked DRAFT')
        if not d['owner']:
            e(f'{d["id"]}: no owner to raise it')
    # language and secrets ---------------------------------------------------------------------------------
    blobs = [(a.state / 'watch.json').read_text(encoding='utf-8')] + [p.read_text(encoding='utf-8') for p in sorted(a.state.rglob('*')) if p.is_file() and p.suffix in ('.json', '.md')]
    for b in blobs:
        m = SECRET.search(b)
        if m:
            e(f'a key or token appears in the state: {m.group(0)[:20]}...'); break
    texts = [x for d in st['drafts'] for x in d.get('suggested_actions', []) + [d.get('title', ''), d.get('notes', '')]] + [st.get('summary', {}).get('headline', '')] + st.get('limitations', [])
    for t in texts:
        m = BANNED.search(t or '')
        if m:
            e(f'text says {m.group(0)!r}: say "no anomalies above the thresholds in the metrics watched"')
    if E:
        print(f'{a.state}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    print(f'{a.state}: valid. {len(st["ticks"])} tick(s); states ' + ', '.join(f'{k} {v["state"]}' for k, v in st['items'].items())
          + f'; {len(st["drafts"])} draft(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
