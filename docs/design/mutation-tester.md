# Design: `mutation-tester-skill`

Status: Built (v1.5.0), see "As built" at the end. Build order: 9.

## Mission

The skill runs mutation testing on the code that changed (or a path the user names) and turns
surviving mutants into a short list of weak assertions: "this test runs line 42 but would still
pass if `>=` became `>`". Each one names the test that should have caught it and suggests the
assertion that would.

How it differs from the other skills:

- **test-suite-evaluator** may cite a mutation score as evidence of effectiveness. This skill
  produces that evidence, scoped and triaged, and the evaluator can read it.
- **dead-test-finder** finds tests that can't fail at all (the extreme case: remove the whole
  function body). This skill works at the level of single operators and values.
- **ticket-to-tdd** optionally runs a mutation tool on the lines it changed. It can hand that run
  to this skill for triage.

The headline is never a bare mutation score. It's the list of surviving mutants that matter,
with the score shown beside the counts it came from.

## Decisions

- **Scope defaults to changed code** (`diff_scope.py` against the merge base of `main`). Whole-repo
  runs are opt-in, because they take hours on real code.
- **The repo's own mutation tool is used when it's already configured.** Otherwise the skill picks
  the standard open-source tool for the language, runs it from a scratch config, and never adds it
  to the repo's dependencies.
- **The Python fallback is stdlib only.** When no tool is available for a Python repo, a built-in AST
  mutator runs the targeted tests per mutant. It is slower and has fewer operators, and the report
  says which engine ran.
- **Tests are proposed, not written, by default.** With `write_tests: true` the skill adds the
  suggested assertions and proves each one kills its mutant.
- **No commits, no Jira write-back.**

## Non-negotiable rules

- **A mutation score is always shown with its counts and scope:** "41 of 52 mutants killed in 3
  changed files (79%); 4 equivalent excluded, 2 timed out". A score for the whole codebase is never
  extrapolated from a changed-code run.
- **No "well-tested" or "strong tests" claims.** A high score says these mutants were killed; it
  says nothing about mutants the operators can't express.
- **Survived and no-coverage are different results.** A mutant on a line no test runs is a coverage
  gap (`no_coverage`), reported separately from a weak assertion.
- **An equivalent mutant needs a reason someone can check:** the two versions behave identically for
  every input, and why (`i < n` vs `i != n` in a loop that steps by 1 from 0). "Equivalent" with no
  reason is rejected. Unproven cases stay `survived`.
- **Timeouts and runtime errors are not silently counted as kills.** They're listed separately, and
  the score is shown both with and without them.
- **Every mutant result comes from a real run**, with the tool, version and command recorded. A
  tool that couldn't run is `not_run` or `errored`, and the scope it would have covered is
  `untested`.
- **Product code is restored after every mutant.** The skill checks the working tree hash before
  and after the run.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | |
| `scope` | `changed` | `changed` (vs `base_ref`), a path, or `all` |
| `base_ref` | merge base with `main` | |
| `engine` | auto | Force a tool, or `builtin` for the Python fallback |
| `budget` | 30 minutes | Mutants are sampled evenly by operator when the full set won't fit; the sample is recorded |
| `write_tests` | false | |

## Workflow

1. **Recon.** `detect_conventions.py` finds the language, test framework and run command.
   `detect_tools.py` finds a configured mutation tool (`setup.cfg [mutmut]`, `stryker.conf.*`,
   `pitest` in Gradle or Maven, `.cargo/mutants.toml`).
2. **Scope.** `diff_scope.py` gives changed files and line ranges. Generated code, migrations,
   test files and pure type declarations are excluded and listed.
3. **Green baseline.** The tests that cover the scope must pass, twice. A failing or flaky baseline
   stops the run (flaky tests make mutant results meaningless) and is reported, with a pointer to
   flaky-test-hunter.
