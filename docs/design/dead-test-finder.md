# Design: `dead-test-finder-skill`

Status: Built (v1.0.0), see "As built" at the end. Build order: 12.

## Mission

The skill finds tests that give false assurance: tests that can't fail, tests that never run,
tests that only check their own mocks, duplicated tests, and skipped tests nobody is coming back
for. Each finding comes with evidence, and the skill proposes what to do (fix, merge, un-skip or
delete). It never deletes or rewrites a test itself unless asked.

How it differs from the other skills:

- **test-suite-evaluator** samples tests to judge effectiveness across the suite. This skill checks
  every test against concrete rules and proves the important ones with a run.
- **mutation-tester** makes small changes to product code. This skill uses the extreme case
  (neutralising a whole function) to find tests that pass whatever the code does, and points to
  mutation-tester for finer analysis.
- **flaky-test-hunter** looks for tests that fail sometimes. This skill looks for tests that never
  fail, or never run.

## Decisions

- **Never deletes.** The output is a set of proposals, each with a patch in the output directory.
  With `apply: true` the skill applies only fixes that strengthen a test (adding the missing
  `await`, the missing assertion) and removes a stray `.only`; deletions and merges are always left to a person.
- **Skips are checked against their tickets.** If a skip names a Jira key and a Jira connector or
  REST credentials are available, the skill reads the ticket's status (read-only). Otherwise the
  ticket is `not_checked`.
- **No commits, no Jira write-back.**

## Non-negotiable rules

- **"Can't fail" is `confirmed` only by a run:** the subject under test was neutralised (its body
  replaced with a neutral return or a raise, in a scratch copy) and the test still passed. A test
  with no assertions is `static`. Reading the code alone is `suspected` (never high).
- **"Never runs" is confirmed by collection, not by name patterns:** the framework's own collect or
  list command doesn't include it.
- **A duplicate needs both similar code and identical behaviour:** AST similarity ≥ 0.9 after
  normalising names and literals, and the same coverage set on the same inputs. Similar code with
  different assertions is `overlap`, not a duplicate.
- **No "no dead tests" claim.** The report states which tests were scanned, which were neutralised,
  and which couldn't be (no coverage map, needs services).
- **The working tree is restored after every neutralisation run,** checked by hash.
- **Tools that didn't run** are `not_run` or `errored`, and the tests they'd have checked stay
  `not_assessed`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | |
| `paths` | whole suite | |
| `neutralise` | on, budget 30 minutes | Off for a static-only pass |
| `skip_age_days` | 180 | A skip older than this (by `git blame`) with no open ticket is stale |
| `apply` | false | Strengthening fixes only |

## Workflow

1. **Recon.** `detect_conventions.py` for the framework and commands; the framework's collect
   command (`pytest --collect-only -q`, `jest --listTests`, `vitest list`, `go test -list`,
   Gradle test reports, `xcodebuild -enumerate-tests`) for what actually runs.
2. **Static scan** (`scan_tests.py`, rules in `data/rules.json`):

   | Category | What it finds |
   |---|---|
   | `never_runs` | Test files outside the collect patterns (`check_x.py`, `x.tests.ts` when the config wants `.test.`), test classes without the right base or annotation, functions missing the `test` prefix, tests excluded by config, a `.only` that silences everything else |
   | `no_assertion` | Tests with no assertion, `expect` or verify call, and no framework "should raise" construct |
   | `tautology` | `assert True`, `expect(x).toBe(x)`, `assertEquals(a, a)`, asserting a constant |
   | `unreached_assertion` | Assertions inside `for` loops over values that may be empty, inside `except` blocks, after an early `return`, or in callbacks never awaited |
   | `swallowed_failure` | `try`/`except: pass` around assertions, `catch {}` in tests, `.catch(() => {})` on an expectation |
   | `async_not_awaited` | `expect(...).resolves` or `.rejects` without `await` or `return`, a `done` callback that's never called, Kotlin `runBlocking` missing, Swift expectations never waited on |
   | `mock_only` | The subject under test is itself mocked, or every assertion checks a mock's return value or a call the test made directly |
   | `skipped` | `skip`, `xfail` without `strict`, `@Disabled`, `@Ignore`, `it.skip`, `xit`, `t.Skip`, `XCTSkip`; with the ticket key, reason and age from `git blame` |
   | `duplicate` | Candidate pairs by AST similarity ≥ 0.9 |

3. **Collection check.** Compare discovered test definitions with the collected list; anything
   defined but not collected is `never_runs`, confirmed.
