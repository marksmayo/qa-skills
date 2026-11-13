---
name: test-impact-selector
description: Given a change (a branch, a commit range or a diff), pick which tests to run, give every selected test its reasons and evidence (coverage of a changed line, an import chain, a journey link, naming, co-failure history), and list every change that no evidence can bound as a blind spot that widens the selection (strict by default). Outputs ready-to-run commands per package and framework, an optional CI snippet for the user to add, and, when past runs are available, measured recall with a Wilson interval from replaying history. Never calls unselected tests "safe to skip".
version: 1.2.0
---

# Test impact selector

## Mission

Pick the tests a change needs, say why each one was picked, and say plainly what couldn't be
reasoned about. The output is a list of test files, each with its reasons, plus the blind spots
and what was done about each one. Then a command to run them, and, when history exists, a
measurement of how often this selection would have missed a failure.

How this differs from the other skills:

- **test-suite-evaluator** judges the whole suite. This skill uses the suite's structure to make
  a decision for one change.
- **journey-mapper** links tests to user journeys. This skill reads those links as one source of
  evidence among several.
- **flaky-test-hunter** and **mutation-tester** scope a change to decide what code to analyse.
  This skill scopes it to decide which tests to run. A flaky-test-hunter report can mark known
  flaky tests, so the replay doesn't count their failures as misses.

Output:

1. `impact_report.json`, conforming to `schemas/impact_report.schema.json`.
2. The run directory, kept with the report as its evidence:
   - `selection.json`;
   - the coverage map;
   - `replay.json` and the history it replayed;
   - the verify `runs.json`, if a verify run was made.
3. The commands to run, and optionally a CI snippet written outside the CI configuration.
4. A rendered summary for the user.

## Non-negotiable rules

- **Every selected test has at least one reason with its evidence.** A reason names its source,
  for example:
  - a test that ran base line 20;
  - an import chain;
  - a journey id;
  - a co-failure count.

  A reason never cites a source that didn't run.
- **Every change that can't be mapped is a blind spot.** Each blind spot has a kind and an action,
  and none is silently dropped. Each changed file ends up in one of three states:
  - linked to a selected test by evidence;
  - a blind spot;
  - classified as docs.
- **No "safe to skip".** Tests that weren't selected are "not selected by this evidence", and the
  report lists which evidence sources ran. The validator rejects "safe", "guaranteed", "no
  risk" and similar.
- **Recall is measured, not asserted.**
  - With history, recall is the number of failing test files selected out of all the failing
    ones, shown as "N of M" with a 95% Wilson interval.
  - Without history, recall is `not_assessed` and no recall figure appears anywhere.
- **A stale coverage map is labelled stale.** Measured files whose hash differs from the base
  tree make the source `stale`. Their coverage is still used, with a warning in the evidence.
- **Sources that couldn't run are recorded** as `not_run` or `errored` with the reason, and they
  lower evidence completeness. A source that doesn't apply to the repo is `not_applicable`.
- **The full suite still runs somewhere.** Every report says when: on merge, nightly, or before
  release.
- **Selection is advice.**
  - Never edit CI configuration. A CI snippet is written elsewhere for the user to add.
  - Never commit.
  - Never write back to Jira.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Change | working tree against the merge base with `main` | Or a commit range (`--committed-only`), or a unified diff (`--diff`; with `--diff-applied` when the repo already contains it) |
| Mode | `strict` | `strict`, `balanced` (allowed only with a coverage map and history), `aggressive` |
| Coverage | none | Per-test coverage the repo already produces; `generate` is opt-in |
| History | none | Past changes with full-run JUnit, for co-failure and replay |
| Journeys | none | journey-mapper's `journey_map.json` |
| Related tests | none | Output of the repo's own related-tests tool (below) |
| Flaky list | none | Test files or ids known to be flaky, for replay |

## Step 1: Recon

```bash
python scripts/detect_conventions.py <repo>                     # packages, frameworks, run commands
python scripts/import_graph.py <repo> --out <run>/graph.json    # files, test levels, import edges, dynamic sites
```

Packages come from manifests: `pyproject.toml`, `package.json` workspaces, `go.mod`, Gradle
modules and the like. The import graph parses Python (`ast`), and JavaScript and TypeScript
(imports, `require`, `import()`, `import.meta.glob`, `require.context`, workspace names, tsconfig
`paths`). It also records which conftest fixtures each pytest file uses.

A directory named `e2e`, `test`, `tests` or `spec` is test code unless it is a product package
(it holds a manifest and ships code: a `src/` directory, a `package.json` with
`main`/`exports`/`bin`, ...). A workspace package called `e2e` is therefore source, and only its
own `tests/` are tests, with levels judged inside the package (its `tests/unit/` are unit tests).
The graph notes such packages; check them, since a misread package turns every change into a
shared-fixture blind spot.

