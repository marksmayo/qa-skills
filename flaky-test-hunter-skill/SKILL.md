---
name: flaky-test-hunter
description: Find tests that pass and fail on the same code, show why with experiments, and propose a fix for each. Reruns the suite under controlled variations (repeats, reversed and shuffled order, isolation, hash seeds, parallel workers, time zone, clock, locale, CPU load), bisects order-dependent failures to their polluter, reports every rate as failures/runs with a 95% Wilson interval, separates flakes from tests that simply fail, and ranks flakes by rate, CI gate and what they hide. Never calls a test or suite "not flaky"; never proposes a retry as the fix; never changes product code.
version: 1.2.0
---

# Flaky test hunter

## Mission

Find the tests that sometimes pass and sometimes fail on identical code, prove what makes each one
fail, and hand over a fix that removes the cause. The evidence is real runs: how many, under which
variation, with which result.

How this differs from the other skills here:

- **test-suite-evaluator** grades a whole suite and treats flakiness as one signal. This skill
  produces per-test run counts, rates and a demonstrated cause.
- **ticket-to-tdd** and **journey-to-tests** rerun a failing test once and may hand you a
  suspected flake. Start from the tests they name.
- **ui-crash-hunter** looks for product crashes. When a flake is a race in the product, report it
  as `product_race` and hand it over; don't patch the product.

Output:

1. `flaky_report.json`, conforming to `schemas/flaky_report.schema.json`.
2. `runs.json` (every run, from `run_tests.py`), `smells.json` and, for order-dependent flakes,
   `bisect.json`, kept with the report as its evidence.
3. Proposed fixes as diffs in the report, applied to test code only if the user asked.
4. A rendered summary for the user.

## Non-negotiable rules

- **Flaky means a pass and a fail on the same code.** Every run records a `code_hash` of the
  source files; only runs with the report's `code_hash` count. A test that fails in every run is
  `consistently_failing`: a real failure to report, not a flake.
- **Never "not flaky".** A test with no failures is `not_observed_flaky` with its run count and
  the upper bound of its interval (0/20 is 0–16.1%). Never write "stable", "flake-free" or "no
  flaky tests"; the validator rejects them.
- **Every rate has its counts and a 95% Wilson interval.** `2/20 (2.8%–30.1%)`, never "10%".
- **A cause is `confirmed` only by an experiment that turns the failure off and on:**
  - a label with 3 or more runs that all pass, and another with 2 or more failures; or
  - a polluter pair that fails while the victim alone passes; or
  - an enabler pair that passes while the test alone fails (it depends on an earlier test).

  A failure message that fits a cause is `indicated`. Reading the code alone is `suspected`
  (agent judgment), and suspected flakes are capped at P2.
- **A retry is never the fix.** `@pytest.mark.flaky`, `--reruns`, Jest `retryTimes`, Playwright
  `retries`, Gradle `test-retry` and the like hide the flake. You may propose a quarantine (skip
  with an owner and a ticket) as a stopgap, but never apply it.
- **Product code is never changed.** Fixes touch test code, fixtures and test config only
  (`vitest.config.*`, `jest.config.*`, setup files, `conftest.py`, `pytest.ini`, the pytest
  section of `pyproject.toml`; RUBRIC.md lists them).
- **Fixes are proposed, not applied, by default.** With `apply_fixes: true`, apply the test-code
  change and prove it with a fresh run of the full budget under the variation that failed; list
  every changed file. Never commit.
- **Record every tool and variation** as ran, not run or not applicable, with the reason. A
  missing plugin is `not_run`, never assumed.
- **An errored run ran nothing.** `run_tests.py` marks a run `errored` when the process crashed,
  timed out, pytest exited 2–5 (collection error, internal error, unknown id, nothing collected)
  or no JUnit was written. Its cases never count, and a variation whose runs all errored is
  `not_run` with the error as the reason; the validator enforces both.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Targets | whole suite | Paths, node ids or a tag; or tests named by another skill or by CI history |
| Budget | 20 repeats, 60-minute cap | `--minutes` changes the cap; stopping early is recorded |
| Variations | all that apply | order, isolate, seed, parallel, tz, clock, locale, cpu_load, latency (e2e) |
| History | none | JUnit XML from past CI runs. Use `gh run download` only if the user allows it |
| Gate | `ci` if a CI workflow runs the tests, else `local` | `required` only when the user says the check blocks merging |
| Critical tests | none | Tests covering P0/P1 journeys (from journey-mapper), which double the ranking |
| Apply fixes | false | |

