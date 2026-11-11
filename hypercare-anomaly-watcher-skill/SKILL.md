---
name: hypercare-anomaly-watcher
description: Watch production for the hypercare window after a release - Datadog metrics and Sentry issues, read-only, on a schedule (one tick per run) - and flag what changed against a statistical baseline (the same hour on the days before the release; robust z-scores for rates and latencies, Poisson tails for counts, a minimum change, persistence across ticks), correlate each anomaly with the release, and draft incidents for a named person to raise. Never pages, never creates incidents, monitors, issues or comments anywhere. Seasonal spikes in the baseline are normal; too little baseline is not_assessed, never normal; a quiet tick is never "all clear".
version: 1.0.0
---

# Hypercare anomaly watcher

## Mission

For the 24 to 72 hours after a release, check production on a schedule and say what changed, whether it
started with the release, and what a person should raise:

- **Datadog**: any metric a query returns (error rates, latency percentiles, throughput, job errors).
- **Sentry**: new issues first seen in the release, and old issues whose rate jumped.
- **Ongoing**: each scheduled run is a tick; anomalies must persist; drafts are updated, not duplicated;
  recoveries are noted.

post-deploy-validator checks actively minutes after the deploy; this skill watches passively for days. A
draft can become a bug-reproducer's input.

## Non-negotiable rules

1. **Read-only**: GET requests to the monitoring APIs only, with `DD_API_KEY`/`DD_APP_KEY` and
   `SENTRY_AUTH_TOKEN` from the environment. Keys and tokens are never printed or written.
2. **Every anomaly carries its statistics**: value, baseline samples, median and MAD (or expected count),
   score, threshold and the query.
3. **Not enough baseline is `not_assessed`** (fewer than 7 samples, no points, a failed fetch), never normal.
4. **Persistence**: one anomalous tick is `suspected`; two in a row `confirmed`. A new Sentry issue is
   confirmed as soon as it reaches the user or event threshold.
5. **Correlation is earned**: "correlated with the release" only if the anomaly began after the deploy and
   the hour before the deploy was normal (or the issue was first seen in the release).
6. **Seasonality**: the baseline is the same hour of the day on previous days, so a nightly spike that
   happens every night is normal.
7. **Drafts only**, `status: draft`, with an owner who raises them. Never page, never create or comment on
   incidents, monitors or issues. Recoveries are noted; drafts are never closed by the skill.
8. **No "all clear"**: say "no anomalies above the thresholds in the metrics watched", and list what wasn't
   assessed.
9. **No event payloads**: Sentry titles, culprits, counts and links only.
10. **The schedule is the user's**: `init` prints a cron line; the skill never installs it.

## Inputs

`hypercare.json` (`schemas/hypercare_config.schema.json`): the release (service, version, deploy time),
`window_hours` (72), `tick_minutes` (60), `baseline_days` (14), `incident_owner`, `critical_services`, the
Datadog queries (id, query, kind `rate`/`latency`/`gauge`/`count`, service, direction, thresholds) and the
Sentry project (org, project, service, thresholds).

## Workflow

```bash
python scripts/watch.py init --config hypercare.json --state state/
python scripts/watch.py tick --state state/                # on the schedule; --now and --from-dir for replays
python scripts/render_report.py state/ [--brief]
python scripts/validate_watch.py state/
```

1. **Init**: check the config; record the window; give the user the schedule line to install.
2. **Each tick** (or when asked "how is the release doing?"): run `tick`; read the changed states; for a new
   or updated draft, add `suggested_actions` and notes (what to look at, who decides on a rollback), then
   `render_report.py --write-drafts`.
3. **Report**: the headline (what's confirmed, what recovered, what isn't assessed), the drafts, and the
   limitations. Hand drafts to the owner; never raise them.
4. **End**: after the window, a last summary.

## Rating

See `RUBRIC.md`. Draft severity: SEV2 for a confirmed error-rate anomaly on a critical service or a new
release issue affecting at least `sev2_users` users; SEV3 for other confirmed, correlated anomalies.

## Output

`state/watch.json` (`schemas/watch_state.schema.json`): config and hash, window, schedule, tick summaries,
item states (with history and correlation), drafts, summary, limitations. `state/ticks/<n>.json`: each
tick's inputs, evaluations and requests. `state/incidents/<id>.json` and `.md`: the drafts.

## Edge cases

- **Low traffic**: rates over few requests are noisy; watch the count instead, or raise `min_abs_change` (a `min_volume` option is not built in v1).
- **Another deploy in the window**: start a new watch for it; correlate each anomaly with the latest deploy.
- **Monitoring outage**: `not_assessed` for that tick.
- **Weekly patterns**: the default baseline ignores weekdays; say so when it matters.
