# Design: `test-impact-selector-skill`

Status: Built (v1.3.0), see "As built" at the end. Build order: 10.

## Mission

Given a change, the skill picks which tests to run, explains why each one was picked, and lists
everything it couldn't reason about. It outputs a ready-to-run command for the repo's framework,
and, when history is available, measures how often its selections would have missed a failure.

How it differs from the other skills:

- **test-suite-evaluator** judges the suite. This skill uses the suite's structure to make a
  per-change decision.
- **journey-mapper** links tests to user journeys. This skill reads those links as one source of
  evidence among several.
- **flaky-test-hunter** and **mutation-tester** use `diff_scope.py` the same way; their scoping
  is about what code to analyse, and this skill's is about what tests to run.

## Decisions

- **Safety mode `strict` is the default.** Any blind spot in the change (a config file, a fixture,
  a dynamic import, a file with no mapped tests) widens the selection to the whole affected package,
  or to the full suite when the package can't be bounded.
- **Selection is advice.** The skill doesn't edit CI configuration. It can write a selection file
  and a command, and, as an optional output, a CI snippet (GitHub Actions or GitLab) for the user to
  add themselves.
- **The full suite still runs somewhere.** Every report recommends a full run on merge or nightly,
  because selection without a periodic full run hides misses.
- **No commits, no Jira write-back.**

## Non-negotiable rules

- **Every selected test has at least one reason** with its evidence (a coverage hit on a changed
  line, an import path, a naming rule, a journey link, a co-failure count).
- **Every change that can't be mapped is a blind spot,** listed with what kind it is and what the
  skill did about it. A blind spot is never silently dropped.
- **No "safe to skip" claims.** Excluded tests are "not selected by this evidence", and the report
  says which evidence sources were available.
- **Recall is measured, not asserted.** When history allows a replay, the report gives missed
  failures with counts and a 95% Wilson interval. Without history, recall is `not_assessed`.
- **A stale coverage map is labelled stale.** Coverage collected on a commit older than the base
  ref is used only with a warning, and changed files it doesn't know about are blind spots.
- **Sources that couldn't run are recorded** as `not_run` or `errored` (no coverage map, no import
  parser for a language), and lower the evidence-completeness figure.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | |
| `change` | working tree vs merge base of `main` | A ref range, or a unified diff file (`--diff change.diff`) |
| `mode` | `strict` | `strict`, `balanced` (widen only for high-risk blind spots; allowed only when a coverage map and history exist), `aggressive` (never widen; label every blind spot) |
| `coverage` | optional | Per-test coverage map the repo already produces (coverage.py contexts, Istanbul JSON per test, JaCoCo per test, Go cover profiles); `generate_coverage: true` runs one instrumented full run to make one |
| `history` | optional | Past runs: JUnit XML plus the change each ran against, for co-failure and replay |
| `journeys` | optional | journey-mapper output, for journey-linked tests |

## Workflow

1. **Change set.** `diff_scope.py` gives files, line ranges, renames and deletions.
2. **Classify each changed file:** source, test, fixture or conftest, config (build, CI,
   dependency lock, env), schema or migration, asset or template, generated, docs. Docs-only
   changes select nothing and say so.
3. **Collect evidence** per changed file, from each source that can run:

   | Source | How | Strength |
   |---|---|---|
   | Coverage | Tests whose recorded coverage includes a changed line (or the changed function when lines moved) | 3 |
   | Import graph | Tests that import the changed module directly or transitively (Python `ast`, JS/TS import and require parsing, Java and Kotlin packages, Go packages, Dart imports, Swift targets) | 2 |
   | Framework tools | `jest --findRelatedTests`, `vitest related`, Nx or Turborepo affected, Bazel `rdeps`, Gradle module dependencies | 2 |
   | Journey links | Tests linked to a journey whose steps reference the changed file | 2 |
   | Naming | `foo.py` ↔ `test_foo.py`, `Foo.kt` ↔ `FooTest.kt`, `x.ts` ↔ `x.test.ts` | 1 |
   | Co-failure history | Tests that failed in past runs when this file changed (≥ 2 times) | 1 |

   Changed test files are always selected (strength 3).
4. **Score and select.** A test's strength is its strongest reason; it's selected at strength ≥ 2,
   or at 1 when two different sources agree.
