# Ferry booking (example package)

A small Python package with a pytest suite and planted flakes, so the flaky test hunter example
has something real to find. Don't copy its tests.

```bash
python -m pytest -q                 # needs pytest; the parallel runs also need pytest-xdist
sh ../run/commands.sh               # from flaky-test-hunter-skill/: regenerates examples/run/
```

## Answer key

| Test | Planted | Cause | How it is confirmed |
|---|---|---|---|
| `test_fares::test_cache_starts_empty` | `fare()` fills a module-level `_CACHE`; this test assumes it is empty and passes only when it runs before any quote | `shared_state` | Reverse and shuffled order fail; bisection finds a `test_quotes` polluter, which then the victim fails while the victim alone passes |
| `test_schedule::test_departs_today` | Compares the UTC date of a departure with the local `date.today()` | `time_and_locale` | Passes every run with `TZ=UTC`, fails every run at UTC+14. In normal runs it fails whenever the local date differs from the UTC date (in New Zealand, every morning) |
| `test_seats::test_family_of_four_gets_two_rows` | Asserts on `list(set)` order of seat codes | `randomness` | `PYTHONHASHSEED=0` fails every run, `PYTHONHASHSEED=1` passes every run |
| `test_notifier::test_confirmation_is_sent` | Sleeps 50 ms for work that takes about 30 ms on another thread | `async_wait` | Passes without load, fails with 20 busy sibling processes |
| `test_status_server::test_health_endpoint_answers` and `..._status_code` | Both bind port 8765 | `resource_contention` | Pass serially; with two xdist workers one of them fails with "address already in use" every run |
| `test_refunds::test_one_third_refund_rounds_to_nearest_cent` | `refund()` rounds up (`ROUND_CEILING`); a real bug | none | Fails every run under every variation: `consistently_failing`, not flaky |
| `test_notifier::test_confirmation_after_flush` | Sleeps 0.5 s, far longer than the work | none | Passes every run: `not_observed_flaky`, and the fixed-sleep smell is listed as not observed |

The status-server tests also sleep 0.4 s and the CI workflow runs `pytest -n auto`, which is why
the fixed port matters there.
