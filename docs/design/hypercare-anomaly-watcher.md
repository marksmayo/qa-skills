# Design: `hypercare-anomaly-watcher-skill`

Status: Built (v1.0.0); see "As built" at the end. Build order: 29.

## Mission

For the hypercare window after a release (typically 24 to 72 hours), the skill watches production
metrics and errors on a schedule. It flags what changed against a statistical baseline, correlates the
change with the release, and drafts incidents for a person to raise. It never raises them itself.

- **Metrics from Datadog**: error rate, latency percentiles, throughput, anything a query returns.
- **Errors from Sentry**: new issues first seen in the release, and existing issues whose rate jumped.
- **Ongoing**: each scheduled check is a *tick* that appends to a watch state; anomalies must persist
  across ticks before they count, and recoveries are recorded.

How it differs from the other skills:

- **post-deploy-validator** (28) checks the release minutes after the deploy, actively, with requests.
  This skill watches passively for days, from monitoring data.
- **ci-failure-triager** looks at test failures before release; **bug-reproducer** reproduces a reported
  bug. An incident draft from this skill can become the bug report the reproducer takes.

## Decisions

Agreed with the user (see `decisions-26-32.md`):

- **Sources: Datadog and Sentry**, read-only through their APIs (`DD_API_KEY` and `DD_APP_KEY`;
  `SENTRY_AUTH_TOKEN`). A recorded directory of responses works offline.
