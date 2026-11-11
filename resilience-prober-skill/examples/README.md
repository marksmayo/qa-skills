# Example: parcel-tracker

`parcel-tracker/` is a small single-page app (`site/`) with a stub API (`server.py`, loopback, port 8147)
that always answers correctly: every fault in the run is injected in the browser. `flows.json` has four
flows (track and address are critical paths) and 11 scenarios. `run/` is the real run (listed in
`run/commands.sh`): the plan with expected outcomes, `run.json` with two runs per scenario, screenshots,
`candidates.json` and a repro script per finding in `run/repros/`. `resilience_report.example.json` is
the report. Chromium 158, one desktop profile.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| The status fetch has no timeout | endless_loading when it hangs | F3, P0: spinner still showing after 30 s |
| A failed order list becomes an empty list | misleading_state | F5, P0: "You have no parcels yet" for a 500 and for a truncated body |
| (not planted) the status page doesn't check `r.ok` | | F4, P0: a 500 renders "undefined: undefined" and "Last scan: none yet" |
| The address form resets when saving fails | lost_input, silent failure on a write | F1 and F2, P0: 11 characters to 0, no message, for a 500 and for offline |
| Recommendations failure throws in the orders render | crash_b2 | F7, P1: the route boundary's "Something went wrong" replaces the orders |
| (follows from the above) no retry on the orders screen | no_recovery | F6, P0: the same #orders route doesn't re-render, so only a reload recovers |
| Notices retry every 200 ms, ignoring Retry-After | retry_storm | F9, P1: requests about 200 ms apart, each sooner than Retry-After: 5 |
| A failed help chunk clears the screen | chunk_failure | F8, P1 |
| Decoy: optimistic "Marked as received" that rolls back with a Try again button | No finding | S10 has no effect; the injected 500 is only a console note |
| Decoy: a font file that doesn't exist | Console note only | A 404 console note in every baseline; the text falls back to Arial |

The notices banner left empty while its endpoint fails was dismissed: it is optional, and the retry
storm is the defect on that endpoint. The 3 s slow status response (S03) showed the spinner and then the
status: no finding.

```bash
python scripts/validate_resilience.py examples/resilience_report.example.json --run examples/run/run.json --scenarios examples/run/scenarios.json --candidates examples/run/candidates.json
python scripts/render_report.py examples/resilience_report.example.json
```
