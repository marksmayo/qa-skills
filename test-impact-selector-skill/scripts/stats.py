#!/usr/bin/env python3
"""Rates with honest uncertainty: Wilson score intervals and rate bands (stdlib only).

    python stats.py 2 20            # -> 2/20 = 10.0% (95% CI 2.8%-30.1%)
    python stats.py --self-test

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


def self_test() -> int:
    """Interval values against the Wilson formula, its edge cases, every band threshold on both sides, the
    formatted line and the command line."""
    import contextlib, io
    checks = []

    def check(name, ok, got=None):
        checks.append((name, bool(ok), got))

    # 95% Wilson score intervals, rounded to 4 places (2/20 is the module docstring's 2.8%-30.1%)
    want = {(0, 1): (0.0, 0.7935), (1, 1): (0.2065, 1.0), (0, 20): (0.0, 0.1611), (1, 20): (0.0089, 0.2361),
            (2, 20): (0.0279, 0.301), (19, 20): (0.7639, 0.9911), (20, 20): (0.8389, 1.0), (3, 10): (0.1078, 0.6032),
            (50, 100): (0.4038, 0.5962)}
    got = {kn: wilson(*kn) for kn in want}
    check('wilson: 95% intervals match the reference values', got == want, got)
    check('wilson: n = 0 (or less) is the uninformative (0.0, 1.0), not a division by zero',
          wilson(0, 0) == (0.0, 1.0) and wilson(3, 0) == (0.0, 1.0) and wilson(0, -1) == (0.0, 1.0), wilson(0, 0))
    check('wilson: one run (n = 1) is an interval, not the n = 0 answer', wilson(0, 1) != (0.0, 1.0), wilson(0, 1))
    check('wilson: k = 0 has a zero lower bound and an upper bound near 3/n (rule of three)',
          wilson(0, 100)[0] == 0.0 and 0.03 <= wilson(0, 100)[1] <= 0.04, wilson(0, 100))
    check('wilson: k = 1 has a lower bound above zero', wilson(1, 20)[0] > 0.0, wilson(1, 20))
    check('wilson: k = n has an upper bound of exactly 1.0 and a lower bound below it',
          wilson(5, 5)[1] == 1.0 and wilson(5, 5)[0] < 1.0, wilson(5, 5))
    check('wilson: symmetric (k of n mirrors n-k of n)',
          wilson(3, 10) == (round(1 - wilson(7, 10)[1], 4), round(1 - wilson(7, 10)[0], 4)),
          (wilson(3, 10), wilson(7, 10)))
    check('wilson: a smaller z gives a narrower interval', wilson(2, 20, z=1.0) == (0.0509, 0.1872), wilson(2, 20, z=1.0))
    for k, n in ((-1, 5), (6, 5)):
        try:
            wilson(k, n)
            ok, err = False, 'no error'
        except ValueError as e:
            ok, err = 'between 0 and n' in str(e), str(e)
        check(f'wilson: k={k} outside 0..n={n} is a ValueError', ok, err)

    bands = {0.0: 1, 0.0099: 1, 0.01: 2, 0.0499: 2, 0.05: 3, 0.1999: 3, 0.20: 4, 0.5: 4, 1.0: 4}
    got = {low: rate_band(low) for low in bands}
    check('rate_band: each threshold (1%, 5%, 20%) is inclusive and the value just below it is the band under',
          got == bands, got)
    got = (rate_band(wilson(2, 20)[0]), rate_band(wilson(19, 20)[0]), rate_band(wilson(0, 20)[0]))
    check('rate_band: bands from real intervals (2/20 -> 2, 19/20 -> 4, 0/20 -> 1)', got == (2, 4, 1), got)

    lines = {(2, 20): '2/20 = 10.0% (95% CI 2.8%-30.1%)', (0, 20): '0/20 = 0.0% (95% CI 0.0%-16.1%)',
             (1, 3): '1/3 = 33.3% (95% CI 6.2%-79.2%)', (19, 20): '19/20 = 95.0% (95% CI 76.4%-99.1%)',
             (0, 0): '0/0 = n/a (95% CI 0.0%-100.0%)'}
    got = {kn: fmt(*kn) for kn in lines}
    check('fmt: the rate and the interval as percentages to one place; n = 0 is n/a', got == lines, got)

    def cli(argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()
    got = cli(['2', '20'])
    check('cli: `stats.py 2 20` prints the formatted line and exits 0', got == (0, lines[(2, 20)] + '\n'), got)
    got = cli(['2'])
    check('cli: a wrong argument count prints the usage line and exits 2',
          got == (2, 'python stats.py 2 20            # -> 2/20 = 10.0% (95% CI 2.8%-30.1%)\n'), got)
    check('cli: three arguments are a wrong count too', cli(['1', '2', '3'])[0] == 2)

    failed = [c for c in checks if not c[1]]
    for name, ok, extra in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{len(failed)} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv == ['--self-test']:
        return self_test()
    if len(argv) != 2:
        print(__doc__.splitlines()[2].strip())
        return 2
    print(fmt(int(argv[0]), int(argv[1])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