5. **Blind spots.** Each one gets a kind and an action:

   | Kind | Examples | `strict` action |
   |---|---|---|
   | `global_config` | `pyproject.toml`, `jest.config.*`, `build.gradle`, CI files, lockfiles | Full suite |
   | `shared_fixture` | `conftest.py`, test helpers, factories, seeds | Every test in the fixture's reach |
   | `dynamic_reference` | `importlib`, `require(variable)`, reflection, DI containers, plugin registries | The whole package |
   | `data_or_schema` | Migrations, OpenAPI files, GraphQL schemas, JSON fixtures | Tests in the package plus contract tests |
   | `unmapped_source` | A source file no source of evidence links to any test | The whole package, plus a "no tests found" note |
   | `deleted_or_renamed` | Removed modules | Tests that imported them, from the base ref's import graph |
   | `non_code` | Templates, CSS, translation files, images | E2E and visual tests that render them |

6. **Command.** Build the run command in the repo's framework (pytest node ids, Jest paths, Gradle
   `--tests`, `go test -run` per package, Playwright `--grep`, `xcodebuild -only-testing`), split
   when it exceeds the shell's limit.
7. **Replay** (when history exists). For each past change with a full run, compute the selection
   and check whether every test that failed was selected. A failure outside the selection is a
   miss, with the change, the test and why it wasn't selected.
8. **Optional verify run.** Run the selection, and the full suite if the user allows, and compare.
9. **Report, validate and render.**

## Ranking

This skill selects rather than ranks findings. The report orders selected tests by strength, then
by journey priority, then by past failure rate, so the most informative tests run first when the
command is used with fail-fast. Misses found by replay are ranked by how often the same reason
would recur: `missing_source` (a source was unavailable) above `weak_rule` (the rules missed a
reachable link).

## Output (`impact_report.json`)

```text
metadata:     repo, change{base, head, diff_sha256}, mode, framework, generated_at
sources[]:    {name, status: ran|not_run|errored|stale, version, reason, collected_at_commit?}
changes[]:    {path, kind, line_ranges, renamed_from?}
selected[]:   {test_id, file, strength, reasons[{source, evidence, changed_path}], order}
blind_spots[]: {path, kind, action, widened_to[], note}
not_selected_count, total_tests
command:      {framework, argv[], split_into}
replay?:      {changes_replayed, failures_total, failures_missed, recall, wilson_low,
               wilson_high, misses[{change, test_id, why_missed}]}
verify?:      {selected_run_id, full_run_id, failures_outside_selection[]}
summary:      {selected, widened_by_blind_spots, evidence_completeness, recall_status}
limitations[]
```

## Validator fails when

- A selected test has no reason, or a reason cites a source recorded `not_run`.
- A changed file is neither linked to a selected test, nor a blind spot, nor classified as docs.
- In `strict` mode, a blind spot's action is weaker than the table says.
- Recall is given without replay counts, or the interval doesn't match them.
- A verify run found failures outside the selection and the report doesn't list them as misses.
- Any text says unselected tests are "safe to skip" or the selection is "safe".

## Edge cases

- **Monorepos.** Package boundaries come from workspaces (npm, pnpm, Yarn), Gradle settings, Go
  modules, Bazel, or Nx and Turborepo project graphs. Cross-package imports are followed.
- **Mobile.** Android Gradle modules and iOS Xcode targets bound the widening. UI tests
  (Espresso, XCUITest, Detox, Maestro) are selected by screen-name links and journey links, and are
  otherwise a `non_code` widening.
- **No tests at all for a changed area.** Reported as `unmapped_source` with a suggestion to hand
  the area to ticket-to-tdd or journey-to-tests.
- **Huge changes** (more than 30% of source files). The skill recommends the full suite and says
  why; selection would save little.
- **Flaky tests in replay.** A failure that history shows as flaky is not counted as a miss, and
  the replay lists it separately.

## Tool stack

Stdlib Python for the diff, import graphs, naming rules and replay. Optional, all open source:
coverage.py (Apache-2.0) contexts, Jest and Vitest related-tests modes, Nx and Turborepo (MIT),
Bazel (Apache-2.0), JaCoCo (EPL-2.0), Go's cover tool. Versions are recorded.

## Files

```text
test-impact-selector-skill/
  SKILL.md  RUBRIC.md  README.md
  data/file_kinds.json              # globs that classify config, fixture, schema, generated files
  schemas/impact_report.schema.json
  scripts/import_graph.py  coverage_map.py  select_tests.py  replay.py  build_command.py
          validate_impact_report.py  render_report.py
          diff_scope.py  parse_results.py  detect_conventions.py   # shared copies
  examples/harbourline/  changes/  history/  impact_report.example.json
```