## Step 1: Recon

```bash
python scripts/detect_conventions.py <repo>                      # framework, run command, CI
python scripts/scan_pitfalls.py <repo> --out <scratch>/smells.json
```

Note which variations the runner supports and which plugins are installed (pytest-xdist,
pytest-randomly). Don't install anything without asking; a variation you can't apply is
`not_run` with the reason.

CI configuration is the source of truth for how the suites run: `detect_conventions.py` reads
GitHub Actions, GitLab and the rest, and Azure DevOps pipelines and templates under any name
(`pipelines/*.yml`, `.azure-pipelines/`, templates with `steps:` and `- script:`). The gate is
`ci` when one of them runs the tests. Look there, and in the Makefile, for the environment the
tests need: a variable a Makefile `export`s or a pipeline sets (`DATABASE_URL`, a storage
connection string) must be given with `--env`, or the first run is a collection error. Give fake
loopback values, never real credentials; `runs.json` records only their names.

**Monorepos.** When packages run in separate virtual environments (as CI often does), run each
suite with its own interpreter: start `run_tests.py` itself with that environment's Python
(`<venv>/bin/python scripts/run_tests.py ...`), one `--cwd` and label prefix per package. Runs of
different packages can share one `runs.json` and one report as long as each records the same
`code_hash`; give every package's runs the same `--cwd` (the repo root, with `--tests <package
path>`) or widen the hash with `--hash-path <sibling package>` so the hash covers the code they
share. pytest may take its rootdir from an ini file above or below `--cwd`; `run_tests.py` maps the
collected ids back to `--cwd` and says so in the run's notes. A test directory that two suites run
(a shared package under two services) is pooled across both: say so. Files that can't be collected
(a missing private dependency or submodule) are left out of `--tests`, listed in `limitations`,
and their tests are not counted as run.

## Step 2: Baseline and repeats

Use `run_tests.py` for every run so each is recorded the same way. Give every batch a label;
`baseline` and `repeat` are the "normal" configuration the report's normal rate is based on.

```bash
R="python scripts/run_tests.py --preset pytest --cwd <repo> --out <scratch>/runs.json"
$R --label baseline --run-prefix R
$R --tests <each baseline failure> --repeat 3 --label confirm_failing --run-prefix F
$R --repeat 20 --minutes 60 --label repeat --run-prefix N        # suspects only, for a slow suite
```

Suspects are tests that failed once, tests history says flaked, and tests with a static smell. For
a slow suite, repeat only the suspects and say how many tests were never repeated.

Use the repo's own test command, not just the preset's default. If `npm test` sets `TZ=...`, adds
`--configLoader native` or other flags (`detect_conventions.py` shows the resolved script), use
`--npm-script test` or pass the same with `--runner-args` and `--env`, or tests that depend on them fail every run and look like real
failures. Say in `metadata.run_command` what you ran.

Keep populations apart: a suspect-only repeat runs far fewer files than the full suite, so the
machine is less loaded. Label full-suite runs `baseline` / `repeat` and suspect-only repeats
something else (`repeat_suspects`), so the normal rate stays one configuration. A suite that is
green serially but red with the default workers is itself a finding: run the suspects serially
(`--workers 1`, 3+ runs) as the off side of a `resource_contention` experiment.

## Step 3: Variations

Run each variation over the suspects it applies to, once at first, then repeat the ones that
change an outcome until the experiment is conclusive (3+ passing runs one way, 2+ failures the
other). Typical commands:

| Variation | Command |
|---|---|
| Order | `--order reverse`, `--order shuffle --seed 11` (pytest reorders explicit ids; Jest, Vitest and Go use their own shuffle flags) |
| Isolation | `--isolate` (each test in its own process) |
| Seed | `--hash-seed 0`, `--hash-seed 1` (PYTHONHASHSEED); framework seeds through `--env` |
| Parallel | `--workers 2` on the suspects (needs pytest-xdist for pytest); the same tests serially as the "off" side |
| Time zone (`tz`) | `--tz UTC`, `--tz Pacific/Kiritimati`, `--tz America/St_Johns` (IANA names) |
| Clock (`clock`) | Python: `--env PYTHONPATH=<path to plugins/> "PYTEST_ADDOPTS=-p clock_shift" TDA_CLOCK_DATE=2026-11-10` (or `TDA_CLOCK_SHIFT=-1d`); Node: `--env "NODE_OPTIONS=--import=<file URL of plugins/clock_shift.mjs>" TDA_CLOCK_SHIFT=-1d`; against the same tests without it |
| Locale | `--locale tr_TR.UTF-8`, `--locale de_DE.UTF-8` |
| Load | `--cpu-load 20` (busy sibling processes) against the same tests without load |

The clock variation moves the date and time the tests see, which a time zone can't do on its
own: a date stamped on one day and checked the next, a month end, 29 February. Prefer the
repo's own clock (freezegun, time-machine, fake timers, an injected `Clock`); otherwise use
`plugins/clock_shift.py`, a pytest plugin that moves Python's `date`, `datetime` and `time`;
`plugins/clock_shift.mjs`, a Node preload that moves `Date` (it works in Vitest's thread and fork
pools, since workers inherit `NODE_OPTIONS`; tests that call `vi.useFakeTimers` replace it with
their own clock); or libfaketime (`FAKETIME`) for whole processes. Give `PYTHONPATH` relative to
`--cwd`, so no home path is recorded, and keep the plugin in its own directory so nothing else on
that path shadows the repo's modules. Record these runs under the `clock` variation, not `tz`;
the validator checks a clock variation's runs set `TDA_CLOCK_SHIFT`, `TDA_CLOCK_DATE` or `FAKETIME`.
`clock_shift.py` gives compiled modules (pandas, numpy, any Cython extension) the real datetime
classes while they load, so they import safely after it, but their own clocks
(`pandas.Timestamp.now()`) are not moved: say so when the code under test reads time through
them. `python plugins/clock_shift.py --self-test`, run with the repo's interpreter, checks the
plugin there first.

Time zones: give IANA names. Node reads them on every platform; Python on Windows can't, so
`run_tests.py` turns them into fixed offsets for Python runs only, and the run's notes say so.
Locales (`LANG`, `LC_ALL`) are ignored by Node's `Intl` and by most runtimes on Windows: if a
locale run could not change anything, record the variation `not_run` with that reason rather
than counting a run that varied nothing.

### Vitest and Jest

- **Pool.** Vitest runs test files in worker threads (`pool: 'threads'`, the default before
  Vitest 2) or child processes (`'forks'`, the default since). In a worker thread, assigning
  `process.env.TZ` does not change the time zone, so a setup file that pins `TZ` is a no-op under
  threads: `scan_pitfalls.py` reports it as `tz-in-thread-setup`. Confirm it with a `tz`
  experiment (the repo's own zone against two others); the fix is test config: set `TZ` in the
  test script or at the top of `vitest.config.*` (which runs in the main process before workers
  start), or switch to `forks`.
- **Order and isolation.** `--order shuffle --seed N` is `--sequence.shuffle --sequence.seed N`;
  Vitest has no reverse order. Vitest isolates each file by default (`isolate: true`), so order
  effects show between tests in one file, or across files only with `isolate: false` or
  `--no-isolate`; check the config before reading a shuffle result. `--isolate` here runs each
  given test (or file) in its own process.
- **Ids.** Vitest and Jest JUnit ids are `file::Describe > Inner > title`. `run_tests.py --tests`
  takes these ids and reruns each as its file plus a `-t` name pattern (a bare id passed to
  Vitest would be read as a file filter).
- **Timeouts under load.** A test that times out only with many workers (passes serially, fails
  with the default pool or under `--cpu-load`) is `resource_contention` (or `async_wait` if it
  waits on time). Fix the work, not the timer: warm heavy imports in `beforeAll` with its own
  hook timeout, type with `userEvent.setup({ delay: null })`, or give the file a timeout that
  matches its real work (`vi.setConfig({ testTimeout })`); never a retry.

## Step 4: Polluters and enablers

Order dependence comes in two shapes:

- **Polluted:** the test passes alone and fails after some earlier test (which left state behind).
- **Enabled:** the test fails alone, and passes only because an earlier test ran first (it
  reloaded a module with other settings, filled a cache, created a row). It looks fine in the
  given order and fails reversed or isolated.

```bash
python scripts/bisect_polluter.py --runs <scratch>/runs.json --from-run <failing run> --victim <case id> \
    --cwd <repo> --out <scratch>/bisect.json                       # polluted: start from a failing run
