# Design: `test-impact-selector-skill`

Status: Designed; not built. Build order: 10.

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