4. **Neutralisation runs** for `no_assertion`, `tautology`, `unreached_assertion`,
   `swallowed_failure`, `async_not_awaited` and `mock_only` candidates. Ordinary tests the rules
   didn't flag are not neutralised, and the report says so:
   - find the function each test is meant to check (from the name, imports and per-test coverage);
   - in a scratch copy, replace its body with a neutral return (`None`, `0`, `""`, empty
     collection) and, separately, a raise;
   - run the test. If it passes both times, it's `cant_fail`, confirmed.
5. **Duplicate check.** For candidate pairs, run both with per-test coverage; same lines covered
   and assertions on the same values make a `duplicate`, otherwise `overlap`.
6. **Skip check.** Read each named ticket's status where possible. A skip is `stale` when its
   ticket is done or closed, or when it has no ticket and is older than `skip_age_days`. A
   conditional skip (platform, optional dependency) with a stated reason is `legitimate`.
7. **Agent review** of every candidate: is a "no assertion" test intentionally a smoke test that
   checks nothing raises? Smoke tests are kept as `weak` with a note, never dismissed silently.
8. **Propose** a fix, merge, un-skip or deletion for each finding, with a patch file.
9. **Rank, report, validate and render.**

## Ranking

`score = category weight x criticality`

| Factor | Values |
|---|---|
| Category weight | `cant_fail` confirmed, `never_runs` = 4; `mock_only`, `async_not_awaited`, `swallowed_failure` = 3; `stale` skip, `unreached_assertion` = 2; `duplicate`, `weak` smoke test = 1 |
| Criticality of what the test claims to cover | Money, auth, permissions, data loss, or a journey-mapper P0/P1 journey = 3; ordinary = 2; trivial helpers = 1 |

Bands: P0 ≥ 12, P1 ≥ 8, P2 ≥ 4, otherwise P3. A `.only` left in committed code is always P0,
because it silences the rest of the file or suite. `suspected` findings are capped at P2.

## Output (`dead_test_report.json`)

```text
metadata:    repo, commit, framework, collect command, tool versions, generated_at
tools[]:     {name, version, status, reason}
collection:  {defined, collected, not_collected[]}
tests[]:     {id, file, line, status: scanned|neutralised|not_assessed, reason?}
findings[]:  {id, category, test_ids[], location, confidence: confirmed|static|suspected,
              evidence{neutralisation?{target, variants[], run_ids, outcome},
                       similarity?, coverage_identical?, ticket?{key, status, checked},
                       skip_age_days?},
              proposal{action: fix|merge|unskip|delete|investigate, patch_path, rationale},
              applied, score, priority}
dismissed[]: {candidate_id, reason}
summary:     {tests_defined, collected, neutralised, findings_by_category{}, by_priority{},
              evidence_completeness}
limitations[], files_changed[]
```

## Validator fails when

- A `cant_fail` finding is `confirmed` without neutralisation runs where the test passed both ways.
- A `never_runs` finding is `confirmed` without the collect output showing the test missing.
- A `duplicate` lacks the similarity score or the coverage comparison (otherwise it must be
  `overlap`).
- A `stale` skip has neither a closed-ticket status nor an age over the threshold.
- A proposal with action `delete` or `merge` is marked `applied`.
- The working-tree hash after the run differs from before (outside `files_changed`).
- Report text says the suite has "no dead tests" or is "healthy".

## Edge cases

- **Tests that check nothing raises** (`test_import`, smoke tests). Kept as `weak`, P3, with a
  suggestion; never `cant_fail` unless neutralisation with a raise also passes.
- **Snapshot tests.** Neutralisation usually fails them (the snapshot changes), so they aren't
  can't-fail; very large snapshots are referred to test-data-auditor.
- **Parametrised tests.** Each parameter set is a test; a duplicate parameter set is reported once.
- **Mobile.** Android unit tests and iOS unit tests are neutralised; instrumented and UI tests are
  scanned statically only, because a neutralisation run per test is too slow, and are listed as
  not neutralised.
- **Tests that need services.** Docker Compose or Testcontainers if present; otherwise
  `not_assessed`.

## Tool stack

Stdlib Python for the scan (Python `ast`; regex and bracket-matching scanners for JS/TS, Java,
Kotlin, Go, Swift and Dart), AST similarity and the neutralisation patches. Optional and open
source: coverage.py, Istanbul via Jest or Vitest, JaCoCo, PIT with the Descartes engine (LGPL-3.0,
run as a tool) for JVM "pseudo-tested methods", the frameworks' own collectors. Versions are
recorded.

## Files

```text
dead-test-finder-skill/
  SKILL.md  RUBRIC.md  README.md
  data/rules.json                   # per-language patterns, collect commands, skip markers
  schemas/dead_test_report.schema.json
  scripts/scan_tests.py  collect_check.py  neutralise.py  similarity.py  check_skips.py
          validate_dead_test_report.py  render_report.py
          run_tests.py  parse_results.py  detect_conventions.py   # shared copies
  examples/ledger/  run/  proposals/  dead_test_report.example.json
```

