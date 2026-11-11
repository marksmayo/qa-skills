# Mutation tester example

`fare-calc/` is a small Python fare calculator for a harbour ferry, with a pytest suite.
`fare-calc/change.diff` is the change under review: it adds a 65+ concession, a booking total with a
booking fee, a passenger manifest, and an operator-cancellation refund with a goodwill credit. The
files in `fare-calc/` are the state after the change; the diff applies in reverse
(`git apply -R --check change.diff`).

`run/` holds the real run behind `mutation_report.example.json`, step by step in
`run/commands.sh`:

| File | From |
|---|---|
| `tools.json` | `detect_tools.py` |
| `scope.json` | `diff_scope.py --diff change.diff` |
| `runs.json`, `junit/` | the two green baseline runs (`run_tests.py`) |
| `coverage.json`, `builtin.json` | `builtin_mutator.py` (Windows, Python 3.14) |
| `mutmut.json`, `mutmut.log` | `run_engine.py mutmut` (WSL Ubuntu, Python 3.12, mutmut 3.8.1 in a throwaway venv) |
| `mutants.json` | `normalize_results.py` |
| `proofs.json`, `suggested_tests/` | `prove_kill.py`, one run per suggested test |

## Answer key

| Planted | Result |
|---|---|
| `total()` is printed by its test but only asserted truthy | F1 `weak_assertion`, P0 (money × value × a unit test ran it): B11, B24, M25 |
| The concession starts at 65; tests use ages 30 and 70 | F2 `missing_case` (boundary 65), P1 (money × boundary × only reached through `fare_cents`): B12, B14, M11 |
| `GOODWILL_CENTS` is only used in the untested branch | F3 `missing_case`, P2 (a module constant, so proximity 1): B25 |
| `while i < len(passengers)` stepping by 1 | F4 `equivalent` for `i != len(passengers)`, with the argument: B4 |
| The INFO log line in `total()` | F5 `accepted`: 2 builtin and 6 mutmut mutants |
| The operator-cancellation branch no test reaches | G1 coverage gap, not ranked: 4 builtin mutants, and 8 mutmut mutants reclassified from survived |
| `i += 1` mutated to `i += 0` / `i = 1` | one timeout per engine, never counted as a kill |

The two engines disagree in instructive ways. mutmut leaves module constants alone, so only the
builtin engine finds B11, B12 and B25. Only the builtin engine has the `<` to `!=` operator, so only
it produces the equivalent mutant. mutmut mutates every argument of the log call. mutmut tracks
tests per function, so it calls the uncovered branch "survived" until line coverage reclassifies it.

The pre-existing refund tiers are outside the scope: mutmut made 7 mutants there, which were dropped.
The four suggested tests in `run/suggested_tests/` were each proved in a scratch copy (P1 to P4); the
repository itself is unchanged.

Every file is real output. Editing `fare-calc/` changes its code hash and the mutants' line texts
and invalidates the example until the run is repeated.
