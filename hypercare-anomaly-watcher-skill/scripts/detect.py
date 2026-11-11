#!/usr/bin/env python3
"""Statistics for the hypercare watch: one evaluation per metric per tick, and the anomaly state machine.

Rates and latencies (kind rate | latency | gauge): the tick value is the mean of the interval's points;
the baseline is the same interval on each of `baseline_days` days before the release (seasonal by hour of
day). robust z = (value - median) / max(1.4826 x MAD, rel_floor x median, abs_floor). Anomalous when
z >= threshold_z in the query's direction and the change is at least min_abs_change.
Counts (kind count): the tick value is the sum; the expected count is the baseline mean; anomalous when the
Poisson tail probability P(X >= value) (or P(X <= value) for direction down) is below p_value and the change
is at least min_abs_change.
Fewer than `min_baseline` (7) baseline samples, or no points in the interval: not_assessed.

State machine per metric, one step per tick:
  normal/recovered --anomalous--> suspected --anomalous--> confirmed --normal--> recovering --normal--> recovered
  suspected --normal--> normal;  recovering --anomalous--> confirmed;  no data at all -> not_assessed (state kept)
"""
from __future__ import annotations
import math, statistics

MIN_BASELINE = 7


def robust(value: float, base: list[float], q: dict) -> dict:
    med = statistics.median(base)
    mad = statistics.median([abs(x - med) for x in base])
    scale = max(1.4826 * mad, q.get('rel_floor', 0.05) * abs(med), q.get('abs_floor', 1e-9))
    z = (value - med) / scale
    change = value - med
    up = q.get('direction', 'up') == 'up'
    anom = (z >= q.get('threshold_z', 4) and change >= q.get('min_abs_change', 0)) if up else (z <= -q.get('threshold_z', 4) and -change >= q.get('min_abs_change', 0))
    return {'method': 'robust_z', 'median': round(med, 6), 'mad': round(mad, 6), 'scale': round(scale, 6), 'score': round(z, 2),
            'threshold': q.get('threshold_z', 4), 'change': round(change, 6), 'anomalous': anom}


def poisson_tail(k: int, lam: float, upper: bool = True) -> float:
    """P(X >= k) (upper) or P(X <= k), X ~ Poisson(lam), computed in log space."""
    if lam <= 0:
        return 0.0 if (upper and k > 0) else 1.0
    def logpmf(i):
        return -lam + i * math.log(lam) - math.lgamma(i + 1)
    if upper:
        if k <= 0:
            return 1.0
        cdf = sum(math.exp(logpmf(i)) for i in range(0, k))
        return max(0.0, 1.0 - cdf)
    return min(1.0, sum(math.exp(logpmf(i)) for i in range(0, k + 1)))


def poisson(value: float, base: list[float], q: dict) -> dict:
    lam = statistics.fmean(base)
    up = q.get('direction', 'up') == 'up'
    p = poisson_tail(int(round(value)), lam, upper=up)
    change = value - lam
    anom = p < q.get('p_value', 0.001) and (change >= q.get('min_abs_change', 0) if up else -change >= q.get('min_abs_change', 0))
    return {'method': 'poisson', 'expected': round(lam, 4), 'score': float(f'{p:.3g}'), 'threshold': q.get('p_value', 0.001),
            'change': round(change, 4), 'anomalous': anom}


def evaluate(value: float | None, base: list[float], q: dict) -> dict:
    if value is None:
        return {'decision': 'not_assessed', 'reason': 'no points in the interval'}
    if len(base) < q.get('min_baseline', MIN_BASELINE):
        return {'decision': 'not_assessed', 'reason': f'{len(base)} baseline sample(s); at least {q.get("min_baseline", MIN_BASELINE)} needed'}
    r = (poisson if q['kind'] == 'count' else robust)(value, base, q)
    r['decision'] = 'anomalous' if r['anomalous'] else 'normal'
    return r


def step(state: str, decision: str) -> str:
    if decision == 'not_assessed':
        return state
    a = decision == 'anomalous'
    return {('normal', True): 'suspected', ('normal', False): 'normal',
            ('suspected', True): 'confirmed', ('suspected', False): 'normal',
            ('confirmed', True): 'confirmed', ('confirmed', False): 'recovering',
            ('recovering', True): 'confirmed', ('recovering', False): 'recovered',
            ('recovered', True): 'suspected', ('recovered', False): 'recovered'}[(state, a)]
