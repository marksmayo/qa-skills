# Test impact selector rubric

Rules for `impact_report.json`. The validator enforces everything marked (computed) and every
"must".

## Evidence and strength

| Source | Evidence it gives | Strength |
|---|---|---|
| `changed_test` | The change edits the test file or its snapshot | 3 |
| `coverage` | The test ran a changed base line, a base line next to an insertion, or the function the change edits | 3 |
| `import_graph` | An import chain from the test to the changed file; pytest fixtures from a `conftest.py` count when the test requests them | 2 |
| `framework_tools` | The repo's related-tests tool lists the test | 2 |
| `journeys` | A journey's step evidence cites the changed file and the journey links the test | 2 |
| `naming` | The test file is named after the changed file (Go: `x_test.go` in `x.go`'s own directory only) | 1 |
| `co_failure` | The test failed in at least 2 past changes to the file | 1 |
| `blind_spot` | A blind spot widened to it | 0 |

A test's strength is its strongest non-widening reason (computed). A test must meet one of these
to be selected:
- strength 2 or more;
- strength 1 from two different sources;
- a widening.

A reason must not cite a source recorded `not_run`, `errored` or `not_applicable`.

## Blind spots

| Kind | Required scope | Allowed scopes |
|---|---|---|
| `global_config` | Full suite for repo-level files, lockfiles and CI; the package for a package's own manifest or test config; every Rust test for the workspace's Cargo files (`Cargo.toml`, `Cargo.lock`, toolchain, `.cargo/config`); no test for a newly added CI file (status `added`), which nothing that exists refers to yet | `full_suite`, `package`, `language`, `no_existing_test` |
| `shared_fixture` | Every test under a `conftest.py`'s directory; a helper's importers, or the package when nothing imports it | `fixture_reach`, `package` |
| `dynamic_reference` | The package; for the candidate of an import the graph could not resolve, the importing tests and the tests that reach the importers | `package`, `unresolved_importers` |
| `data_or_schema` | The package's tests plus contract tests | `package_and_contract` |
| `unmapped_source` | The package's tests of the file's language family: Rust files only Rust tests, Go files only Go tests (the package directory's and its importers', else the module's), other source files every test but Rust and Go ones. When the file's language has no import parser and that scope is empty, the full suite | `package`, `full_suite` |
| `deleted_or_renamed` | Tests that imported it in the base tree | `importers` |
| `non_code` | E2E and visual tests that reach it, falling back to the package's e2e and visual tests, then the tests that reach it through rendering views and templates (a server-rendered app with no browser tests), then the package | `e2e_and_visual` |
| `huge_change` | Full suite | `full_suite` |
| `fixture_project` | A file in an excluded fixture project: the tests under the folder that owns the sample and the tests that name or import it | `fixture_host` |

At a repository root with no manifest, the `package` scope of a file in a top-level folder that
holds tests is that folder's tests (with the guide checks that run its scripts and the tests that
reach the file), not every test of the repository. A selected test may never lie inside a folder
listed under `excluded`.

A file of no language (data, styles, configuration, an unread script language) widens to every
test of its scope, except that in the root package Rust and Go tests are left out when no file of
that language names the file or its folder; the blind spot lists them under `left_out` with the
count of that family's root-package tests and the reason. A Rust
bench file is change kind `bench`, not a blind spot: `cargo test` never builds or runs it.

When `language_coverage.warning` is set (a changed file in a language with no import parser),
every language in `changed_unparsed` must be named in `limitations`. Unread languages (PowerShell,
shell, Slint, Lua ...) count.

Selected Rust tests in `command.not_run_by_default` are not run by the commands (required-features,
`#[ignore]`, a feature) or not on every platform: a limitation must name each group's features, the
ignore, or the platforms.

| Mode | What widens |
|---|---|
| `strict` (default) | Every blind spot widens to its full scope, and every test in that scope is selected |
| `balanced` | Only `global_config`, `shared_fixture`, `dynamic_reference`, `deleted_or_renamed` and `huge_change` widen; the rest are `labelled`. It needs a coverage map that ran and isn't stale, plus history |
| `aggressive` | Nothing widens; every blind spot is `labelled`, with `would_widen_to` filled in |

## Coverage map freshness

When the map is built (`coverage_map.py`), it records a hash of every measured file. When the
selector reads the map, it compares those hashes with the base tree:

- **A file's hash differs:** the source is `stale`. That file's coverage is still used, marked
  "(coverage map is stale for this file)", and the coverage factor is halved.
- **A changed file the map doesn't know:** it gets no coverage evidence. When nothing else links
  it, it becomes `unmapped_source`.

## Evidence completeness (computed)

Completeness is the mean of each applicable source's factor:

| Source | Factor |
|---|---|
| coverage | The share of changed source files in packages the map covers, halved when stale |
| import_graph | The share of changed files in languages with a parser |
| journeys, co_failure | 1 when given, 0 when not |
| framework_tools | 1 when given; 0 when the repo has a related-tests tool but no output was given; left out when the repo has none |
| naming | 1 |

## Recall (computed from replay)

- **Counting unit:** one (past change, failing test file) pair.
- **Recall:** selected pairs divided by all pairs.
- **Interval:** the 95% Wilson interval (`stats.py`).
- **Not counted:** failures of tests on the `--flaky` list, and changes without a full run.

Each miss needs one of:

| `why_missed` | Meaning | Rank |
|---|---|---|
| `missing_source` | A source that would have linked the test was unavailable, such as no coverage for the package, no journey map or no related-tests output; the explanation names it | first |
| `weak_rule` | The rules missed a link that exists: a registry, runner configuration such as `setupFiles`, reflection, a framework convention | second |

Without a replay that has failures:
- `recall_status` is `not_assessed`;
- `recall_text` is null;
- no text gives a recall percentage.

A verify run's failures outside the selection are misses too, listed in
`verify.failures_outside_selection` and in `limitations`. A failed case is inside the selection
when its file is selected or its test id is one of a selected file's `test_ids`. A failure in
`verify.known_failures` is not a miss only when the runs bear it out: `flaky` needs a pass and a
failure of the test in the named runs on the same code, `baseline` a failure in a `verify-base`
run; it is still named in `limitations`.

## Honesty rules

- **Unselected tests:** they are "not selected by this evidence". The validator rejects:
  - "safe to skip";
  - "the selection is safe";
  - "guaranteed";
  - "no risk";
  - "won't break";
  - "nothing else is affected".
- **The headline:**
  - It must say "N of M" test files.
  - Any percentage in it must be one of the computed figures: the share selected, evidence
    completeness, or recall and its interval bounds.
- **The full suite:** `recommendations.full_suite` must say when it runs (on merge, nightly,
  before release).
- **CI configuration:** never edited. Neither `files_written` nor the snippet path may match a
  CI file.
- **Computed sections:** the sections copied from `selection.json` must match it exactly. Your
  only addition is a blind spot `comment`. `--recompute` reruns the selection from its inputs
  and must give the same result.
