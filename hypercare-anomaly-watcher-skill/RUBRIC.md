# Rubric: hypercare anomaly watcher

## Per tick, per metric

The interval is the last `tick_minutes` before the tick. The baseline is the same interval on each of the
`baseline_days` days before, using only intervals that ended before the deploy.

| Kind | Value | Score | Anomalous when |
|---|---|---|---|
| `rate`, `latency`, `gauge` | mean of the interval's points | robust z = (value - median) / max(1.4826 x MAD, `rel_floor` x median, `abs_floor`) | z >= `threshold_z` (4) in the query's direction, and the change >= `min_abs_change` |
| `count` | sum of the interval's points | Poisson tail P(X >= value) (direction up) or P(X <= value) (down), expected = baseline mean | p < `p_value` (0.001), and the change >= `min_abs_change` |

Fewer than 7 baseline samples, no points, or a failed fetch: `not_assessed`.

## Sentry

- **New issue** (first release is the release, or first seen after the deploy): anomalous when its users
  >= `min_users` (5) or events >= `min_events` (20). Confirmed at once.
- **Existing issue**: events in the interval against its hourly rate before the deploy (Poisson, p <
  `regression_p_value`, change >= `regression_min_abs`).

## States

| From | Anomalous | Normal |
|---|---|---|
| normal, recovered | suspected | (stays) |
| suspected | confirmed | normal |
| confirmed | confirmed | recovering |
| recovering | confirmed | recovered |

`not_assessed` leaves the state unchanged.

## Correlation with the release

Decided at the first anomalous tick: `yes` if the interval began after the deploy and the hour before the
deploy was normal (for a new Sentry issue: first seen in the release); `no` if it began before the deploy
or the hour before was already anomalous; `unknown` if that hour couldn't be assessed.

## Drafts

One draft per service, for confirmed items correlated `yes`. SEV2 when an `is_error_rate` query on a
critical service is in it, or a new Sentry issue in it has at least `sev2_users` users; otherwise SEV3.
Later ticks update the signals and the timeline (confirmations, severity changes, recoveries). Status is
always `draft`.
