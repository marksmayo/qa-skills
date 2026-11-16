---
name: mutation-tester
description: Run mutation testing on the code that changed (or a path you name) and turn the surviving mutants into a short, ranked list of weak assertions and missing cases, each naming the test that should have caught it and the assertion or input that would. Uses the repo's own mutation tool when it is configured, the standard open-source tool otherwise (from a scratch config, never added to the repo), and a stdlib engine for Python (pytest or unittest) and Go as a fallback; reads cargo-mutants for Rust. It says loudly when the product language was not mutated or the mutated code is tooling. Every score is shown with its counts and scope; equivalent mutants need a checkable reason; timeouts and errors are never counted as kills; no-coverage mutants are coverage gaps, not weak assertions. Never claims code is "well tested".
version: 1.9.0
---

# Mutation tester

## Mission

Find where the tests run the changed code but would not notice it being wrong: "this test runs
line 25 but still passes if `+` becomes `-`". Each finding names the test that should have caught
the mutant and suggests the assertion or input that would, ideally proved: the new test passes on
the original code and fails on the mutant.

The headline is never a bare mutation score. It is the list of surviving mutants that matter,
with each score beside the counts and scope it came from.

How this differs from the other skills here:

- **test-suite-evaluator** may cite a mutation score as evidence of test effectiveness. This skill
  produces that evidence, scoped and triaged, and the evaluator can read the report.
- **dead-test-finder** finds tests that can't fail at all (remove the whole function body). This
  skill works at the level of single operators and values.
- **ticket-to-tdd** may run a mutation tool on the lines it changed; hand that run here for triage.
- **flaky-test-hunter**: a flaky baseline makes mutant results meaningless. Stop and send the
  flaky tests there.

Output:

1. `mutation_report.json`, conforming to `schemas/mutation_report.schema.json`.
2. The run directory: `scope.json`, `runs.json` (the baseline), each engine's raw results
   (`builtin.json`, `coverage.json`, `mutmut.json`, Stryker's JSON), `mutants.json` and
   `proofs.json`, kept with the report as its evidence.
3. Proposed tests in the report; written to the repo only with `write_tests: true`.
4. A rendered summary for the user.

## Non-negotiable rules

- **A score is always shown with its counts and scope**: "18 of 29 mutants killed in 3 changed
  files (62.1%); 1 equivalent excluded, 1 timed out". `--write-summary` writes this text; any
  percentage in the headline must be one of the computed scores, next to its "N of M". A score
  for changed code is never presented as the codebase's score.
- **No "well tested" or "strong tests".** A high score says these mutants were killed; it says
  nothing about faults the operators can't express. The validator rejects such claims.
- **Survived and no-coverage are different results.** A mutant on a line no test executes is a
  coverage gap, listed separately and never ranked as a weak assertion. When an engine only
  tracks coverage per function (mutmut), per-test line coverage reclassifies its survivors on
  unexecuted lines as `no_coverage`, and the mutant records that it was reclassified.
- **An equivalent mutant needs a reason someone can check**: why the two versions behave the same
  for every input (`i < n` vs `i != n` when `i` steps by 1 from 0 and `n` is never negative).
  "Equivalent" with no argument is rejected. Unproven cases stay survivors.
- **Timeouts and runtime errors are never silently counted as kills.** They are listed
  separately and the score is shown with and without them. An engine's "killed" on a non-test
  exit (mutmut counts some) becomes `runtime_error`.
- **Every mutant result comes from a real run**, with the engine, version, config source and
  command recorded. An engine that couldn't run is `not_run` or `errored` with the reason, and its
  scope stays untested.
- **The baseline must be green twice** before any mutant runs. A failing or flaky baseline stops
  the run.
- **Product code is restored after every mutant**, also when the engine is stopped (Ctrl-C,
  SIGTERM; a run killed outright is caught by the next run, which refuses to start until
  `--restore`). The working tree is hashed before and after each engine run, and the builtin
  engine also checks it after every mutant: a mutant whose test run writes outside the scoped
  files stops the run there, so no later result reads a changed tree. External engines run in a
  scratch copy.
- **Tests are proposed, not written, by default.** Proofs run in a scratch copy. With
  `write_tests: true`, add the test to the repo, prove it, and list the file in
  `files_changed`. Never commit, and never write back to Jira.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Scope | `changed` | `changed` (vs `base_ref`, or a diff file), a path, or `all` (only when asked: hours on real code) |
| Base ref | merge base with `main` | |
| Engine | auto | Force a tool, or `builtin` for the Python fallback |
| Budget | 30 minutes | Mutants run in an order interleaving operators and files, so a run cut off by the clock is still an even sample; the ones not reached are recorded |
| Critical paths | none | Functions or files on journey-mapper P0/P1 steps; they get criticality 3 |
| `write_tests` | false | |

## Step 1: Recon

```bash
python scripts/detect_conventions.py <repo>          # language, test framework, run command
python scripts/detect_tools.py <repo> --out <run>/tools.json
```

Use the repo's configured mutation tool if there is one. Otherwise use the standard tool for the
language (table below) from a scratch config, if it's installed; don't install anything without
asking. For Python with no usable tool, use the builtin engine. Record each tool as ran, not run
or not applicable, with the reason.

| Language | Tool (licence) | Run |
|---|---|---|
| Python | mutmut 3 (BSD-3-Clause); builtin fallback | `run_engine.py mutmut` (needs fork(): Linux, macOS, WSL); `builtin_mutator.py` |
| Python | Cosmic Ray (MIT) | its own commands; convert results to mutation-testing-elements JSON |
| JavaScript, TypeScript | StrykerJS (Apache-2.0) | `run_engine.py stryker` (needs `@stryker-mutator/core` and the runner plugin, e.g. `@stryker-mutator/vitest-runner`, in the repo) |
| C# | Stryker.NET (Apache-2.0) | `dotnet stryker --reporter json --mutate <scoped files>` |
| Java, Kotlin, Android unit tests | PIT (Apache-2.0) + JUnit 5 plugin | `mvn org.pitest:pitest-maven:mutationCoverage -DoutputFormats=XML,HTML` (or Gradle `pitest`) with the mutation-testing-elements report plugin |
| Rust | cargo-mutants (MIT) | `run_engine.py cargo-mutants` (needs cargo-mutants installed); `normalize_results.py --cargo-mutants` |
| Go | builtin Go engine; gremlins (Apache-2.0), go-mutesting (MIT) | `builtin_mutator.py` (needs `go`); gremlins/go-mutesting by hand, converted to mutation-testing-elements JSON (`normalize_results.py --mte`) |
| Swift (iOS unit tests) | Muter (MIT) | `muter --files-to-mutate <files>` |
| Dart, Flutter | mutation_test (BSD-3-Clause) | `dart run mutation_test` |

`normalize_results.py` reads the builtin engine, mutmut and the mutation-testing-elements JSON
(Stryker with `--stryker`; PIT with its report plugin, or any other tool's output converted to
that JSON, with `--mte`). For other tools, convert their output to that JSON or record the engine
as `not_run`; never hand-copy results. A converted report keeps its own engine's name (from its
`framework.name`, or `--mte-engine`), never "stryker", and without a run record it is
`run_by: "external"`, which counts half in evidence completeness.

