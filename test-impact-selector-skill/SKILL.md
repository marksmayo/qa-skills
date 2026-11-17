---
name: test-impact-selector
description: Given a change (a branch, a commit range or a diff), pick which tests to run, give every selected test its reasons and evidence (coverage of a changed line, an import chain, a journey link, naming, co-failure history), and list every change that no evidence can bound as a blind spot that widens the selection (strict by default). Outputs ready-to-run commands per package and framework, an optional CI snippet for the user to add, and, when past runs are available, measured recall with a Wilson interval from replaying history. Never calls unselected tests "safe to skip".
version: 1.8.0
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
| Fixture projects | left out | `--include-fixtures` counts their tests; `--fixture-dir NAME` names another fixture folder; `--exclude DIR` leaves out any folder |

## Step 1: Recon

```bash
python scripts/detect_conventions.py <repo>                     # packages, frameworks, run commands
python scripts/import_graph.py <repo> --out <run>/graph.json    # files, test levels, import edges, dynamic sites
```

Packages come from manifests: `pyproject.toml`, `package.json` workspaces, `go.mod`, Gradle
modules and the like. The import graph parses Python (`ast`), and JavaScript and TypeScript
(imports, `require`, `import()`, `import.meta.glob`, `require.context`, workspace names, tsconfig
`paths`). It also records which conftest fixtures each pytest file uses.

Python test files that put a folder on `sys.path` and then import a module by its bare name
(`sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))`,
`os.path.join(os.path.dirname(__file__), "..", "scripts")`, also in a `conftest.py` above them)
reach that folder's module (`via: import (sys.path)`). When the folder can't be worked out
statically (an environment variable, `PYTHONPATH`), the import of a module that exists in the
repository is recorded under `unresolved` with its candidate files. A change to such a candidate
is a `dynamic_reference` blind spot with scope `unresolved_importers`: the tests among the
importers and the tests that reach them are run, unless import evidence already links each of
them. It is never silently dropped.

A script imports its siblings by bare name (`from rainfall import total` in `scripts/report.py`):
Python puts a script's own folder first on `sys.path`, so the import resolves there
(`via: import (script folder)`), and a change to `rainfall.py` selects the tests of every script
that imports it, with the chain. Tests that load a script by its file path
(`importlib.util.spec_from_file_location`, `SourceFileLoader`, `runpy.run_path`, with a literal
path or one built from `__file__`, also through a helper called with a literal name; the shared
`py_loads.py`) reach it (`via: spec_from_file_location`). A load whose path can't be worked out
(from the environment, a loop over names) is a dynamic site: a changed script nothing else links
is a `dynamic_reference` blind spot that names it. Naming matches `test_install_gauge.py` to a
hyphenated `install-gauge.py`.

A directory named `e2e`, `test`, `tests` or `spec` is test code unless it is a product package
(it holds a manifest and ships code: a `src/` directory, a `package.json` with
`main`/`exports`/`bin`, ...). A workspace package called `e2e` is therefore source, and only its
own `tests/` are tests, with levels judged inside the package (its `tests/unit/` are unit tests).
The graph notes such packages; check them, since a misread package turns every change into a
shared-fixture blind spot.

**What counts as a test.** Not file names alone:
- **Fixture projects are left out.** Sample projects under `examples/`, `fixtures/`, `testdata/`,
  `samples/` (and the like) that hold their own manifest, expected outputs or an app of their own
  are the repository's test data, not its tests: their planted failing tests, their lockfiles and
  their packages are not counted. `selection.json` lists them under `excluded` (copy it into the
  report; the validator rejects a selected test inside one). `--include-fixtures` counts them,
  `--fixture-dir NAME` adds a folder name, `--exclude DIR` leaves out any folder.
- **Python test files are decided by configuration and content.** A file is a pytest or unittest
  test when its governing pytest configuration's `python_files` (or the default `test_*.py` /
  `*_test.py`) matches and it defines a test function or class (`python_functions`,
  `python_classes`, a `unittest.TestCase` subclass). A `benchmarks/pytest.ini` with
  `python_files = bench_*.py` makes the benchmarks tests; a product module named
  `test_paths.py` that defines no test is source (listed in the notes).
