---
name: dead-test-finder
description: Find tests that give false assurance - tests the framework never collects, tests that pass whatever the code does, tests that only check their own mocks, un-awaited async assertions, swallowed failures, duplicates, and skips whose reason has gone - and prove the important ones with real runs. Never-runs is confirmed by the framework's own collect output; can't-fail by neutralising the code under test in a scratch copy and seeing the test still pass; duplicates by identical coverage and values. Each finding gets a patch, and every strengthening patch is proved to pass on the code and fail when it's neutralised. Never deletes or merges tests, never commits, and never claims a suite has "no dead tests".
version: 1.4.0
---

# Dead test finder

## Mission

Find the tests that would stay green if the code they claim to check were broken, and the tests
that never run at all. Each finding gives the evidence (a collect listing, a neutralisation run,
a coverage comparison, a ticket status) and a proposal with a patch. Fix, merge, un-skip or
delete is a person's decision; the skill proves its strengthening fixes but applies them only
when asked.

How this differs from the other skills:

- **test-suite-evaluator** samples tests to judge the suite. This skill checks every test against
  concrete rules and proves the important ones with a run.
- **mutation-tester** makes small changes to product code. This skill uses the extreme case,
  neutralising a whole function, and points to mutation-tester for finer analysis.
- **flaky-test-hunter** looks for tests that fail sometimes. This skill looks for tests that never
  fail, or never run.
- **test-data-auditor** gets very large snapshots referred to it.

Output:

1. `dead_test_report.json`, conforming to `schemas/dead_test_report.schema.json`.
2. The run directory, kept with the report as its evidence:
   - `candidates.json`, `collection.json` and the raw collect output;
   - `runs.json` and its JUnit files;
   - `neutralisations.json`, `similarity.json`, `tickets.json` and `skips.json`.
3. A patch for every proposal, in a proposals directory.
4. A rendered summary for the user.

## Non-negotiable rules

- **"Can't fail" is `confirmed` only by a run.**
  - **How:** in a scratch copy, the function the test calls gets a neutral body (`return None`
    or `return undefined`), and the test still passes.
  - **The raise variant:** the same function is also made to raise, separately. If the test
    fails then, it at least checks that nothing raises; say so.
  - **Without a run:** a test with no assertion is `static`, and reading the code alone is
    `suspected` (never above P2).
- **"Never runs" is confirmed by collection, not by name patterns.** The framework's own list
  command must leave the test out (`pytest --collect-only`, `vitest list`, `jest --listTests`,
  `playwright test --list`). A list command that fails, or lists nothing for files that define
  tests, is errored: its tests are not assessed, never not collected.
  A `.only` left in the code is always P0.
- **A duplicate needs similar code and identical behaviour.**
  - **The test:** AST similarity at or above 0.9 after normalising names and literals, the same
    product lines run, and the same values.
  - **Otherwise:** similar code that runs the same lines with different values is an `overlap`.
- **A stale skip needs evidence.** Either its ticket is done (Jira status category, read-only),
  or it has no ticket and `git blame` says it is older than `skip_age_days`. A skip you found by
  review with no `skips.json` verdict (`source: agent`) carries the same evidence itself: a basis
  citing the skip line, and `evidence.skip` with `verdict: stale` and either a checked done ticket or
  `age_days` over the threshold with `age_source` (`git blame (<date>)` or the commit that added it);
  the validator checks both. A conditional skip
  with a stated reason is legitimate and is dismissed with that reason. A guard skip (a
  `pytest.skip()` inside an `if` or `except`, or after an early return) is conditional, and age
  alone never makes it stale.
- **Never deletes or merges.**
  - **Proposals:** every one has a patch file.
  - **With `apply: true`:** apply only fixes that strengthen a test (the missing assertion or
    `await`, the rename that gets a file collected) and remove a stray `.only`. Then list the
    changed files.
  - **Never:** commit, or write back to Jira.
- **The repository is never changed by a run.**
  - Neutralisation and proof runs work in copies outside the repo, or in a scratch directory you
    name.
  - Every patched file is restored afterwards.
  - The tree hash is checked before and after.
  - Every list or test command the scripts start runs inside a guard (`tree_guard.py`): the test
    files it could touch are kept byte for byte and compared afterwards (with `git status` of the
    directory). A command that changed one (an option that took the next argument as its output
    file, as `vitest list --json <file>` does) has them put back, and the script stops with exit
    code 3 and records nothing. Never pass a runner an option with an optional value followed by
    a path: write `--json=<file in the run directory>`.
