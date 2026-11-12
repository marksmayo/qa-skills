# Design: `dead-test-finder-skill`

Status: Built (v1.1.0), see "As built" at the end. Build order: 12.

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

## As built (v1.0.0 to v1.2.0)

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
- **Found on a real repository, and fixed:** the JavaScript scan matched `it(`, `test(` and `describe(` in
  comments and strings, so the comment "before each test (order matters ...)" inside a `beforeEach` became
  a test with no assertion. Calls are now found in the source with comments and string contents blanked
  (names are still read from the original), and a call on an object (`regex.test(`, `obj.it(`) doesn't
  count.
- **Found on a second real repository, and fixed** (a static site's pytest suite: about 585
  tests in 170 files, parametrised over about 2,000 HTML pages):
  - **Root-level package.** `neutralise.py` crashed with `FileExistsError` when the tests sat at
    the repo root (package `.`), because the copy's path was the temporary directory itself. It
    is now named `repo`.
  - **Parametrised tests** were `not_found`: the JUnit cases are `test_x[case]`. A function id
    now runs every case and passes only if every case passes; a case id runs that case. The
    validator reads the cases the same way.
  - **Copies.** `neutralise.py` copied the whole repository, images included, for every test.
    It now copies each package once per invocation, without VCS data, caches, virtualenvs,
    `node_modules` or the run directory, plus anything named with `--copy-ignore`, and restores
    each neutralised file from its text after the run. `neutralisations.json` records each copy.
    `similarity.py` likewise copies once and runs every paired test in one traced pytest session,
    tracing only product frames line by line (it took 62 minutes with a copy, a session and
    every library line traced per pair),
    and falls back to one test at a time if pytest stops on an id it can't run.
  - **Duplicate search.** The all-pairs `SequenceMatcher` took 9 minutes. Pairs are now pruned
    with three bounds that can never drop a pair at or above the threshold (length, token counts,
    and a bit-parallel longest common subsequence), so the result is unchanged; identical
    sequences skip the comparison. Re-scanning that repository (read-only) took 412 s before and
    6 s after, with the same 151 duplicate pairs; the rescan also found the module-level skip,
    made 73 runtime skips conditional and dropped the 8 swallowed-failure false positives. A
    synthetic suite where 8,404 of 179,700 pairs really are duplicates gives the same pairs, but
    still needs the exact comparison for each (about 13 minutes against 15).
  - **Tested** with synthetic inputs: a root-level package with parametrised tests, guard and
    unconditional runtime skips, every module-level skip form, three swallowing and three
    non-swallowing excepts; the bit-parallel LCS against a dynamic-programming LCS on 2,000 random
    pairs; `--repo` on the example plus 300 MB of images (3.2 s before, 0.2 s after); and three
    neutralisations on the example's core package plus 300 MB of images (43 s before, 31 s
    after, 13 s with `--copy-ignore images`; the rest is the tree hash).
  - **Guard skips.** Every runtime `pytest.skip()` counted as unconditional, so guard skips
    (`if not path.exists(): pytest.skip(...)`, `except FileNotFoundError: pytest.skip(...)`) aged
    into `stale` and the validator refused to dismiss them. A runtime skip inside an `if`,
    `except`, `try`, loop, short-circuit expression or after an early return is now conditional
    with that guard as its condition; `check_skips.py` never makes a conditional skip stale by
    age (with a reason it is `legitimate`, without one `unknown`), and the validator accepts the
    dismissal of a conditional skip that an older skips file called stale by age.
  - **Swallowed failures** were 8 of 8 false positives: any `except` around an assertion counted.
    Only an `except` that can catch the `AssertionError` (bare, `AssertionError`, `Exception`,
    `BaseException`, also in a tuple) and doesn't re-raise, fail, assert, or keep the error for a
    later assertion is flagged now. `assert_in_except` likewise needs the except to hold the
    test's only assertions (`except Exception as e: pytest.fail(...)` around a checked read is
    not unreached).
  - **Module-level skips** raised nothing. `pytestmark = pytest.mark.skip/skipif/xfail`,
    `pytest.importorskip(...)` and a guarded `pytest.skip(..., allow_module_level=True)` are now
    `skipped` candidates, and an unconditional module-level `pytest.skip` is `never_runs` (pytest
    collects none of the module's tests); each candidate lists every test in the module. Skip
    marks on a test's class count too.
  - **`--repo` in the validator** copied the repository for each patch. Each patch is now applied
    to a sparse copy holding only the files it touches.
  - The example's candidates, similarity verdict and validation are unchanged; its
    neutralisation runs were not repeated (they need the Node client copy).
- **Testing:**
  - The validator catches 31 of 31 seeded violations.
  - 18 scanner, similarity and skip scenarios pass:
    - tautologies, loops, except-only and post-return assertions;
    - swallowed failures and strict xfail;
    - class patterns and config `python_files`;
    - `.only`, `describe.skip`, empty catches and `done` callbacks;
    - an overlap with the same lines but different values;
    - git-blame ages either side of 180 days.
- **v1.1.0: found on a Next.js, TypeScript, Vitest and Playwright repository** (3,630 Vitest
  tests in 403 files, 51 Playwright tests), and fixed:
  - **A lexer for JavaScript.** Regex literals holding a quote (`/can't take bookings/`) opened a
    "string" that swallowed the rest of the file (4 tests lost, silently); names were cut at the
    first inner quote (19 wrong ids); a commented-out `expect` counted as an assertion (a
    Playwright test that can never fail went unflagged). `js_lex` now knows where strings,
    template literals (with nested `${}`), regex literals and comments start and end; calls and
    assertions are matched in the masked code, names read from the literal. Strings end at a
    newline at the latest, so an apostrophe in JSX text costs one line.
  - **Parametrised names.** `it.each`/`describe.each`/`.for` tables (including ones holding `)`),
    template names with `${...}` and computed names get a `name_pattern` (printf `%s`, `$var`,
    `${}` become wildcards); `collect_check.py` matches the runner's expanded names against it.
  - **Playwright.** `test.describe` (named, anonymous, `.configure`), a runtime
    `test.skip(cond, reason)`/`test.fixme(cond)` (now a conditional skip of its test or block, not
    a test named "?"), `test.step`, `expect.soft`/`expect.poll`, `test as setup` aliases and
    `*.setup.ts` setup projects. Playwright specs are listed with `playwright test --list
    --reporter=json`; before, `vitest list` (which excludes `e2e/`) made all 51 "not collected".
  - **Rules for JavaScript** that were Python-only: mock-only (every called product name comes
    from a `vi.mock`ed module and every `expect` is on a mock), unreached assertions (all inside a
    loop over a non-literal, all inside `catch`, after a top-level `return`), swallowed failures
    (a `catch` around an `expect`, an empty `catch`, an empty `.catch()` except on a
    `waitFor...()`), and duplicates. Every rule now runs on every test instead of stopping at
    `no_assertion`. A determinism check `expect(f(x)).toBe(f(x))` is no longer a tautology.
  - **JavaScript duplicates** with the Python settings gave 1,719 candidates on that repo (tests
    that differ only in a URL or label). There is no per-test coverage for JavaScript to tell a
    duplicate from an overlap, so the JavaScript tokens keep calls and literal values and the
    threshold is 1.0 (identical up to variable names): 15 candidates. They stay static.
  - **Loops over a `const` array literal** (`const cases = [...]; for (const c of cases)`) are not
    unreached: 24 candidates fell to 7, each a loop over something read at run time.
  - **Collection.** `--env` for setup files that read configuration at import; `--scope
    candidates`/`--files` to list only some files (a full `vitest list` took 12 minutes there);
    `--reuse` keeps a list while the command and files are unchanged; a list that names no tests
    for files that define some is errored, never "not collected" (Vitest 4.1 on Windows printed
    nothing for a file filter in that repo).
  - **Neutralisation.** Targets were only `{ named }` imports from relative paths, and the call
    list held `render`, `fn`, `mockResolvedValue` and keywords; 15 of 23 tests were "not run".
    Targets are now the imported product names a test calls or renders, resolved through
    tsconfig `paths` and vite/vitest `alias`, default exports and re-exports, with nested generics
    and expression-bodied arrows patched. Re-run on five of those tests (the component through
    `@/` and a default import, the soft-delete and audit-log helpers, `getEnv`) in a scratch copy:
    every target resolved once nested generics (`<T extends Record<K, V>>`) were handled, in 74 s,
    and the copy's `src/` hashed the same before and after. Files are restored from their
    original bytes and checked by SHA-256 (the old LF rewrite left four CRLF files modified in the
    copy); run_tests.py's error is shown instead of "runs.json not found"; `--tests-file`, `--env`,
    `--npm-script` and `--runner-args` added; Playwright tests are recorded as not run.
  - **Speed.** `walk()` used `rglob` and filtered `node_modules` afterwards (46 s of a 4.5-minute
    scan under load). It prunes before descending now. On that repository (read-only), idle with a
    warm cache: the walk took 4.5 s before and under 0.1 s after, listing the same 1,016 files;
    the whole scan, with the lexer and the new rules, took 10.3 s before and 4.6 s after
    in-process (13 s from the command line under other load).
  - **Findings found by review** (`source: "agent"`, a `basis` re-read by `--repo`) and
    **dismissals of misread candidates** (`scanner_error` with a basis) are in the contract; the
    example gains one (F8, a test asserting only the keys of the trial balance).
  - Not done: Prisma-style and other per-test coverage for JavaScript duplicates; running
    Playwright tests under neutralisation; a Vitest file filter that works where Vitest 4.1
    printed nothing (the list is reported as errored instead).
- **v1.2.0: found on a Python monorepo (FastAPI services, about 970 pytest tests in seven
  packages) and a React/Vitest front end**, and fixed:
  - **Ancestor rootdir.** A `pytest.ini` above the packages made pytest print ids relative to it,
    and `collect_check.py` prefixed the package again (`services/api/api/tests/...`): 300 collected
    tests came out `not_collected` and 436 "collected but not defined". Listed ids are now mapped
    back through their files (an ancestor, the package or below), and `scan_tests.py` reads
    `python_files` from the nearest configuration at or above the package, as pytest does.
  - **Nested packages.** A package inside another (`services/a` and `services/a/api`) was listed
    and traced twice. The outer list command now passes `--ignore` for each inner package.
  - **Similarity verdicts.** Pairs whose session exited 4 (ids not found) or whose tests failed
    had no traced lines but came out `overlap`. The plugin now records each test's outcome and
    maps ids through the test's file; a pair is `duplicate` or `overlap` only when both tests
    passed and product lines were traced for each, otherwise `not_assessed` with the reason and
    exit code. Cross-package pairs say so instead of "only collected for Python tests". The copy
    keeps the ini and `conftest.py` files above the package. A latent bug traced `<frozen os>`
    lines as product lines (realpath put them under the copy): the example's duplicate pair ran
    2 product lines, not 10.
  - **Literal values** were stored verbatim in `similarity.json` (one was a real-looking email
    address). They are hashed now (`sha256:` plus 12 hex digits), and the validator rejects raw
    values.
  - **Neutralisation outcomes.** A test whose original and neutralised runs were all
    `not_found` was `failed_neutralised`, counted as neutralised and lifted evidence completeness
    to 100%. Such a run is now `errored` with `error_reason` (also when the test fails unchanged),
    and the validator checks every result's outcome against its runs.
  - **`it.each` neutralisation.** The pattern id (`... > %s shows ...`) never matched the JUnit
    names and the expanded ids were "unknown". A pattern id now matches every case its pattern
    matches; an expanded id resolves to its definition and runs that case. The validator maps
    both to the definition.
  - **Targets.** Methods can be named (`module.Class.method`); a class gets a hint instead of
    "not found"; standard-library names are never targets (filtered in the scan and in
    `skipped_targets`); a name from another package says so.
  - **Environment.** `--env` takes several pairs per flag in every script (as `run_tests.py`
    does), and `--package-env PACKAGE:K=V` sets a package's own variables (two services with
    their own `PYTHONPATH`) in `collect_check.py`, `similarity.py` and `neutralise.py`;
    `similarity.py` gained `--cwd`. Only variable names are recorded.
  - **False positives.** `duplicate`: 1,767 candidates, none kept (792 across two services that
    each keep a copy of the same tests, the rest input cases with different literals). Pairs are
    now within one package, and Python pairs need the same literal values; the rest are counted
    as `near_duplicates` with a note. `unreached_assertion`: 52, none kept; a loop is no longer
    flagged when a length, truthiness, indexed or helper assertion outside it, or a non-empty
    literal binding, fails on an empty iterable. `no_assertion` follows helpers of the same file
    that assert (`_test_shape(...)`, `self._check(...)`).
  - **Judgement.** SKILL.md now covers "should not raise" allow-path tests of a "return or
    raise" guard (rank by the sibling deny-path tests and what the name promises) and no-op paths
    (the neutral body is the expected behaviour: `weak` with `investigate`, or dismissed).
  - **Tested** with synthetic fixtures: an ancestor `pytest.ini` with nested packages (ids map,
    no double collection, per-package env names recorded); loops with each guard form and one
    without; helper delegation; same-shape pairs with equal and different literals; a traced
    session with a passing duplicate, a failing pair and a pair with no product lines (no literal
    in the output); method, class, stdlib and failing-unchanged neutralisations; and validator
    probes (a `not_found` run reported as `failed_neutralised`, raw values, an overlap without
    lines). The example's candidates are unchanged; its `similarity.json` was regenerated (hashed
    values, 2 lines) and the example validates.
  - Not done: following helpers imported from other modules for `no_assertion`; Vitest file
    filters for `it.each` cases (each run is still the whole file).