## Example

`examples/harbourline/` is a small monorepo: a Python API package with pytest, and a TypeScript web
package with Vitest. There's no nested git repository; `changes/*.diff` are the changes the example
selects for, and `history/` holds 12 past changes with their real full-run JUnit results. The
answer key:

| Change | Expected |
|---|---|
| A pricing function edit | Its unit tests by coverage, two API tests by import, one journey-linked e2e test |
| `conftest.py` fixture edit | `shared_fixture` blind spot widening to every API test |
| A plugin loaded with `importlib` | `dynamic_reference`, whole package |
| `pnpm-lock.yaml` change | `global_config`, full suite |
| A CSS change | `non_code`, the visual and e2e tests that render it |
| Replay of 12 changes | One planted miss (a test reaching the code through a registry), recall 11/12 with its interval |

## Resolved questions

Agreed with the user before the build.

1. **Default mode?** `strict`; `balanced` only when a coverage map and history exist.
2. **Generate per-test coverage?** Use maps the repo already produces; generating them is opt-in.
3. **Write a CI snippet?** Yes, as an optional output.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Selection is per test file.** Coverage gives test cases, but import, naming, journey and
  framework evidence are all file-level, so a selected file runs every test in it. The coverage
  reason names the test cases that ran the lines.
- **Two trees, never the repo.** `select_tests.py` builds the base and the changed tree in
  scratch copies:
  - `--diff`: the repo is the base and the diff is applied to a copy;
  - `--diff-applied`: the repo is the changed state and the diff is reverse-applied;
  - git mode: the merge base comes from `git archive`.

  The diff is applied by the script, with exact hunk matching that ignores line endings, so git
  isn't needed for diffs. Every example diff applies and reverse-applies back to the same code
  hash.
- **Coverage on the base tree.** Changed lines are mapped to base lines with `difflib`. A test
  counts when it ran any of these:
  - a replaced or deleted base line;
  - a base line next to an insertion;
  - any line of a base function the change edits.

  A change to a module-level constant has no per-test lines, because those lines run at import;
  the other sources cover it.
- **Coverage formats.** `coverage_map.py` reads coverage.py JSON with contexts, and the mutation
  tester's `coverage.json`. Paths are matched by longest suffix, so coverage from a container
  maps.
- **Freshness is per file.** The map records each measured file's hash. Where one differs from
  the base tree, the source is `stale`, the evidence is tagged, and the coverage factor is
  halved. The design compared commits; per-file hashes work for diff mode too.
  - With `generate`, the hashes are taken at collection.
  - With `normalise`, they are taken at normalisation, so normalise straight after collecting.
  - In WSL, `generate --source harbourline_api` gave exactly the shipped map.
- **pytest fixtures.** A test file depends on a `conftest.py` above it only when it requests one
  of its fixtures, or one is autouse. Following every conftest would link every API test to
  everything the conftest imports.

  A change to the conftest itself is still a `shared_fixture` blind spot. It covers every test
  under the conftest, used or not.
- **Not followed by the import graph** (documented in `import_graph.py`):
  - a package's `__init__.py` when a submodule is imported;
  - Vitest or Jest `setupFiles`.

  A setup file nobody imports is a `shared_fixture` that widens to its package.
- **Dynamic loading.** These count as dynamic sites:
  - `import_module` or `__import__` with a computed name (a literal prefix gives the target
    modules);
  - `iter_modules` and `entry_points` (targets unknown);
  - `import()` or `require()` with a non-literal argument (a template-literal prefix gives
    target paths).

  A changed file is a `dynamic_reference` when any of these holds:
  - it contains a site;
  - it is a site's target;
  - it has no static importer in a package with a site whose targets are unknown.

  `import.meta.glob` and `require.context` with literal patterns resolve to static edges.
- **Kinds and scopes:**
  - A package's own manifest or test config (`web/package.json`, `api/pyproject.toml`) is
    `global_config` bounded to that package. Repo-level files, lockfiles and CI files widen to
    the full suite.
  - `snapshot` was added: a changed snapshot selects its test.
  - `huge_change` was added: more than 30% of source files changed means the full suite.
  - For `non_code` files, evidence isn't collected; only the widening selects tests.
    `checkout.test.ts` imports the stylesheet through `checkout.ts`, but asserts markup.