- **No "no dead tests" claim.** The report lists what was scanned, collected and neutralised,
  and what couldn't be (no collect command, services needed). The validator rejects "no dead
  tests", "the suite is healthy" and similar.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Paths | whole suite | |
| Neutralise | on, for flagged candidates only | Ordinary tests the rules don't flag are not neutralised, and the report says so |
| Tickets | none | Jira issues for skip keys, via fetch_ticket.py (REST, a connector export, or a file) |
| `skip_age_days` | 180 | |
| `apply` | false | Strengthening fixes only |
| Critical paths | none | Money, auth, permissions, data loss, or journey-mapper P0/P1 journeys get criticality 3 |

## Step 1: Scan and collect

```bash
python scripts/scan_tests.py <repo> --out <run>/candidates.json [--test-files "*.e2e.ts" ...]
python scripts/collect_check.py <repo> --candidates <run>/candidates.json --run-dir <run> [--cwd client=<copy with node_modules>] \
    [--env DATABASE_URL=postgresql://fake:fake@127.0.0.1:5432/fake] [--scope candidates | --files <file> ...] [--reuse] \
    [--split-on-error [MAX_RUNS]]
```

`scan_tests.py` supports **Python, JavaScript/TypeScript and Go test suites**. Languages are counted by
the shared `lang_census.py`, which leaves out vendored, submodule and embedded third-party trees (an
embedded driver with its own manifest and licence, header directories, built bundles); those trees are
also never scanned for tests, and `excluded_trees` in the output names each with its reason. When the
dominant language is one it cannot parse (for example Rust, Java or Ruby): the largest unsupported
language has more source files than all supported files combined, it adds a `language_coverage` block and
`summary.language_warning`, prints a loud warning and **exits 3** (`--allow-minority` exits 0 with the
warning kept). The few supported files it did scan are a minority and the result is **not** a dead-test
analysis of that repository. Do not present it as one. Either stop with a report whose headline and
`limitations` say the primary language was not analysed, or review that language by hand: findings you
find are `source: agent` with a `basis` (file, line, excerpt), and their runs are recorded in the
skill's shapes (Go: `go_tests.py`, below).

**Go.** `_test.go` files are read with comment- and string-aware patterns (nothing is compiled). Each
`func TestX(t *testing.T)` is a definition (`<file>::TestX`, framework `go-test`; subtests are part of
their test), with these rules:

- `no_assertion`: no failure path. Only the test's own `t` counts (`t.Error*`, `t.Fatal*`, `t.Fail*`, and
  the `t` of a `t.Run` closure), plus `require.`/`assert.`/`is.` calls, `panic` and `log.Fatal`.
  `fmt.Errorf` is never one. A same-package helper given `t` is read (a helper that only logs is no
  failure path); a helper in another package, or a method given `t`, is assumed to fail.
- `log_only` (category `no_assertion`): the test logs with `t.Log`/`t.Logf` where a failure was meant.
- `skip`: the body starts with an unconditional `t.Skip`/`t.Skipf`/`t.SkipNow`; `check_skips.py` gives it
  a ticket and age verdict like any other skip. A `testing.Short()` or `runtime.GOOS` guard is not flagged.
- `empty_table` (category `unreached_assertion`): a table declared empty in the test and ranged over.
- `compare_to_self` (category `tautology`): `if x != x`, `assert.Equal(t, x, x)`, `assert.True(t, true)`.
- `name_pattern` (category `never_runs`): `func Testlower(t *testing.T)`. go test's vet check rejects
  the malformed name and the whole package fails to build; with `-vet=off` it is silently not a test.