4. **Coverage map.** Per-test coverage of the scoped lines (coverage.py contexts, Jest/Vitest
   coverage per file, PIT's own map, `go test -coverprofile`), so each mutant runs only the tests
   that reach it.
5. **Mutate and run** with the chosen engine. Raw results are kept in the run directory.
6. **Normalise** every engine's output into one list: `killed`, `survived`, `no_coverage`,
   `timeout`, `runtime_error`, `compile_error` (dropped from the score), with the covering tests
   for each mutant.
7. **Triage survivors.** For each surviving mutant the agent reads the code and the covering tests,
   then decides:
   - `weak_assertion`: the covering test runs the line but doesn't check the result it changes.
     Names the test and the missing assertion.
   - `missing_case`: no test uses an input that distinguishes the versions (a boundary value).
     Names the input.
   - `equivalent`: with a checkable reason.
   - `accepted`: the behaviour doesn't matter (log message text, a debug-only branch), with the
     reason.
8. **Prove suggestions** (with `write_tests`): add the assertion or case, run it on the original
   code (must pass) and on the mutant (must fail).
9. **Rank, report, validate and render.**

## Tool stack

| Language | Tools (licence) |
|---|---|
| Python | mutmut (BSD-3-Clause), Cosmic Ray (MIT); built-in AST fallback |
| JavaScript and TypeScript | StrykerJS (Apache-2.0) |
| C# | Stryker.NET (Apache-2.0) |
| JVM (Java, Kotlin, Android unit tests) | PIT (Apache-2.0) with its JUnit 5 plugin |
| Rust | cargo-mutants (MIT) |
| Go | go-mutesting (MIT) |
| Swift (iOS) | Muter (MIT) |
| Dart and Flutter | mutation_test (BSD-3-Clause) |

The built-in Python operators: comparison flip and boundary shift (`<` ↔ `<=`), arithmetic swap,
boolean negation and `and`/`or` swap, constant change (`0` ↔ `1`, `n` → `n+1`, `""` → `"X"`),
return-value replacement (`None`, `True`/`False`), and removal of a call statement. Tool versions
are recorded in the report.

## Ranking (surviving mutants only)

`score = criticality x kind x proximity`

| Factor | Values |
|---|---|
| Criticality of the code | Money, auth, permissions, data deletion, or a journey-mapper P0/P1 step = 3; ordinary logic = 2; formatting, logging, display text = 1 |
| Mutant kind | Changes a branch or boundary (comparison, negation, condition) = 3; changes a value or removes a call = 2; changes a string or constant used only for display = 1 |
| Proximity | A unit test of this function ran the line and still passed = 2; only distant integration tests ran it = 1 |

Bands: P0 ≥ 12, P1 ≥ 8, P2 ≥ 4, otherwise P3. `no_coverage` mutants are not ranked here; they're
grouped by function as coverage gaps. `accepted` and `equivalent` mutants are never ranked.

## Output (`mutation_report.json`)

```text
metadata:    repo, commit, base_ref, scope{files[], line_ranges, excluded[{path, why}]},
             engine{name, version, config_source: repo|scratch|builtin}, budget, sampled
tools[]:     {name, version, status, reason}
baseline:    {runs[], passed, flaky_tests[]}
mutants[]:   {id, file, line, operator, original, mutated,
              status: killed|survived|no_coverage|timeout|runtime_error|compile_error,
              covering_tests[], killed_by?}
survivors[]: {mutant_id, triage: weak_assertion|missing_case|equivalent|accepted, reason,
              should_have_caught: test_id, suggestion{assertion_or_input, test_file},
              proof?{run_on_original, run_on_mutant}, score, priority}
coverage_gaps[]: {function, file, lines, mutants[]}
summary:     {mutants_total, killed, survived, no_coverage, timeout, runtime_error, equivalent,
              score_excluding_timeouts, score_including_timeouts, by_priority{},
              evidence_completeness}
limitations[], files_changed[]
```

## Validator fails when

- A score is shown without its numerator, denominator and scope, or doesn't match the counts.
- A survivor has no triage, or an `equivalent` or `accepted` triage has no reason.
- A `weak_assertion` names no covering test, or names a test that isn't in `covering_tests`.
- A proved suggestion lacks either run, or the mutant run passed.
- `no_coverage` mutants appear in the survivor ranking.
- The working-tree hash after the run differs from before (outside `files_changed`).
- Report text claims the code or suite is "well tested".

## Edge cases

- **Huge mutant counts.** The budget caps the run; mutants are sampled evenly across operators and
  files, and the report shows how many were skipped.
- **Slow integration tests covering everything.** Proximity keeps unit-level gaps on top. A
  per-mutant timeout of 3× the baseline duration (minimum 10 s) stops hangs.
- **Mutants that crash the interpreter or make infinite loops** are `timeout` or `runtime_error`,
  never `killed`.
- **Mobile.** Android unit tests via PIT; instrumented tests are too slow for mutation and are
  listed as out of scope. iOS via Muter on unit-test targets only.
- **Generated or vendored code in the diff** is excluded and listed.

## Files

```text
mutation-tester-skill/
  SKILL.md  RUBRIC.md  README.md
  data/operators.json               # per-engine operator names mapped to the common kinds
  schemas/mutation_report.schema.json
  scripts/detect_tools.py  run_engine.py  builtin_mutator.py  normalize_results.py
          validate_mutation_report.py  render_report.py
          diff_scope.py  run_tests.py  parse_results.py  detect_conventions.py   # shared copies
  examples/fare-calc/  run/  mutation_report.example.json
```

## Example

`examples/fare-calc/` is a small Python fare calculator with a pytest suite and a patch file
(`change.diff`) that the example scope is taken from. The answer key:

| Planted | Expected triage |
|---|---|
| Concession discount applies at `age >= 65`; tests use 30 and 70 only | `missing_case` (boundary 65), P1 |
| `total()` result is printed by the test but never asserted | `weak_assertion` naming the test, P0 (money) |
| A loop `while i < len(x)` that steps by 1 | `equivalent` for `i != len(x)`, with the reason |
| A log message text | `accepted` |
| A refund branch no test reaches | `no_coverage` gap, not ranked |

The example runs both mutmut and the built-in fallback so the report shows the two engines'
results side by side; both runs are real.

## Resolved questions

Agreed with the user before the build.

1. **Allow whole-repo runs in v1?** Only when asked; the default scope is changed code.
2. **Add the mutation tool to the repo's dev dependencies?** No: always run it from a scratch config.
3. **Default budget?** 30 minutes.

## As built (v1.0.0)

- **Shared files.** `diff_scope.py` is new in `shared/`. It reads git ranges (the merge base
  against the working tree by default, including untracked files; `--committed-only` for
  `base...HEAD`) or a unified diff (`--diff`), and handles renames, `-p0` and `-p1` paths and
  deletion-only hunks. Hunk line counts decide where a hunk ends, so a deleted line starting
  `--` isn't mistaken for a file header. Each file gets a kind (source, test, generated,
  migration, vendored, types, config, docs, binary) with the reason. Python functions come from
  `ast`, others from a brace/indent heuristic. The skill also carries `run_tests.py`,
  `parse_results.py` and `detect_conventions.py`.
