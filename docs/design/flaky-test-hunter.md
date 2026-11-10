# Design: `flaky-test-hunter-skill`

Status: Built (v1.0.0), see "As built" at the end. Build order: 8.

## Mission

The skill finds tests that pass and fail on the same code, works out why, and proposes a fix for
each one. It reruns the suite under controlled variations (repeats, random order, seeds,
parallelism, time zone, locale, added latency), separates real flakes from tests that simply
fail, and tracks each flake back to a cause it can show with an experiment.

How it differs from the other skills:

- **test-suite-evaluator** grades the whole suite and mentions flakiness as one signal among many.
  This skill produces per-test evidence: run counts, flake rates and a demonstrated cause.
- **ticket-to-tdd** and **journey-to-tests** rerun a failing test once and report pre-existing
  flakes. They can hand those tests to this skill.
- **ui-crash-hunter** looks for product crashes. When a flake turns out to be a real race in the
  product rather than in the test, this skill reports it as a suspected product bug, and
  ui-crash-hunter or a developer takes it from there.

## Decisions

- **Default budget is 20 repeats of the selected tests,** plus one run of each variation, under a
  60-minute wall-time cap. When the cap is reached the skill stops and records how many runs
  completed. The user can change both. Whole-suite repeats of a slow suite are never the default:
  the skill first runs the suite once, then repeats only suspect tests (failed once, history says
  flaky, or matches a pitfall pattern).
- **Fixes are proposed, not applied, by default.** With `apply_fixes: true` the skill edits test
  code only (never product code), and proves each fix with a fresh run of the same budget.
- **No commits, no Jira write-back.** Changes stay uncommitted and are listed in the report.
- **Retry plugins are never a fix.** Adding `@flaky`, `pytest-rerunfailures`, Jest `retryTimes`,
  Playwright `retries` or Gradle `test-retry` hides the problem, so the skill never proposes it as
  the fix. It may propose a quarantine with an owner and a ticket as a stopgap, but never applies one.

## Non-negotiable rules

- **"Flaky" needs both a pass and a fail on identical code and identical inputs to the variation.**
  The commit, the dependency lock and the command are recorded for every run. A test that fails on
  every run is `consistently_failing`, not flaky.
- **No "not flaky" and no "flake-free".** A test with no failures in N runs is
  `not_observed_flaky (0/N)` with an upper bound on its flake rate (95%, roughly 3/N). The report
  says what was run, never that the suite is stable.
- **Every flake rate carries its counts and a 95% Wilson interval.** "Flaky 2/20 (2.8%–30%)", never
  "10% flaky" alone.
- **A cause is `confirmed` only by an experiment that turns the failure on or off:** a seed that
  reproduces it, an order that reproduces it while isolation passes, a frozen clock that removes
  it. A failure signature matching a cause is `indicated`, and reading the code alone is
  `suspected` (agent judgment, never high confidence).
- **Tools that didn't run are recorded** as `not_run` or `errored` with the reason, and lower the
  evidence-completeness figure.
- **Product code is never changed.** A flake caused by a product race is reported, not patched.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | |
| `targets` | whole suite | Test paths, node ids or a tag; or `--from-history` to pick tests that failed in CI |
| `budget` | 20 repeats, 60-minute cap | Per suspect test; `--minutes` changes the cap, and runs completed are recorded |
| `variations` | all applicable | `order`, `seed`, `parallel`, `tz`, `locale`, `latency`, `cpu` |
| `history` | optional | JUnit XML from past CI runs (a directory the user provides, or fetched with `gh run download` only when the user allows it) |
| `apply_fixes` | false | |

## Workflow

1. **Recon.** `detect_conventions.py` finds the framework, run command, parallel runner and
   random-order support. `scan_pitfalls.py` lists static flake smells: fixed `sleep`, wall-clock
   reads in assertions, unseeded random and Faker, module-level mutable state, real network calls,
   fixed ports, shared temp paths, `setTimeout` in tests, missing `await`.
2. **Baseline.** One full run with JUnit output. Failing tests are rerun 3 times to separate
   consistent failures from flakes.
3. **History** (if given). Parse past JUnit results with `parse_results.py`. A test that both
   passed and failed on the same commit in CI is a flake candidate, with the CI evidence attached.
4. **Repeat.** `run_tests.py` reruns suspects `budget` times in their normal order, recording
   pass or fail, duration and the failure message for each run.
5. **Variations,** one run each over the suspect set and their neighbours, then repeated for the
   variations that change the outcome:

   | Variation | How |
   |---|---|
   | Order | Reverse and shuffled order with a recorded seed (`pytest-randomly`/`-p random_order`, `jest --randomize --seed`, `vitest --sequence.shuffle`, JUnit 5 `MethodOrderer.Random`, `go test -shuffle`, `rspec --order rand`) |
   | Isolation | The test alone in a fresh process |
   | Seed | Fixed and varied `PYTHONHASHSEED`, framework seeds, Faker seeds |
   | Parallel | Serial vs the repo's parallel runner at 2× CPU workers (`pytest -n`, Jest workers, Gradle forks, `go test -p`, Playwright workers) |
   | Time | `TZ=Pacific/Kiritimati`, `TZ=America/St_Johns`, `TZ=UTC`; a run near midnight and month end via `faketime` or the repo's own clock-freezing library |
   | Locale | `LANG=tr_TR.UTF-8` (the dotted-i problem), `de_DE` (comma decimals) |
   | Latency and load | Network latency via the browser harness for e2e tests; a CPU-hog sibling process for timing-sensitive tests |

