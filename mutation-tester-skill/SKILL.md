---
name: mutation-tester
description: Run mutation testing on the code that changed (or a path you name) and turn the surviving mutants into a short, ranked list of weak assertions and missing cases, each naming the test that should have caught it and the assertion or input that would. Uses the repo's own mutation tool when it is configured, the standard open-source tool otherwise (from a scratch config, never added to the repo), and a stdlib AST engine for Python as a fallback. Every score is shown with its counts and scope; equivalent mutants need a checkable reason; timeouts and errors are never counted as kills; no-coverage mutants are coverage gaps, not weak assertions. Never claims code is "well tested".
version: 1.2.0
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
- **Product code is restored after every mutant**, and the working tree is hashed before and
  after each engine run. External engines run in a scratch copy.
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
| Budget | 30 minutes | Mutants are sampled evenly across operators and files when they won't fit; the sample is recorded |
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
| Rust | cargo-mutants (MIT) | `cargo mutants --in-diff <diff>` |
| Go | go-mutesting (MIT) | `go-mutesting ./...` |
| Swift (iOS unit tests) | Muter (MIT) | `muter --files-to-mutate <files>` |
| Dart, Flutter | mutation_test (BSD-3-Clause) | `dart run mutation_test` |

`normalize_results.py` reads the builtin engine, mutmut and the mutation-testing-elements JSON
(Stryker, and PIT with its report plugin). For other tools, convert their output to that JSON or
record the engine as `not_run`; never hand-copy results.

`detect_tools.py` lists a language only when the repo has a project marker for it
(`package.json`, `pyproject.toml`, `go.mod`, ...), so one helper script doesn't make a Python
project. StrykerJS is often not installed: installing it (`npm i -D @stryker-mutator/core
@stryker-mutator/vitest-runner`, plus a fresh `npm ci` if you work in a copy) can take a long
time on a large repo, so ask first, and say in the report what was installed and where. Without
`@stryker-mutator/typescript-checker`, a mutant that doesn't compile shows up as killed or as a
runtime error rather than `compile_error`; say so in the limitations.

## Step 2: Scope

```bash
python scripts/diff_scope.py --repo <repo> --base main --out <run>/scope.json      # or --diff change.diff
python scripts/path_scope.py --repo <repo> --path src/billing [--path src/auth/rules.py:40-120] --out <run>/scope.json
```

`diff_scope.py` is the `changed` scope. `path_scope.py` is the path scope: every line of the
files under each path (a file may take a line range), in the same `scope.json` shape, with
`source.kind: "path"`; set `metadata.scope.mode` to `path` and `paths` to `source.paths`.

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
directories or node ids). Every run it makes (the coverage run, the timing run and each
mutant's run) is limited to them, and `--pytest-args` is added to each. Without `--tests`,
pytest collects whatever the repo's configuration collects, which may include integration
tests that need services, take minutes, or fail without them; the engine then stops with "the
suite does not pass unmutated".

The engine checks that the tests imported the scoped files themselves. If they imported another
copy (a virtual environment with an editable install pointing at another clone, or the package
installed from a wheel), it stops: no mutant in this checkout could be seen. Install this
checkout into the environment, or put it first on `PYTHONPATH`. Its mutant ids (B1..) follow
file, line and column and are kept by `normalize_results.py`, so the console, `builtin.json` and
the report agree; other engines' ids are renumbered, and the renderer shows the engine's own id
beside the report's.

For JavaScript and TypeScript:

```bash
python scripts/run_engine.py ranges --scope <run>/scope.json        # the scope as Stryker --mutate ranges
python scripts/run_engine.py stryker --repo <repo> --scope <run>/scope.json --run-dir <run> [--concurrency 4]
```

`run_engine.py stryker` writes a config to the run directory that mutates only the changed line
ranges (`src/x.ts:12-30`), with perTest coverage and Stryker's sandbox and JSON report in the run
directory; a JSON Stryker config in the repo is used underneath. Stryker mutates only its
sandbox, but the repository tree is still hashed before and after, and the code hash it ran on is
recorded in `stryker.run.json`; the validator rejects an engine that ran on other code than the
baseline. Run it with the same environment as the baseline (`TZ=... python scripts/run_engine.py ...`).

The builtin engine also writes `coverage.json` (per-test line coverage). Run it with
`--coverage-only` when another engine does the mutating, so mutants on unexecuted lines can be
classified. The per-mutant timeout is 3 times one untraced suite run, at least 10 seconds.

## Step 5: Normalise

```bash
python scripts/normalize_results.py --scope <run>/scope.json --repo <repo> --builtin <run>/builtin.json \
    --mutmut <run>/mutmut.json [--stryker <run>/stryker-report.json --stryker-run <run>/stryker.run.json] \
    --coverage <run>/coverage.json --out <run>/mutants.json
```

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
`""` ranks with the logic it changes. A builtin mutant inside a typing `Literal[...]` carries a
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

For a JS/TS test file it runs Vitest (or `--runner jest`) on that file with `-t "<describe> <title>"`;
give the test's full name as `--test-name`, and the repo's test environment with `--env TZ=...`
and `--extra-args "--configLoader native"`. The scratch copy links the repo's `node_modules`
rather than copying it.

The new test runs on the original code (must pass) and on each mutant (must fail), in a scratch
copy. Prove every ranked suggestion you can; cite the proof ids on the finding or gap. With
`write_tests: true`, add `--in-place` and list the test file in `files_changed`.

## Step 8: Validate and report

```bash
python scripts/validate_mutation_report.py <run>/mutation_report.json --mutants <run>/mutants.json \
    --runs <run>/runs.json --proofs <run>/proofs.json --repo <repo> --write-summary
python scripts/render_report.py <run>/mutation_report.json [--brief]
```

`--write-summary` computes each ranked finding's kind factor, proximity, score and priority, and
every count, score and score text in the summary, and sorts the findings by priority; errors
name the finding id as well as its index. `render_report.py` needs the computed summary, so
run `--write-summary` first. Write the headline yourself, and fix every error rather than
editing computed fields. Show the user:

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
  package directory, so pytest finds the package's own `pyproject.toml` and rootdir.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, commit, code hash, scope (files, ranges, exclusions), engines (version, config source, counts, tree check), coverage, budget, write_tests, platform |
| `tools[]` | Each tool ran, not run or not applicable, with the reason |
| `baseline` | The green runs, flaky tests, note |
| `mutants[]` | Exactly the normalised results |
| `findings[]` | Triage, mutants, function, reason, test that should have caught it, suggestion, proofs, criticality and the computed ranking |
| `coverage_gaps[]` | Unexecuted lines by function, their mutants, a suggested test and proofs |
| `summary` | Headline, per-engine counts and scores (computed), triage counts, priorities, evidence completeness |
| `limitations[]`, `files_changed[]` | What the run couldn't show; files written with `write_tests` |