python scripts/bisect_polluter.py --runs <scratch>/runs.json --from-run <passing run> --victim <case id> \
    --cwd <repo> --relation enabler --out <scratch>/bisect-enabler.json   # enabled: from a passing run
```

A `minimal_pair` names the test: for a polluter the pair fails and the victim alone passes; for
an enabler the pair passes and the test alone fails. Record it in the flake's `polluter` with
`relation` (`polluter` or `enabler`) and the id the bisection gives (`polluter` in its output is
the case id as `runs.json` writes it). `needs_several`, `fails_alone`, `passes_alone` or
`errored` mean there is no single test to name; say so. One polluter often explains several
flakes: a test that reloads a module without reloading it back pollutes the tests that need the
original settings and enables the ones that need the changed ones. Fix the polluter first, then
check the enabled tests fail consistently (they were relying on it) and fix them too. Pass every
bisect file to the validator (`--bisect a.json b.json`).

`scan_pitfalls.py` points at this shape: `module-reload` flags `importlib.reload` in a test, and
`env-read-at-import` the module-level `os.getenv` reads in the module it reloads.

## Step 5: Classify and propose fixes

For each flaky test, write one flake entry:

- **Cause:** `async_wait`, `shared_state`, `order_dependence`, `external_dependency`,
  `time_and_locale`, `randomness`, `resource_contention`, `product_race` or `unknown`.
- **Confidence and experiment:** the experiment's run ids, what changed and what happened, and
  the confidence that follows.
- **Evidence:** a passing and a failing run id.
- **Failure excerpts.**
- **Fix:** a unified diff of test code with a rationale. Leave the scores to the validator.

RUBRIC.md has the causes, their experiments and the usual fixes. Account for every static smell:

- `linked_to_flake` when it caused one. The smell must sit in the flake's test: same file, and
  the same name. A Python smell names the test function, so it links to a flake on any of its
  parametrised cases (`test_x[case]`); a Vitest/Jest smell names the describe chain and title
  (`checkout > applies a code`), matching the JUnit id `file::checkout > applies a code`. A smell
  with no test (a setup file, product code) may link to the flake it explains;
- `linked_to_failure` when it sits in a consistently failing test;
- `not_observed` when its test never failed. Say how many runs, so nobody reads it as "safe".

A smell with a `mitigation` (Math.random stubbed, fake timers in the file or a setup file) is a
weaker lead: check the mitigation covers the test and say so in the note. Smells are line
patterns, so some are false leads; keep them `not_observed` with a note saying why.
`wall-clock-today` is not raised for a clock read that only fills a keyword argument or a dict
value (`created_at=datetime.now()`) whose name nothing in the file compares or asserts on.

List consistently failing tests separately, with what they show.

## Step 6: Validate and report

```bash
python scripts/validate_flaky_report.py <scratch>/flaky_report.json --runs <scratch>/runs.json \
    --smells <scratch>/smells.json --bisect <scratch>/bisect.json --repo <repo> --write-summary