`collect_check.py` lists Go tests with `go test -list . ./...` from the module directory (it builds the
test binaries, vet included, and runs nothing). A package that does not build on this machine
(`[build failed]`, a `syscall` that exists only on another OS) leaves its tests `not_assessed`, never
`not_collected`. Set `GOFLAGS=-mod=readonly` and `GOPROXY=off` with `--env` when modules must not be
downloaded. `neutralise.py` takes Go ids: a target is `<package dir>:<Func>` or `<package dir>:<Type>.<Method>`
(from the definition's `calls`, or `--target`); the body becomes zero values (`return *new(T)` per
result, a bare return for named results) or a `panic`, and imports the file no longer uses become blank
imports so it still builds. Runs are `go test -json -count=1 -run '^TestX$' ./<package>`, recorded in
`runs.json` by `go_tests.py`; a neutralised package that does not build is an `errored` run with a
`not_viable` note on the target, never a failure. To record a Go run you made yourself (another
wrapper, a CI log), save the `go test -json` output and run
`python scripts/go_tests.py record --json <file> --out <run>/runs.json --label original --run-prefix N1
--command "<what you ran>"`: the run is then tool output the validator can read.

**Host safety.** Before any list or neutralisation run, grep the tests for commands that control
devices or processes (`adb`, `emulator`, `xcrun simctl`, `pgrep`/`pkill`/`kill`, `taskkill`, `docker`) and
for writes under the home directory. Run with those tools off `PATH`, with the network refused (the
skill's `net_guard_proxy.py` for Go and other compiled runners) and with a scratch `HOME` **and**
`USERPROFILE`. A test that kills "every matching emulator" is left `not_run` with that reason.

`scan_tests.py` lists every test definition, whether or not it would be collected:
- Python is parsed with `ast`;
- JavaScript and TypeScript go through a small lexer first, so strings, template literals,
  regular expression literals (`/can't/`) and comments are never read as code, and a
  commented-out `expect` is not an assertion. Calls are then matched: `describe`/`it`/`test`
  and their `x`/`f` forms, `.only`/`.skip`/`.todo`/`.each`/`.for`/`.skipIf`/`.runIf`, Playwright's
  `test.describe` (named or anonymous), `test.fixme`, and `test` imported under another name
  (`import { test as setup }`). Names keep their quotes and apostrophes. A name from `it.each`,
  `describe.each`, a template literal with `${...}` or a variable is parametrised: the definition
  carries a `name_pattern` that the expanded names the runner lists are matched against.
- **Where `test` comes from.** `test`/`it`/`describe` is a test head only when it is a global,
  comes from a test framework (`vitest`, `@jest/globals`, `@playwright/test`, `node:test`, `mocha`,
  `bun:test` ...), or from a relative fixtures module that extends or re-exports one
  (`export const test = base.extend(...)`). Imported from the product itself (a testing library's
  own source exporting `test()` and `describe()`, called inside Vitest tests to build fixtures:
  `test.only('focused', noop)` there is data, not a focused test), or declared locally (a helper
  `const test = (id) => ...`, hidden in its own scope only), it is not a test. A file that imports
  its `test` from a package and no framework at all is a suite of the repository's own runner (a
  testing framework running its dogfood suite on itself): its tests are `framework: custom` with
  the `runner_module`, and collect_check has no list command for them. Such suites usually use
  their own file names: add them with `--test-files "*.e2e.ts"` (repeatable; a name or a
  repo-relative pattern). Tests importing from `node:test` are `framework: node-test`. A
  definition skipped conditionally (`it.skipIf(cond)`, or inside such a block) carries
  `skip_condition`.
- **Test directories.** A directory named `e2e`, `test`, `tests`, `spec` or `testing` holds tests
  only when it is not a product package: a manifest (`package.json`, `pyproject.toml`,
  `Cargo.toml` ...) that ships code (a `src/` directory, `main`/`exports`/`bin`, a `[lib]`, the
  Python package it names) makes it product code, the rule of the shared `diff_scope.py`. A
  workspace package of spec files and a runner config (an `apps/x/e2e/` Playwright package) is
  still a test layout. So `packages/e2e/src/` of a product named e2e is product code, while
  `packages/e2e/tests/` holds tests.
- Playwright: a file that imports `@playwright/test` or sits in the config's `testDir` is marked
  `framework: playwright`, and setup projects (`auth.setup.ts`) are scanned too. A runtime
  `test.skip(condition, reason)` or `test.fixme(condition)` inside a test is a conditional skip of
  that test, not a test; at describe level it covers the block. `test.step` bodies belong to their
  test, and `expect.soft`/`expect.poll` are assertions.
