# Resilience prober rubric

`classify.py` computes effects and ratings; `validate_resilience.py` checks them.

## Effects

| Effect | Measured |
|---|---|
| `crash_b1` / `crash_b2` | Boundary fallback text ("something went wrong", "please reload") or a page error with an empty app root; B1 when the whole page has under 20 visible characters |
| `blank_screen` | Under 20 visible characters and no crash signal |
| `chunk_failure` | A static asset failed and the app root shows under 20 characters |
| `endless_loading` | A spinner (`role=progressbar`, `aria-busy`, known classes) still showing after 30 s, with no error |
| `silent_failure` | A failing fault, and no error element or new error words within 10 s; on a write it is rated Misleading |
| `misleading_state` | Empty-state wording that wasn't in the baseline and no error ("You have no parcels yet" after a 500), or a failed write shown as done |
| `lost_input` | A field's length or SHA-256 after the fault differs from before (recorded as length and hash only) |
| `no_recovery` | After the fault is removed, the app's retry control or the step's actions (no reload) don't complete the step |
| `retry_storm` | More than 10 requests to the endpoint in the window, or requests sooner than `Retry-After` without growing gaps |
| `console_only` | New console errors and nothing visible: a note, never a finding |

## Impact

| Impact | Weight | Effects |
|---|---|---|
| Fatal | 4 | crash_b1, crash_b2, blank_screen |
| Stuck | 3 | endless_loading, lost_input, no_recovery, chunk_failure |
| Misleading | 3 | misleading_state, silent_failure on a write |
| Friction | 2 | silent_failure on a read, retry_storm |

## Likelihood

Latency, server errors, timeouts, offline and static assets 3; rate limits, auth expiry, connection
drops, partial failure and flapping 2; malformed payloads 1. A merged candidate takes its most likely fault.

## Reach

A step in a critical-path flow 3; any other flow step 2; a step reachable only by an uncommon route 1.
With a HAR, an endpoint carrying 20% or more of requests raises reach by one, under 1% lowers it by
one (within 1 to 3).

## Priority

`score = impact weight × likelihood × reach`. P0 ≥ 27, P1 ≥ 18, P2 ≥ 9, otherwise P3.

- Lost input or a misleading result on a write is at least P1.
- A retry storm against a 429 or 503 is at least P2.
- A finding not reproduced on a second run is capped at P2; a P0 must be reproduced.

## Confidence

`reproduced` when every run of the scenario showed the effect (two runs by default); `observed` when one
did; `suspected` (code reading) is a note and never ranked.

## Headline

Priority counts, flows with a P0, scenarios run of planned. Overall confidence is high only when every
planned scenario ran, every baseline passed, every finding is reproduced and nothing needs review.
