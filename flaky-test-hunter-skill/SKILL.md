---
name: flaky-test-hunter
description: Find tests that pass and fail on the same code, show why with experiments, and propose a fix for each. Reruns the suite under controlled variations (repeats, reversed and shuffled order, isolation, hash seeds, parallel workers, time zone, clock, locale, CPU load), bisects order-dependent failures to their polluter, reports every rate as failures/runs with a 95% Wilson interval, separates flakes from tests that simply fail, and ranks flakes by rate, CI gate and what they hide. Never calls a test or suite "not flaky"; never proposes a retry as the fix; never changes product code.
version: 1.7.0
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
python scripts/scan_pitfalls.py <repo> --out <scratch>/smells.json   # [--include-fixtures] [--exclude DIR]
```

Note which variations the runner supports and which plugins are installed (pytest-xdist,
pytest-randomly).

**What the scan reads.** Nested sample and fixture projects (a project with its own manifest,
app entry point or expected outputs under `examples/`, `fixtures/`, `testdata/`, `samples/` and the
like) are left out by default and listed under `fixtures_excluded`: their planted flakes are not
this repository's tests. `--include-fixtures` scans them, `--fixture-dir NAME|PATH` names another
fixture folder, and `--exclude DIR` (a relative path, or a folder name anywhere) leaves out anything
else, listed under `excluded`. Say what was left out in `limitations`. A file kept identical in
several folders (a shared helper copied into every package, a vendored file) is scanned once, on
its canonical copy, and its smells list the other paths under `copies` (`copies_scanned_once` lists
the groups): one smell, one entry in `static_smells`. Patterns match code only: text inside a
string, docstring or comment (a fixture source held in a string, a usage example) is never a smell.
Python module state (`module-global-state`) is reported only in modules a test imports, or in a
script that tests itself; a script no test imports shares nothing between tests.

Test code is what the test-name patterns name, what a pytest configuration collects below its own
folder by `python_files` (`benchmarks/pytest.ini` with `python_files = bench_*.py`), every
self-test script (`self_test.py`), and the body of a script's own `self_test()` function.
`smells.json` lists the self-test scripts under `self_tests` (with their commands and copies), and
`suites` lists them as a suite of their own (language `self-test`, preset `selftest`); see
**Script self-tests** below.

`scan_pitfalls.py` reads pytest's own collection settings (`testpaths`, `python_files` in
`pytest.ini`, `pyproject.toml`, `tox.ini` or `setup.cfg`): a root script named `*_test.py` that pytest
never collects gets only the source checks, and `smells.json` lists such files under
`pytest_collection.not_collected`. Python smells include an HTTP verb on a `requests`/`httpx`/`aiohttp`
session the file creates (`real-network-session`), an assertion on elapsed seconds
(`wall-time-assertion`: `assert took < 2.0`, a helper such as `check(took < 2.0, ...)` or
`expect(elapsed < 5)`, `self.assertLess(elapsed, 0.5)`, or a two-sided bound on a ratio of timings
such as `0.7 <= est / actual <= 1.6`; load-sensitive: confirm it with a `--cpu-load` variation), and a file
modification time read in a test file that also reads the wall clock (`mtime-vs-wall-clock`: two
clocks that disagree across midnight, a month end or a moved test clock). Don't install anything without asking; a variation you can't apply is
`not_run` with the reason.

CI configuration is the source of truth for how the suites run: `detect_conventions.py` reads
GitHub Actions, GitLab and the rest, and Azure DevOps pipelines and templates under any name
(`pipelines/*.yml`, `.azure-pipelines/`, templates with `steps:` and `- script:`). The gate is
`ci` when one of them runs the tests. Look there, and in the Makefile, for the environment the
tests need: a variable a Makefile `export`s or a pipeline sets (`DATABASE_URL`, a storage
connection string) must be given with `--env`, or the first run is a collection error. Give fake
loopback values, never real credentials; `runs.json` records only their names.

`detect_conventions.py` lists CI's test gates under `ci_gates` (`cargo llvm-cov`, `cargo test`,
`cargo nextest`, `coverage run -m unittest`, `pytest`, `go test`) and each Python area's runner
(`python_runner`). When CI runs Python tests with `unittest` (often `coverage run -m unittest
discover -s tests`), the `pytest` preset still runs those `TestCase` classes and gives per-test
JUnit: say in `metadata.run_command` that pytest stood in for unittest, set the `sys.path` the tests
expect if they add a folder to it themselves, and resolve `pytest-collects-helper` smells (a helper
pytest runs as a test and unittest never does) before comparing counts with CI.

**Monorepos.** When packages run in separate virtual environments (as CI often does), run each
suite with its own interpreter: start `run_tests.py` itself with that environment's Python
(`<venv>/bin/python scripts/run_tests.py ...`), one `--cwd` and label prefix per package. Runs of
different packages can share one `runs.json` and one report as long as each records the same
`code_hash`; give every package's runs the same `--cwd` (the repo root, with `--tests <package
path>`) or widen the hash with `--hash-path <sibling package>` so the hash covers the code they
share. pytest may take its rootdir from an ini file above or below `--cwd`; `run_tests.py` maps the
collected ids back to `--cwd` and says so in the run's notes. A test directory that two suites run
(a shared package under two services) is pooled across both: say so. In a JavaScript
workspace, `--npm-script` needs `--cwd` at the package (its `package.json`); add `--repo-root
<repo>` so the run still hashes the whole repository (one `code_hash` for every package) and
records Vitest, Jest and node:test ids and files relative to the repository
(`packages/web/tests/unit/cli.test.ts::...`), so two packages with the same test path never pool.
Without `--repo-root`, ids are package-relative: then write one report per package.

**node:test.** `--preset node-test` runs `node --test` with the JUnit reporter, applies
`--order shuffle --seed N` as `--test-randomize --test-random-seed=N` (recorded as such), and
records ids with their file. `--npm-script` works with it when the script is a `node --test`
command. Files that can't be collected
(a missing private dependency or submodule) are left out of `--tests`, listed in `limitations`,
and their tests are not counted as run.

**Suites per language.** `smells.json` lists every language the repository has tests in under
`suites`: test files (fixture projects left out, as the scan leaves them out), the CI gates that
run them, and a test count (Rust from the Cargo model in `cargo_project.py`: `#[test]` functions in
`#[cfg(test)]` modules and `tests/*.rs`, doc tests apart; other languages a static count of test
definitions), plus a `self-test` suite for script self-tests (`tests: null`: their checks are
printed, not declared). Copy each into `metadata.suites` (keep `language`, `tests`,
`count_source` and `presets`; the report schema takes no other inventory field) with `status` `ran` (its runs used one of
its `presets`) or `not_run` with the reason ("cargo was not installed", "out of budget").
Evidence completeness is multiplied by the share of tests in suites that ran, so a report that ran
only the Python half of a Rust and Python repository says so in its number, not only in a
limitation. A suite that ran with `tests: null` counts the distinct test ids its presets' runs
recorded (the self-test checks), so a repository whose tests are mostly self-tests is measured by
its checks. The validator checks every language the scan found is listed (pass `--smells`), and
that `ran` and `not_run` agree with the runs.

**Script self-tests.** Many tool repositories test themselves with scripts that print a line per
check (`ok name`, `FAIL name`, `name: ok`) instead of a test framework. Run them with
`--preset selftest`: `--tests scripts/a.py scripts/self_test.py ...` runs each as
`python <script> --self-test` (or `python <script>` for a `self_test.py`), and
`--selftest-cmd "python scripts/x.py --self-test --quick"` (repeatable) runs any other command.
Take the scripts and commands from `smells.json` `self_tests` (or `detect_conventions.py`
`self_tests`); run one copy of a file kept identical in several folders unless the copies import
different neighbours. Each check line becomes a case `<script>::<check name>`, with measurements
stripped from the name (`(took 0.6 s)`, `(got 98)`, `(ratio 0.70)`), so a check keeps one id
across runs and pools like any test; a script with no check lines is one `<script>::(exit status)`
case. `--repeat`, `--cpu-load`, `--tz`, `--env` and the clock variation work as for pytest. A
script runs whole: `--tests <script>::<check>` still runs every check of the script (the named
check must print a result, or it is listed under `tests_not_run`), so repeat a suspect by naming
its script, and narrow it only through the script's own selector if it has one
(`--selftest-cmd "python self_test.py --only budget"`). `--isolate`, `--order` and
`bisect_polluter.py` don't apply: checks inside one script share its process and run in its own
order; when only self-tests ran, record those variations `not_applicable` with that reason, and
otherwise say in the variation's reason that it did not reach the self-tests. A smell in a self-test script names
a function, while the cases name checks by message: the validator counts the smell's test as run
when any check of its script ran (so `not_observed`, with the run count, or linked), never
`not_run`.

**Rust / cargo.** Both presets give per-test results, so Rust suites run, repeat and rank like any
other:

- `--preset cargo` runs `cargo test --no-fail-fast` and parses libtest's own output per test (no
  extra tool). Ids are `<target>::<module path::name>`: the library's name for unit tests in
  `#[cfg(test)]` modules (`tidecalc::tests::height_is_linear`), the file stem for an integration
  test in `tests/` (`table::odd_nanos_pass`), `doctest:<crate>::...` for doc tests. A failing case
  carries its panic message and file; a test binary that dies mid-run is a `<target>::(crashed)`
  error case. A compile error, a missing target or a selection that matched nothing is an errored
  run that says so; a run whose tests ran is never errored.
- `--preset nextest` runs `cargo nextest run --no-fail-fast` with the default profile, or the one
  in `--runner-args "--profile ci"` (or `NEXTEST_PROFILE`); a profile must exist in
  `.config/nextest.toml`, so don't name `ci` unless the repository defines it. The JUnit a profile
  configures is read from the target directory (`CARGO_TARGET_DIR`, `.cargo/config.toml`'s
  `target-dir`, else `<workspace>/target`) only when this run wrote it; otherwise nextest's own
  output is parsed. A missing cargo-nextest is an errored run naming it: use `--preset cargo`.
- `--tests <id>` reruns chosen tests (`-p <crate> --lib|--bin|--test <target> -- --exact <path>`
  for cargo, one `-E` filterset for nextest); ids from two crates in one cargo run are refused, so
  use `--isolate` for those. `--workers N` is `--test-threads N`. `--runner-args` takes cargo
  options, and after ` -- ` libtest options (`"--runner-args=--features cli -- --include-ignored"`;
  write it with `=` so a value starting with `-` is not read as an option). Use the repository's CI
  command as the guide: `smells.json`'s `suites` lists its gates (`cargo llvm-cov --workspace ...`
  runs the same tests as `cargo test --workspace`, so pass `--workspace` and the features it uses).
- Build before the first counted run (`cargo test --no-run`, or one uncounted run): the first build
  writes `Cargo.lock` when the repository has none, which changes the `code_hash` between runs, and
  compile time lands in the first run's duration. Set `CARGO_TARGET_DIR` to a scratch directory
  when the repository's `target/` is shared or large; remove it afterwards.

Rust variation recipes (record each under its kind):

| Variation | Rust recipe |
|---|---|
| Repeat | `--repeat 20` (each run reuses the build) |
| Parallel (`parallel`) | `--workers 1` (one thread: `--test-threads=1`) against the default thread count. cargo runs a binary's tests as threads of one process, so a test that passes serially and fails threaded is `shared_state` (env, cwd, statics) or `resource_contention` (ports, files) |
| Order (`order`) | libtest shuffles only on nightly: `--env RUSTUP_TOOLCHAIN=nightly "--runner-args=-- -Z unstable-options --shuffle-seed 11"` (record the seed; the nightly build is a second build, so give it its own `CARGO_TARGET_DIR`). `--order` is refused for both presets, and nextest has no seeded shuffle. Without nightly, record order `not_run` ("libtest shuffle needs nightly") and lean on `--workers 1` against the default and `--isolate` |
| Isolation (`isolate`) | `--isolate --tests <ids>` with ids from a baseline run (each id in its own `cargo test` invocation, one process per test; cargo ids are not collected for you) |
| Load (`cpu_load`) | `--cpu-load N` against the same tests without it; timing assertions and sample counts over a fixed duration are the usual victims |
| Time zone (`tz`) | `--tz <IANA>` sets `TZ`. chrono's and time's local zone read `TZ` on Linux and macOS; on Windows they ask the system, so record `tz` `not_run` there for Rust |
| Clock (`clock`) | On Linux: libfaketime, `--env LD_PRELOAD=<path>/libfaketime.so.1 FAKETIME=-1d` (it moves `SystemTime::now()` and chrono's clock; build first, so cargo itself is not faked). Not available on Windows or macOS with SIP: record `not_run`. An injected clock in the code is better evidence: say if there is one |
| Locale | Rust's standard library ignores the locale; record `not_applicable` unless the code reads it through a crate |

`scan_pitfalls.py` has a Rust smell pack and reads unit tests in `#[cfg(test)]` modules as test
code: fixed sleeps, wall-clock and monotonic-clock reads, randomness (a seeded RNG is noted as a
mitigation), fixed ports, hardcoded `/tmp` paths and `env::temp_dir()`, process-global env/cwd
changes (`env::set_var`, `set_current_dir`: they leak across cargo's parallel test threads),
`static mut`, an assertion on elapsed time (`wall-time-assertion-rust`), a lower bound on a count
of samples taken over a fixed duration (`wall-time-count-assertion`, Python too), and a scratch
folder created and never removed in the file (`scratch-dir-leak-rust`: folders pile up across runs
under the temp or target directory, a `resource_contention` lead). A Rust smell names a function
only when it has a test attribute (`#[test]`, `#[tokio::test]`, `#[rstest]`); a smell in a helper
has no test. Not counted as leads: a clock read or `uuid4()` that only makes a unique folder or
file name and never reaches an assertion, a sleep inside a string passed to a child process, and
helper programs in test directories (a `main` and no tests, such as `tests/fixtures/*.rs`, listed
under `helper_programs`). `pytest-collects-helper` flags a module-level `def test_*` helper that
other code in the file calls: pytest collects it as a test while `unittest` does not, so the two
runners count different suites.

If `scan_pitfalls.py` prints a loud warning that the repo's dominant language has no smell pack,
treat it as "not scanned", not "clean": the tests of that language were skipped.

**Go.** Use `--preset go-test` (plain `go test -json`, no extra tool) or `--preset gotestsum`
(JUnit from gotestsum). Both run every package (`./...`) when no `--tests` are given, set
`-count=1` (results are never cached), and make a run whose package did not build or set up
`errored` (its `<import path>::(build failed)` case never counts as a failure or a flake: it is a
build failure on that platform).

- Ids are `<import path>::TestX` and `<import path>::TestX/sub`; each case's `file` is the
  `_test.go` file that defines the test (found through `go.mod`). A parent test is folded into its
  `t.Run` subtests, so one failing subtest counts once (a parent that failed on its own is kept).
- `--tests <id>` reruns one test: the id becomes its package plus `-run '^TestX$/^sub$'`. Ids from
  several packages in one run are refused; use `--isolate` (one run per id) for those.
- `-shuffle=N` is applied with `--order shuffle --seed N`. Race runs go through
  `--runner-args=-race` (write it with `=`: argparse reads a separate value that starts with `-` as
  an option and refuses it) and are their own variation (`race`, not `parallel`); `-race` needs cgo,
  so on a host without a C compiler it has to run elsewhere (record which platform). A wrapper such
  as `wsl.exe` or `sh -c` (`--preset custom`) re-parses arguments, so `|`, `(` and `\` in a `-run`
  pattern need quoting again or the run errors.
- `bisect_polluter.py` refuses Go (`go test -run` runs tests in source order). Use seeded shuffles
  (`-shuffle=N`, 3+ passes one way, 2+ failures the other) as the order experiment.
- The clock plugins don't load into a Go test binary (libfaketime doesn't reach it either), and
  neither do `net_guard.py`/`net_guard.cjs`. For the network and the home directory use
  `plugins/net_guard_proxy.py`: `python plugins/net_guard_proxy.py env --port <p> --home <scratch>/home`
  prints the `--env` pairs for `run_tests.py` (a refusing loopback proxy in `HTTP_PROXY`/`HTTPS_PROXY`,
  and `HOME`, `USERPROFILE`, `XDG_*`, `APPDATA` pointing into the scratch home), with
  `python plugins/net_guard_proxy.py serve --port <p> --log <scratch>/net_guard.log` running
  meanwhile; or wrap one command with its `run` mode. It guards only clients that honour the proxy
  variables (Go's default transport does; raw `net.Dial` and custom transports do not, and nothing
  is logged for them), so record network isolation as partial and grep the tests for `net.Dial`.
- `scan_pitfalls.py` has a Go pack: fixed sleeps, `time.Now` with no injected clock, draws from
  `math/rand`'s global source, fixed ports in `net.Listen`/`ListenAndServe`/`Addr:`/`net.Dial`,
  `os.Setenv`/`os.Chdir` (use `t.Setenv`/`t.Chdir`), `os.TempDir()` and `/tmp/` paths (use
  `t.TempDir()`), a package-level var written in a test that calls `t.Parallel()`, and a goroutine
  a test starts and never waits for. A Go smell links to a flake by package: the flake's import path
  is mapped to its directory through `go.mod` (pass `--repo`), and a smell in `TestX` links to a
  flake in `TestX/sub`.

**Tests that write outside the tree.** Before the first run, grep the tests for what they touch
on the host: writes under the home directory (`os.UserHomeDir`, `~`, `$HOME`, `Path.home()`,
`os.homedir()`), caches and downloads (a browser fetched on first use), and commands that
control devices or processes (`adb`, `xcrun simctl`, `emulator`, `pgrep`/`kill`, `docker`). Run
with a scratch home: set `HOME` **and** `USERPROFILE` (Windows reads the profile from
`USERPROFILE`, so setting only `HOME` still writes into the real profile), plus `XDG_CONFIG_HOME`
and `XDG_CACHE_HOME`, all to a directory outside the repository, and keep device and process tools
off `PATH` unless the user agreed to them. A test that needs one of those is `not_run` with the
reason. Toolchains find themselves through the home directory: with a scratch home, rustup finds no
default toolchain and cargo, Go and npm lose their caches, so tests that run them fail every run
for a reason that is not theirs. Pass the real locations along (`--env RUSTUP_HOME=<real
~/.rustup> CARGO_HOME=<real ~/.cargo> GOPATH=<real> GOMODCACHE=<real> npm_config_cache=<real>`;
`rustup show home` and `go env GOPATH GOMODCACHE` print them), and if a test still fails only in
the scratch home, record it as an environment failure in `consistently_failing` with that note,
not as a product failure. Check the tree after the runs too: a test that writes stray files into the source tree
changes the `code_hash` between runs (report it, and exclude only those files from the hash).

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

**Suites too big to run once in the budget.** When `--collect-only` shows far more cases than the
budget allows (a data-validation suite parametrised over thousands of data files, one case per file
and check), don't start a whole-suite baseline you cannot finish. Count cases per file
(`pytest --collect-only -q` and group by file), and separate the code tests from the data checks
(a parametrised test whose ids are data file names or records). Run the code tests in full, and the
data checks as a recorded sample: the same few files per data test, named with `--tests`. Record
the scope in `limitations` ("N of M collected cases run; the rest are per-file data checks of K
tests, sampled ...") and in `metadata.budget.stopped_early` when the plan was cut. Never call the
unrun cases not flaky: they are `not_run`.

Use the repo's own test command, not just the preset's default. If `npm test` sets `TZ=...`, adds
`--configLoader native` or other flags (`detect_conventions.py` shows the resolved script), use
`--npm-script test` or pass the same with `--runner-args` and `--env`, or tests that depend on them fail every run and look like real
failures. Say in `metadata.run_command` what you ran.

Keep populations apart: a suspect-only repeat runs far fewer files than the full suite, so the
machine is less loaded. Label full-suite runs `baseline` / `repeat` and suspect-only repeats
something else (`repeat_suspects`), so the normal rate stays one configuration. A suite that is
green serially but red with the default workers is itself a finding: run the suspects serially
(`--workers 1`, 3+ runs) as the off side of a `resource_contention` experiment. The computed normal
rate counts only `baseline` and `repeat` runs, so a test whose suspect-only repeats all passed can
show "normal 0/1": spell those runs out in the headline or the flake's experiment ("0/1 normal,
0/3 in suspect-only repeats, 2/3 under load").

## Step 3: Variations

Run each variation over the suspects it applies to, once at first, then repeat the ones that
change an outcome until the experiment is conclusive (3+ passing runs one way, 2+ failures the
other). Typical commands:

| Variation | Command |
|---|---|
| Order | `--order reverse`, `--order shuffle --seed 11` (pytest reorders explicit ids; Jest, Vitest and Go use their own shuffle flags; Rust: see the Rust recipes in Step 1) |
| Isolation | `--isolate` (each test in its own process) |
| Seed | `--hash-seed 0`, `--hash-seed 1` (PYTHONHASHSEED); framework seeds through `--env` |
| Parallel | `--workers 2` on the suspects (needs pytest-xdist for pytest); the same tests serially as the "off" side |
| Time zone (`tz`) | `--tz UTC`, `--tz Pacific/Kiritimati`, `--tz America/St_Johns` (IANA names) |
| Clock (`clock`) | Python: `--env PYTHONPATH=<path to plugins/> "PYTEST_ADDOPTS=-p clock_shift" TDA_CLOCK_DATE=2026-11-10` (or `TDA_CLOCK_SHIFT=-1d`); Node: `--env "NODE_OPTIONS=--import=<file URL of plugins/clock_shift.mjs>" TDA_CLOCK_SHIFT=-1d`; against the same tests without it |
| Locale | `--locale tr_TR.UTF-8`, `--locale de_DE.UTF-8` |
| Load | `--cpu-load 20` (busy sibling processes) against the same tests without load |
| Race (`race`) | Go `--runner-args=-race`, a sanitizer build elsewhere; against the same tests without it |
| Platform (`platform`) | The same code on a second OS, each run recording `platform` (below) |

**Two platforms, one code hash.** Runs of the same code on two operating systems can share one
`runs.json` only if every run records its `platform` (add `"platform": "linux"` / `"windows"` to
each run record; `run_tests.py` does not write it). The validator then works per platform: a test
that passes every run on one platform and fails every run on another is `platform_dependent`, a
deterministic portability failure listed in `consistently_failing` with `platforms` (the ones it
failed on), never a flake. Only a test that both passes and fails on one platform is flaky. Without
`platform` on the runs, keep each platform's runs in its own file and report.

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

`clock_shift.mjs` also moves the times `fs.stat` and its relatives report, so code that ages a
lock or cache file by `Date.now() - mtimeMs` stays consistent; `clock_shift.py` does not move
`os.stat` times. Child processes that don't load the preload or plugin, databases and other
services keep the real clock, so lock, cache or row ages computed against them look far in the
past or the future under a shifted clock. A test that fails only under the shift for that reason
shows skew sensitivity between two clocks, not a date bug: say which clocks disagree in the
flake's experiment. Such a shift makes the failure certain (3/3), so the worst rate ranks the
flake far above what the real race (a write just before midnight) gives in normal runs: say so
in the flake's `experiment.result`, quote the normal rate beside it, and propose a fix that pins
one clock (`os.utime` the file, or derive the expected date from the file time).

**Network guard.** Run every suite with the network guard so a test that reaches the internet is
refused and logged instead of passing or failing on the network: `plugins/net_guard.cjs` for Node
(`--env "NODE_OPTIONS=--require <abs path with forward slashes>/net_guard.cjs"
QA_NET_GUARD_LOG=<scratch>/net_guard.log`, reaching Vitest and Jest workers) and
`plugins/net_guard.py` for pytest (`PYTHONPATH` to `plugins/`, `PYTEST_ADDOPTS=-p net_guard`).
Check the log has a `net_guard: loaded` line per process, or the guard was not on. Each refusal
names the host and the calling file and line (`from <file>:<line>`), so it can be tied to a test:
a test refused the network is `external_dependency`, never a pass. Loopback is allowed. A test that
catches the refused connection and calls `pytest.skip` drops out of every count: grep the JUnit
skip messages for the refusal, count those tests, and list them in `limitations` as
"skipped after the guard refused the network", never as passes.

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

Bisection is pytest-only: it needs a runner that runs explicit test ids in the order given.
Vitest and Jest rerun a file with a `-t` name pattern in file order, and node:test (`--preset node-test`) with a
`--test-name-pattern` the same way, so "this test, then the victim" can't be expressed; `bisect_polluter.py` writes
`outcome: not_supported` for those presets and runs nothing. Confirm their order dependence with
fixed seeds instead: the file in its own order passes 3+ times and fails 2+ times under each of one
or two `--order shuffle --seed N` runs (`--sequence.seed` for Vitest, `--randomize --seed` for
Jest; narrow with a `-t` subset if the file is large). Record that as an `order_dependence`
experiment and name no polluter; read the file for the shared state (a module-level `let`
reassigned by one test and never restored is the usual one).

`scan_pitfalls.py` points at this shape: `module-reload` flags `importlib.reload` in a test, and
`env-read-at-import` the module-level `os.getenv` reads in the module it reloads. For JavaScript and
TypeScript, `module-let-reassigned` flags a module-level `let`/`var` that a test reassigns (a
reset in `beforeEach`/`afterEach` is noted as its mitigation).

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
- `not_observed` when its test ran and never failed. Say how many runs, so nobody reads it as "safe";
- `not_run` when its test has no counted run (its language's suite was not run, the test was not
  selected or was skipped). Nothing was observed, so it is never `not_observed`; the validator
  checks both ways.

A smell with a `mitigation` (Math.random stubbed, fake timers in the file or a setup file) is a
weaker lead: check the mitigation covers the test and say so in the note. Smells are line
patterns, so some are false leads; keep them `not_observed` with a note saying why.
`wall-clock-today` is not raised for a clock read that only fills a keyword argument or a dict
value (`created_at=datetime.now()`) whose name nothing in the file compares or asserts on.

List consistently failing tests separately, with what they show.

**Flakes seen only in CI history.** A test that passed and failed in CI history (on one commit, or
a re-run attempt that passed) but never failed in your runs is still a flake entry, with
`history_only: true`: set `metadata.history` to `used` with its source, list the CI runs in
`history_evidence` (an id, the source, a reference such as the run and job, `pass` or `fail`, and
the failing line), and name those ids in `evidence_runs`. Its cause is `indicated` or `suspected`,
never `confirmed` (nothing was reproduced), it names no polluter, and its priority is capped at
P2. If the local runs do show it flaky, it is an ordinary flake: drop the flag. CI runs that never
reached the tests (an approval wait, a job-less failure, a cancelled run) are not evidence.

## Step 6: Validate and report

```bash
python scripts/validate_flaky_report.py <scratch>/flaky_report.json --runs <scratch>/runs.json \
    --smells <scratch>/smells.json --bisect <scratch>/bisect.json --repo <repo> --write-summary
python scripts/render_report.py <scratch>/flaky_report.json [--brief]
```

`--brief` lists only the flaky and consistently failing tests and summarises the rest in one line.

`--write-summary` computes every test's counts, intervals and status from the runs, each flake's
score and priority, and the summary counts (with `single_test_runs`, the `--isolate` runs of one
test each, so "87 runs" is read as suite runs plus single-test runs). An "upper bound N%" in the headline must be a Wilson
upper bound of the runs (`python scripts/stats.py 0 21` gives 0/21); the validator checks it, and
checks every priority label the headline names ("ranks P2", "2 P1 flakes", "no P0") against the
computed ranking. `--repo` checks that every proposed fix applies cleanly to the repository's files, so generate
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
| `metadata` | Repo, code hash, framework, run command, budget, gate and its evidence, critical tests, platform, `suites` (each language with tests: ran or not run, with its test count) |
| `tools[]`, `variations[]` | Each with ran, not run or not applicable and the reason; variations name their run labels |
| `tests[]` | Per test: runs, passes, fails, the normal rate and the worst label with Wilson bounds, `by_platform` when runs name more than one platform, and status (computed; `platform_dependent` for an all-pass/all-fail split by platform) |
| `flakes[]` | Cause, confidence, experiment, evidence runs, polluter or enabler (`polluter.relation`), excerpts, ranking, fix and optional quarantine; `history_only` with `history_evidence` for a flake seen only in CI |
| `consistently_failing[]` | Tests that failed every run (or every run on some platforms, with `platforms`), with what they show |
| `static_smells[]` | Every smell from `scan_pitfalls.py` and what became of it |
| `summary` | Headline, counts, causes, priorities, total runs (and `single_test_runs`), minutes and evidence completeness (variations ran × share of tests in suites that ran) |
| `limitations[]`, `files_changed[]` | What the runs couldn't show; files changed by applied fixes |

Values the schema fixes (each one otherwise costs a validation round):

- `"schema_version": "1.3.0"` for a new report: the report schema's own version, not the skill's
  `version` (1.0.0 to 1.2.0 reports are still accepted).
- `"skill": {"name": "flaky-test-hunter", "version": "<the skill version>"}` is an object, not a
  string.
- `tools[].status` is `ran`, `not_run` or `errored` (no `not_applicable`: leave out a tool that
  does not apply, or record it `not_run` with the reason); each tool has `name`, `version`,
  `status` and `reason`.
- `variations[].status` is `ran`, `not_run` or `not_applicable`, with `kind`, `labels` and `reason`.
- `metadata.suites[].status` is `ran` or `not_run`; `static_smells[].status` is `linked_to_flake`,
  `linked_to_failure`, `not_observed` or `not_run`.
- Every flake carries `quarantine` (null when you propose none) and `polluter` (null unless a
  bisection named one); the validator fills the scores, so write them as the rubric gives or run
  `--write-summary`.