- Every rule runs on every test (a test with no assertion and an empty catch gets both
  candidates). Dependency, build and cache directories (`node_modules`, `.next`, `dist`, ...) are
  pruned before the walk descends into them.

It flags these candidates:

| Candidate | Rule |
|---|---|
| `never_runs` | File outside `python_files` (read from the pytest config), class outside `python_classes`, `.only`, a module-level `pytest.skip(..., allow_module_level=True)` that always runs (one candidate listing every test in the module) |
| `no_assertion` | No `assert`, assertion call, `expect` (also `expect.soft`, `expect.poll`, `expect.assertions`, helpers named `expectX`/`assertX`/`verifyX`) or `pytest.raises`. Python: a call to a function or method of the same file that asserts (`_test_shape(result)`, `self._check_row(row)`, two calls deep) counts as an assertion. JavaScript: `expect` or `assert` imported under another name (`import { expect as vexpect } from 'vitest'`) is an assertion, and so is a call to a function that asserts, defined in the same file (`function failsWith(p, code) { return expect(p).rejects... }`, two calls deep) or imported from a relative or alias module (`import { expectRow } from './helpers/checks'`). When the test only uses Testing Library `getBy*`/`findBy*` queries, the candidate says so: they throw when nothing matches, so the test checks only that elements exist |
| `tautology` | `assert True`, `x == x`, `expect(v).toBe(v)` (not `expect(f(x)).toBe(f(x))`, a determinism check, and not an `expect` the product itself exports, whose matchers are the code under test) |
| `unreached_assertion` | Assertions inside a loop over a possibly empty iterable, only inside `except` (the test has no other assertion), or after `return`. A Python loop is not flagged when something outside it fails on an empty iterable: an assertion on its length (`assert len(rows) == 3`, also split over several lines), its truthiness or value (`assert rows`), an indexed assertion (`assert rows[0] == ...`), an assertion helper given it (`_verify_shape(rows)`), or a name bound only to a non-empty literal (`expected = ["a", "b"]`). JavaScript: every assertion inside a `for ... of`/`.forEach`/`.map` over something that isn't an array literal (or a `const` holding one, also with a type annotation such as `: [() => void, () => void][]`), only inside `catch`, or after a top-level `return` (a guarded `if (x) return;` is not one) |
| `swallowed_failure` | An `except` that can catch the assertion (bare, `AssertionError`, `Exception` or `BaseException`, alone or in a tuple) around it, and doesn't re-raise, fail (`pytest.fail`, `self.fail`), assert, or keep the error for an assertion after the `try`. JavaScript: a `catch` around an `expect` that neither rethrows nor fails, an empty `catch {}`, and an empty `.catch(() => {})` (not on a best-effort `waitFor...()`). Cleanup may fail quietly: an empty catch inside `finally`, around nothing but cleanup calls (`rm`, `close`, `kill`, `dispose` ...), or in a method of a fake object the test builds is not one. `except FileNotFoundError: pytest.skip(...)` is not one |
| `async_not_awaited` | `expect(...).resolves/.rejects` neither awaited nor returned; a `done` callback never called. Held in a name that is awaited or returned later (`const assertion = expect(p).rejects...; await vi.advanceTimersByTimeAsync(); await assertion`), an element of an awaited `Promise.all([...])`, or passed to another call, it is settled |
| `mock_only` | The test patches a function, then asserts on that same function. JavaScript: every product function the test calls comes from a module it mocks (`vi.mock`/`jest.mock`), and every `expect` is on a mock |
| `skipped` | `skip`, `skipif`, a non-strict `xfail` (on the test or its class), a runtime `pytest.skip()` or `self.skipTest()`, `it.skip`, `describe.skip`, `todo`, `test.fixme`, `it.skipIf(cond)` (conditional), a runtime Playwright `test.skip(cond, reason)` (conditional, on its test or block); at module level `pytestmark = pytest.mark.skip/skipif/xfail`, `pytest.importorskip(...)` and a guarded `pytest.skip(..., allow_module_level=True)` (one candidate listing every test in the module). Each with the reason, any ticket key, and whether it is conditional: a runtime skip inside an `if`, `except`, `try`, loop or short-circuit expression, or after an early return, is conditional, with that guard as its condition |
| `duplicate` | Pairs within one package only (two services that each keep a copy of the same tests are each testing their own code). Python: normalised AST similarity ≥ 0.9 and the same literal values; same-shape tests with different values are input cases, counted as `summary.near_duplicates` with a note (worth parametrising), not candidates. JavaScript: test bodies identical apart from variable names (calls, object keys and literal values kept, at least 40 tokens), because no per-test coverage run can tell a JavaScript duplicate from an overlap later. Pairs are pruned first with bounds that never drop a pair at or above the threshold (length, token counts, longest common subsequence), so the result is the same as comparing every pair, in seconds rather than minutes on a large suite |

