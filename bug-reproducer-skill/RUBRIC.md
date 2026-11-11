# Bug reproducer rubric

## Outcome

| Outcome | Needs | Confidence |
|---|---|---|
| `reproduced` | A matched attempt with an artifact. The basis is the as-reported candidate if it matched, otherwise the first variation that did | high: ≥ 2/3 of the as-reported runs matched, on the stated version. medium: fewer, only after a variation, or on another version |
| `reproduced_differently` | No match, but a run showed another failure (page error, console error, dialog) | medium; reported separately, not counted as the bug |
| `not_reproduced` | At least `attempts` attempts, including a variation, all without the failure | n/a |
| `cannot_attempt` | No steps and no failure signature, or the version, data or access is missing | n/a; the questions or the blocker are listed |

`matched_attempts` and `total_attempts` count the basis candidate's runs; `all_attempts` counts
every run. `versions_tried` is the set of versions the runs saw on the page or in the build, not
the ones the spec asked for.

## Severity

This rubric decides the rating. Reporter and tool labels are inputs only, recorded as `reporter_label`.

| Severity | Weight | Definition |
|---|---|---|
| S1 Critical | 4 | Data loss or corruption, a security exposure, money wrong, or a core journey blocked with no workaround |
| S2 Major | 3 | A core function broken; only a hard workaround exists (including one the user has no reason to know they need) |
| S3 Minor | 2 | A non-core function broken, or an easy workaround exists |
| S4 Trivial | 1 | Cosmetic or wording |

## Reach

| Reach | Who |
|---|---|
| 3 | All users of the function |
| 2 | A role, platform, browser, time zone or locale |
| 1 | An edge configuration (one device model, one data shape, a feature flag) |

## Frequency (recomputed from the attempts)

| Frequency | When |
|---|---|
| 3 | Every run of the basis candidate matched |
| 2 | At least two runs matched, but not all |
| 1 | One run matched, or it didn't reproduce |

## Priority

`score = severity weight × reach × frequency`. P0 ≥ 24, P1 ≥ 12, P2 ≥ 6, otherwise P3.

- S1 is at least P1.
- A bug that didn't reproduce keeps its reported severity: `basis: as_reported`, confidence
  low, never P0.
- A security-looking bug is also routed to the code security auditor (`route_to`).

## Duplicates

`similarity = 0.5 × signature match (0/1) + 0.3 × summary token Jaccard + 0.2 × same component or label`.

| Similarity | Verdict |
|---|---|
| ≥ 0.7 | `likely_duplicate` |
| 0.4–0.7 | `related` |
| < 0.4 | not listed |

The signature matches when the normalised errors are equal (or one contains the other), or
when the reports' content words overlap by at least half of the smaller set. Nothing is closed
or linked; a person decides.

## Localisation confidence

| Confidence | Evidence |
|---|---|
| high | A run or test points at the line: a stack frame, or a unit test of that function that fails the same way |
| medium | The code on the path the minimal reproduction exercises, with a mechanism that explains the failure |
| low | A file or recent commit near the feature, with no run pointing at it |

## Regression test red

| Red status | Accepted? |
|---|---|
| Every case failed by assertion (`parse_results.py` `failure_type: assertion`) | yes |
| A case errored (import, syntax, fixture, collection) | no: fix the test |
| A case passed | no: it doesn't capture the bug |

## Wording

Say what was tried and seen. "Not reproduced in 15 attempts on 1.2.0 across ..." is right;
"not a bug", "invalid", "works as designed" and "fixed" are not, unless evidence is cited
(`fixed_in_candidate` needs a reproduction on the reported version).