- **Scripts that test themselves are tests.** detect_conventions' `self_tests` (a script with an
  argparse `--self-test` flag, a `self_test()` run under `__main__`, a `self_test.py`) are tests run
  by their command from the repository root. A script with its own `--self-test` is also source,
  so a change to it selects its own self-test, and every self-test that imports it.
- **The guide's checks are tests.** `python <script> ...` lines in shell code blocks of root-level
  Markdown (the contributor or agent guide, the README) that run a validator or checker
  (`validate*`, `check*`, `verify*`, `--check`, `--self-test`) on files that exist, with no
  placeholder, pipe or output flag, are tests with the id `<guide>:<line>`. Each depends on its
  script and on every file or folder it names, so a change to a sample it validates selects it.

Languages without a parser (Java, Kotlin, C#, Swift, Dart and others) are listed as
`no_parser`, and so are the changed files in a language nothing here reads at all: script
languages (PowerShell, shell, batch), UI markup (Slint, QML, XAML) and Lua, C and the like
(`lang_census.py`'s names). Their files rely on the other evidence sources, and on blind spots. `graph.json`
carries `language_coverage` (vendored and embedded third-party trees left out), and the console
warns when the repository's dominant language has no parser. When a changed file is in such a
language, `selection.json` carries `language_coverage` with `warning` set and the languages in
`changed_unparsed`: copy it into the report and name each of those languages in `limitations`
(the validator checks). Each such file is an `unmapped_source` blind spot widened to its
package, or to the full suite when no test file of its package could be found, so the
selection never shrinks to nothing because a parser is missing.

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

**Rust.** A repository (or a top-level folder) with a `Cargo.toml` is read with
`cargo_project.py`: crates, their targets (lib, bins, `tests/*.rs`, benches, examples, build
script) and their `#[test]` functions. No cargo or build is needed.
- A file holding `#[test]` functions is a test file: a source file with a `#[cfg(test)]` module
  (unit tests; the file is still source, and a change to it selects it) or a file of a
  `tests/*.rs` target (integration tests). Helper binaries under `tests/fixtures/` are not tests;
  a change to one widens to its crate's tests. A shared module such as `tests/common/mod.rs`
  reaches the integration tests that include it.
- A file's build is the targets whose module tree (`mod x;`) holds it (`cargo_project.py`'s
  `targets_reaching`). A library file reaches its crate's lib, bin and test targets and those of
  every crate that depends on it by path (through normal dependencies transitively; a
  dev-dependency adds its crate's tests but goes no further). A binary's module (no library holding
  it) reaches that binary's unit tests and its crate's integration tests, which cargo builds the
  binary for and which may run it (`CARGO_BIN_EXE_<name>`, assert_cmd). A build script reaches
  what its crate's library and binaries reach. Each test file of a reached target is import-graph
  evidence (`via: cargo <kind> target <crate>/<target>`).
- A bench file is kind `bench` (also with `--cargo-metadata`): `cargo test` does not build or
  run benches, so it selects nothing and the notes say to check it with
  `cargo bench -p <crate> --no-run`.
- The workspace `Cargo.toml`, `Cargo.lock`, `rust-toolchain(.toml)` and `.cargo/config(.toml)`
  are `global_config` widened to every Rust test (scope `language`), not to the Python or
  JavaScript tests beside them. A crate's own `Cargo.toml` widens to its crate. A Python or
  JavaScript change never widens to Rust tests, and a Rust change never to theirs.
- Commands come from the libtest ids (`<target>::<path>`, as `run_tests.py` records them): every
  Rust test file selected runs the CI's own `cargo test --workspace ...` line (or `cargo test
  --workspace`); a whole crate `cargo test -p <crate>`; otherwise
  `cargo test -p <crate> --lib|--bin <b>|--test <t> -- --exact <paths>`, with the ids in
  `test_ids` for `run_tests.py --preset cargo --tests`. `#[rstest]` and `#[test_case]` functions
  generate child tests (`<fn>::case_1...`) that `--exact <fn>` would not match: they get commands
  of their own without `--exact` (name filters, which may also run tests whose names contain them),
  with a `note` saying so; repeat it when you hand the command over. Tests behind a cargo feature
  the crate does not enable by default, directly or through the features its defaults enable (a
  `#[cfg(feature = "x")]` on the test, its module, or the `mod` declaration in its own target
  that includes it; `any(feature = "x", test)` does not gate) are listed in
  `command.feature_gated`. The commands do not run them, so say so and give the `--features`
  command the CI uses for them.
- `--cargo-metadata <file>` (the output of `cargo metadata --format-version 1 --no-deps`, if the
  user can run it) takes the targets and features from cargo itself. Its absolute paths may come
  from another checkout or a scratch copy: they map through the metadata's `workspace_root` (else
  each package's manifest folder). It never makes the reading worse: a crate whose metadata targets
  are not files of this tree (another project's or an older commit's output, a generated target) is
  read from its manifest instead, and a note says how many crates came from cargo and which did not,
  and why. Metadata naming none of the repository's crates is ignored, and the note says so.
- **Tests the command does not run.** `cargo_project.py` decides for each test whether a plain
  `cargo test` runs it (`runs_by_default`, with the gates in `why`): a binary or test target with
  `required-features` the defaults do not enable (cargo skips the whole target), `#[ignore]`, a
  feature cfg, a platform cfg (`unix`, `windows`, `target_os`) or a cfg this reader cannot decide.
  Selected tests with such a gate are listed in `command.not_run_by_default` (crate, gates,
  detail, tests, files, `platforms`, and `run_with` when a flag runs them: `--features x`,
  `-- --ignored`), never counted as run. A test no platform runs by default is left out of the
  `--exact` names (cargo would run nothing and pass); a command's `tests` counts only the files it
  runs a test of, `not_run` the selected tests it never runs and `platform_dependent` those it runs
  on some platforms only (a Windows run passes without running a `#[cfg(unix)]` test). Name each
  gate in a limitation (the validator checks: the feature, the ignore, the platforms). A selected
  Rust file carries its libtest ids in `test_ids`.
- There is no cargo coverage ingestion: `coverage_map.py` reads coverage.py JSON (or the mutation
  tester's `coverage.json`), not cargo-llvm-cov or grcov output, so Rust coverage stays `not_run`.
- Doc tests are not selected by file; `cargo test --doc -p <crate>` runs them.

## Step 2: Evidence sources

Prepare the sources that the repo already has. Install nothing without asking.

**Coverage.** Use per-test coverage the repo already produces:

```bash
python scripts/coverage_map.py normalise coverage.json --repo <repo> --package api --out <run>/coverage_map.json
```

It reads either of these:
- coverage.py JSON with contexts, from `COVERAGE_CORE=ctrace pytest --cov=<pkg> --cov-context=test
  --junitxml=junit.xml` then `coverage json --show-contexts`;
- the mutation tester's `coverage.json`.

On Python 3.12+ coverage.py may pick its sys.monitoring core, which does not support per-test
contexts: it warns "Dynamic contexts aren't supported with core=sysmon" and keeps contexts for a
handful of tests, and the map silently misses the rest. Always collect with `COVERAGE_CORE=ctrace`,
and give `normalise --junit junit.xml` (the same run's JUnit): it refuses a map whose contexts
name fewer than half the tests that ran. Narrow `--cov` to the product packages (`--cov=app
--cov=api`): a JSON with contexts grows with tests times lines, and a large app can reach hundreds
of megabytes.

Normalise straight after collecting, because the file hashes are taken then. Only when the user
agrees, `coverage_map.py generate` runs one instrumented full run itself (it needs coverage.py
and pytest-cov); it sets `COVERAGE_CORE=ctrace` (unless set), records it in the command, and
makes the same check.

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
- `bazel query 'rdeps(//..., <target>)'`;
- Rust: the import graph already uses the crate graph; `cargo metadata --format-version 1
  --no-deps` (passed as `--cargo-metadata`) or `cargo nextest list` gives cargo's own targets.

`vitest related` runs the tests, so only use it with the user's agreement.

**History.** List the past changes in `history.json` as
`{changes: [{id, description, diff, full_run: true, runs: [{package, junit}]}]}`. Each diff
applies to the repo, unless the change names a `base` git ref it applies to; or name a `commit`
instead of a `diff` (the diff is the commit against its parent, replayed on a fresh checkout of
the parent), which makes real git history usable while the repo is at HEAD. Each run is that
package's full suite after the change, as JUnit. A `commit` or `base` must be a commit id or ref name
(never starting with `-`); a commit must resolve with `git rev-parse --verify`, and git gets the resolved
id after `--end-of-options`, so a history file can never pass git an option such as `--output`.

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
| `bench` | A Rust bench target's file (`cargo test` does not run benches; noted, nothing selected) |
| `fixture` | A file inside an excluded fixture project or folder |

Evidence is collected for source files:

| Source | Evidence | Strength |
|---|---|---|
| `coverage` | A test ran a changed base line, a line next to an insertion, or the edited function | 3 |
| `import_graph` | The test imports the file, directly or through a chain (pytest fixtures count) | 2 |
| `framework_tools` | The repo's related-tests tool lists the test | 2 |
| `journeys` | A journey cites the file and links the test | 2 |
| `naming` | `pricing.py` matches `test_pricing.py`, `x.ts` matches `x.test.ts`, `Foo.kt` matches `FooTest.kt` | 1 |
| `co_failure` | The test failed in at least 2 past changes to the file | 1 |

**Server-rendered templates** (Flask, FastAPI and Django with Jinja or Django templates) are part
of the web surface. The import graph links a view module to each template it renders by a
literal name (`render_template("x.html")`, `TemplateResponse`, `render(request, "x.html")`,
`template_name = "x.html"`), a template to the ones it extends, includes or imports, and a
template to the static files it links (`url_for('static', filename=...)`, `{% static %}`,
`/static/...`). A changed template therefore gets import-graph evidence through the views that
render it, and coverage evidence from tests that ran a rendering view function. It stays a
`non_code` blind spot (only a rendering test notices), widened as below.
A changed test file, or a test whose snapshot changed, is always selected at strength 3. A test
is selected at strength 2 or more, or at strength 1 when two different sources agree. Tests that
had only one weak reason are listed under `considered_not_selected`.

## Step 4: Blind spots

| Kind | When | `strict` action |
|---|---|---|
| `global_config` | A lockfile, CI or repo-level build file; a package's own manifest or test config | Full suite; the package for a package's own file; every Rust test (scope `language`) for the workspace's Cargo files; no test (scope `no_existing_test`) for a newly added CI file, which adds jobs and cannot change how an existing test runs |
| `shared_fixture` | `conftest.py`; a helper or setup file | Every test under the conftest's directory; a helper's importers, or the package if nothing imports it (it's loaded by runner config) |
| `dynamic_reference` | The file contains a computed import, is a target of one (by literal prefix), or has no static importer in a package with an unbounded one (a load by a path that can't be worked out included); or it is the candidate of an import the graph could not resolve | The package; for an unresolved import (scope `unresolved_importers`), the importing tests and the tests that reach the importers |
| `data_or_schema` | A migration, schema, or data file in a directory (also in a single-package repo; only a data file at the repository root is `global_config`) | The package's tests plus any contract tests |
| `unmapped_source` | No test is selected for the file by its own evidence | The package's tests of the file's language (never Rust tests for a Python file, or the reverse); the full suite when the language has no parser and the package has no test that could be found. The note says "No tests found" (hand it to ticket-to-tdd or journey-to-tests) only when nothing links the file and its scope holds no test |
| `deleted_or_renamed` | A removed or renamed module | Tests that imported it in the base tree |
| `non_code` | A style, template, translation or image | E2E and visual tests that reach it; otherwise every e2e and visual test in the package; otherwise the tests that reach it through the views and templates that render or link it; otherwise the package |
| `huge_change` | More than 30% of source files changed | The full suite: selection would save little |
| `fixture_project` | A file in an excluded fixture project (its lockfile too: it is fixture-local, never `global_config`) | `fixture_host`: the tests under the folder that owns the sample (`tool/` for `tool/examples/app/`) and every test that names or imports it |

A data file, a script in an unread language (PowerShell, shell) or UI markup in the root package
widens by evidence, not to everything: Rust and Go tests read such a file only through a path
written in their source, so they stay in only when a file of that language names the file or its
folder. Otherwise they are left out and the blind spot lists them under `left_out` (family, count,
reason) and says so in its note, with the test files that name the file (`tests/test_x.py:14`).
Tests of interpreted languages stay. A helper under a crate's `tests/fixtures` is never described
as loaded by a JavaScript runner's `setupFiles`.

A repository root with no manifest (a tool repository of folders with scripts) is not one package:
for a file in a top-level folder that holds tests, "the package" is that folder's tests, the
guide checks that run its scripts and every test that reaches the file; the note says so.

Dynamic loading sites are matched in code only: `import(` inside a JavaScript string or template
literal is text. A site only reaches files its language can load (a JavaScript `import()` never a
`.py` module, Python's importlib never a `.js` file).

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

Python tests run with the package's runner (`detect_conventions.py`'s `python_runner`): when CI
runs `unittest` and no test uses pytest, the command is `python -m unittest <files>`, and for the
whole suite the CI's own command (`command_from` names the CI line), run from the repository root
because its paths are repository-relative. Otherwise `python -m pytest`, from the folder of the
pytest configuration that governs the tests when that is not the package (`benchmarks/`). A
command without test arguments is only given when no excluded fixture project lies below its
folder; otherwise the selected files are named, so a bare `pytest` never collects planted failing
tests. Self-tests (framework `selftest`) and guide checks (framework `command`) run one command
each from the repository root; to run them with JUnit results, give the commands to
`run_tests.py --preset selftest --selftest-cmd "<command>"`. Rust commands are described under Step 1. A test file with no runner this selector
knows is in `no_runner`, with the reason in `no_runner_reasons`.

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
python scripts/run_tests.py --preset unittest --cwd <copy> --runner-args "-s tests" --label "verify-full ." --out <run>/verify/runs.json
python scripts/run_tests.py --preset cargo --cwd <copy> --runner-args "-p <crate>" --label "verify-selected ." --out <run>/verify/runs.json
```

The label's last word is the package (`.` for the repository root; a last word that is no
package's root is read as the root). The cargo and unittest presets need `--runner-args`: the
crate (`-p <crate>`, or `--workspace`) and the start folder (`-s tests`; the preset runs discover itself). List every test file that failed
in a full run:
- in `failures_inside_selection` if it was selected. A libtest unit test may be recorded under its
  crate's root file (`src/lib.rs`) when the run was parsed without the Cargo model: it counts as
  inside when its id is one of a selected file's `test_ids`;
- otherwise in `failures_outside_selection`. Each of these is a miss, and is also named in
  `limitations`;
- unless the runs show it is a known failure, listed in `verify.known_failures`
  (`{id, kind, run_ids, note}`): `flaky` when the named runs of the same code (same `code_hash`)
  hold a pass and a failure of that test (re-run it alone and in a second full run), `baseline`
  when it fails in a `verify-base ...` run of the tree before the change. A known failure is not a
  miss, but it is still named in `limitations`; the validator re-reads the runs.

## Step 8: Report, validate, render

Build `impact_report.json`. Copy `sources`, `changes`, `selected`, `blind_spots`, the counts,
`command`, the packages and `language_coverage` (when present) from `selection.json` unchanged.
Copy the replay figures from `replay.json`.

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

`--brief` keeps the limitations, the tests the commands do not run and the known failures.

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
| `excluded` | Fixture projects and folders left out of the tests, with the reason (when any) |
| `command` | Commands per package and framework (`command_from` when it is the CI's own line, `test_ids` for cargo), split count, `no_runner` and reasons, `feature_gated` Rust tests, `not_run_by_default` Rust tests (required-features, ignore, platform, feature) |
| `language_coverage` | Parsed and unparsed languages, changed files in unparsed ones, and the warning to name in limitations |
| `replay` | Counts, recall, interval, mean share selected, misses with why and fix, flaky failures |
| `verify` | Run ids, failures inside and outside the selection, known failures (flaky or failing at the base) with the runs that show it |
| `recommendations`, `ci_snippet` | When the full suite runs; other advice; where the snippet is |
| `summary` | Headline, computed counts, evidence completeness, recall status and text, sources not run |
| `limitations[]`, `files_written[]` | What the selection can't show; files written (never CI configuration) |