Languages without a parser (Java, Kotlin, C#, Swift, Dart and others) are listed as
`no_parser`. Their files rely on the other evidence sources, and on blind spots. `graph.json`
carries `language_coverage` (vendored and embedded third-party trees left out), and the console
warns when the repository's dominant language has no parser.

**Go.** Go is parsed from text: each `go.mod`'s module path maps an import path to a package
directory, a file depends on every non-test file of each package it imports, a `_test.go` file
also on its own package's files, and `//go:embed` patterns are edges to the embedded files. A
Go package is a directory, not the module: an unmapped Go file widens to its directory's tests
and the tests that import it, and only when there are none to the module's Go tests, never to
other languages' tests beside it. Naming matches `x_test.go` only in `x.go`'s own directory.
Better than imports alone, when `go` is available and the user agrees:
- `--related` from `go list -deps -test -json ./...` (reverse test dependencies per package, the
  toolchain's own graph, build tags included) in the shape below;
- per-test coverage from `coverage_map.py go` (Step 2);
- the Go command keeps the `go test` flags of the repo's CI or Makefile line (`-race`,
  `-timeout 25m`, `-tags`, ...; `flags_from` says which line), or `--go-test-flags`.
History from gotestsum JUnit (classname is the import path) maps to the `_test.go` file through
`go.mod` when `--repo` is given; a package that failed to build is `<dir>/(package)`, counted as
selected when any test file of that directory is.

**Language support / Rust.** The import parsers are Python (`ast`), JavaScript/TypeScript
(regex) and Go (text, with go.mod). Rust (`.rs`) is `no_parser`, so it yields no import edges, and there is no cargo coverage
ingestion either: `coverage_map.py` reads coverage.py JSON (or the mutation tester's
`coverage.json`), not cargo-llvm-cov or grcov output. So for a Rust-only change with no other
source, every evidence source is empty and the selector honestly falls back to running everything
(a blind spot per changed file widening to the package or full suite) rather than claiming an
impact-based subset. Don't read that empty selection as "nothing to run".

## Step 2: Evidence sources

Prepare the sources that the repo already has. Install nothing without asking.

**Coverage.** Use per-test coverage the repo already produces:

```bash
python scripts/coverage_map.py normalise coverage.json --repo <repo> --package api --out <run>/coverage_map.json
```

It reads either of these:
- coverage.py JSON with contexts, from `pytest --cov=<pkg> --cov-context=test` then
  `coverage json --show-contexts`;
- the mutation tester's `coverage.json`.

Normalise straight after collecting, because the file hashes are taken then. Only when the user
agrees, `coverage_map.py generate` runs one instrumented full run itself (it needs coverage.py
and pytest-cov).

**Vitest and Jest** have no per-test contexts. `coverage_map.py per-file` runs each test file alone
with JSON coverage (Istanbul's `coverage-final.json`; Vitest's V8 provider writes the same shape)
and attributes its lines to the whole file (test id `<file>::(whole file)`):

```bash
python scripts/coverage_map.py per-file --repo <repo> --package packages/core --tests tests/unit \
    --extra-args "--project unit" --jobs 4 --keep <run>/percov --out <run>/coverage_map.json
```

It needs the coverage provider already installed (`@vitest/coverage-v8`), runs the runner copy
the package resolves, and takes one run per test file (minutes for a few hundred files, so ask
first). Lines outside every function (module level, run at import) are dropped and counted, so
importing a module is not evidence that a test exercises its constants. Test files that failed or
left no coverage are listed in the map's summary: their lines are missing, so say so in the
limitations. A single whole-run `coverage-final.json` is refused: it cannot say which test ran a
line. Collect coverage on the base side of the change (before it is applied): a map taken with
the change in place is stale for the changed files.

**Go.** `coverage_map.py go` builds one test binary per package with tests
(`go test -c -cover -covermode=set -coverpkg=<module>/...`, so a test in one package covers code
in the packages it calls) and runs it once per test file (its top-level tests; `--per test` for
one run per test), each with its own coverprofile, from the package directory as `go test` does.
Binaries and profiles go to `--keep`; nothing is written into the repository. `--skip-dirs`
leaves out packages that need a browser, device or service (name them in the limitations).
It runs the tests, so ask first. A single whole-run coverprofile is refused. Run it under the
environment the repo's tests need (and nothing that reaches a real device or network).

For JaCoCo per-test data, convert it to the coverage.py JSON shape, or leave coverage
`not_run` for that package.

**Related-tests tools.** Run the repo's own tool and pass its output to `--related` as
`{tool, version, command, related: {changed path: [test files]}}`. Examples:
- `npx jest --listTests --findRelatedTests <files>`;
- `npx nx show projects --affected`;
- `bazel query 'rdeps(//..., <target>)'`.

`vitest related` runs the tests, so only use it with the user's agreement.

**History.** List the past changes in `history.json` as
`{changes: [{id, description, diff, full_run: true, runs: [{package, junit}]}]}`. Each diff
applies to the repo, unless the change names a `base` git ref it applies to; or name a `commit`
instead of a `diff` (the diff is the commit against its parent, replayed on a fresh checkout of
the parent), which makes real git history usable while the repo is at HEAD. Each run is that
package's full suite after the change, as JUnit.

**Failures at the base.** Tests that fail before any change (only in this environment, a port
already in use, a missing SDK) are not evidence. List them in a JSON file
(`{"failures": [test files or ids]}`; an id marks its whole file) and pass it as
`--baseline-failures` to `select_tests.py` (they are left out of co-failure) and `replay.py`
(not counted, listed as `baseline_failures`). Don't put them in `--flaky`: they are not flaky.

**Journeys.** journey-mapper's output. The selector reads, for each journey:
- its `tests`;
- its priority;
- the evidence files in its steps and variants.

## Step 3: Select

```bash
python scripts/select_tests.py --repo <repo> [--diff change.diff | --base main] [--mode strict] \
    --coverage <run>/coverage_map.json --journeys journey_map.json --history history.json \
    [--related related.json] --out <run>/selection.json
```

The script builds the base tree and the changed tree in scratch copies, and never touches the
repo. It then classifies each changed file:

| Kind | What it covers |
|---|---|
| `source` | Source code |
| `test` | A test file |
| `snapshot` | A test snapshot |
| `shared_fixture` | `conftest.py`, helpers, factories, setup files |
| `global_config` | Lockfiles, CI, build configuration (a package's own manifest is bounded to that package) |
| `data_or_schema` | Migrations, schemas, data files |
| `non_code` | Styles, templates, translations, images |
| `docs` | Documentation |

Evidence is collected for source files:

| Source | Evidence | Strength |
|---|---|---|
| `coverage` | A test ran a changed base line, a line next to an insertion, or the edited function | 3 |
| `import_graph` | The test imports the file, directly or through a chain (pytest fixtures count) | 2 |
| `framework_tools` | The repo's related-tests tool lists the test | 2 |
| `journeys` | A journey cites the file and links the test | 2 |
| `naming` | `pricing.py` matches `test_pricing.py`, `x.ts` matches `x.test.ts`, `Foo.kt` matches `FooTest.kt` | 1 |
| `co_failure` | The test failed in at least 2 past changes to the file | 1 |

A changed test file, or a test whose snapshot changed, is always selected at strength 3. A test
is selected at strength 2 or more, or at strength 1 when two different sources agree. Tests that
had only one weak reason are listed under `considered_not_selected`.

## Step 4: Blind spots

| Kind | When | `strict` action |
|---|---|---|
| `global_config` | A lockfile, CI or repo-level build file; a package's own manifest or test config | Full suite; the package for a package's own file |
| `shared_fixture` | `conftest.py`; a helper or setup file | Every test under the conftest's directory; a helper's importers, or the package if nothing imports it (it's loaded by runner config) |
| `dynamic_reference` | The file contains a computed import, is a target of one (by literal prefix), or has no static importer in a package with an unbounded one | The package |
| `data_or_schema` | A migration, schema, or data file inside a package | The package's tests plus any contract tests |
| `unmapped_source` | No test is selected for the file by its own evidence | The package, and say no tests were found (hand it to ticket-to-tdd or journey-to-tests) |
| `deleted_or_renamed` | A removed or renamed module | Tests that imported it in the base tree |
| `non_code` | A style, template, translation or image | E2E and visual tests that reach it; otherwise every e2e and visual test in the package; otherwise the package |
| `huge_change` | More than 30% of source files changed | The full suite: selection would save little |

How the other modes handle blind spots:
- **`balanced`** widens only `global_config`, `shared_fixture`, `dynamic_reference`,
  `deleted_or_renamed` and `huge_change`, and labels the rest. Use it only when the user asks,
  and only with a fresh coverage map and history.
- **`aggressive`** labels every blind spot and widens nothing.

Labelled blind spots still list `would_widen_to`.

Read each blind spot. Add a `comment` where it helps the reader, for example why a dynamic
import's reach is what it is. Never change its action.

## Step 5: Commands

`selection.json` holds one command per package and framework, run from the package directory.
A command that runs a whole suite has no test arguments, and a long command is split
(`--max-chars`, default 8000).

```bash
python scripts/build_command.py <run>/selection.json --ci github --ci-out <run>/ci_snippet.yml   # optional
```

A JavaScript package's own `test` script decides its runner when it names one (vitest, jest,
mocha, `playwright test`, `cypress run`, `node --test`). A package whose script runs something
else (the repository's own CLI, a script that needs a device or a model) gets no command: its
selected tests are in `command.no_runner`, with the script in `command.no_runner_reasons`; tell
the user to run them with that script. When selected tests in one workspace package import a
changed package by name and that package publishes a build (`main`/`exports` pointing at
`dist/`), `command.build_first` builds it first (`pnpm run build` in that package); otherwise the
tests would run the previous build. A package with no build script gets a note instead.

## Step 6: Replay (when there is history)

```bash
python scripts/replay.py --repo <repo> --history history.json --coverage <run>/coverage_map.json \
    --journeys journey_map.json [--flaky flaky.json] [--baseline-failures base.json] --out <run>/replay.json
```

A change whose diff does not apply (to `--repo`, or to its `base`) is skipped with the reason in
`changes_skipped`; the replay goes on. Say how many were skipped.

Each past change is selected the way it would be now, using only the history before it. Every
test file that failed in its full run is then checked against the selection. Recall is counted
per (change, failing test file) and given with a 95% Wilson interval.

Failures of known-flaky tests are listed but not counted.

Recall says little when the selections take most of the suite: if the replay's
`mean_selected_share` is 0.5 or more, the headline must give it beside the recall (the validator
checks), for example "26 of 26 failures selected, but replayed selections took 82% of the tests".

For every miss, write `why_missed` and an `explanation`:
- **`missing_source`**: a source that would have linked the test was unavailable. Name the
  source.
- **`weak_rule`**: the rules missed a link that exists, such as a registry, runner
  configuration, or reflection.

Add the `fix`, and rank `missing_source` misses first.

## Step 7: Verify (optional)

With the change applied in a scratch copy, run the selection and the full suite per package:

```bash
python scripts/run_tests.py --preset pytest --cwd <copy>/api --tests <selected> --label "verify-selected api" --out <run>/verify/runs.json
python scripts/run_tests.py --preset pytest --cwd <copy>/api --label "verify-full api" --out <run>/verify/runs.json
```

The label's last word is the package. List every test file that failed in a full run:
- in `failures_inside_selection` if it was selected;
- otherwise in `failures_outside_selection`. Each of these is a miss, and is also named in
  `limitations`.

## Step 8: Report, validate, render

Build `impact_report.json`. Copy `sources`, `changes`, `selected`, `blind_spots`, the counts,
`command` and the packages from `selection.json` unchanged. Copy the replay figures from
`replay.json`.

Then write these yourself:
- the headline, with "N of M test files";
- each miss's `why_missed`, `explanation` and `fix`;
- `recommendations.full_suite`, saying when the full suite runs;
- other recommendations;
- limitations.

```bash
python scripts/validate_impact_report.py <run>/impact_report.json --selection <run>/selection.json \
    --replay <run>/replay.json [--runs <run>/verify/runs.json] --recompute --write-summary
python scripts/render_report.py <run>/impact_report.json [--brief]
```

The two flags:
- `--write-summary` computes the summary: counts, evidence completeness, recall status and text.
- `--recompute` reruns the selection from its recorded inputs. Run it from the directory those
  paths are relative to.

Fix every error rather than editing computed fields.

Show the user:
1. the headline;
2. the commands;
3. the blind spots and their actions;
4. the selected tests with their strongest reason;
5. recall or `not_assessed`, the misses, and what didn't run.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, change (diff hash, base, head, title), mode and why, code hash, selection file, file-kinds hash, packages with their test files |
| `sources[]` | coverage, import_graph, framework_tools, journeys, naming, co_failure: status, reason, version, completeness factor |
| `changes[]` | Path, status, kind, line ranges, rename |
| `selected[]` | Test file, package, level, strength, reasons, journey priority, past failures, the blind spots that widened to it, order |
| `blind_spots[]` | Kind, action, required scope, widened_to, would_widen_to, note, your comment |
| `command` | Commands per package and framework, split count |
| `replay` | Counts, recall, interval, mean share selected, misses with why and fix, flaky failures |
| `verify` | Run ids, failures inside and outside the selection |
| `recommendations`, `ci_snippet` | When the full suite runs; other advice; where the snippet is |
| `summary` | Headline, computed counts, evidence completeness, recall status and text, sources not run |
| `limitations[]`, `files_written[]` | What the selection can't show; files written (never CI configuration) |