`builtin_mutator.py`, `run_engine.py ranges` and `prove_kill.py` refuse a language they don't
cover with a non-zero exit and name the files they left out; record that engine as `not_run`
with the message as its reason. `builtin_mutator.py` also refuses a scope that names a file outside `--repo`
(`../` or an absolute path), before anything runs: it only ever writes inside the repository.

`detect_tools.py` lists a language only when the repo has a project marker for it
(`package.json`, `pyproject.toml`, `go.mod`, ...), so one helper script doesn't make a Python
project. StrykerJS is often not installed: `run_engine.py stryker` needs `@stryker-mutator/core`
and the runner plugin where the package resolves them (its own `node_modules`, or a hoisted
parent). Installing changes `package.json` and the lockfile and can take a long time on a large
repo, so ask first, use the package manager's own form (`detect_tools.py` prints it: `npm i -D`,
`pnpm add -D --filter <package>` in a pnpm workspace, `yarn workspace <package> add -D`), and say
in the report what was installed and where. Without `@stryker-mutator/typescript-checker`, a
mutant that doesn't compile shows up as killed or as a runtime error rather than
`compile_error`; say so in the limitations. `detect_tools.py` also reports, under `node`, the
package manager, whether the package sits in a workspace, and the TypeScript and runner
versions: with TypeScript 7 or later, Stryker's tsconfig rewrite and the typescript checker
cannot run (TypeScript 7 has no JavaScript API they can call), `run_engine.py` disables both,
and compile errors are never `compile_error`; say so in the limitations.

**Which language is the product.** `detect_tools.py` counts lines of product code per language
(the shared `lang_census.py`: test files and tooling folders such as `scripts/`, `tools/`, `ci/`
are counted apart) and prints a `WARNING:` line when the language with the most product code has
no engine that can run here (a Rust workspace without cargo-mutants installed), and another when
a language's only non-test code sits in tooling folders while the product is elsewhere (Python
helper scripts beside a Rust product). Both are in `tools.json` (`language_coverage`, `tooling`,
`warnings`). Mutating the helper scripts is still worth doing when asked, but the report must
say what it covered: `normalize_results.py` records `language_coverage` in `mutants.json`
(copy it into `metadata.language_coverage`), and the validator requires a limitation naming the
product language when no mutant is in it (`unmutated_dominant`) and one saying the mutated files
are tooling (`tooling_scope_files`). When the folder is the product despite its name (a
repository whose product is its `scripts/`), pass `normalize_results.py --product-path scripts`:
the declaration is recorded (`declared_product`) and no tooling limitation is asked for.

