#!/usr/bin/env sh
# The run behind examples/mutation_report.example.json, in order. Run from mutation-tester-skill/.
# Every step is real. mutmut 3 needs fork(), so its step ran under WSL (Ubuntu, Python 3.12, a
# throwaway venv with mutmut, pytest and coverage); everything else ran on Windows, Python 3.14.
# Mutant ids (B.., M..) are the ones normalize_results.py assigns, not the engines' raw ids.
# builtin.json was made by builtin_mutator.py 1.0.0, before ids by position: a rerun with 1.1+
# numbers the builtin mutants differently (and keeps those ids), adds the newer operators, and
# would need the report, proofs and the ids below redone.
# Durations and the timeout differ on another machine; validate against these files.
set -e
X=examples/fare-calc
O=examples/run
python scripts/detect_tools.py $X --out $O/tools.json
python scripts/diff_scope.py --repo $X --diff $X/change.diff --out $O/scope.json
python scripts/run_tests.py --preset pytest --cwd $X --out $O/runs.json --repeat 2 --label baseline --run-prefix G --check-tree
python scripts/builtin_mutator.py --repo $X --scope $O/scope.json --run-dir $O
# under WSL, with mutmut installed in a scratch venv (never in the repo):
#   python scripts/run_engine.py mutmut --repo $X --scope $O/scope.json --run-dir $O --mutmut ~/mt-venv/bin/mutmut
python scripts/normalize_results.py --scope $O/scope.json --repo $X --builtin $O/builtin.json --mutmut $O/mutmut.json \
    --coverage $O/coverage.json --out $O/mutants.json
P="python scripts/prove_kill.py --repo $X --mutants $O/mutants.json --run-dir $O/proof --out $O/proofs.json"
$P --finding F1 --mutant B11 --mutant B24 --mutant M25 --test-file tests/test_pricing.py \
   --test-code $O/suggested_tests/total_fee.py --test-name test_total_adds_one_booking_fee
$P --finding F2 --mutant B12 --mutant B14 --mutant M11 --test-file tests/test_pricing.py \
   --test-code $O/suggested_tests/concession_boundary.py --test-name test_concession_starts_at_65
$P --finding F3 --mutant B25 --test-file tests/test_refunds.py \
   --test-code $O/suggested_tests/operator_cancellation.py --test-name test_operator_cancellation_refunds_everything_plus_goodwill
$P --gap G1 --mutant B28 --mutant B31 --mutant M27 --mutant M28 --test-file tests/test_refunds.py    --test-code $O/suggested_tests/operator_cancellation.py --test-name test_operator_cancellation_refunds_everything_plus_goodwill