## Example

`examples/ledger/` is a small bookkeeping app with a Python core (pytest) and a TypeScript client
(Vitest). The answer key:

| Planted | Expected |
|---|---|
| `check_balance.py`, a test file pytest never collects | `never_runs`, confirmed, P0 (money) |
| A test that calls `post_entry()` and asserts nothing | `cant_fail`, confirmed by neutralisation |
| `expect(save()).resolves.toBe(true)` without `await` | `async_not_awaited`, fix applied with `apply: true` |
| A test that mocks `calculate_tax` and asserts on the mock's return value | `mock_only` |
| Two copy-pasted VAT tests with renamed variables | `duplicate`, merge proposed |
| `@pytest.mark.skip(reason="LEDG-12")` where the example ticket file says Done | `stale` skip |
| A Windows-only skip with a reason | `legitimate`, dismissed with the reason |
| `test_imports` checking that the package imports | `weak`, P3 |

The collect output and neutralisation runs in `examples/run/` are real.

## Resolved questions

Agreed with the user before the build.

1. **Neutralise a sample of ordinary tests by default?** No: only the candidates the static rules flag.
2. **Age at which an unticketed skip is stale?** 180 days.
3. **May `apply: true` remove a stray `.only`?** Yes.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Every run has an `original`.** `neutralise.py` runs each test three ways:
  - unchanged;
  - with each target's body replaced by `return None` or `return undefined`;
  - with that body raising instead.

  Confirmed can't-fail needs the unchanged run to pass and the neutral variant to pass too.
  When the raise variant fails, the report must say the test does check that nothing raises.
  The design's "passes both times" applies only to smoke tests, which stay `weak` unless even
  the raise passes.
- **Proofs.** `neutralise.py --patch` applies a proposal in the scratch copy first. A
  strengthening fix counts as `proved` when the patched test passes unchanged and fails when
  neutralised. Test ids may name tests that only exist after the patch, such as a renamed file.
- **Scratch copies live outside the repo.** Python packages are copied to a temporary
  directory. JavaScript runs in a scratch copy with `node_modules` (`--cwd`), and every file
  patched there is restored. The tree hash leaves out the run directory, which can sit inside
  the repo.

  An edge-case scenario caught the first version copying its work directory into itself when
  the run directory was inside the repo.
- **Collection.**
  - Python: `pytest --collect-only -q` per package, with `python_files`, `python_classes` and
    `python_functions` read from the pytest config for the static rule.
  - JavaScript: `vitest list --json`, or `jest --listTests --json` (file level).
  - The raw output is kept, with the scratch path replaced.
- **Duplicates.**
  - Shape: the token similarity of a normalised AST dump, with names, string and number
    literals replaced.
  - Behaviour: a small `sys.settrace` pytest plugin records the product lines each test runs,
    and the literal values in each test are compared.
  - Python only. JavaScript pairs are `not_assessed`.
- **Skips.** Ticket status comes from `fetch_ticket.py` output; `status_category` was added to
  the shared script for this. Age comes from `git blame --porcelain` when the repo is a git work
  tree. A ticket key with no ticket file is `unknown`, never assumed open or closed.
- **Shared files.** The diff applier moved from the test impact selector into
  `shared/scripts/unified_diff.py`. The skill also uses `diff_scope.py` and `fetch_ticket.py`.
- **Example results:** 15 test definitions, 14 collected, 8 candidates, 7 findings and 1
  dismissal.

  | Finding | Result |
  |---|---|
  | `never_runs` (check_balance.py) | P0 |
  | `cant_fail` (post_entry) | P0 |
  | `mock_only` | P1 |
  | `async_not_awaited` | P2 |
  | `stale_skip` (LEDG-12 Done) | P2 |
  | `duplicate` | P3 |
  | `weak` | P3 |

  - **The un-awaited Vitest assertion:** it failed when `save()` was neutralised. Vitest 3.2.4
    auto-awaits hanging assertions and warns, so the finding stays static at P2 instead of
    becoming a can't-fail, as the design's answer key assumed.
  - **The five strengthening and un-skip patches** are all proved. The merge and the deletion
    are only proposed.
- **Testing:**
  - The validator catches 31 of 31 seeded violations.
  - 18 scanner, similarity and skip scenarios pass:
    - tautologies, loops, except-only and post-return assertions;
    - swallowed failures and strict xfail;
    - class patterns and config `python_files`;
    - `.only`, `describe.skip`, empty catches and `done` callbacks;
    - an overlap with the same lines but different values;
    - git-blame ages either side of 180 days.
