# Mutation tester rubric

Scoring anchors and rules for `mutation_report.json`. The validator enforces everything marked
(computed) and every "must".

## Mutant statuses

| Status | Meaning | In the main score |
|---|---|---|
| `killed` | A covering test failed (pytest exit 1, or the engine's "killed" on a test failure) | killed |
| `survived` | The covering tests passed with the mutant in place | not killed (unless triaged `equivalent`) |
| `no_coverage` | No test executed the mutant's line | not killed |
| `timeout` | The run passed the per-mutant timeout (3 times one untraced suite run, at least 10 s) | left out; counted as a kill only in the second score |
| `runtime_error` | The test process crashed or exited without test results; an engine "killed" on a non-test exit | left out; counted as a kill only in the second score |
| `compile_error` | The mutant doesn't compile or type-check | dropped |
| `not_run` | Sampled out by the budget, or the budget ran out | dropped, and lowers evidence completeness |

A mutmut survivor on a line no test executed (per `coverage.json`) becomes `no_coverage` with
`reclassified_from: "survived"`, because mutmut tracks tests per function, not per line.

## Scores (computed, per engine)

- **Score excluding timeouts** = killed / (killed + survived − equivalent + no_coverage).
- **Score including timeouts** = (killed + timeout + runtime_error) / (that denominator + timeout
  + runtime_error).
- `accepted` mutants stay in the denominator: accepting a survivor is a decision about what
  matters, not a kill.
- Scores from different engines are never added or compared as if they were the same
  measurement: their operators differ.
- The score text names the scope: "in 3 changed files", "in src/billing", or "in the whole
  repository (N files)". A changed-code score is never extrapolated to the codebase.

## Triage

Every `survived` mutant is in exactly one finding; no other status is. Every `no_coverage`
mutant is in exactly one coverage gap.

| Triage | Needs | Ranked |
|---|---|---|
| `weak_assertion` | `should_have_caught`: a test in every mutant's `covering_tests`; the missing assertion | yes |
| `missing_case` | the distinguishing input; `should_have_caught` optional (a covering test if given) | yes |
| `equivalent` | a reason of 40+ characters arguing both versions behave the same for every input | never |
| `accepted` | a reason (20+ characters) why the behaviour doesn't matter | never |

How to tell them apart:

- The test runs the line and the mutant changes a value the test could see, but the test asserts
  something weaker (truthiness, type, "no exception", a printed value): **weak assertion**.
- Every test input gives the same result on both versions; a different input would not: **missing
  case**. Boundaries (`>=` vs `>`, `n` vs `n + 1`) are the usual case.
- No input could tell them apart: **equivalent**. Write the argument, not a feeling. Typical:
  `i < n` vs `i != n` when `i` steps by 1 from 0 and `n ≥ 0`; a dictionary key changed in both the
  write and the read; a cache size changed where only hits and misses differ. If you can't argue
  it, it stays a survivor.
- The behaviour is real but nobody relies on it (log text, debug output, metrics labels nobody
  reads): **accepted**, with the reason.

## Ranking (weak_assertion and missing_case only)

`score = criticality × kind × proximity` (kind and proximity computed)

| Factor | Values |
|---|---|
| Criticality (yours, with a reason) | 3: money, auth, permissions, data deletion, or a journey-mapper P0/P1 step (`critical_paths`). 2: ordinary logic. 1: formatting, logging, display text |
| Kind (computed) | The highest over the finding's mutants, from `data/operators.json`: branch or boundary = 3 (comparison, negation, condition, logical, boolean constant, loop control); value = 2 (arithmetic, number, returned value, `None`, arguments, removed call); string literal = 1 |
| Proximity (computed with `--repo`) | 2 when a covering test calls the mutated function directly (a unit test ran the line and still passed); 1 when only tests that reach it through other functions ran it, and always 1 for a module-level mutant. Python tests are read with `ast`; JS/TS tests are found by their describe chain and title (`ts_scan.py`) and their body searched for a call |

| Priority | Score |
|---|---|
| **P0** | ≥ 12 |
| **P1** | ≥ 8 |
| P2 | ≥ 4 |
| P3 | < 4 |

Coverage gaps are not ranked; they are listed by function with a suggested test.

## Proofs

A proof (`prove_kill.py`) appends the suggested test to a scratch copy, runs it on the original
code, then on each mutant. A cited proof must have passed on the original and failed on every
mutant it lists; every mutant it lists must belong to the finding or gap that cites it, and it
must be in the suggestion's test file. A timeout on the mutant is not a kill. Proofs written to
the repo (`--in-place`) need `write_tests: true` and the test file in `files_changed`.

## Evidence completeness (computed)

The mean of: the baseline was green in two runs; per-test line coverage was recorded; the share
of requested engines that ran; the share of runnable mutants that were run (not sampled out or
cut by the budget).

## Honesty rules

- Any percentage in the headline is one of the computed scores, and the headline shows its
  "N of M".
- Never "well tested", "strong tests", "thoroughly tested", "fully tested" or "bulletproof".
- With a `changed` or `path` scope, no text puts a score on the whole codebase, repo or suite.
- `baseline.passed` must match the runs; a baseline that isn't green twice means no mutants.
- `files_changed` is empty unless `write_tests` is true, and every file an engine run changed
  (per its tree check) is in it. An engine run with no tree check is an error.
- An engine that records the code hash it ran on (`run_engine.py stryker`) must have run on the
  baseline's code; otherwise its mutants aren't tied to the green baseline.
- For a scoped run the baseline is the tests that exercise the scoped code (SKILL.md Step 3),
  named in `baseline.note`; a flaky test in that set stops the run.
