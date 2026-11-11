# Example: tidewater, CI run 109

`make_repo.py` builds "tidewater", a small ferry-fares package, with a fixed history on `main`:

| Commit | Subject | Planted |
|---|---|---|
| `1e537ac` | Fares, bookings and boarding slots | the last green run (101); the slot test is already flaky |
| `613509c` | Use banker's rounding for fares | a regression: half-price 24.25 prints 12.12, not 12.13 |
| `902084e` | Add sample bookings fixture | a data problem: a booking sailing on 2026-11-10 |
| `5a2cbd7` | Docs: describe fare rounding | decoy: docs only, mentions rounding |

`recorded/github/` is GitHub Actions run 109 on `5a2cbd7`, with two attempts: the failure and a re-run of
the failed jobs. `recorded/azure/` is Azure DevOps build 2207 of the same commit, with no re-run.
`record_example_run.py` made both from real local runs, on 2026-11-12: pytest at the failing commit,
repeated until the flaky test failed for attempt 1 and passed for attempt 2, and pip against an index on a
closed loopback port. The envelopes (ids, timestamps, job and step lists) follow each API's response
shapes; no CI system was called. `run/` and `run-azure/` are the normalised runs, listed in `commands.sh`.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| Rounding regression (`test_fare_rounds_half_up`) | real, owned by `613509c`, defect drafted | X02 real, confirmed (failed again on re-run); owning change `613509c` through the imported `tidewater/fares.py`; P1 |
| Fixture booking dated 2026-11-10 (`test_sample_booking_is_open`) | data, test-data issue drafted | X01 data, indicated (`DATA-EXPIRED`, and a date before the run date); owned by `902084e` (the fixture and the test) |
| Two random slots with replacement (`test_pick_two_gives_two_different_slots`) | flaky | X03 flaky, confirmed: failed in attempt 1 and passed in attempt 2 on the same commit; owning change not identified (no commit in range touches it); hand to flaky-test-hunter |
| Package index unreachable (install-check job) | environment | X04 environment, indicated (`ENV-REGISTRY` on log line 10); it failed again on re-run, so not `confirmed`; owning change not identified |
| Docs-only commit `5a2cbd7` | never the owning change | not picked: it shares no file with any failure |

`drafts.md` holds the defect and the test-data issue, ready for a person to file.

**Without the re-run** (`run-azure/`, the same failures from Azure DevOps), the flaky slot test is
`real, indicated` (an assertion failure, not re-run), and so is the rounding test. Only the re-run
separates them. That is why the skill asks to re-run failed jobs.

```bash
python scripts/validate_triage.py examples/triage.example.json --run examples/run
python scripts/render_report.py examples/triage.example.json
```

Run these from `ci-failure-triager-skill/`. Running `commands.sh` (which needs `TW`, an empty directory
for the sample repository) gives the same files. Only `record_example_run.py` makes new runs: those change
the flaky test's picks and the timings, and invalidate the triage until it is rebuilt.
