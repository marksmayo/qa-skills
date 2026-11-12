# Performance budget auditor rubric

`attribute.py` computes everything here; `validate_report.py` recomputes it from the runs.

## Statistics and status

Over the measured and extra runs that loaded (never the warm-up or the diagnostic run): n,
median, min, max and CV (sample standard deviation divided by the mean; 0 for one run or a zero
mean).

| Status | When |
|---|---|
| `over` | median > budget and stable |
| `within` | median ≤ budget and stable |
| `unstable` | not stable after up to 10 runs: it can't pass or fail on its own |
| `measured` | no budget for this metric |
| `untested` | INP on a page without an interaction, or no run produced a value |
| `not_supported` | LCP, CLS, INP or TBT in Firefox or WebKit |

**Stable** means CV ≤ 0.25, or a spread too small to matter for the budget: max − min within
the floor (10 ms, 0.01 CLS, 1 KB, as in the baseline comparison), or every run at or under half the
budget. Near-zero metrics (TBT 17 ms with a 39 ms max against 300 ms; CLS 0.003 against 0.1) have
large CVs that can't change the verdict. `measure.py` adds runs only for metrics that aren't stable
by this rule.

`bimodal` marks runs that split into two groups of at least two, with a gap more than three times
the larger group's range.

## Severity

By how far the worst over-budget median is over (ratio = median / budget):

| Severity | Weight | When |
|---|---|---|
| Critical | 4 | ratio ≥ 2, or LCP over 4 s, or CLS over 0.25 (Google's "poor") |
| High | 3 | ratio ≥ 1.5 |
| Medium | 2 | ratio ≥ 1.2 |
| Low | 1 | just over |

A page marked `critical_path` raises the severity one level (Critical stays Critical).

## Confidence

| Confidence | When |
|---|---|
| high | CV ≤ 0.1 and every run is over the budget |
| medium | stable and the median is over |
| low | otherwise, or the measurement is unstable: `needs_review`, never a finding |

## Reach, leverage and priority

- **Reach:** pages in the candidate (the same cause across a template's pages): 1, 2, or 3 for
  three or more.
- **Leverage:** 2 when the same cause (one script, one image, one server step) is behind
  candidates for more than one metric, otherwise 1.
- **Score** = severity weight × reach × leverage. P0 ≥ 24, P1 ≥ 12, P2 ≥ 6, otherwise P3. A
  Critical candidate is at least P1.

## Verdict, completeness and confidence

- **Verdict:** `over_budget` when any measurement is `over`; otherwise `within_budget_in_lab`, whose
  summary says "under the tested profiles; field performance not established".
- **Evidence completeness:** `partial` when a measurement is unstable or has fewer runs than
  configured, the calibration found the machine busy, a tool errored, or anything is in
  needs_review; otherwise `complete`.
- **Overall confidence:** medium when partial or any finding isn't high; high otherwise; low when
  nothing could be compared with a budget.

## Comparison with a baseline

A change counts only when `|current − baseline|` exceeds the largest of 10% of the baseline
median, the mean of the two ranges (max − min), and a floor of 10 ms, 0.01 CLS or 1 KB. Unstable
measurements are `not_compared`. A comparison across machines is flagged.

## Default budget

Google's "good" thresholds for LCP (2.5 s), CLS (0.1) and INP (200 ms) on both profiles. TBT
(300 ms mobile, 200 ms desktop) and weight (1.6 MB and 400 KB of JavaScript on mobile, 2.5 MB and
600 KB on desktop) are this skill's starting points, labelled as such; replace them with the
team's own budget when there is one.