- **Balanced mode.** It widens `global_config`, `shared_fixture`, `dynamic_reference`,
  `deleted_or_renamed` and `huge_change`, and labels the rest. It refuses to run without a
  coverage map and history.
- **Framework tools take a file.** `--related` reads
  `{tool, version, command, related: {path: [tests]}}`. Nothing runs Jest, Nx or Bazel itself.
  In the example the source is `not_run`, because `vitest related` runs the tests.
- **Replay** selects each past change against the current coverage map, journey map and file
  kinds; `replay.json` says so. Co-failure uses only earlier changes. Recall is counted per
  (change, failing test file).
- **Verify.** The validator reads `run_tests.py` runs labelled `verify-selected <package>` and
  `verify-full <package>`.
- **The validator.** Beyond the design's list, it checks:
  - the report's computed sections equal `selection.json`;
  - with `--recompute`, the selection reruns to the same result;
  - percentages in the headline;
  - when the full suite runs;
  - CI files in `files_written`.
- **Shared-file fixes found while building:**
  - **Windows shims:** `run_tests.py` couldn't start `npx` on Windows. It now looks the program
    up with `shutil.which`.
  - **`code_hash` ordering:** it sorted `Path` objects, which Windows compares
    case-insensitively, so the example's selection failed `--recompute` under Linux. It now
    sorts by POSIX path string, and the five reports validate, with `--recompute`, on both
    Windows (Python 3.14) and WSL Ubuntu (Python 3.12).

    This changes the hash of trees whose files sort differently. The flaky test hunter's
    example runs carry the old value and still validate, because its validator compares the
    report with the runs.
- **The example:**
  - **Replay:** 11 of 12 failing test files selected (91.7%, 95% CI 64.6% to 98.5%). The one
    miss is H07: the e2e test reaches `compact.ts` through the receipt registry that
    `setupFiles` fills. `vitest related` misses it as well.
  - **Verify:** the C1 run found no failures at all, so it shows the mechanism, not a miss.
  - **Coverage:** collected with coverage.py 7.17.2 and pytest-cov 7.1.0 in the throwaway WSL
    venv.
  - **History and verify runs:** made on Windows with Python 3.14, pytest and Vitest 3.2.4.
  - **Hostnames:** replaced with `host` in the JUnit files.
- **Testing:**
  - 22 selector scenarios pass: git mode, deletions, renames, unmapped files, docs-only, each
    mode, stale coverage, `--related`, huge changes, flaky replay, template-literal imports,
    command splitting, snapshots, setup files, changed tests, a diff that doesn't apply.
  - The validator catches 42 of 42 seeded violations.
  - The scripts have only been run on Python 3.12 and 3.14. For 3.9, they were parsed with
    `feature_version=(3, 9)` and checked by hand for 3.12-only f-strings.
- **Found on a TypeScript pnpm monorepo with Vitest 5 (v1.1.0)**:
  - **A product package named `e2e`.** `*/e2e/*` and the shared `diff_scope.py` made every file of
    the core package a test, so one source change became a shared-fixture blind spot reaching 501
    test files with no coverage evidence used. The import graph now judges test-ness and level by
    the path inside such a package (the shared `detect_conventions.strip_product_dirs`), and
    `diff_scope.classify` gets the repo root (shared fix). `data/file_kinds.json` is unchanged,
    so the example's recorded hash still holds. A JSON or manifest in a test directory no test
    imports is no longer described as a runner setup file.
  - **Vitest/Jest coverage.** `coverage_map.py per-file` runs each test file alone with JSON
    coverage and normalises a per-file manifest (`istanbul-per-test-file`); module-level lines
    are dropped like coverage.py's import-time lines. Failed files are listed in the summary.
  - **Commands.** A JS package's own `test` script decides the runner; one that runs an unknown
    tool goes to `no_runner` with `no_runner_reasons`. `build_first` builds a workspace package
    whose `dist/` output selected tests in other packages load.
  - **Validator and renderer.** The replay's `mean_selected_share` is an allowed headline figure,
    and from 0.5 up it must stand beside a recall figure. `--brief` caps the selected list and
    the test arguments of a command at 20.
  - Not done: verify/replay against a base run's failures (pre-existing failures still count as
    misses), per-case flaky handling in replay, import-chain attenuation, a coverage map taken
    with the change applied (still stale for the changed files), pnpm-workspace.yaml globs for
    package membership, a Vitest listing-only related-tests recipe, and history building
    helpers. The examples are unchanged and validate with `--recompute`.