6. **Polluter search.** For order-dependent failures, bisect the tests that ran before the victim
   to find the polluter (the approach of `detect-test-pollution`, with a stdlib bisect fallback).
   The result is a minimal pair: polluter then victim fails, victim alone passes.
7. **Classify** each flake (below), with the experiment that confirms the cause.
8. **Propose fixes.** For each flake, write the test-code change, why it removes the cause, and the
   run that will prove it. With `apply_fixes`, apply and rerun the same budget and variations; a
   fix counts only if the failure no longer appears and the test still fails when its subject is
   broken (one neutralisation run, borrowed from dead-test-finder).
9. **Rank, report, validate and render.**

## Cause classes

| Cause | Typical evidence | Confirming experiment |
|---|---|---|
| `async_wait` | Fixed sleeps, polling without a deadline, missing `await`, e2e waits on time not state | Added CPU load or latency raises the failure rate; replacing the sleep with a condition wait removes it |
| `shared_state` | Module globals, class attributes, singletons, a shared database row, cached env | A polluter pair from bisection |
| `order_dependence` | Passes alone, fails after a specific test | Polluter pair; fixed order reproduces |
| `external_dependency` | Real HTTP, DNS, clock servers, third-party sandboxes | Blocking the network makes it fail every time |
| `time_and_locale` | `now()` in assertions, date arithmetic over midnight or DST, locale formatting | A TZ, faked-clock or locale run fails every time |
| `randomness` | Unseeded random, Faker, UUID ordering, hash ordering of sets and dicts | A recorded seed reproduces the failure every time |
| `resource_contention` | Fixed ports, shared temp files, file locks, too few connections in a pool | Fails under the parallel variation only |
| `product_race` | The race is in product code, not the test | The failure reproduces outside the test harness, or the stack is entirely product code |
| `unknown` | Failure seen, no experiment changes it | Listed with every run and every variation tried |

## Ranking

`score = rate x gate x masking`

| Factor | Values |
|---|---|
| Rate (Wilson lower bound) | ≥ 20% = 4, 5–20% = 3, 1–5% = 2, < 1% = 1 |
| Gate | Required check before merge = 3, runs in CI but not required = 2, local only = 1 |
| Masking | 2 when the test covers a critical journey (journey-mapper priority P0 or P1) or the cause is `product_race`; otherwise 1 |

Bands: P0 ≥ 18, P1 ≥ 9, P2 ≥ 4, otherwise P3. Floors and caps: `product_race` is at least P1;
a flake whose cause is only `suspected` is capped at P2; history-only flakes (never reproduced
locally) are capped at P2 and say so.

## Output (`flaky_report.json`)

```text
metadata:     repo, commit, lockfile hash, framework, run command, tool versions, budget,
              generated_at
tools[]:      {name, version, status: ran|not_run|errored, reason}
runs[]:       shared run format + variation {kind, value, seed}
tests[]:      {id, file, line, runs, passes, fails, errors, rate, wilson_low, wilson_high,
               status: flaky|consistently_failing|not_observed_flaky|not_run}
flakes[]:     {id, test_id, cause, cause_confidence: confirmed|indicated|suspected,
               experiment{run_ids, what_changed, result}, polluter?, failure_excerpts[],
               fix{diff, rationale, applied, verification_run_ids}, score, priority}
static_smells[]: {file, line, pattern, test_ids}   # not flakes until a run shows it
summary:      {tests_run, suspects, flaky, consistently_failing, not_observed, by_cause{},
               by_priority{}, evidence_completeness, total_runs, wall_minutes}
limitations[], files_changed[]
```

## Validator fails when

- A test is `flaky` without at least one pass and one fail on the same commit and command.
- A rate has no counts or no interval, or the interval doesn't match the counts.
- Any text claims the suite or a test is "not flaky", "stable" or "flake-free".
- A cause is `confirmed` without a run that toggles the failure.
- A retry or rerun plugin is proposed as the fix.
- An applied fix has no verification runs at the original budget, or the fix edits product code.
- A tool is used in the evidence but recorded `not_run`.

## Edge cases

- **Slow suites.** The baseline runs once; repeats cover suspects only, and the report says how
  many tests were never repeated.
- **Tests that need services.** Docker Compose or Testcontainers if present; otherwise those tests
  are `not_run`, never assumed stable.
- **E2E suites** (Playwright, Cypress, Detox, Maestro, XCUITest with `-test-iterations`, Espresso
  with the Android Test Orchestrator). Latency and CPU variations matter most here, and each run
  keeps its trace or video as evidence.
- **Failures that change every run** (different messages each time) are grouped by normalised
  failure signature before rates are computed.