- **Engines run:**
  - **Built-in** (`builtin_mutator.py`): AST operators on the scoped lines, each replacing one
    node's source text.
  - **mutmut 3** (`run_engine.py`): runs in a scratch copy, with a scratch `[tool.mutmut]` when the
    repo has none. mutmut reads its config even for `--version`, so the version check runs
    inside the configured copy.
  - **Stryker/PIT:** `normalize_results.py` reads mutation-testing-elements JSON. No example
    exercises this reader.
- **Coverage.** The built-in engine records per-test line coverage with a `sys.settrace` pytest
  plugin it writes to the run directory. Lines run only at import are recorded under `<import>`,
  so a module-level mutant runs every test that touched its file.
  - mutmut tracks tests per function, not per line. Its survivors on lines no test executed are
    reclassified as `no_coverage`, and keep `reclassified_from`.
- **Timeouts.** The per-mutant timeout is 3× one untraced suite run, with a 10 s minimum. The
  traced coverage run isn't used for timing: in a trial run it took 12.6 s against about 0.5 s
  for the plain suite. In the example run the two were close (2.9 s and 3.1 s).
- **Stale bytecode.** Bytecode is redirected to an empty cache (`PYTHONPYCACHEPREFIX`) and never
  written, so a stale `.pyc` can't stand in for a mutant.
- **Ids.** `normalize_results.py` assigns the report's ids (B.., M.., S..), ordered by file and
  line. The raw ids are kept as `raw_id`. The first proofs used raw built-in ids and tested the
  wrong mutants; `commands.sh` now uses the normalised ids.