`collect_check.py` runs each framework's list command and keeps the raw output. Every
definition that isn't listed is `not_collected` (a parametrised one is matched by its name
pattern).

- **Playwright** specs are listed with `npx playwright test --list --reporter=json` (no browser
  starts), never with Vitest or Jest, which exclude `e2e/` and would report every spec as not
  collected.
- **Vitest** is listed with `npx vitest list --json=<file in the run directory>`: its `--json`
  takes an optional path, and `--json <test file>` overwrites the test file with the listing.
  Vitest escapes a double quote in a name a second time in that listing; names are compared with
  the escapes undone. The runner comes from the package's dependencies, then its scripts, then the
  nearest workspace root's.
- **No list command.** `node:test`, a repository's own runner (`framework: custom`) and a package
  whose `package.json` names no runner are recorded as `no_list_command`; their tests are
  `not_assessed` with that reason, never `not_collected`. Confirm a never-runs finding there by
  reading the runner's config, and say so.
- **One file that can't be listed.** A runner can fail its whole list on one file (a static check
  of options the config doesn't define) although a run collects it. `--split-on-error` lists such
  a package again in halves (at most 40 extra commands by default), so the other files are listed:
  the package is `partial` with `errored_files`, and only the tests in those files are
  `not_assessed`.
- **Conditional skips.** A test skipped by `skipIf`/`runIf` that is missing from the list is
  `not_assessed`: a runner may leave it out on this machine only (a Windows-only skip on Windows).
- **Environment.** Setup files often read configuration at import (`vitest.setup.ts` reading
  `DATABASE_URL`). Pass fake values with `--env K=V [K=V ...]`, never real credentials. When
  packages need different values (two services that each have a top-level `shared` package and
  their own `PYTHONPATH`), add `--package-env <package>:K=V` per package. Only the variable names
  are recorded. `similarity.py` and `neutralise.py` take the same two options.
- **pytest rootdir.** pytest names tests relative to its rootdir, the directory of the nearest
  `pytest.ini`/`pyproject.toml`/`setup.cfg`/`tox.ini`, which can be above the package
  (`services/pytest.ini` for `services/api`). The listed ids are mapped back through their files,
  so they match the definitions whichever directory pytest chose. `scan_tests.py` reads
  `python_files` from that same nearest configuration.
- **Nested packages.** When one package holds another (`services/api` and `services/api/worker`),
  the outer list command runs with `--ignore` for the inner package, which gets its own command;
  no test is collected or counted twice.
- **Cost.** `vitest list` and `playwright test --list` import every file they list, so on a large
  suite a list costs about as much as a run. `--scope candidates` lists only the files holding a
  candidate (`--files` names them yourself); everything else is `not_assessed` with the reason.
  `--reuse` keeps an earlier list while the command and the listed files are unchanged. A list
  that comes back empty for files that define tests (a filter that matched nothing) is errored,
  not "nothing collected"; rerun without the scope.

## Step 2: Neutralise the flagged tests

```bash
python scripts/neutralise.py --repo <repo> --candidates <run>/candidates.json --run-dir <run> \
    --test <id> [--test <id> ...] [--tests-file ids.txt] [--target <id>=module.function] [--cwd client=<copy>] \
    [--env K=V ...] [--package-env <package>:K=V] [--npm-script test] [--runner-args "..."]
```

