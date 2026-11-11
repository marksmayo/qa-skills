#!/usr/bin/env bash
# The real run behind test_data_report.example.json, from the repository root.
# Experiments run on a scratch copy; clinic-api/ itself is never changed.
# Hostnames in the JUnit files were replaced with "host" afterwards.
set -euo pipefail
SKILL=test-data-auditor-skill
EX=$SKILL/examples
COPY=${COPY:-$(mktemp -d)/clinic-api}

python $SKILL/scripts/inventory.py $EX/clinic-api --out $EX/run/inventory.json
python $SKILL/scripts/scan_data.py $EX/clinic-api --inventory $EX/run/inventory.json --today 2026-11-11 --out $EX/run/candidates.json
python $SKILL/scripts/schema_drift.py $EX/clinic-api --inventory $EX/run/inventory.json --out $EX/run/drift.json

# Experiments (Windows 11, Python 3.14, pytest), on a copy with clock_shift.py at its root.
rm -rf "$COPY" && cp -r $EX/clinic-api "$COPY" && cp $SKILL/scripts/clock_shift.py "$COPY/"
OUT=$EX/run/runs.json
RT="python $SKILL/scripts/run_tests.py"
$RT --preset pytest --cwd "$COPY" --out $OUT --label baseline --run-prefix B --repeat 2
CLOCK="python -m pytest -q -p no:cacheprovider -p clock_shift --junitxml={junit}"
$RT --preset custom --cmd "$CLOCK" --cwd "$COPY" --env TDA_CLOCK_SHIFT=+400d --out $OUT --label "clock +400d" --run-prefix S || true
$RT --preset custom --cmd "$CLOCK" --cwd "$COPY" --env TDA_CLOCK_DATE=2027-01-31 --out $OUT --label "clock month-end 2027-01-31" --run-prefix M || true
$RT --preset custom --cmd "$CLOCK" --cwd "$COPY" --env TDA_CLOCK_DATE=2028-02-29 --out $OUT --label "clock leap-day 2028-02-29" --run-prefix L || true
WAIT="tests/test_waitlist.py::test_waitlist_starts_empty tests/test_waitlist.py::test_join_waitlist"
$RT --preset pytest --cwd "$COPY" --out $OUT --label "order reverse" --run-prefix O --order reverse --tests $WAIT || true
$RT --preset pytest --cwd "$COPY" --out $OUT --label "isolate" --run-prefix I --isolate --tests $WAIT || true

# Locale run (WSL Ubuntu, Python 3.12 with pytest in a throwaway venv): Windows ignores LANG.
#   cp -r $EX/clinic-api ~/tda
#   ~/venv/bin/python $SKILL/scripts/run_tests.py --preset pytest --cwd ~/tda --out $OUT \
#       --label "locale C.UTF-8 (WSL Ubuntu)" --run-prefix C --locale C.UTF-8 --tests tests/test_billing.py

# The report adds the review decisions; the computed fields are then filled in and checked.
python $SKILL/scripts/validate_test_data_report.py $EX/test_data_report.example.json --candidates $EX/run/candidates.json \
    --repo $EX/clinic-api --runs $OUT --evidence $EX/run --write-summary
