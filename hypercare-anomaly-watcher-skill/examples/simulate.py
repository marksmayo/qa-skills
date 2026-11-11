#!/usr/bin/env python3
"""Simulate the monitoring data for the example (stdlib, fixed seed): no real Datadog or Sentry is called.

    python simulate.py --out recorded/

harbour-shop 2.4.0 is deployed at 2026-11-11T22:00:00Z. The simulation covers 2026-10-27T00:00Z to
2026-11-12T05:00Z at five-minute points, in the shapes the APIs return:

  recorded/datadog/<query id>.json     a /api/v1/query response: {"series": [{"metric", "scope", "pointlist": [[ms, value], ...]}]}
  recorded/sentry/<tick time>.json     the /api/0/projects/{org}/{project}/issues/ list as it would read at each hourly tick
                                       (count and userCount so far, stats.24h hourly buckets)

Planted after the deploy: the checkout error rate rises from about 0.4% to about 2% and stays up; a new
issue (a TypeError in the cart) first seen in 2.4.0; one hour of high search p95 latency (00:00 to 01:00),
then normal. Decoys: the nightly export job logs a burst of errors at 02:00 every day; an old payment
timeout issue keeps its usual rate.
"""
from __future__ import annotations
import argparse, json, math, random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 10, 27, tzinfo=timezone.utc)
END = datetime(2026, 11, 12, 5, tzinfo=timezone.utc)
DEPLOY = datetime(2026, 11, 11, 22, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def iso(t: datetime) -> str:
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def series(fn, rnd) -> list[list]:
    out, t = [], START
    while t < END:
        out.append([ms(t), round(fn(t, rnd), 6)])
        t += STEP
    return out


def daily(t: datetime) -> float:
    """Shop traffic shape: low at night (UTC+11 customers), peak in the evening."""
    local_h = (t.hour + 11) % 24 + t.minute / 60
    return 0.55 + 0.45 * math.sin((local_h - 9) / 24 * 2 * math.pi)


def error_rate(t, rnd):
    base = 0.004 + rnd.gauss(0, 0.0006)
    if t >= DEPLOY:
        base = 0.020 + rnd.gauss(0, 0.002)
    return max(0.0, base)


def search_p95(t, rnd):
    v = 180 + 25 * daily(t) + rnd.gauss(0, 9)
    if datetime(2026, 11, 12, 0, tzinfo=timezone.utc) <= t < datetime(2026, 11, 12, 1, tzinfo=timezone.utc):
        v += 340 + rnd.gauss(0, 20)
    return max(50.0, v)


def batch_errors(t, rnd):
    n = rnd.random() < 0.02
    if t.hour == 2:
        return float(max(0, round(rnd.gauss(4.0, 1.2))))  # the nightly export, every day
    return float(n)


def orders(t, rnd):
    lam = 30 * daily(t)
    return float(max(0, round(rnd.gauss(lam, math.sqrt(lam)))))


def poisson(rnd, lam):
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= l:
            return k
        k += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    rnd = random.Random(20261111)
    dd = a.out / 'datadog'
    dd.mkdir(parents=True, exist_ok=True)
    qs = {
        'checkout-error-rate': ('trace.http.request.errors', 'service:checkout,env:prod', error_rate),
        'search-p95': ('trace.http.request.duration.by.service.95p', 'service:search,env:prod', search_p95),
        'nightly-export-errors': ('batch.job.errors', 'job:nightly-export,env:prod', batch_errors),
        'orders': ('shop.orders.count', 'env:prod', orders),
    }
    for qid, (metric, scope, fn) in qs.items():
        body = {'status': 'ok', 'res_type': 'time_series', 'from_date': ms(START), 'to_date': ms(END),
                'series': [{'metric': metric, 'scope': scope, 'interval': 300, 'pointlist': series(fn, rnd)}],
                'note': 'SIMULATED by examples/simulate.py; not from a real Datadog account'}
        (dd / f'{qid}.json').write_text(json.dumps(body) + '\n', encoding='utf-8', newline='\n')
    # Sentry: hourly events per issue
    hours = []
    t = START
    while t < END:
        hours.append(t)
        t += timedelta(hours=1)
    old = {h: poisson(rnd, 3.0) for h in hours}
    new = {h: (poisson(rnd, 55.0) if h >= DEPLOY else 0) for h in hours}
    new_users = {h: (min(new[h], poisson(rnd, 16.0)) if h >= DEPLOY else 0) for h in hours}
    sd = a.out / 'sentry'
    sd.mkdir(parents=True, exist_ok=True)
    tick = DEPLOY + timedelta(hours=1)
    while tick <= END:
        past = [h for h in hours if h < tick]
        last24 = [h for h in past if h >= tick - timedelta(hours=24)]
        issues = [
            {'id': '4100021', 'shortId': 'HARBOUR-SHOP-WEB-2F', 'title': "TypeError: Cannot read properties of undefined (reading 'lineItems')",
             'culprit': 'cart/summary.js in renderTotals', 'level': 'error', 'status': 'unresolved',
             'firstSeen': '2026-11-11T22:06:41Z', 'lastSeen': iso(tick - timedelta(minutes=2)), 'firstRelease': {'version': '2.4.0'},
             'count': str(sum(new[h] for h in past)), 'userCount': sum(new_users[h] for h in past),
             'permalink': 'https://sentry.example.test/organizations/harbour/issues/4100021/',
             'stats': {'24h': [[int(h.timestamp()), new[h]] for h in last24]}},
            {'id': '3300410', 'shortId': 'HARBOUR-SHOP-WEB-1A', 'title': 'TimeoutError: payment provider did not answer within 10000 ms',
             'culprit': 'checkout/pay.ts in submitPayment', 'level': 'error', 'status': 'unresolved',
             'firstSeen': '2026-09-02T04:11:09Z', 'lastSeen': iso(tick - timedelta(minutes=9)), 'firstRelease': {'version': '2.2.1'},
             'count': str(2210 + sum(old[h] for h in past)), 'userCount': 640 + sum(min(old[h], 2) for h in past),
             'permalink': 'https://sentry.example.test/organizations/harbour/issues/3300410/',
             'stats': {'24h': [[int(h.timestamp()), old[h]] for h in last24]}},
        ]
        (sd / f'{tick.strftime("%Y-%m-%dT%H%M")}.json').write_text(json.dumps(issues, indent=1) + '\n', encoding='utf-8', newline='\n')
        tick += timedelta(hours=1)
    (a.out / 'README.txt').write_text('SIMULATED monitoring data for the hypercare example (examples/simulate.py, seed 20261111). '
                                      'Shapes follow the Datadog /api/v1/query and Sentry project issues APIs; no real account was used.\n',
                                      encoding='utf-8', newline='\n')
    print(f'simulated {len(qs)} Datadog queries and {len(list(sd.iterdir()))} Sentry snapshots -> {a.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
