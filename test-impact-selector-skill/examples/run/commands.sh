#!/usr/bin/env bash
# The real run behind the example reports, from the repository root. Nothing here edits
# examples/harbourline: every change is applied to a scratch copy. Hostnames in the JUnit files
# were replaced with "host" after each run.
set -euo pipefail
SKILL=test-impact-selector-skill
EX=$SKILL/examples
SCRATCH=${SCRATCH:-$(mktemp -d)}

# 1. Per-test coverage for the api package: coverage.py 7.17.2 and pytest-cov 7.1.0 in a throwaway
#    venv under WSL (Ubuntu, Python 3.12), on a copy of api/. The raw JSON is run/coverage-api.json.
cp -r $EX/harbourline/api "$SCRATCH/api"
(cd "$SCRATCH/api" && COVERAGE_FILE="$SCRATCH/.coverage" python -m pytest -q -p no:cacheprovider \
    --cov=harbourline_api --cov-context=test --cov-report= \
  && COVERAGE_FILE="$SCRATCH/.coverage" python -m coverage json --show-contexts -o coverage-api.json)
cp "$SCRATCH/api/coverage-api.json" $EX/run/coverage-api.json
python $SKILL/scripts/coverage_map.py normalise $EX/run/coverage-api.json --repo $EX/harbourline --package api \
    --out $EX/run/coverage_map.json

# 2. History: each of the 12 diffs applied to a fresh copy, then both full suites (Windows 11,
#    Python 3.14, pytest; Node 26, Vitest 3.2.4 installed in a scratch copy of web/).
for diff in $EX/history/H*.diff; do
  id=$(basename "$diff" .diff)
  rm -rf "$SCRATCH/h" && cp -r $EX/harbourline "$SCRATCH/h"
  (cd "$SCRATCH/h" && git apply "$OLDPWD/$diff")
  mkdir -p $EX/history/$id
  (cd "$SCRATCH/h/api" && python -m pytest -q -p no:cacheprovider --junitxml="$OLDPWD/$EX/history/$id/api.xml") || true
  (cd "$SCRATCH/h/web" && npm install --no-audit --no-fund >/dev/null \
    && npx vitest run --reporter=junit --outputFile="$OLDPWD/$EX/history/$id/web.xml") || true
done
# history/history.json lists each change, its diff and its two runs (package, JUnit file, exit code).

# 3. One selection per example change.
for change in C1-late-sailing-discount C2-refund-fixture C3-silver-loyalty C4-pin-vitest C5-pay-button-colour; do
  id=${change%%-*}
  python $SKILL/scripts/select_tests.py --repo $EX/harbourline --diff $EX/changes/$change.diff \
      --coverage $EX/run/coverage_map.json --journeys $EX/journeys/journey_map.json \
      --history $EX/history/history.json --out $EX/run/$id/selection.json
done
python $SKILL/scripts/build_command.py $EX/run/C1/selection.json --ci github --ci-out $EX/run/C1/ci_snippet.yml

# 4. Replay the 12 past changes in strict mode.
python $SKILL/scripts/replay.py --repo $EX/harbourline --history $EX/history/history.json \
    --coverage $EX/run/coverage_map.json --journeys $EX/journeys/journey_map.json --out $EX/run/replay.json

# 5. Verify C1: the selection, then both full suites, with the change applied to a scratch copy.
rm -rf "$SCRATCH/c1" && cp -r $EX/harbourline "$SCRATCH/c1"
(cd "$SCRATCH/c1" && git apply "$OLDPWD/$EX/changes/C1-late-sailing-discount.diff")
(cd "$SCRATCH/c1/web" && npm install --no-audit --no-fund >/dev/null)
V=$EX/run/C1/verify/runs.json
python $SKILL/scripts/run_tests.py --preset pytest --cwd "$SCRATCH/c1/api" --out $V --label "verify-selected api" \
    --run-prefix SA --tests tests/api/test_booking_api.py tests/unit/test_pricing.py tests/api/test_loyalty_api.py \
    tests/unit/test_bookings.py tests/api/test_refund_api.py
python $SKILL/scripts/run_tests.py --preset vitest --cwd "$SCRATCH/c1/web" --out $V --label "verify-selected web" \
    --run-prefix SW --tests e2e/checkout.e2e.test.ts
python $SKILL/scripts/run_tests.py --preset pytest --cwd "$SCRATCH/c1/api" --out $V --label "verify-full api" --run-prefix FA
python $SKILL/scripts/run_tests.py --preset vitest --cwd "$SCRATCH/c1/web" --out $V --label "verify-full web" --run-prefix FW

# 6. The reports copy the computed sections and add the headline, the misses' explanations, the
#    recommendations and the limitations; then the summary is computed and everything checked.
python $SKILL/scripts/validate_impact_report.py $EX/impact_report.example.json --write-summary \
    --selection $EX/run/C1/selection.json --replay $EX/run/replay.json --runs $EX/run/C1/verify/runs.json --recompute
for id in C2 C3 C4 C5; do
  python $SKILL/scripts/validate_impact_report.py $EX/reports/$id.impact_report.json --write-summary \
      --selection $EX/run/$id/selection.json --replay $EX/run/replay.json --recompute
done