Nested sample and fixture projects (a folder under `examples/`, `fixtures/`, `testdata/`,
`samples/` with its own manifest or app) are not the product: `detect_tools.py` leaves them out
of the languages and the census and lists them under `fixtures_excluded` (`--include-fixtures`
counts them, `--fixture-dir DIR` names another fixture folder), and the report's
`language_coverage` is computed without them.

**Rust / cargo-mutants.** cargo-mutants is never installed by this skill; ask first
(`cargo install --locked cargo-mutants`). Once it is installed:

```bash
python scripts/run_engine.py cargo-mutants --repo <workspace root> --scope <run>/scope.json --run-dir <run> \
    [--minutes 30] [--cargo-mutants-arg=--package --cargo-mutants-arg=<crate>] [--cargo-mutants-arg=--test-tool=nextest]
python scripts/normalize_results.py --scope <run>/scope.json --repo <workspace root> \
    --cargo-mutants <run>/mutants.out --cargo-mutants-run <run>/cargo-mutants.run.json --out <run>/mutants.json
```

`run_engine.py cargo-mutants` runs `cargo mutants --no-shuffle --output <run>` from the
workspace root with `--in-diff <diff>` when the scope came from a diff file, `--file <f>` per
scoped `.rs` file otherwise; cargo-mutants builds in its own copy of the tree, and the repository
is hashed before and after. Without cargo-mutants it exits 2 with the install command: record the
engine `not_run` with that reason and Rust as not mutated in the limitations. A run by hand
(`cargo mutants --in-diff <diff> --output <run>`) normalises the same way without
`--cargo-mutants-run`; it is then `run_by: "external"` and has no tree check, which the validator
reports. The normaliser reads `mutants.out/outcomes.json`: CaughtMutant is `killed` (with
`killed_by` from the mutant's log when it names a failing libtest test), MissedMutant `survived`,
Timeout `timeout`, Unviable `compile_error`, and a failed baseline makes the engine `errored`.
Operators come from the genre and the replaced text (`>=` to `>` is `comparison_boundary`,
`&&` to `||` `logical`, FnValue `return_value`, MatchArm `remove_block`). cargo-mutants has no
per-test coverage, so `covering_tests` is empty and every missed mutant carries a note that its
line may be unexecuted: check before calling it a weak assertion (a `missing_case` with no
`should_have_caught` is the honest default), and proximity is 1. Prove suggestions with
`prove_kill.py` (Step 7: cargo test by test id). `builtin_mutator.py` does not mutate Rust.

**Go.** The builtin engine mutates Go: `builtin_mutator.py` picks Go when the scope's changed
source is Go (`--language go` when Python changed too). It makes text-level mutants from Go
tokens on the changed lines, never inside comments or string, raw-string and rune literals
(comparisons one step, `==`/`!=`, `&&`/`||`, binary arithmetic but not string `+`, `+=`/`-=`,
`++`/`--`, `break`/`continue`, `true`/`false`, `return ..., err` to `nil`, `if cond` to
`if !(cond)`). Each go command runs from the nearest `go.mod` on one package directory with
`-count=1 -vet=off`, and each mutant is handed to the compiler with `go test -overlay`, so the
repository is never written (the tree is still hashed before and after). Coverage is per
top-level test: the package's test binary is built once with `-cover`, and each test runs alone
with its own coverprofile; test ids are `<package dir>/<file>_test.go::TestName`. A mutant that
does not build is `compile_error`, never a kill. `--go-run` and `--go-skip` narrow the tests (for
example tests that fail only in this environment: name them in the baseline note), and
`--go-flags "-tags x"` adds build flags. Only the scoped package's own tests run: a line covered
only by tests in other packages (an `internal/` helper exercised from its callers) is
`no_coverage` here; say so, and check those callers' tests before calling it a gap. Proximity
reads Go tests (`func TestX` bodies); `prove_kill.py` proves a Go test with
`go test -run '^TestX$'` and `-overlay` (nothing copied). gremlins and go-mutesting are
alternatives: run them yourself, convert the results to mutation-testing-elements JSON (keep
the tool's mutator name in `mutatorName`; `data/operators.json` maps gremlins' and go-mutesting's
names) and pass it with `--mte`. Two of their operators can make the same edit (`n-1` to `n+1`);
`normalize_results.py` merges such duplicates. Check a sample of a converted tool's kills by
hand: one run of gremlins on Windows reported kills for mutants that survive `go test`, and
counted mutants that do not compile as killed. For the baseline use `run_tests.py --preset
gotestsum` (or `go test`) on the scoped packages, e.g. `--cmd "go test -count=1 ./rates/"`.

## Step 2: Scope

```bash
python scripts/diff_scope.py --repo <repo> --base main --out <run>/scope.json      # or --diff change.diff
python scripts/path_scope.py --repo <repo> --path src/billing [--path src/auth/rules.py:40-120] --out <run>/scope.json
```

`diff_scope.py` is the `changed` scope. `path_scope.py` is the path scope: every line of the
files under each path (a file may take a line range), in the same `scope.json` shape, with
`source.kind: "path"`; set `metadata.scope.mode` to `path` and `paths` to `source.paths`. It
records the commit it read (`source.head`, and `source.dirty`: scoped paths with uncommitted
changes); copy `source.head` into `metadata.commit`.

Only `source` files' added or changed lines (or, for a path, all their lines) are mutated.
Tests, generated code, migrations, vendored code, type declarations, config and docs are
excluded and listed with the reason.

When `--repo` is a package inside a larger git repository (a monorepo's `libs/<name>`), check
that `scope.json` lists the changed files with `present: true` and their functions; if the
paths came back relative to the git root instead, make the diff yourself with
`git -C <repo> diff --relative <base>` and pass it with `--diff`.

## Step 3: Green baseline

```bash
python scripts/run_tests.py --preset pytest --cwd <repo> --out <run>/runs.json --repeat 2 --label baseline --run-prefix G --check-tree
```

Both runs must pass on the same code hash. If either fails, or a test passes in one and fails in
the other, stop: report `baseline.passed: false`, run no mutants, and point at
flaky-test-hunter.

**What the baseline is for a scoped run.** The baseline proves the tests the engine will run are
green on this code. For `all`, that is the whole suite. For `changed` or a path, it is the test
files that exercise the scoped code: the files that import the scoped modules (directly, or
through the module that re-exports them), or the related tests the engine itself selects
(Stryker's `related` mode, mutmut's per-function test map). Pass those files with `--tests`, use
the repo's own test command and environment (`--cmd`, `--env TZ=...` when `npm test` sets them),
and list the files, and how you chose them, in `baseline.note`. A failure elsewhere in the suite
doesn't stop a scoped run, but it goes in the limitations.

**Use the repository's runner.** `detect_conventions.py` gives each area's `python_runner`
(unittest when CI runs `python -m unittest` or `coverage run -m unittest` and nothing uses
pytest) and the CI command (`ci_gates`). For a unittest suite, run the builtin engine and the
proofs with `--runner unittest --unittest-args "-s tests"` (the CI's discovery options), so the
same tests run as in CI. pytest also runs `unittest.TestCase` classes, but it collects more: a
plain function named `test_*` (a `@contextmanager` helper, say) becomes a passing "test", and
pytest may not be installed at all. `run_tests.py` has no unittest preset yet: when pytest is
installed, take the baseline with `--preset pytest --tests <the TestCase files>` and say in
`baseline.note` that pytest ran the unittest suite; otherwise the builtin engine's own coverage
and timing runs (both must pass unmutated) are the only green runs, which the baseline must say.
For Rust, the baseline is `run_tests.py --preset cargo` (cargo-mutants also runs its own
baseline and stops when it fails).

**Self-tests and other pytest configurations.** `detect_conventions.py` also lists `self_tests`
(scripts with a `--self-test` flag, a `self_test()` function or a `self_test.py`) and, per area,
`pytest_configs`: a `pytest.ini` in a folder with no manifest (`benchmarks/` with
`python_files = bench_*.py`) and the command that collects it. When the scoped code is tested by
a script's own self-test, take the baseline with `run_tests.py --preset selftest --selftest-cmd
"python tools/x.py --self-test" --repeat 2` and mutate with `builtin_mutator.py --runner
selftest` and the same commands (Step 4). When it is tested by such a benchmark folder, pass that
folder with `--tests benchmarks` (the engine records ids relative to `--repo`).

**Flaky tests.** If a test in the baseline set passes in one run and fails in the other, the
baseline is flaky: list it in `baseline.flaky_tests`, set `passed: false`, and stop; a mutant
"killed" by a flaky test proves nothing. Don't drop the flaky test to get a green baseline unless
it covers none of the scoped lines (check the engine's coverage), and then say so in the note.
If flaky-test-hunter has already reported tests in this set as flaky, start from its report.

## Step 4: Mutate and run

```bash
python scripts/builtin_mutator.py --repo <repo> --scope <run>/scope.json --run-dir <run> [--minutes 30] \
    [--tests tests/unit/test_rules.py --tests tests/unit/test_access.py] [--pytest-args "-p no:randomly"]
python scripts/run_engine.py mutmut --repo <repo> --scope <run>/scope.json --run-dir <run>
```

Give the builtin engine the same test files as the baseline with `--tests` (repeatable: files,
directories or node ids, relative to `--repo`). Every run it makes (the coverage run, the timing run and each
mutant's run) is limited to them, and `--pytest-args` is added to each. Without `--tests`,
pytest collects whatever the repo's configuration collects, which may include integration
tests that need services, take minutes, or fail without them; the engine then stops with "the
suite does not pass unmutated".

The engine checks that the tests imported the scoped files themselves. If they imported another
copy (a virtual environment with an editable install pointing at another clone, or the package
installed from a wheel), it stops: no mutant in this checkout could be seen. Install this
checkout into the environment, or put it first on `PYTHONPATH`.

Covering-test ids are recorded relative to `--repo`, where every mutant run starts, whatever
rootdir pytest chose: a `pytest.ini` in a subfolder (`benchmarks/pytest.ini` with
`python_files = bench_*.py`) makes pytest write node ids relative to that folder.
`coverage.json` records pytest's `rootdir` and `inifile`. Before the first mutant the engine
collects the covering ids from `--repo`; if pytest cannot (an id that does not resolve), it stops
with exit 2 instead of recording every mutant as a `runtime_error`, and a mutant run in which
pytest cannot find a covering test stops the engine the same way (`stopped.kind: engine_error`);
a conftest that fails to import under a mutant stays a `runtime_error`.

Its mutant ids (B1..) follow
file, line and column and are kept by `normalize_results.py`, so the console, `builtin.json` and
the report agree; other engines' ids are renumbered, and the renderer shows the engine's own id
beside the report's.

For JavaScript and TypeScript:

```bash
python scripts/run_engine.py ranges --scope <run>/scope.json        # the scope as Stryker --mutate ranges
python scripts/run_engine.py stryker --repo <repo> --scope <run>/scope.json --run-dir <run> [--concurrency 4]
```

`run_engine.py stryker` writes a config to the run directory (`qa-stryker.config.json`) that
mutates only the changed line ranges (`src/x.ts:12-30`), with perTest coverage, the runner and
checker plugins named (pnpm's isolated `node_modules` hide them from Stryker otherwise) and the
JSON report in the run directory; a JSON Stryker config in the repo is used underneath. Run it
from, and with `--repo` set to, the package directory. Stryker mutates only its sandbox, but the
repository tree is still hashed before and after, and the code hash it ran on is recorded in
`stryker.run.json`; the validator rejects an engine that ran on other code than the baseline.
Run it with the same environment as the baseline (`TZ=... python scripts/run_engine.py ...`).

- **Sandbox.** A package inside a workspace (pnpm, npm or Yarn workspaces, Nx, Turborepo, ...)
  usually reaches outside itself: `"extends": "../../tsconfig.base.json"`, and pnpm's relative
  links in `node_modules`. `--sandbox auto` (the default) then recreates the workspace levels
  above the package in `<run>/ws` (files copied, directories linked, nothing written to the
  repo) and puts Stryker's sandbox at the package's own depth there, so both resolve. Outside a
  workspace the sandbox is `<run>/stryker-tmp`. `--sandbox in-package` keeps Stryker's default
  `.stryker-tmp` inside the package. Stryker's tsconfig rewrite only suits that default, so the
  record says when it was disabled (`tsconfig_preprocessor`).
- **Options.** `--stryker-option key=value` (repeatable) sets any other Stryker option; dotted
  keys nest and values are JSON when they parse. The common ones: a Stryker-only test config
  (`vitest.configFile=vitest.mutation.config.ts`, `jest.configFile=...`) that holds only the unit
  tests the baseline ran, `vitest.related=false`, `ignoreStatic=true`. Without one, Stryker runs
  the Vitest config as it is, including projects that need browsers or built packages.
- **Stryker's own dry run.** Stryker first runs its related test set unmutated, and it is often
  wider than your baseline. A test that fails there (a flaky timing test under load, a test that
  fails on this OS at HEAD, a test calling `process.chdir()`, which Stryker's Vitest worker
  threads refuse) stops the run with "There were failed tests in the initial test run". Leave
  such tests out with a Stryker-only test config, check they cover none of the scoped lines,
  and name them in the baseline note and the limitations. The config file is a file in the
  package, so creating it changes the code hash: write it before the baseline runs, or in a
  copy.
- **Engine failure.** When Stryker writes no report, `stryker.run.json` has `status: "errored"`,
  the first error line from the log and, for known integration problems (TypeScript 7, an
  unloaded plugin, an unresolved dependency or tsconfig), a `hint`. Record the engine as
  errored with that reason; never as a run with zero mutants. `normalize_results.py` accepts
  `--stryker-run` without a report for such a run.

The builtin engine also writes `coverage.json` (per-test line coverage). Run it with
`--coverage-only` when another engine does the mutating, so mutants on unexecuted lines can be
classified. The per-mutant timeout is 3 times one untraced suite run, at least 10 seconds.

- **Lines run at import.** Code that runs only while modules are imported (a conftest that loads
  data at import, a cached loader, an app factory) runs during collection, never inside a test.
  The engine records such lines under `<import>` and treats them as covered by every test that
  touched the file (every test it saw, when none did), with a note on the mutant; they are not
  `no_coverage`. Expect those mutants to be killed (or survive) by tests far from the line.
- **Budget.** Before the loop the engine prints its plan: how many mutants, and an estimate per
  mutant of the measured start-up (a run that loads the runner, conftest and the test modules
  and runs no test) plus the share of the suite's test time its covering tests take (a mutant
  runs only its covering tests, failing fast). Start-up often dominates (two or three seconds
  per run on Windows), so this is the figure to plan with; after the first few mutants a
  `measured:` line gives the real mean and what the rest would take, and `builtin.json` keeps
  both (`estimate`). Mutants run in an interleaved order until `--minutes` is spent, so a short
  budget still gives an even sample and an over-estimate never leaves budget unused. Keep
  crawl-everything tests (a test that renders every page) out of `--tests`: they dominate the
  coverage run, which traces every line.
- **unittest suites.** `--runner unittest --unittest-args "-s tests"` runs every coverage, timing
  and mutant run with unittest's own discovery (the CI's `python -m unittest discover` options),
  traced per TestCase method; test ids are `tests/test_x.py::Class::test_method`, and `--tests`
  takes test files, folders or such ids. A test module that fails to import under a mutant is a
  `runtime_error`, never a kill. `--runner auto` (the default) uses pytest when it imports.
- **Scripts tested by their own `--self-test`.** `--runner selftest --tests tools/x.py` runs
  `python tools/x.py --self-test` (a `self_test.py` runs as it is), and `--selftest-cmd "python
  tools/check_all.py"` (repeatable) names any other command that runs a Python script. Each
  command is one test, run from `--repo`. Coverage is traced in every Python process the command
  starts (a `sitecustomize` module on `PYTHONPATH`), so a script the self-test runs as a child
  process is covered too. A mutant is killed when a check fails (`killed_by` is
  `<script>::<check>`, the check names `run_tests.py --preset selftest` reads) or the command
  exits non-zero (`<script>::(exit status)`); it survives when every covering command passes.
  The baseline is the same commands through `run_tests.py --preset selftest`.
- **`if __name__ == '__main__':`.** The guard is never mutated (that would run `main()` with the
  test runner's argv at import), and a module-level statement that never ran, such as the guard's
  body under pytest, is `no_coverage`, not covered by every test of the file.
- **Tree check after every mutant.** After each mutant the engine hashes the tree again (a stat
  walk; unchanged files are not re-read). When a mutant's test run changed a file outside the
  scoped files (a dry run that the mutant turned into a write, run on a real fixture), the
  mutant gets `wrote_outside_scope`, `builtin.json` records `stopped` (`kind: tree_changed`,
  the mutant and the files) and `tree.stopped`, every later mutant is `not_run` with a note
  naming it, and the engine exits 3. Restore those files (git checkout, or a fresh copy), make
  the test hermetic (copy the fixture to a temporary folder), and run again. The report keeps the
  engine's `stopped` record, lists the changed files in `files_changed`, and a limitation names
  the mutant.
- **Stopping the engine.** Ctrl-C, SIGTERM, SIGHUP (and SIGBREAK on Windows) kill the test run,
  put the original source back and write `builtin.json` with `interrupted: true` (the mutants not
  finished are `not_run`); the engine exits 130. A run killed outright cannot restore anything:
  the next run into the same run directory finds originals in `<run>/backup` that differ from the
  source and refuses (exit 2) until `--restore` has put them back.
- **Code run only in a child process.** A test that runs a script with `subprocess` (`sys.executable
  scripts/report.py`) exercises it out of the tracer's sight, so its mutants are `no_coverage`.
  The engine notes each such mutant with the test lines that launch the file
  (`coverage.json` `launched_as_child`). Read that test: it may check the output well, or not at
  all. Either way the in-process mutants stay `no_coverage`; say in the gap's note that the file
  is exercised only as a child process (on which platforms, when the test is skipped on some).
- **Templates.** The engine mutates Python only. When the change also edits Jinja or Django
  templates, list them in the scope's exclusions as "not mutated: template", and check survivors
  whose values flow into a template (a context variable the template branches on) by reading it.

## Step 5: Normalise

```bash
python scripts/normalize_results.py --scope <run>/scope.json --repo <repo> --builtin <run>/builtin.json \
    --mutmut <run>/mutmut.json [--stryker <run>/stryker-report.json --stryker-run <run>/stryker.run.json] \
    [--mte <run>/gremlins.mte.json --mte-engine gremlins] \
    [--cargo-mutants <run>/mutants.out --cargo-mutants-run <run>/cargo-mutants.run.json] \
    --coverage <run>/coverage.json --out <run>/mutants.json
```

`mutants.json` also holds `language_coverage`: the language with the most product code, the
languages the mutants are in, `unmutated_dominant` when the product's language has no mutant, and
`tooling_scope_files` when the mutated files sit in tooling folders while the product is
elsewhere (both are printed as warnings). Copy it into `metadata.language_coverage` unchanged.
cargo-mutants' mutants get C1.. ids (Step 1, Rust).

A mutation-testing-elements report other than Stryker's goes in with `--mte`; its mutants get
P1.. (PIT) or X1.. ids and its own engine name. Mutants of one engine that make the same edit on
the same line are merged (the note names the others; `duplicates_merged` counts them).

For Stryker, covering tests and `killed_by` are test names from the report's `testFiles`
(`src/__tests__/x.test.ts::describe > title`, the same form as Vitest's JUnit ids), never
Stryker's numeric ids. Each mutant's `function` comes from `ast` for Python and from
the shared `diff_scope.py` scanner otherwise (the same function ranges as `scope.json`), so
TypeScript findings name their function; `ts_scan.py` finds JS/TS tests by name for proximity,
which can then be 2.

It drops mutants outside the scope (and counts them), maps operators to the common set in
`data/operators.json`, and assigns the ids the report uses (B1.., M1.., S1..). It exits non-zero
when a mutant can't be mapped to a line; fix that, don't hand-edit the file.

## Step 6: Triage every survivor

Group the survived mutants into findings: one finding per weakness, often several mutants
(several engines, or a constant and the comparison using it). Every survived mutant belongs to
exactly one finding, and nothing else does:

- `weak_assertion`: a covering test runs the line but doesn't check the result it changes. Name
  that test in `should_have_caught` (it must be in every mutant's `covering_tests`) and the
  missing assertion.
- `missing_case`: no test uses an input that tells the versions apart (usually a boundary). Name
  the input; name the closest covering test if there is one.
- `equivalent`: with the argument that both versions behave the same for every input.
- `accepted`: the behaviour doesn't matter (log text, a debug-only branch), with the reason.

A string compared in a condition (`role != "*"`, `kind in ("a", "b")`) or held in an UPPER_CASE
constant is not display text: its operator is `constant_string_compared` (kind branch) or
`constant_string_named` (value), so a mutant that turns a wildcard check or a role name into
`""` ranks with the logic it changes. A string used as a key or a default is data, not text:
`c.get("platforms", [p])`, `row["kind"]`, a dict literal's key, `def f(kinds=("sea", "river"))`
give `constant_string_data` (kind value), since emptying the key disables a lookup and emptying a
default changes which items a rule applies to. A builtin mutant inside a typing `Literal[...]` carries a
note: it changes behaviour only where the type is checked at run time (pydantic models,
typeguard), which may be outside the tests in this repo; triage it on that basis.

Give each `weak_assertion` and `missing_case` a criticality (RUBRIC.md) with a reason. Group every
`no_coverage` mutant into a coverage gap by function and lines, with a suggested test.
Timeouts, runtime and compile errors are not triaged; they appear in their own list.

## Step 7: Prove suggestions

```bash
python scripts/prove_kill.py --repo <repo> --mutants <run>/mutants.json --mutant B24 --mutant M25 \
    --test-file tests/test_pricing.py --test-code new_test.py --test-name test_total_adds_one_booking_fee \
    --finding F1 --run-dir <run>/proof --out <run>/proofs.json
```

For a JS/TS test file it runs Vitest (or `--runner jest`) on that file with `-t` and the test's
full name; give it as `--test-name "describe > title"` (the JUnit form). The pattern accepts both
separators the runners match on (spaces for Jest and Vitest up to 3, " > " for Vitest 4 and
later). Give the repo's test environment with `--env TZ=...` and `--extra-args "--configLoader
native"`. The scratch copy links the repo's `node_modules` rather than copying it, and a package
inside a workspace is copied into a mirror of the workspace levels, as for Stryker. When the new
test does not run at all (`test_status: "not_run"`), the proof keeps its output
(`proof-*.log`) and JUnit in the run directory and records the first error line: read them
before calling the suggestion wrong. The runner starts as `node <runner entry>` from `node_modules`;
on Windows, when only the `npx` shim is there and the test file name or pattern holds a character
cmd.exe acts on (`^ & | < > ( ) % ! "`, always true of the `-t` pattern), the proof is refused as
`not_run` rather than handed to cmd.exe, which would run a command hidden in a file name.

For a Go test file (`*_test.go`) it runs `go test -count=1 -vet=off -run '^TestName$'` on the
package from the nearest `go.mod`, with the test file and each mutant swapped in by `-overlay`:
nothing is copied. A mutant that does not build is `not_viable`, never a kill.

For a unittest suite give `--runner unittest --unittest-args "-s tests"` and the test as
`--test-name Class.test_method` (the appended code is a `unittest.TestCase` class, using the
test file's imports); it runs with unittest's own discovery, and the proof's `test_id` is
`tests/test_x.py::Class::test_method`.

For a Rust test file (`.rs`: an integration test in `tests/`, or a `#[test]` appended to a source
file's module) it appends the test in a scratch copy (without `target/`), finds the test's id with
`cargo_project.py` (`crossing::high_from_300`, `<lib>::tests::name`) and runs it with
`run_tests.py --preset cargo --tests <id>` (`cargo test -p <crate> --test <target> -- --exact`).
Builds go to `CARGO_TARGET_DIR` (default `<run-dir>/cargo-target`, removed afterwards; set it to
reuse a build). A mutant that does not compile is `not_viable`. The default timeout is at least
300 s, since each run compiles.

Any other test file (Java, ...) is refused before anything is copied; prove it by hand and leave
the finding's `proof_ids` empty.

The new test runs on the original code (must pass) and on each mutant (must fail), in a scratch
copy. Prove every ranked suggestion you can; cite the proof ids on the finding or gap. With
`write_tests: true`, add `--in-place` and list the test file in `files_changed`.

Each run gets `--timeout` seconds; by default the cited mutants' engines' measured per-mutant
timeout from `mutants.json` (`engines[].timeout_s`), never less than 60 s, recorded on the proof as
`timeout_s`. An app whose import loads a large data tree may need more: give `--timeout`. The
verdicts: `kills` (passed on the original, failed on the mutant), `does_not_kill` (passed on
both), `fails_on_original` (the new test is wrong), `original_not_run` (the test never ran on the
original, a timeout or a collection error: inconclusive, the mutants are not run; raise
`--timeout` or read the kept log), `timeout` and `runtime_error` (the test never ran on the
mutant, for example a mutant that breaks collection), and `not_viable` (a Go mutant that does not
build). Only `kills` backs a suggestion; never call a suggestion wrong on `original_not_run`.

## Step 8: Validate and report

```bash
python scripts/validate_mutation_report.py <run>/mutation_report.json --mutants <run>/mutants.json \
    --runs <run>/runs.json --proofs <run>/proofs.json --repo <repo> --write-summary
python scripts/render_report.py <run>/mutation_report.json [--brief]
```

`--write-summary` computes each ranked finding's kind factor, proximity, score and priority, and
every count, score and score text in the summary, and sorts the findings by priority; errors
name the finding id as well as its index. `render_report.py` needs the computed summary, so
run `--write-summary` first. Write `"schema_version": "1.4.0"`. When an engine record carries
`stopped`, a limitation names it (the mutant id, and for `tree_changed` the files its run
changed, which also go in `files_changed`). Write the headline yourself: it
names every P0 finding by id (`F1`), and a placeholder is rejected. Copy
`metadata.language_coverage` from `mutants.json`; when it names an `unmutated_dominant`
language, a limitation says that language was not mutated (and why: no engine installed, out of
scope), and when it lists `tooling_scope_files`, a limitation says the mutated code is tooling,
not the product. Fix every error rather than editing computed fields. Show the user:

1. the headline;
2. the ranked findings, each with its mutants, the test that should have caught them, the
   suggestion and whether it's proved;
3. the coverage gaps;
4. each engine's score text, and what wasn't run.

## Stack notes

- **Tests in other packages.** Each engine runs the tests of `--repo` only. When a shared library
  is also exercised by tests in the packages that depend on it (a monorepo's services, each with
  its own environment), those tests are not run, so a survivor may be killed there. The builtin
  engine can't run them; say so in the limitations, and before calling a survivor a weak
  assertion, check whether a dependent package's test covers that input (if one does, the
  finding is still real for this package, but name that test in the reason).
- **Editable installs.** See Step 4: the builtin engine refuses a run whose tests import another
  copy of the scoped code. mutmut and Stryker work in a copy and have the same risk; check
  `covering_tests` is not empty for code the tests obviously call.
- **Monorepo packages.** Run every step with `--repo` (and the baseline's `--cwd`) set to the
  package directory, so pytest finds the package's own `pyproject.toml` and rootdir, and Vitest or
  Jest the package's own config. From a workspace root, `diff_scope.py` and `path_scope.py` treat
  a directory named `e2e/` or `tests/` as test code only when it is not a product package (a
  manifest and shipped code, such as `src/`); still check `scope.json` marks the scoped files
  `source`.
- **Stryker and Vitest projects.** A Vitest config with `projects` (unit, integration) is run as
  a whole by Stryker's Vitest runner; give it a unit-only config with `--stryker-option
  vitest.configFile=...` (Step 4).
- **Stryker's MethodExpression.** Swaps such as `some`/`every`, `startsWith`/`endsWith` and
  `min`/`max` are normalised as `method_swap_logic` (a branch change); a dropped call (`filter`,
  `sort`, `trim`) as `remove_method_call`. The engine's own mutator name is kept in
  `engine_operator`.
- **Per-test coverage from Stryker.** With `coverageAnalysis: perTest` (what `run_engine.py`
  sets), each Stryker mutant lists the tests that ran it, and `normalize_results.py` records that
  as the run's coverage (`Stryker coverageAnalysis perTest`), which counts toward evidence
  completeness. Copy `coverage` from `mutants.json` into `metadata.coverage`.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, commit, code hash, scope (files, ranges, exclusions), engines (version, config source, counts, tree check), coverage, budget, write_tests, platform, language_coverage (the product language and what was mutated) |
| `tools[]` | Each tool ran, not run or not applicable, with the reason |
| `baseline` | The green runs, flaky tests, note |
| `mutants[]` | Exactly the normalised results |
| `findings[]` | Triage, mutants, function, reason, test that should have caught it, suggestion, proofs, criticality and the computed ranking |
| `coverage_gaps[]` | Unexecuted lines by function, their mutants, a suggested test and proofs |
| `summary` | Headline, per-engine counts and scores (computed), triage counts, priorities, evidence completeness |
| `limitations[]`, `files_changed[]` | What the run couldn't show (always: a product language left unmutated, tooling mutated instead of the product); files written with `write_tests` |