- **Findings group mutants.** A finding is one weakness and lists every survived mutant it
  explains, from either engine. Every survivor is in exactly one finding, and every
  `no_coverage` mutant is in exactly one coverage gap.
- **Scores.** They are per engine and never added together, because the engines' operators
  differ. `accepted` mutants stay in the denominator.
- **Proximity is computed.** It is 2 when a covering test's body calls the mutated function
  directly, and always 1 for module-level mutants.
- **Proofs.** `prove_kill.py` proves a suggested test in a scratch copy: it must pass on the
  original and fail on each mutant. This gives evidence without writing to the repo, and
  `write_tests` adds `--in-place`.
- **Baseline JUnit bug.** The built-in engine first cleaned up `<run-dir>/junit`, which deleted
  the baseline's JUnit files. It now uses a private directory, and the example was rerun.
- **Example** (Windows for the baseline, the built-in engine and the proofs; WSL for mutmut 3.8.1):
  - **Built-in:** 18 of 29 killed (62.1%).
  - **mutmut:** 17 of 33 killed (51.5%); 7 of its mutants fell outside the scope.
  - **Findings:** F1 weak assertion (P0, score 12), F2 missing case (P1, 9), F3 missing case
    (P2, 6), F4 equivalent, F5 accepted.
  - **Coverage gap:** G1 holds 12 mutants.
  - **Timeouts:** one per engine.
  - **Proofs:** all four kill every mutant they target.
- **Validator:** 47 of 47 seeded violations are caught.
- **Found on a real Next.js/TypeScript repository with Vitest and StrykerJS 10 (v1.1.0)**:
  - **Stryker test ids.** `covering_tests` and `killed_by` held Stryker's numeric test ids, so no
    finding could name its test. They are now mapped through the report's `testFiles`; where
    the embedded test source shows the describe chain, the name is joined with ` > `, the form
    Vitest's JUnit ids (and `run_tests.py`) use. An unknown id is a normalisation problem.
  - **Engine metadata.** The version now comes from `framework` (the report has no
    `frameworkInfo`), and the command, config source and tree check come from the run record.
  - **`run_engine.py stryker`** runs StrykerJS in place: a config in the run directory mutates
    only the scope's line ranges (`run_engine.py ranges` prints them), uses perTest coverage, and
    puts Stryker's sandbox and JSON report in the run directory. A JSON Stryker config in the
    repo is used underneath (`repo`), otherwise the config is `scratch`. The repository tree is
    hashed before and after, and the code hash it ran on is recorded; the validator rejects an
    engine whose code hash differs from the baseline's (optional `code_hash` on the engine
    record).
  - **TypeScript functions.** Mutants in non-Python files get their function from the shared
    `diff_scope.functions()` JS/TS scanner (multi-line typed parameters, arrows, methods; no
    `new Error(`), the same ranges `scope.json` uses. `ts_scan.py` (new, in this skill) finds a
    JS/TS test by describe chain and title and checks whether its body calls the function, so
    TypeScript findings can reach proximity 2 (and P0). On the real
    repository 130 of 132 mutants got a function, and proximity was 2 for the promotion-engine
    findings (their tests call the function) and 1 for the URL-safety ones (reached through
    calendar sync).
  - **Proofs for Vitest and Jest.** `prove_kill.py` runs one test by file and `-t` name, links
    the repo's `node_modules` into the scratch copy (symlink or Windows junction, removed before
    the copy), and passes the repo's test environment (`--env`, `--extra-args`).
  - **`detect_tools.py`** took 85 s walking `node_modules`; it now prunes before descending
    (3.5 s there), lists TypeScript apart from JavaScript, and needs a project marker for a
    language, so one helper script no longer makes a Python recommendation.
  - SKILL.md now says what the baseline is for a scoped run (the tests that exercise the scoped
    code, named in `baseline.note`) and how a flaky test in it is handled.
  - The example (Python) is unchanged and still validates; no example uses Stryker.
