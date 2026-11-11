# Example: harbour-shop 2.4.0, the first seven hours

`hypercare.json` watches harbour-shop 2.4.0, deployed at 2026-11-11T22:00Z, with four Datadog queries
(checkout error rate, search p95 latency, nightly export job errors, orders placed) and the Sentry project
`harbour-shop-web`. Ticks are hourly.

`recorded/` is **simulated** monitoring data, made by `simulate.py` with a fixed seed: 15 days of
five-minute points per query, and the Sentry issue list as it would read at each hourly tick, in the shapes
the Datadog `/api/v1/query` and Sentry project issues APIs return. No real account was used. `state/` is
the watch after seven ticks (23:00 to 05:00), made by `commands.sh`. The headline, limitations and
suggested actions are the agent's.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| Checkout error rate from about 0.4% to about 2% after the deploy | suspected, then confirmed; correlated; SEV2 (an error rate on a critical service) | suspected at tick 1, confirmed at tick 2 (robust z about 70 to 85); correlated: the hour before the deploy was normal |
| A new TypeError in the cart, first seen in 2.4.0 | confirmed at once; correlated; in the checkout draft | HARBOUR-SHOP-WEB-2F confirmed at tick 1; merged into INC-DRAFT-checkout |
| Search p95 high from 00:00 to 01:00 only | suspected, then normal: no draft | suspected at tick 3, normal at tick 4; no draft |
| Decoy: nightly export errors at 02:00, every night | normal (the same hour has them in the baseline) | normal at tick 5 (53 errors against an expected 49) |
| Decoy: an old payment timeout issue at its usual rate | normal (not new, no jump) | HARBOUR-SHOP-WEB-1A normal |
| Orders placed | normal | normal (Poisson p above 0.001 in every tick) |

There is one draft, `state/incidents/INC-DRAFT-checkout.md` (SEV2, status draft, for the checkout team's
on-call engineer). It was not raised anywhere.

```bash
python scripts/validate_watch.py examples/state --recorded examples/recorded
python scripts/render_report.py examples/state
```

Run these from `hypercare-anomaly-watcher-skill/`. `commands.sh` regenerates the simulation and the ticks,
which gives the same files. The agent's notes are then added again.
