# DRAFT SEV2: checkout: HARBOUR-SHOP-WEB-2F, checkout error rate after 2.4.0

*Draft for On-call engineer, checkout team to review and raise. Not raised, paged or posted anywhere.*

- Service: checkout
- Release: harbour-shop 2.4.0
- Started: 2026-11-11T22:00:00Z (first anomalous interval)
- Detected: 2026-11-11T23:00:00Z

## Signals

- **HARBOUR-SHOP-WEB-2F** (sentry_issue): HARBOUR-SHOP-WEB-2F "TypeError: Cannot read properties of undefined (reading 'lineItems')" at cart/summary.js in renderTotals: 342 events, 93 users since 2026-11-11T22:06:41Z (https://sentry.example.test/organizations/harbour/issues/4100021/)
- **checkout error rate** (metric): 0.020799 against a median of 0.00396 (robust z 85.05, threshold 4)

## Timeline

- 2026-11-11T23:00:00Z tick 1: draft created
- 2026-11-11T23:00:00Z tick 1: HARBOUR-SHOP-WEB-2F confirmed (first seen in 2.4.0 after the deploy)
- 2026-11-12T00:00:00Z tick 2: checkout error rate confirmed (began after the deploy, and the hour before it was normal)
- 2026-11-12T00:00:00Z tick 2: severity SEV3 -> SEV2

## Suggested actions

- Check cart/summary.js renderTotals in 2.4.0 for a cart with no lineItems (empty or restored carts), using the Sentry issue's events.
- Compare checkout errors by endpoint in Datadog to see whether the TypeError explains the whole error-rate rise.
- Decide with the release manager whether to roll back 2.4.0 or ship a fix; the draft does not decide.