- **CI-only flakes** that never reproduce locally stay in the report as history-only, with the
  environment difference listed (CPU count, OS, container).

## Tool stack

All open source and optional except the test runner itself. The stdlib scripts (`run_tests.py`,
the bisect and the Wilson interval) always work.

| Purpose | Tools |
|---|---|
| Random order | pytest-randomly, pytest-random-order, Jest/Vitest built-ins, JUnit 5, `go test -shuffle`, RSpec |
| Parallel | pytest-xdist, the repo's own runner settings |
| Clock | libfaketime, freezegun/time-machine (if already used), `TZ`/`LANG` env |
| Polluter search | detect-test-pollution (MIT) or the built-in bisect |
| CI history | `gh` CLI for artifacts (optional) |

## Files

```text
flaky-test-hunter-skill/
  SKILL.md  RUBRIC.md  README.md
  data/pitfalls.json                 # static flake smells per language, with cause and fix
  schemas/flaky_report.schema.json
  scripts/scan_pitfalls.py  bisect_polluter.py  stats.py   # Wilson interval, rate bands
          validate_flaky_report.py  render_report.py
          run_tests.py  parse_results.py  detect_conventions.py   # shared copies
  examples/ferry-booking/  run/  flaky_report.example.json
```

## Example

`examples/ferry-booking/` is a small Python package with a pytest suite and these planted flakes
(the answer key lives in its README):

| Planted | Cause | Expected result |
|---|---|---|
| A fare cache in a module global that one test fills and another assumes empty | `shared_state` | Confirmed by a polluter pair |
| A "departs today" test using local date against a UTC timestamp | `time_and_locale` | Confirmed by `TZ=Pacific/Kiritimati` |
| A seat allocator test asserting on `set` ordering of strings | `randomness` | Confirmed by `PYTHONHASHSEED` |
| A worker thread test with `sleep(0.05)` before the assertion | `async_wait` | Confirmed under CPU load |
| Two tests binding port 8765 | `resource_contention` | Confirmed under xdist only |
| A test that always fails (a real bug) | — | `consistently_failing`, not flaky |
| A sleep-based test that never failed in 20 runs | — | `not_observed_flaky (0/20)`, static smell listed |

The runs in `examples/run/` are real pytest output.

## Resolved questions

Agreed with the user before the build.

1. **Default: 20 repeats or a time box?** 20 repeats, with a time cap that stops early and records the runs completed.
2. **Offer quarantine as a stopgap?** Yes, as a proposal only (skip with an owner and a ticket); the skill never applies it.
3. **Fetch CI history with `gh`?** Only when the user provides JUnit files or allows `gh`.

## As built (v1.0.0)

- **Shared files.** `run_tests.py` and `stats.py` are new in `shared/`, alongside
  `parse_results.py` and `detect_conventions.py`.
- **`run_tests.py`:**
  - Has presets for pytest, Jest, Vitest, Playwright and gotestsum, plus a `custom` preset with
    `{junit}`, `{tests}`, `{seed}` and `{n}` placeholders.
  - pytest order variations reorder explicit node ids, so no plugin is needed. pytest-randomly
    is disabled when present, so the order is the recorded one.
  - A variation a preset can't apply is an error, never silently skipped.
  - Every run records a `code_hash` of the source tree, the variation, its duration, and
    whether it timed out.
  - The timeout kills the whole process tree.
  - `--check-tree` exits 3 if the working tree changed.
  - Recorded commands show `python` for the interpreter and JUnit paths relative to
    `runs.json`.
- **Windows time zones:** the C runtime only reads `XXX-hh:mm` (a three-letter name, no DST
  rules). IANA names map to fixed standard offsets (`Pacific/Kiritimati` → `LIN-14`), and the
  run notes say so. The first example run used `LINT-14`, which the runtime silently treated as
  UTC; it was regenerated.
- **Status and rates are computed, not written.** `validate_flaky_report.py --runs` rebuilds
  `tests[]` from the runs:
  - the normal rate covers the `baseline` and `repeat` labels;
  - the worst rate is the label with the highest Wilson lower bound, leaving out `bisect`.

  `--write-summary` writes those, plus each flake's score and priority and the summary.
- **Confirmed** means one of:
  - a polluter pair (the pair fails and the victim alone passes); or
  - among the experiment's runs, one label with 3 or more runs that all pass and another with 2
    or more failures.
- **Fix diffs must apply.** `--repo` applies each unapplied fix's hunks to the files with a
  stdlib checker. The example's first, hand-written diffs had wrong hunk counts and were caught
  this way; the example now generates its diffs with `difflib`. Each fix was also applied to a
  scratch copy and the failing variation rerun, and every fixed test passed (noted in the
  report's limitations).
- **Example results** (87 real runs on Windows, about 4.5 minutes):
  - 6 flakes, all confirmed: the notifier at P0, because it is a critical test, and five at P1;
  - one consistently failing test (the refund rounding bug);
  - 9 tests not observed flaky in 21 normal runs, with an upper bound of 15.5%.

  The time-zone test failed every normal run because the runs were made while the local NZ date
  was ahead of the UTC date.
- **Validator:** 37 of 37 seeded violations are caught.
