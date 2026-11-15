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
| `unstable` | not stable within the run cap (`max_runs`, 10 by default): it can't pass or fail on its own; its note says "unstable at the cap of N runs" when the cap was reached |
| `measured` | no budget for this metric |
| `untested` | INP on a page without an interaction, or no run produced a value |
| `not_supported` | LCP, CLS, INP or TBT in Firefox or WebKit |

**Stable** means CV ≤ 0.25, or a spread too small to matter for the budget: max − min within
the floor (10 ms, 0.01 CLS, 1 KB, as in the baseline comparison), or every run at or under half the
budget. Near-zero metrics (TBT 17 ms with a 39 ms max against 300 ms; CLS 0.003 against 0.1) have
large CVs that can't change the verdict. Last, the **robust** rule: with at least five runs and a
budget, at most a quarter of the runs (at least one) may be outliers, more than 3 robust standard
deviations (1.4826 × the median absolute deviation, at least the floor) from the median; set
aside, the remaining runs must have a CV within 0.25 and a median on the same side of the budget as
the median of all runs. The measurement then records `stable_by: robust` and the outlier run ids.
`stable_by` names whichever rule made a measurement stable (`cv`, `spread_floor`, `far_under`,
`robust`). `measure.py` adds runs only for metrics that aren't stable by these rules, and never past
`max_runs`; each measurement records `runs_used` and `run_cap`.

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
| low | otherwise, or the measurement is unstable, or a CPU-bound overrun within the machine's slowdown (below): `needs_review`, never a finding |

**Machine slowdown.** For CPU-bound metrics (LCP, TBT, INP), when the calibration is slower than the
machine's idle reference (its largest slowdown: start, end or any `--append` pass), an overrun whose
worst ratio is no larger than that slowdown is low confidence: the machine alone could explain it
(TBT 1.19x the budget on a machine 1.49x slower than idle). The calibration's `state` is `loaded` from
1.2x to 1.5x (under the busy threshold) and `busy` from 1.5x; a loaded machine needs a limitation.

An unstable measurement whose median is under its budget is a candidate too (`unstable:<metric>`),
rated like any other (its ratio under 1 makes it Low) and always low confidence: it goes to
`needs_review` with the triage saying whether the machine or the page varied.

## Reach, leverage and priority

- **Reach:** pages in the candidate: 1, 2, or 3 for three or more. A specific cause (a resource
  URL, a Server-Timing step, an element with an id or class) found on several templates is one
  site-wide candidate (`site_wide: true`, `templates`), so its reach counts every page it was found
  on; generic causes (unattributed long tasks, weight spread over many resources, an element
  without an id or class, a service worker's own fetches) stay per template. A TBT cause names every
  script within 10% of the top one's time and is keyed on that set, and TBT keys that share a script
  are merged into one cause (one hydration task split across framework chunks).
- **Leverage:** 2 when the same cause (one script, one image, one server step) is behind
  candidates for more than one metric, otherwise 1.
- **Score** = severity weight × reach × leverage. P0 ≥ 24, P1 ≥ 12, P2 ≥ 6, otherwise P3. A
  Critical candidate is at least P1.

## Verdict, completeness and confidence

- **Verdict:** `over_budget` when any measurement is `over`; otherwise `within_budget_in_lab`, whose
  summary says "under the tested profiles; field performance not established".
- **Evidence completeness:** `partial` when a measurement is unstable, has fewer runs than
  configured, was stopped early by an HTTP error, or lost its interaction in some runs (any
  measurement note), the calibration found the machine busy (CV, slowdown against the idle
  reference, or start/end drift), a tool errored, or anything is in
  needs_review; otherwise `complete`.
- **Overall confidence:** medium when partial or any finding isn't high; high otherwise; low when
  nothing could be compared with a budget.

## Comparison with a baseline

A change counts only when `|current − baseline|` exceeds the largest of 10% of the baseline
median, the mean of the two ranges (max − min), and a floor of 10 ms, 0.01 CLS or 1 KB. Unstable
measurements are `not_compared`. A comparison across machines is flagged.

## Delivery

A first-party resource has a browser cache lifetime when its `Cache-Control` has `max-age` above 0 or
`immutable`, or it has `Expires`; `s-maxage` applies to shared caches only. An image is oversized when
its natural width is more than 1.5x its rendered width times the device pixel ratio (SVGs excepted).

## Default budget

Google's "good" thresholds for LCP (2.5 s), CLS (0.1) and INP (200 ms) on both profiles. TBT
(300 ms mobile, 200 ms desktop) and weight (1.6 MB and 400 KB of JavaScript on mobile, 2.5 MB and
600 KB on desktop) are this skill's starting points, labelled as such; replace them with the
team's own budget when there is one. A budget may also set `transfer_image`, `transfer_font`,
`transfer_stylesheet`, `transfer_document` and `transfer_sw` (what a service worker fetched); the
resource-type weights are measured only where a budget sets them.
