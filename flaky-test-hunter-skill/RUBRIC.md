# Flaky test rubric

Anchors for test status, rates, causes, confidence and ranking. `SKILL.md` says when to apply
them, and `scripts/validate_flaky_report.py` enforces the rules marked **(checked)**.

## Test status (checked, computed from the runs)

Only runs whose `code_hash` matches the report's count. Failed and error results both count as
fails; skipped results are left out.

| Status | When |
|---|---|
| `flaky` | At least one pass and one fail |
| `consistently_failing` | Failed in every run: a real failure, reported separately |
| `not_observed_flaky` | Passed in every run. Report the count and the upper bound: 0/20 is 0–16.1% |
| `not_run` | No runs on this code |

## Rates

Every rate is `fails/runs` with a 95% Wilson score interval (`scripts/stats.py`).

- **Normal rate:** over the `baseline` and `repeat` labels, the configuration CI normally uses.
- **Worst rate:** the label with the highest Wilson lower bound, leaving out `bisect` runs. A
  flake that only shows under load or in another time zone still happens in real CI, so ranking
  uses the worst rate.

| Wilson lower bound of the worst rate | Rate factor |
|---|---|
| ≥ 20% | 4 |
| 5–20% | 3 |
| 1–5% | 2 |
| < 1% | 1 |

## Causes and their experiments

| Cause | Typical evidence | Experiment that confirms it | Usual fix |
|---|---|---|---|
| `async_wait` | Fixed sleeps, polling with no deadline, a missing `await` | Fails under `--cpu-load`, passes without it | Wait on the condition (Event, join with timeout, a waiting assertion) |
| `shared_state` | Module globals, singletons, class attributes, a shared row | A polluter pair | Reset the state in a fixture, or inject it |
| `order_dependence` | Passes alone, fails after a specific test | A polluter pair, or reverse order fails and isolation passes | Make each test set up what it needs |
| `external_dependency` | Real HTTP, DNS, outside sandboxes | Blocking the network fails it every time | Stub or record and replay |
| `time_and_locale` | `today()`, `now()`, DST and month ends, locale formatting | One TZ, clock date or locale passes every run, another fails (`tz`, `clock` and `locale` variations) | Freeze the clock, or compare in one zone |
| `randomness` | Unseeded random or Faker, set and dict ordering | One seed fails every run, another passes | Seed it, sort it, or assert on properties |
| `resource_contention` | Fixed ports, shared temp files, locks | Fails only with parallel workers | Port 0, `tmp_path`, per-worker resources |
| `product_race` | The race is in product code | Reproduces outside the test, or the stack is all product code | Report it; don't patch the product |
| `unknown` | Fails, and nothing changes it | None found: list every variation tried | Keep collecting runs |

## Confidence (checked)

| Confidence | Needs |
|---|---|
| `confirmed` | An experiment that turns the failure off and on (below) |
| `indicated` | The failure message or the variation fits the cause, but no off/on pair yet |
| `suspected` | Reading the code only: agent judgment. Capped at P2 |

The off/on experiment is one of:

- a label with 3 or more runs that all pass, and another label with 2 or more failures, among the
  experiment's runs; or
- a polluter pair that fails while the victim alone passes, both from the bisection's record.

An `unknown` cause is always `suspected`.

## Ranking (checked)

`score = rate factor × gate × masking`

| Factor | Values |
|---|---|
| Gate | `required` (the check blocks merging, as the user confirmed) 3, `ci` 2, `local` 1 |
| Masking | 2 when the test covers a critical journey (in `critical_tests`) or the cause is `product_race`; otherwise 1 |

| Band | Score |
|---|---|
| P0 | ≥ 18 |
| P1 | ≥ 9 |
| P2 | ≥ 4 |
| P3 | < 4 |

Floors and caps:

- a `product_race` is at least P1;
- a `suspected` cause is at most P2;
- a history-only flake (seen in CI, never reproduced here) is at most P2.

Examples:

- A shuffled-order flake failing 4/4 on a required check: rate 4 (lower bound 51%) × gate 3 = 12, P1.
- The same on a critical journey: 24, P0.
- A load-only flake at 2/5 on a CI check: Wilson low 11.8% gives rate 3, × gate 2 = 6, P2.

## Fixes (checked)

- Never a retry or rerun (`@pytest.mark.flaky`, `--reruns`, `retryTimes`, Playwright `retries`,
  `test-retry`, `@RepeatedTest`).
- The diff names only test files, fixtures (`conftest.py`) or test config.
- An applied fix has at least the budget's number of verification runs, and its files are in
  `files_changed`. An unapplied fix has none.
- A quarantine is a proposal (`applied: false`), with an owner and a ticket.

## Evidence completeness

`evidence_completeness` = variations ran ÷ variations ran or not run. Not applicable variations
(no e2e tests, so no latency variation) are left out. A variation that used a tool must have
that tool recorded as ran **(checked)**. A `clock` variation that ran must name runs that moved
the clock (`TDA_CLOCK_SHIFT`, `TDA_CLOCK_DATE` or `FAKETIME` in the run's environment)
**(checked)**; record clock runs under `clock`, never `tz`.

A smell links to a flake on the same test function, whichever parametrised case the flake names
**(checked)**.

## Wording (checked)

Never "not flaky", "flake-free", "no flaky tests", or "the suite is stable/reliable/
deterministic". Say "not observed flaky in N runs (upper bound X%)".

## Reviewer checklist

- [ ] Every run recorded by `run_tests.py`, on one code hash.
- [ ] Consistently failing tests separated from flakes.
- [ ] Each flake: cause, confidence, the off/on experiment or why there isn't one, a fix to test code.
- [ ] Every static smell accounted for.
- [ ] Every variation and tool recorded, with reasons for what didn't run.
- [ ] No retries proposed; no product code changed; nothing committed.
