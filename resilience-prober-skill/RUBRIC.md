# Resilience prober rubric

`classify.py` computes effects and ratings; `validate_resilience.py` checks them.

## Effects

| Effect | Measured |
|---|---|
| `crash_b1` / `crash_b2` | Boundary fallback text (the defaults, "something went wrong", "please reload", Next.js's "application error: a client-side exception", plus the app's own `boundary_fallback_words`), a visible `boundary_selectors` element that wasn't in the baseline, or a page error not in the baseline with an empty app root whose baseline had text; B1 when the whole page has under 20 visible characters, no visible video, canvas or large image, and the baseline step had 20 or more |
| `blank_screen` | Under 20 visible characters, no visible video, canvas or large image (`media_visible`), no crash signal, and a baseline step with 20 or more. A step whose baseline was already under 20 gives a `baseline_blank` note instead |
| `chunk_failure` | A static asset failed and either the app root shows under 20 characters (where the baseline's had text) or the step did not complete |
| `persisted_failure` | After recovery the page is reloaded with the fault gone and the flow run to the step again: no request to the endpoint after the reload, and no good (2xx) answer from the server in the whole run, so the app replayed a failure it stored (Cache Storage, IndexedDB, localStorage). The reload's Cache Storage entries (URL, status, size) are recorded |
| `endless_loading` | A spinner (`role=progressbar`, `aria-busy`, known classes, the app's `spinner_selectors` or `loading_words`) still showing after 30 s, with no error |
| `silent_failure` | A failing fault (not a static asset: a kept screen is its expected outcome), no error element or new error words within 10 s, and either the step didn't complete, controls the baseline showed are gone (`controls_gone`), or it is a write; on a write it is rated Misleading |
| `misleading_state` | Empty-state wording that wasn't in the baseline and no error ("You have no parcels yet" after a 500), or a failing fault shown as done: the step's `done` (and none of its `done_not`) showed with no error, on a write, or on a read unless the step lists the endpoint in `optional_endpoints` |
| `lost_input` | A field's length or SHA-256 after the fault differs from what was typed (taken after the last fill, before the action that sends it; recorded as length and hash only) |
| `no_recovery` | After the fault is removed, the app's retry control or the step's actions (no reload) don't complete the step. A step whose only action is a navigation has recovery `reload`: not tried, a `recovery_not_tried` note, never recovered. A step that only types input (type-ahead) is recovered by re-sending the field's value (`refill_input`); a run that repeated such a step with nothing left to do is a `recovery_not_tried` note, never no_recovery |
| `retry_storm` | More than 10 requests to one URL (path and query, cache-busting parameters such as `_`, `t`, `cb` or a long number dropped) in the window, or requests to it sooner than `Retry-After` without growing gaps. A wildcard endpoint matching many different files once each, or type-ahead queries for different terms, is not a storm; requests the page aborted itself (a superseded query) don't count |
| `console_only` | New console errors and nothing visible: a note, never a finding |
| `document_fault` | The fault answered the page's own HTML document (a navigation request, or `resource_type: document`): no app code ran and the page shows the fault catalogue's injected body, so the run is a note, never an effect (the edge's or server's real error page is not tested) |

Only runs whose fault matched at least one request (`fault_triggered`) are classified. A scenario with
no such run is `not_triggered`: no effects, not counted as run, listed under faults not run.

The watch window: failing faults are watched until an error shows or the budget runs out (10 s, extended
to 30 s while a spinner or loading text shows); latency faults may end once the step is done with
nothing pending.

## Impact

| Impact | Weight | Effects |
|---|---|---|
| Fatal | 4 | crash_b1, crash_b2, blank_screen, persisted_failure |
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

`reproduced` when every run of the scenario triggered the fault and showed the effect (two runs by
default); `observed` when one did; `suspected` (code reading) is a note and never ranked.

## Headline

Priority counts, flows with a P0, scenarios run of planned, and every flow whose baseline failed named
in the summary. Overall confidence:

- `low`: fewer than half the planned scenarios ran, a critical-path flow's baseline didn't pass, or
  scenarios ran once (`--runs 1`);
- `medium`: otherwise, if any scenario didn't run (untested, errored, not triggered), any baseline didn't
  pass, a finding isn't reproduced, or something needs review;
- `high`: otherwise.

`confidence_caps` (`{cap: medium | low, reason}`, a reason of 20 characters or more) lower the computed
level for reasons the run cannot measure; they never raise it.