- **Ongoing watching**: `watch.py tick` runs on a schedule (cron, a CI schedule, or an agent's scheduler)
  for the window; each tick is recorded with the inputs it used.
- **Method: statistics against a baseline**: the same hour of the day over the days before the release
  (a robust z-score from the median and MAD for rates and latencies; a Poisson tail probability for
  counts), with a minimum absolute change, and persistence across ticks.
- **Drafts incidents only.** Never pages, never creates incidents, monitors, issues or comments in
  Datadog, Sentry, PagerDuty or a tracker.

## Non-negotiable rules

- **Read-only**: GET requests to the monitoring APIs only.
- **An anomaly needs its statistics**: the value, the baseline (median, MAD or rate, number of samples),
  the score and the threshold, and the query that produced them. Without enough baseline samples (fewer
  than 7) the metric is `not_assessed`, never "normal".
- **Persistence before an incident**: a metric anomalous in one tick is `suspected`; two in a row is
  `confirmed`. A new Sentry issue is confirmed at once only when it reaches the user threshold.
- **Correlation is stated, not assumed**: an anomaly is "correlated with the release" only when it began
  after the deploy and the hour before the deploy was normal; otherwise "not correlated" or "unknown".
- **Seasonality is respected**: the baseline compares the same hour of the day, so a nightly batch spike
  present every day is not an anomaly.
- **Drafts only**, with status `draft`, for a named person to raise. Updated, never duplicated, on later
  ticks; recoveries noted, never auto-closed.
- **No "all clear"**: a quiet tick says "no anomalies above the thresholds in the metrics watched",
  listing the metrics not assessed.
- **Secrets never output**; error event payloads are not copied (titles, culprits, counts and links only).

## Inputs

| Input | Default | Notes |
|---|---|---|
| `config` | required | `hypercare.json`: release (version, deploy time, service), window, tick interval, sources and queries, thresholds |
| `window_hours` | 72 | |
| `tick_minutes` | 60 | Each tick evaluates the last interval |
| `baseline_days` | 14 | Same hour of the day on each of the days before the release |
| `threshold_z`, `p_value`, `min_abs_change` | 4, 0.001, per query | |
| `persistence_ticks` | 2 | |
| `incident_owner` | required | Who raises a drafted incident |

## Workflow

```bash
python scripts/watch.py init --config hypercare.json --state state/
python scripts/watch.py tick --state state/ [--now 2026-11-12T10:00:00Z] [--from-dir recorded/]   # on a schedule
python scripts/validate_watch.py state/
python scripts/render_report.py state/ [--brief]
```

1. **Init**: check the config, record the release and the schedule, and write the schedule line for cron
   or a CI schedule (the skill never installs it).
2. **Tick**: fetch the interval and the baseline windows for each query, and Sentry issues for the
   release; evaluate each metric; update anomaly states (`normal`, `suspected`, `confirmed`, `recovering`,
   `recovered`, `not_assessed`); create or update incident drafts for confirmed anomalies; write the tick.
3. **Report**: the current state, the timeline of ticks, and the drafts; the agent adds context and
   suggested actions to each draft.
4. **Stop** at the end of the window; a last tick summarises.

## Rating

| Draft severity | When |
|---|---|
| SEV2 | a confirmed error-rate anomaly on a critical service, or a new release issue affecting at least `sev2_users` users |
| SEV3 | any other confirmed anomaly correlated with the release |
| none | suspected only, not correlated, or not assessed |

## Output (state directory)

```text
watch.json      config, release, schedule, ticks[] summaries, anomaly states, drafts
ticks/<n>.json  per tick: the inputs (current values, baseline samples), each evaluation, requests made
incidents/<id>.md and .json   the drafts (status draft), updated on later ticks
```

## Validator fails when

- A tick's evaluation can't be recomputed from its stored inputs (median, MAD, z, p, the decision).
- An anomaly is confirmed without persistence, or a metric with too few baseline samples isn't `not_assessed`.
- A draft exists without a confirmed anomaly, or a confirmed correlated anomaly has no draft.
- A draft is anything but `draft`, or a request other than GET was made.
- An anomaly is called correlated with the release without starting after the deploy and a normal hour before it.
- Text says "all clear", "stable" or "healthy"; a secret or an event payload appears.

## Edge cases

- **Low traffic**: rates over few requests are noisy; a query can set `min_volume`, below which the tick
  is `not_assessed`.
- **Several releases in the window**: each anomaly is correlated with the latest deploy before it.
- **Monitoring outage**: a failed fetch is `not_assessed` for that tick, never normal.
- **Weekly seasonality**: the default baseline (same hour, previous days) doesn't model weekdays; a query
  can set `baseline: same_hour_same_weekday` with enough weeks of data.

## Files

```text
hypercare-anomaly-watcher-skill/
  SKILL.md  RUBRIC.md  README.md
  schemas/hypercare_config.schema.json  watch_state.schema.json
  scripts/sources.py  detect.py  watch.py  validate_watch.py  render_report.py
  examples/
```

## Example

`examples/recorded/` holds Datadog query responses and Sentry issue lists in the APIs' shapes, simulated
from a fixed seed by `examples/simulate.py` (no real services). There are 15 days of five-minute points
around a release of "harbour-shop" 2.4.0, and six hourly ticks after it. Planted:

- the checkout error rate rises after the release and stays up (confirmed, correlated, SEV2);
- a new Sentry issue first seen in 2.4.0 (a TypeError in the cart) affecting many users (merged into the
  same checkout draft);
- search p95 latency spikes in one tick and recovers (suspected, then normal: no draft);
- decoys: a nightly batch spike every day at 02:00 (in the baseline: normal), and an old Sentry issue
  with a steady rate (not new).

## Resolved questions

Agreed with the user before the build (`decisions-26-32.md`).

1. **Sources?** Datadog and Sentry.
2. **Snapshot or ongoing?** Ongoing.
3. **Method?** Statistics against a baseline.
4. **Incidents?** Drafted only.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **The example's monitoring data is simulated** (`examples/simulate.py`, fixed seed) in the Datadog and
  Sentry response shapes, because there is no real account behind the sample. The live clients in
  `sources.py` are written but not exercised by the example.
- **Each tick makes one Datadog query per metric** covering the baseline days and the interval, then
  slices locally, rather than one query per baseline day.
- **`min_volume` and the `same_hour_same_weekday` baseline are not built.** The baseline is the same hour
  on each of the previous days, and the limitations say so.
- **A new Sentry issue skips `suspected`.** It is confirmed as soon as it reaches the user or event
  threshold, and goes to `recovering` if it later drops below it.
- **Drafts are one per service**, merging the metric and the Sentry issue for checkout in the example.
  `render_report.py --write-drafts` rewrites the Markdown after the agent adds suggested actions.
- **Example results:** after seven hourly ticks there is one SEV2 draft for checkout (the error rate,
  confirmed at tick 2, and the new TypeError, confirmed at tick 1). The search latency spike was
  suspected for one tick, then normal, with no draft. The nightly export burst and the old payment
  issue stayed normal.
- **Testing:** 19 of 19 seeded violations are caught, including a confirmation without persistence, a
  correlation without a normal hour before the deploy, a draft marked raised, a POST to Datadog, an API
  key, and a copied event payload.