Run this for every `no_assertion`, `tautology`, `unreached_assertion`, `swallowed_failure`,
`async_not_awaited` and `mock_only` candidate, all in one invocation where you can. First read
the candidates and dismiss the scanner's mistakes (a helper it didn't follow, a name that is not
the test framework's) with a reason and a basis: neutralising them costs runs and teaches
nothing. When most candidates of a rule turn out to be the same mistake, sample a few, dismiss the
rest by the shared reason, and report the scanner gap in the limitations.

- **Network guard.** Run the list commands, neutralisations and similarity sessions with the
  network blocked, using the guards this skill ships: for Node,
  `NODE_OPTIONS="--require <absolute path to this skill>/scripts/net_guard.cjs"` (forward slashes
  on Windows; it reaches child processes and Vitest's and Jest's workers); for Python,
  `scripts/net_guard.py` as a pytest plugin (`-p net_guard` with `PYTHONPATH` reaching
  `scripts/`). Set `QA_NET_GUARD_LOG=<run>/net_guard.log`: with it set the guards write only to
  the log, never to stderr, so tests of a CLI that assert on a child's stderr are not broken by
  the guard. Each process writes `net_guard: loaded pid=<pid>` and each refusal
  `net_guard: refused <host>:<port>`. Check the log has a `loaded` line before relying on the
  results: a guard that never loaded (a wrong path) refuses nothing. A test that fails because a
  connection was refused needs a service; record it as errored, never as a neutralisation result.
  `--self-test` on either file checks it works here.
- **One copy per invocation.** Each package (the repo root too) is copied once, outside the
  repo, without VCS data, caches, virtualenvs, `node_modules` or the run directory, and every test
  in the invocation shares it; each neutralised file is written back straight after its run. Add
  `--copy-ignore <glob>` for large files the tests never open (`--copy-ignore "*.mp4"`), and only
  those: a test that checks an image exists needs the image. `neutralisations.json` records each
  copy and what it left out. For a very large repo, make one scratch copy yourself and pass it
  with `--cwd <package>=<copy>` to reuse it across invocations.
- **Ids from a file.** `--tests-file` takes one id per line, so ids holding quotes, `>` or `$`
  need no shell quoting.