- **Found on a real Python monorepo library with pytest and the builtin engine (v1.2.0)**:
  - **`--tests` and `--pytest-args`** on `builtin_mutator.py`. Every run was a bare `pytest`, so
    the engine collected the package's integration tests (which need a storage emulator): many
    minutes and failures, so it would have stopped as "not green". The coverage run, the
    timing run and each mutant's run are now limited to the named tests, and both options are
    recorded in the engine command.
  - **Shadowed imports.** The coverage plugin records whether each scoped file was imported
    and any other copy of the same module that was; the engine stops when the tests imported
    only another copy (an editable install pointing at another clone), since every mutant would
    look unexercised.
  - **Path scope.** `path_scope.py` (skill-local) writes a `scope.json` of every line of the
    files under one or more paths (a file may take a line range), classified like
    `diff_scope.py`, with `source.kind: "path"`. Before, a path scope needed a diff against the
    empty tree.
  - **Ids.** The builtin engine numbered mutants in `ast.walk` order and the normaliser
    renumbered them by line, so the console said B49 where the report said B2. The engine now
    numbers by file, line and column and writes `"ids": "position"`, and the normaliser keeps
    those ids. Older `builtin.json` files (the example's) are renumbered as before, and the
    renderer shows the engine's own id beside the report's when they differ.
  - **New operators.** `remove_method_call` (`name.upper() == X` -> `name == X`, also
    `strip`/`lower`/`casefold`...): a case-insensitive match was never mutated, so a 100% score
    said nothing about the "any case" requirement. `constant_string_compared` (kind branch) and
    `constant_string_named` (kind value): a wildcard `"*"` -> `""` in a permission comparison
    widened a grant but ranked as display text (kind 1). mutmut and
    Stryker string mutants are refined from the original line's tokens. Builtin mutants inside
    `typing.Literal[...]` carry a note that they matter only where the type is checked at run
    time.
  - **Renderer and validator.** `render_report.py` on a report without the computed summary
    says to run `--write-summary` instead of failing with a `KeyError`. Schema errors on
    findings, mutants and gaps name the item's id (`$.findings[7] (F1)`), because
    `--write-summary` re-sorts the findings before the check. A cited proof's `test_code` must
    match its recorded hash, and when the finding's suggestion has code, the proved code must be
    that code (whitespace aside).
  - SKILL.md has stack notes: tests in other packages of a monorepo are not run, editable
    installs, and running from the package directory.
  - The example's run is unchanged and still validates (its `builtin.json` predates position
    ids, so it is renumbered as before).
