#!/usr/bin/env python3
"""Watch production after a release: one tick per scheduled run (stdlib only).

    python watch.py init --config hypercare.json --state state/
    python watch.py tick --state state/ [--now 2026-11-12T01:00:00Z] [--from-dir recorded/]

init checks the config, writes state/watch.json, and prints a schedule line (cron) for the tick interval;
the skill never installs a schedule itself.
tick evaluates the last `tick_minutes` for every Datadog query and every Sentry issue (see detect.py),
moves each one through the anomaly states, and creates or updates incident drafts in state/incidents/ for
confirmed anomalies correlated with the release. Every tick's inputs (current values and baseline samples)
and requests are kept in state/ticks/<n>.json, so validate_watch.py can recompute it. Drafts stay drafts:
nothing is paged, raised, posted or created anywhere.
"""
from __future__ import annotations
import argparse, hashlib, json, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detect import evaluate, step  # noqa: E402
from sources import Sources, parse  # noqa: E402


def iso(t: datetime) -> str:
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def save(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def window_value(pts, lo: float, hi: float, kind: str):
    xs = [v for t, v in pts if lo <= t < hi]
    if not xs:
        return None
    return round(sum(xs), 6) if kind == 'count' else round(statistics.fmean(xs), 6)


def metric_eval(pts, q: dict, start: datetime, end: datetime, deploy: datetime, days: int) -> dict:
    lo, hi = start.timestamp(), end.timestamp()
    value = window_value(pts, lo, hi, q['kind'])
    base = []
    for d in range(1, days + 1):
        s, e = start - timedelta(days=d), end - timedelta(days=d)
        if e <= deploy:
            v = window_value(pts, s.timestamp(), e.timestamp(), q['kind'])
            if v is not None:
                base.append(v)
    return {'value': value, 'baseline': base, **evaluate(value, base, q)}


def sentry_eval(issue: dict, cfg: dict, start: datetime, end: datetime, deploy: datetime) -> dict:
    sc = cfg['sources']['sentry']
    rel = cfg['release']['version']
    first_rel = (issue.get('firstRelease') or {}).get('version')
    new = first_rel == rel or parse(issue['firstSeen']) >= deploy
    buckets = issue.get('stats', {}).get('24h', [])
    in_win = sum(n for ts, n in buckets if start.timestamp() <= ts < end.timestamp())
    out = {'id': issue['id'], 'short_id': issue.get('shortId'), 'title': issue.get('title', '')[:160], 'culprit': issue.get('culprit'),
           'first_seen': issue['firstSeen'], 'first_release': first_rel, 'new_in_release': new, 'count': int(issue.get('count') or 0),
           'users': int(issue.get('userCount') or 0), 'events_in_window': in_win, 'permalink': issue.get('permalink')}
    if new:
        hit = out['users'] >= sc.get('min_users', 5) or out['count'] >= sc.get('min_events', 20)
        out.update(method='new_issue', decision='anomalous' if hit else 'normal',
                   reason=f'first seen in {first_rel} at {issue["firstSeen"]}; {out["count"]} events, {out["users"]} users '
                          f'(thresholds {sc.get("min_events", 20)} events or {sc.get("min_users", 5)} users)')
    else:
        base = [float(n) for ts, n in buckets if ts + 3600 <= deploy.timestamp()]
        q = {'kind': 'count', 'direction': 'up', 'p_value': sc.get('regression_p_value', 0.001), 'min_abs_change': sc.get('regression_min_abs', 10)}
        out.update(baseline=base, **evaluate(float(in_win), base, q))
    return out


def correlate(key: str, ev: dict, tick_start: datetime, deploy: datetime, pre: dict | None) -> tuple[str, str]:
    if ev.get('method') == 'new_issue':
        return ('yes', f'first seen in {ev["first_release"]} after the deploy') if ev['new_in_release'] else ('no', 'seen before the release')
    if tick_start < deploy:
        return 'no', 'the anomaly began before the deploy'
    if not pre or pre.get('decision') == 'not_assessed':
        return 'unknown', 'the hour before the deploy could not be assessed'
    if pre['decision'] == 'anomalous':
        return 'no', 'the hour before the deploy was already anomalous'
    return 'yes', 'began after the deploy, and the hour before it was normal'


def draft_md(d: dict) -> str:
    lines = [f'# DRAFT {d["severity"]}: {d["title"]}', '', f'*Draft for {d["owner"]} to review and raise. Not raised, paged or posted anywhere.*', '',
             f'- Service: {d["service"]}', f'- Release: {d["release"]}', f'- Started: {d["started_at"]} (first anomalous interval)', f'- Detected: {d["detected_at"]}', '',
             '## Signals', '']
    for i in d['items']:
        lines.append(f'- **{i["label"]}** ({i["kind"]}): {i["summary"]}' + (f' ({i["link"]})' if i.get('link') else ''))
    lines += ['', '## Timeline', ''] + [f'- {t["at"]} tick {t["tick"]}: {t["note"]}' for t in d['timeline']]
    if d.get('suggested_actions'):
        lines += ['', '## Suggested actions', ''] + [f'- {x}' for x in d['suggested_actions']]
    return '\n'.join(lines) + '\n'


def item_summary(key: str, ev: dict, q: dict | None) -> str:
    if ev.get('method') == 'new_issue':
        return f'{ev["short_id"]} "{ev["title"]}" at {ev["culprit"]}: {ev["count"]} events, {ev["users"]} users since {ev["first_seen"]}'
    unit = (q or {}).get('unit', '')
    if ev.get('method') == 'robust_z':
        return f'{ev["value"]}{unit} against a median of {ev["median"]}{unit} (robust z {ev["score"]}, threshold {ev["threshold"]})'
    return f'{ev["value"]} against an expected {ev["expected"]} (Poisson p {ev["score"]}, threshold {ev["threshold"]})'


def cmd_init(a) -> int:
    cfg = json.loads(a.config.read_text(encoding='utf-8'))
    for k in ('release', 'window_hours', 'tick_minutes', 'sources', 'incident_owner'):
        if k not in cfg:
            raise SystemExit(f'config: missing {k}')
    dep = parse(cfg['release']['deployed_at'])
    tm = cfg['tick_minutes']
    st = {'schema_version': '1.0.0', 'skill': {'name': 'hypercare-anomaly-watcher', 'version': '1.0.0'},
          'config': cfg, 'config_sha256': hashlib.sha256(a.config.read_bytes()).hexdigest(),
          'window': {'from': iso(dep), 'to': iso(dep + timedelta(hours=cfg['window_hours']))},
          'schedule': {'every_minutes': tm, 'cron': f'*/{tm} * * * *' if tm < 60 else f'{dep.minute} */{tm // 60} * * *',
                       'command': f'python scripts/watch.py tick --state {a.state.as_posix()}', 'installed_by_skill': False},
          'ticks': [], 'items': {}, 'drafts': []}
    save(a.state / 'watch.json', st)
    print(f'watching {cfg["release"]["service"]} {cfg["release"]["version"]} from {st["window"]["from"]} to {st["window"]["to"]}; '
          f'schedule (install it yourself): {st["schedule"]["cron"]} {st["schedule"]["command"]}')
    return 0


def cmd_tick(a) -> int:
    st = json.loads((a.state / 'watch.json').read_text(encoding='utf-8'))
    cfg = st['config']
    dep = parse(cfg['release']['deployed_at'])
    now = parse(a.now) if a.now else datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if now > parse(st['window']['to']):
        print('the hypercare window has ended; no tick'); return 0
    tm, days = timedelta(minutes=cfg['tick_minutes']), int(cfg.get('baseline_days', 14))
    start = now - tm
    src = Sources(cfg, a.from_dir)
    n = len(st['ticks']) + 1
    evals = []
    for q in cfg['sources'].get('datadog', {}).get('queries', []):
        try:
            pts = src.datadog(q['id'], q['query'], start - timedelta(days=days) - tm, now)
        except Exception as e:  # a monitoring outage is not "normal"
            evals.append({'key': f'datadog:{q["id"]}', 'source': 'datadog', 'decision': 'not_assessed', 'reason': f'fetch failed: {type(e).__name__}'}); continue
        ev = metric_eval(pts, q, start, now, dep, days)
        key = f'datadog:{q["id"]}'
        pre = None
        if key not in st['items'] or st['items'][key].get('pre_release') is None:
            pre = metric_eval(pts, q, dep - tm, dep, dep, days) if start >= dep else None
        evals.append(dict(key=key, source='datadog', **ev, **({'pre_release': pre} if pre else {})))
    try:
        for issue in src.sentry_issues(now):
            ev = sentry_eval(issue, cfg, start, now, dep)
            evals.append(dict(key=f'sentry:{issue["id"]}', source='sentry', **ev))
    except Exception as e:
        evals.append({'key': 'sentry:*', 'source': 'sentry', 'decision': 'not_assessed', 'reason': f'fetch failed: {type(e).__name__}'})
    qmap = {f'datadog:{q["id"]}': q for q in cfg['sources'].get('datadog', {}).get('queries', [])}
    changed = []
    for ev in evals:
        key = ev['key']
        it = st['items'].setdefault(key, {'state': 'normal', 'history': [], 'correlated': None, 'correlation_basis': None, 'pre_release': None,
                                          'first_anomalous_tick': None, 'first_anomalous_at': None, 'confirmed_tick': None})
        if ev.get('pre_release') is not None:
            it['pre_release'] = {k: ev['pre_release'].get(k) for k in ('value', 'decision', 'score')}
        before = it['state']
        if ev.get('method') == 'new_issue':
            it['state'] = 'confirmed' if ev['decision'] == 'anomalous' else ('recovering' if before == 'confirmed' else before if before != 'suspected' else 'normal')
        else:
            it['state'] = step(before, ev['decision'])
        if ev['decision'] == 'anomalous' and it['first_anomalous_tick'] is None:
            it['first_anomalous_tick'] = n
            it['first_anomalous_at'] = iso(start)
            it['correlated'], it['correlation_basis'] = correlate(key, ev, start, dep, it.get('pre_release'))
        if it['state'] == 'confirmed' and it['confirmed_tick'] is None:
            it['confirmed_tick'] = n
        it['history'].append({'tick': n, 'decision': ev['decision'], 'state': it['state']})
        if it['state'] != before:
            changed.append({'key': key, 'from': before, 'to': it['state']})
    # drafts ------------------------------------------------------------------------------------------
    sc = cfg['sources'].get('sentry', {})
    touched = []
    for key, it in st['items'].items():
        if it['state'] not in ('confirmed', 'recovering', 'recovered') or it['correlated'] != 'yes' or it['confirmed_tick'] is None:
            continue
        ev = next((e for e in evals if e['key'] == key), None)
        q = qmap.get(key)
        service = q['service'] if q else sc.get('service', 'unknown')
        did = f'INC-DRAFT-{service}'
        d = next((x for x in st['drafts'] if x['id'] == did), None)
        if d is None:
            d = {'id': did, 'status': 'draft', 'service': service, 'release': f'{cfg["release"]["service"]} {cfg["release"]["version"]}',
                 'owner': cfg['incident_owner'], 'severity': 'SEV3', 'title': '', 'started_at': None, 'detected_at': iso(now),
                 'items': [], 'timeline': [], 'suggested_actions': []}
            st['drafts'].append(d)
            d['timeline'].append({'tick': n, 'at': iso(now), 'note': 'draft created'})
        item = next((x for x in d['items'] if x['key'] == key), None)
        label = (q or {}).get('label') or (q or {}).get('id') or (ev or {}).get('short_id') or key
        if item is None:
            item = {'key': key, 'kind': 'metric' if q else 'sentry_issue', 'label': label, 'first_anomalous_tick': it['first_anomalous_tick'],
                    'first_anomalous_at': it['first_anomalous_at'],
                    'confirmed_tick': it['confirmed_tick'], 'summary': '', 'link': None if q else (ev or {}).get('permalink'),
                    'query': q['query'] if q else None}
            d['items'].append(item)
            d['timeline'].append({'tick': n, 'at': iso(now), 'note': f'{label} confirmed ({it["correlation_basis"]})'})
        if ev and ev.get('decision') != 'not_assessed':
            item['summary'] = item_summary(key, ev, q)
            item['state'] = it['state']
        if it['state'] in ('recovering', 'recovered') and not any(t['note'].startswith(f'{label} {it["state"]}') for t in d['timeline']):
            d['timeline'].append({'tick': n, 'at': iso(now), 'note': f'{label} {it["state"]} (back within the baseline); the draft stays open for the owner'})
        d['started_at'] = min(x['first_anomalous_at'] for x in d['items'])
        sev2 = any((qmap.get(x['key']) or {}).get('is_error_rate') and service in cfg.get('critical_services', []) for x in d['items']) or \
            any(x['kind'] == 'sentry_issue' and (next((e for e in evals if e['key'] == x['key']), {}) or {}).get('users', 0) >= sc.get('sev2_users', 25) for x in d['items'])
        new_sev = 'SEV2' if sev2 else 'SEV3'
        if new_sev != d['severity']:
            d['timeline'].append({'tick': n, 'at': iso(now), 'note': f'severity {d["severity"]} -> {new_sev}'})
            d['severity'] = new_sev
        d['title'] = f'{service}: ' + ', '.join(x['label'] for x in d['items']) + f' after {cfg["release"]["version"]}'
        touched.append(did)
    for d in st['drafts']:
        (a.state / 'incidents').mkdir(parents=True, exist_ok=True)
        save(a.state / 'incidents' / f'{d["id"]}.json', d)
        (a.state / 'incidents' / f'{d["id"]}.md').write_text(draft_md(d), encoding='utf-8', newline='\n')
    counts = {k: sum(1 for e in evals if e['decision'] == k) for k in ('normal', 'anomalous', 'not_assessed')}
    tick = {'n': n, 'at': iso(now), 'window': [iso(start), iso(now)], 'counts': counts, 'changed': changed, 'drafts_touched': sorted(set(touched))}
    save(a.state / 'ticks' / f'{n:03d}.json', {'n': n, 'at': iso(now), 'window': [iso(start), iso(now)], 'evaluations': evals, 'requests': src.log})
    st['ticks'].append(tick)
    save(a.state / 'watch.json', st)
    print(f'tick {n} at {iso(now)}: ' + ', '.join(f'{k} {v}' for k, v in counts.items()) + '; '
          + ('; '.join(f'{c["key"]} {c["from"]}->{c["to"]}' for c in changed) or 'no state changes') + (f'; drafts {", ".join(sorted(set(touched)))}' if touched else ''))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('init'); i.add_argument('--config', type=Path, required=True); i.add_argument('--state', type=Path, required=True)
    t = sub.add_parser('tick'); t.add_argument('--state', type=Path, required=True); t.add_argument('--now'); t.add_argument('--from-dir', type=Path)
    a = ap.parse_args()
    return cmd_init(a) if a.cmd == 'init' else cmd_tick(a)


if __name__ == '__main__':
    sys.exit(main())