python scripts/render_report.py <scratch>/flaky_report.json [--brief]
```

`--brief` lists only the flaky and consistently failing tests and summarises the rest in one line.

`--write-summary` computes every test's counts, intervals and status from the runs, each flake's
score and priority, and the summary counts. An "upper bound N%" in the headline must be a Wilson
upper bound of the runs (`python scripts/stats.py 0 21` gives 0/21); the validator checks it. `--repo` checks that every proposed fix applies cleanly to the repository's files, so generate
diffs from real edits (`git diff`, `difflib`) rather than writing them by hand. Fix every error
rather than editing those by hand.
Show the rendered report:

1. the headline;
2. the ranked flakes, each with its cause and the experiment;
3. the consistently failing tests, which are real failures to fix first;
4. what wasn't run, and why.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, code hash, framework, run command, budget, gate and its evidence, critical tests, platform |
| `tools[]`, `variations[]` | Each with ran, not run or not applicable and the reason; variations name their run labels |
| `tests[]` | Per test: runs, passes, fails, the normal rate and the worst label with Wilson bounds, and status (computed) |
| `flakes[]` | Cause, confidence, experiment, evidence runs, polluter or enabler (`polluter.relation`), excerpts, ranking, fix and optional quarantine |
| `consistently_failing[]` | Tests that failed every run, with what they show |
| `static_smells[]` | Every smell from `scan_pitfalls.py` and what became of it |
| `summary` | Headline, counts, causes, priorities, total runs, minutes and evidence completeness |
| `limitations[]`, `files_changed[]` | What the runs couldn't show; files changed by applied fixes |
