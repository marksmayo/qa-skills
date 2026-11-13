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
| `global_config` | Full suite for repo-level files, lockfiles and CI; the package for a package's own manifest or test config | `full_suite`, `package` |
| `shared_fixture` | Every test under a `conftest.py`'s directory; a helper's importers, or the package when nothing imports it | `fixture_reach`, `package` |
| `dynamic_reference` | The package | `package` |
| `data_or_schema` | The package's tests plus contract tests | `package_and_contract` |
| `unmapped_source` | The package (Go: the package directory's tests and its importers' tests, else the module's Go tests; never another language's) | `package` |
| `deleted_or_renamed` | Tests that imported it in the base tree | `importers` |
| `non_code` | E2E and visual tests that reach it, falling back to the package's e2e and visual tests, then the package | `e2e_and_visual` |
| `huge_change` | Full suite | `full_suite` |

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
`verify.failures_outside_selection` and in `limitations`.

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