- **JavaScript targets** are the names the test imports from relative or alias paths (`@/lib/x`,
  read from tsconfig `paths` or a vite/vitest `alias`) and calls or renders as JSX. Each is
  followed through default imports and re-exports to its function (`function`, `const` arrow,
  `export default function`). Mocked modules and test-library helpers are never targets, and
  neither is anything defined in test code (a test file, or a helper, fixture or mock under a test
  directory, `__mocks__/` or `test-utils/`): neutralising an assertion helper makes every test that
  uses it pass. Such a target is reported with that note; a class is reported as a class (name
  the function under test with `--target`).
  `--env`, `--npm-script test` (the repo's own runner flags and env) and `--runner-args` are
  passed to `run_tests.py`. Each run is the whole test file (Vitest start-up dominates, about
  10 s a run), so a test costs one run plus two per target.
- **Restores.** Every patched file is written back byte for byte (CRLF kept) and checked by
  SHA-256; the script stops if a restore doesn't match.
- **Windows paths.** Deep trees (`prisma/migration_history_archive/...`) pass 260 characters
  inside a long scratch path. Put the `--cwd` copy at a short path (`C:\t\app`); when
  `run_tests.py` records no run, its own error is shown and kept in the result (`errors`).
- **Playwright tests** are not neutralised (they drive a running app); they are recorded as not
  run with the reason.
- **Parametrised tests.** Name the function (`tests/test_x.py::test_y`) to run every case: it
  passes only if every case passes. Name one case (`tests/test_x.py::test_y[a-1]`) to run just
  that one. JavaScript `it.each`/`test.each` and template names work the same way: the
  definition id (`src/rows.test.ts::rows > %s keeps its fields`) covers every case its name
  pattern matches in the run, and an expanded name (`src/rows.test.ts::rows > rye keeps its
  fields`) covers that case.
- **Python targets** are module-level functions by default. Name a method with
  `--target <id>=package.module.Class.method`; a class on its own is reported with that hint, not
  neutralised. Standard-library names (`datetime.datetime`) are never targets (they are listed in
  `skipped_targets`), and a name from another package or an installed library is reported as not
  found in this package.

The targets are the product functions the test imports and calls; use `--target` to name another.
Each test runs three times:
- once unchanged;
- once per target with a neutral body;
- once per target with a raising body.

| Result | Meaning |
|---|---|
| Passes with the neutral body | It can't fail on that code: `cant_fail` (or `mock_only` when it mocks its subject), confirmed |
| Fails with the neutral body | It can fail. Keep the static finding (e.g. an un-awaited assertion that today's runner happens to check), with that result as evidence |
| Nothing to neutralise | It calls no product code: usually a smoke test, so `weak` |
| Targets not found | Name them with `--target`; until then the result is `not_run` and counts against evidence completeness |
| `errored` | The unchanged test did not run (`not_found`: an id the runner doesn't report, a collection error) or failed, or no neutral-body run ran it. Nothing was learnt: the reason is in `error_reason`, the test is not `neutralised`, it counts against evidence completeness, and the validator rejects a passed or failed outcome that its runs don't support. Fix the cause (the id, the environment) and run it again |

## Step 3: Duplicates and skips

```bash
python scripts/similarity.py --repo <repo> --candidates <run>/candidates.json --run-dir <run> \
    [--cwd <package>=<prepared copy>] [--env K=V ...] [--package-env <package>:K=V]
python scripts/fetch_ticket.py --from-file <issue.json> --source connector --out <run>/tickets.json    # or a REST fetch
python scripts/check_skips.py --repo <repo> --candidates <run>/candidates.json --tickets <run>/tickets.json --out <run>/skips.json
```

`similarity.py` runs every test in a candidate pair once, in one traced pytest session per
package (in one scratch copy), and compares the lines each test ran and its literal values. Only
product code is traced line by line. A parametrised test's cases count together. If a session
stops on an id pytest can't run, its tests are rerun one at a time. The copy keeps the pytest
configuration and `conftest.py` files of the directories above the package, so pytest picks the
same rootdir and options as in the repository.

| Verdict | When |
|---|---|
| `duplicate` | Both tests passed in the traced session, ran the same product lines (not none) and have the same literal values |
| `overlap` | Both passed and ran product lines, but the lines or the values differ |
| `not_assessed` | A test failed, errored, was skipped or never ran (the session's exit code is kept), no product line in the package was traced for a test (the code under test lives in another package), the tests are in different packages, or they are not Python. The reason is recorded; the validator rejects a duplicate or overlap verdict without both tests passing and traced lines |

Literal values are compared by hash: `similarity.json` stores `sha256:` prefixes, never the
values, because a test literal can be an email address or a credential.

`check_skips.py` gives each skip a verdict:

| Verdict | When |
|---|---|
| `stale` | The ticket is done, or the skip has no ticket and is older than `skip_age_days` |
| `open` | The ticket is still open |
| `legitimate` | A conditional skip with a reason (a skipif, an `importorskip`, a guard such as `if not path.exists(): pytest.skip("... not found")`) |
| `unknown` | No ticket file, no git history, or a conditional skip with no reason (never stale by age) |

## Step 4: Review, propose, prove

Every candidate becomes a finding or is dismissed with a reason, for example a legitimate
conditional skip. A candidate the scanner got wrong (a misread skip, a name cut short) is
dismissed with `scanner_error: true` and a `basis` of `{file, line, excerpt}` items showing the
real code; this is the only way to dismiss a stale-skip or confirmed never-runs candidate, and
the validator re-reads each line with `--repo`.

**Problems no candidate covers** (an assertion commented out in a file the rules couldn't parse,
a test that asserts only the keys of a result) are reported too, as findings with `"source":
"agent"`, `candidates: []` and a `basis`: `{file, line, excerpt}` items the validator re-reads, or
`{evidence: <file in the run directory>}`. Reading alone makes them `suspected` (at most P2);
`confirmed` still needs a neutralisation run. The rendered report labels them "found by review". A stale verdict can be dismissed only when the skip is conditional and the
verdict came from its age rather than a done ticket (a `skips.json` written before guard skips
were recognised); say what the condition guards against.

| Finding | From |
|---|---|
| `never_runs` | A never-runs candidate that collection confirms, or a `.only` |
| `cant_fail` | A test that passed neutralised, or a tautology |
| `mock_only`, `async_not_awaited`, `swallowed_failure`, `unreached_assertion` | The matching candidate |
| `stale_skip` | A stale verdict |
| `duplicate` or `overlap` | The similarity verdict |
| `weak` | A smoke test you keep, with a note. It becomes `cant_fail` instead if it passes even when the code raises |

Write a patch for each proposal (`fix`, `merge`, `unskip`, `delete`). Prove every strengthening
patch:

```bash
python scripts/neutralise.py ... --patch proposals/<fix>.diff --test <id after the patch> [--target ...]
```

The patched test must pass unchanged and fail with the code neutralised.

Give each finding a criticality with a reason: 3 for money, auth, permissions, data loss or a
journey-mapper P0/P1 journey, 2 for ordinary code, 1 for trivial helpers.

**Judgement cases the run alone can't settle:**

- **"Should not raise" tests of a guard.** An authorisation or validation guard whose contract is
  "return nothing, or raise" (`check_permission(user, action)` returns `None` when access is
  allowed) has a neutral body that means "allow everything". Its allow-path tests ("a reader may
  GET") pass with that body, so they are `cant_fail` by the rule, but the return variant alone
  can't judge the guard. Before ranking:
  - look for the sibling deny-path tests of the same guard (wrong role, wrong tenant, unknown
    resource) and check they failed when it was neutralised: then the guard's logic is tested,
    and the allow-path tests are tested only for "does not raise";
  - check what the test's name promises. A test named for a mapping (`get_requires_read`) that
    only shows the call doesn't raise never checks the mapping: that is the real gap, and the
    proposal is an assertion on it (or a deny case for the other side of the mapping);
  - keep the finding `cant_fail`, confirmed, and set the criticality from what is actually
    missing, with the reason (for example 2 when the deny paths are tested, 3 when nothing fails
    with the guard neutralised). Say all this in `why`.
- **A no-op path.** When the behaviour under test *is* the neutral body (`skips the check when no
  tenant is given`, `returns None for an empty basket`), passing with it is correct and no
  assertion can make it fail. Report it as `weak` with an `investigate` proposal that says the
  test can only be strengthened by checking a side effect (nothing was called, nothing was
  written), or dismiss the candidate with that reason; never as a confirmed `cant_fail` with a
  fix.

## Step 5: Rank, report, validate

Each finding's priority is computed:
- **Score:** category weight × criticality.
- **Weights:**

  | Category | Weight |
  |---|---|
  | `never_runs`, confirmed `cant_fail` | 4 |
  | `mock_only`, `async_not_awaited`, `swallowed_failure`, unconfirmed `cant_fail` | 3 |
  | `stale_skip`, `unreached_assertion` | 2 |
  | `duplicate`, `overlap`, `weak` | 1 |

- **Bands:** P0 at 12 or more, P1 at 8, P2 at 4, otherwise P3.
- **Overrides:** a `.only` is always P0, and a `suspected` finding is never above P2.

```bash
python scripts/validate_dead_test_report.py <run>/../dead_test_report.json --repo <repo> --write-summary
python scripts/render_report.py <run>/../dead_test_report.json [--brief]
```

The report's `metadata.evidence_files` and patch paths are relative to the report's directory.
Write the headline yourself, naming the P0 findings, and fix every error rather than editing
computed fields.

Show the user:
1. the headline;
2. the ranked findings, each with its evidence and whether the fix is proved;
3. what was dismissed and why;
4. what couldn't be collected or neutralised.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, frameworks and collect commands, `apply`, `skip_age_days`, platform, evidence files |
| `tools[]` | Each tool ran, not run, errored or not applicable, with the reason |
| `collection` | Defined, collected, not collected, not assessed |
| `tests[]` | Every defined test: scanned, neutralised or not assessed (with the reason) |
| `findings[]` | Category, `source` (tool or agent) and `basis`, candidates, tests, location, confidence, why, criticality and reason, evidence (neutralisation, collection, similarity or skip), proposal (action, patch, rationale, proof), `applied`; weight, score and priority are computed |
| `dismissed[]` | Candidates set aside, with the reason |
| `summary` | Headline, tests defined, collected and neutralised, counts by category and priority, evidence completeness |
| `limitations[]`, `files_changed[]` | What the scan can't see; files changed with `apply: true` |