- **Found on a TypeScript pnpm monorepo with TypeScript 7, Vitest 5 and StrykerJS 10 (v1.3.0)**:
  - **Workspace sandbox.** Stryker's sandbox in the run directory broke a package's
    `extends: "../../tsconfig.base.json"` and pnpm's relative links (on Windows a relative link
    reached through a junction resolves against the junction's path). `run_engine.py stryker
    --sandbox auto` now mirrors the workspace levels above the package in `<run>/ws` (files
    copied, directories linked, by the skill-local `js_workspace.py`) and puts the sandbox at the
    package's depth; `run-dir` and `in-package` remain. `prove_kill.py` places its scratch copy
    the same way. The mirror is removed without following any link.
  - **TypeScript 7.** Stryker's tsconfig rewrite calls an API TypeScript 7 does not export, and
    it only suits the default in-package sandbox anyway, so it is disabled (tsconfigFile set to
    a missing file) for TypeScript >= 7 and for the mirror and run-dir sandboxes; the typescript
    checker is then removed and the record says so.
  - **Plugins and options.** The scratch config names the runner and checker plugins (pnpm hid
    them from Stryker core), is called `qa-stryker.config.json` (it shared a name with the
    repo's config), and `--stryker-option key=value` sets any other option
    (`vitest.configFile=...` for a unit-only config). A missing plugin is `not_run` with the
    package manager's install command (`pnpm add -D --filter <pkg> ...`).
  - **Engine failure.** A run without a report is `errored` with the first error line and a
    hint for known integration problems; a stale report from an earlier run is deleted first.
    `normalize_results.py` records an errored run from `--stryker-run` alone.
  - **prove_kill `-t`.** Vitest 4+ matches the name pattern against the describe chain joined
    with " > ", Jest and older Vitest with spaces; the pattern now accepts either (verified on
    Vitest 5: the space-joined form selected nothing). The runner is started through its entry
    script under node (cmd.exe rewrites `^` in an unquoted argument via npx.cmd). A test that
    does not run keeps its output and JUnit and records the first error line.
  - **Operators and coverage.** Stryker's MethodExpression is refined into `method_swap_logic`
    (kind branch: some/every, startsWith/endsWith, min/max) and `remove_method_call`; every
    Stryker mutant keeps its mutator name in `engine_operator` (schema 1.1.0, optional). A
    Stryker run with perTest coverage records it as the run's coverage, so evidence completeness
    can reach 1.0 for JS/TS; the validator counts it only when Stryker ran and checks
    `metadata.coverage` against `mutants.json`.
  - **Recon.** `detect_tools.py` finds Stryker where Node would resolve it (hoisted or in the
    package) and reports `node`: package manager, workspace root, runner and TypeScript versions.
  - `path_scope.py` passes the repository root to the shared `diff_scope.classify`, so a product
    package named `e2e` is source (the shared fix). The example is unchanged apart from schema_version 1.1.0 and
    validates.
- **v1.4.0: Go.** A review on a Go CLI found every Go path quiet or wrong; fixed with red/green
  cases in the new `scripts/self_test.py` (a synthetic Go module built in a temp dir; the Go
  cases skip without `go`).
  - **Builtin Go engine** (`go_mutator.py`, called by `builtin_mutator.py` 1.2.0 when the
    changed source is Go): text-level mutants from Go tokens (`go_scan.py`; comments, strings,
    raw strings and runes are never touched): comparisons, `==`/`!=`, `&&`/`||`, binary
    arithmetic (not string concatenation), assignments, `++`/`--`, `break`/`continue`,
    `true`/`false`, `return ..., err` to `nil`, `if` negation. Mutants go to the compiler with
    `go test -overlay`, so the repository is never written; coverage is per top-level test from
    one `-cover` test binary per package; a mutant that does not build is `compile_error`.
  - **Loud refusals.** `builtin_mutator.py` exits 2 naming the files of languages it does not
    mutate and the tool to use; `run_engine.py ranges` warns about non-JS/TS files and exits 2
    when there is no range; `prove_kill.py` refuses a test file it has no runner for before
    copying anything, and proves Go tests with `go test -run` and `-overlay`.
  - **Normalising.** `--mte` (with `--mte-engine`, `--mte-run`) reads any
    mutation-testing-elements report and names the engine after its framework; `--stryker`
    given another framework's report no longer labels it "stryker". `data/operators.json` has
    gremlins and go-mutesting tables (`INVERT_LOOPCTRL` is control_flow). Mutants of one engine
    that make the same edit on the same line are merged (`duplicates_merged`).
  - **Validator.** Proximity reads Go tests; when every covering test is in a language it does
    not parse, proximity is not computed and the error says why (it was silently 1). Engines
    carry `run_by` (schema 1.2.0, optional): an external engine counts half in evidence
    completeness, which also averages in the share of ranked findings backed by a proof, so a
    run where the skill's engine or prover did not run is never 1.0. The example is unchanged
    (both engines are the skill's, every ranked finding is proved) and validates.
- **v1.5.0: slow imports and the budget.** A review on a Flask app whose import loads a large data
  tree, with red/green cases in `scripts/self_test.py` (a synthetic "tea shop" package whose rate
  table is loaded while conftest is imported).
  - **Import-time coverage** (`builtin_import_time_coverage`): a function line that ran only under
    `<import>` (collection) is covered by every test that touched its file, with a note; it was
    `no_coverage` although tests depended on the data it built.
  - **Budget** (`builtin_budget_uses_covering_tests`): mutants run in a round-robin order over
    operators and files until `--minutes` is spent, instead of a sample sized as if every mutant
    ran the whole `--tests` selection; the plan (count, estimate from the covering-test share) is
    printed first and progress lines are flushed. `sampled.chosen` is the number attempted.
  - **Proofs** (`prove_kill_original_timeout`, `prove_kill_default_timeout`,
    `prove_kill_collection_error`): `--timeout` defaults to the engines' measured `timeout_s` in
    `mutants.json` (at least 60 s) and is recorded on the proof; a test that never ran on the
    original is `original_not_run` (was `fails_on_original`) and its mutants are not run; a test
    that never ran on a mutant (collection broke) is `runtime_error` (was `does_not_kill`). The
    validator still accepts only `kills`. The example is unchanged and validates.
