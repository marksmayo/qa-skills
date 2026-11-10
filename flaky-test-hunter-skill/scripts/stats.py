#!/usr/bin/env python3
"""Rates with honest uncertainty: Wilson score intervals and rate bands (stdlib only).

    python stats.py 2 20            # -> 2/20 = 10.0% (95% CI 2.8%-30.1%)

wilson(k, n) gives the 95% Wilson score interval for k events in n trials. With k = 0 the upper
bound is roughly 3/n (the "rule of three"), which is why "0 failures in 20 runs" is reported as
an upper bound, never as "not flaky". rate_band() turns a lower bound into the ranking factor
the flaky test hunter uses; the release readiness gate reads the same bands.
"""
from __future__ import annotations
import math, sys

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """(low, high) of the Wilson score interval, as fractions. n = 0 gives (0.0, 1.0)."""
    if n <= 0:
        return 0.0, 1.0
    if not 0 <= k <= n:
        raise ValueError(f'k={k} must be between 0 and n={n}')
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    if k == 0:
        low = 0.0
    if k == n:
        high = 1.0
    return round(low, 4), round(high, 4)


def rate_band(low: float) -> int:
    """Ranking factor from a Wilson lower bound: >= 20% -> 4, 5-20% -> 3, 1-5% -> 2, < 1% -> 1."""
    if low >= 0.20:
        return 4
    if low >= 0.05:
        return 3
    if low >= 0.01:
        return 2
    return 1


def fmt(k: int, n: int) -> str:
    low, high = wilson(k, n)
    rate = f'{100 * k / n:.1f}%' if n else 'n/a'
    return f'{k}/{n} = {rate} (95% CI {100 * low:.1f}%-{100 * high:.1f}%)'


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(__doc__.splitlines()[2].strip())
        raise SystemExit(2)
    print(fmt(int(sys.argv[1]), int(sys.argv[2])))