- **v1.2.0: Go.** A review on a Go CLI (Go module at the root, beside a vendored Swift agent and
  Node tooling) found Go quiet or wrong in every script; fixed with red/green cases in the new
  `scripts/self_test.py` (a synthetic Go module; the go and git cases skip without them).
  - `import_graph.py` parses Go: go.mod module paths map imports to package directories, a file
    depends on every non-test file of an imported package, `_test.go` files on their own
    package, `//go:embed` on the embedded files. graph.json carries `language_coverage`
    (lang_census.py) and `--reach` says why nothing reaches a file.
  - A Go package is a directory: `unmapped_source` and the other package-scoped blind spots for a
    `.go` file widen to the directory's tests plus its importers', else the module's Go tests,
    never Swift or Node tests. Go naming requires the same directory.
  - `coverage_map.py go`: one `-cover -coverpkg=<module>/...` test binary per package, one
    coverprofile per test file (or test); `normalise` reads its manifest and refuses a single
    whole-run profile.
  - History: Go JUnit cases map to their `_test.go` file through the shared parse_results (go.mod
    under `--repo`); a package build failure is `<dir>/(package)`, selected when any test file of
    that directory is. A change can name a `base` ref or a `commit` (diff against its parent,
    replayed on a fresh `git archive` of the parent), and a diff that does not apply is skipped
    with its reason instead of ending the replay.
  - `--baseline-failures` (select and replay): tests failing at the base are no co-failure
    evidence and are not counted in recall (`failures_baseline`).
  - The Go command keeps the CI or Makefile `go test` flags (`-race`, `-timeout`, `-tags`, ...;
    `flags_from`), or `--go-test-flags`; it carries `args` (its package arguments), which the
    renderer's `--brief` now uses, so the Run line shows `go test ./a ./b`, not `<N test files>`.
  - Not done: a `-run` pattern when only some files of a package are selected (a Go package runs
    as a unit); verify runs for Go still need `run_tests.py --preset gotestsum` by hand. The
    examples are unchanged and validate with `--recompute`.
- **v1.3.0: a Flask and Jinja site with a large data tree.** Red/green cases in
  `scripts/self_test.py` on a synthetic "ferry timetable" app (views rendering Jinja templates, a
  partial, a stylesheet, a directory of JSON records, a test that never touches the app).
  - **Templates** (`template_render_links_views`): `import_graph.py` links a Python module to each
    template it renders by a literal name (`render_template`, `TemplateResponse`, `get_template`,
    Django `render`/`render_to_string`, `template_name =`), recorded under `renders`, and reads
    Jinja/Django templates for `extends`/`include`/`import`/`from` and linked static files. A
    template change gets import-graph evidence through its views and coverage evidence from the
    tests that ran a rendering view; it was a `non_code` blind spot widened to the whole suite.
  - **Static files** (`static_reaches_rendering_tests`): a `non_code` blind spot in a package with
    no e2e or visual tests now widens to the tests that reach the file through the views and
    templates that render or link it, before falling back to the package.
  - **Data files** (`data_file_in_root_package`): a data file in a directory is `data_or_schema`
    even when the package is the repository root (was `global_config`, so balanced mode ran the
    whole suite for one record); only a data file at the root itself stays `global_config`.
  - **Coverage contexts** (`coverage_core_ctrace`, `normalise_refuses_lost_contexts`): coverage.py
    on Python 3.12+ may use the sys.monitoring core, which drops per-test contexts. `generate`
    sets `COVERAGE_CORE=ctrace`, records it, refuses the sysmon warning and a run whose contexts
    name fewer than half the tests pytest ran; `normalise --junit` makes the same check.
  - **Hash** (`committed_only_hashes_head`): `--committed-only` records the hash of the HEAD tree
    it analysed, so an uncommitted edit in the checkout no longer fails `--recompute`.
  - Not done: data-path evidence (tests that glob or open a data directory, parametrised ids per
    record), the conftest-autouse strength (G4), pytest `testpaths`/`python_files` in test lists,
    per-id baseline failures, and grouping blind spots by directory in `--brief`. The examples
    are unchanged and validate with `--recompute`.
