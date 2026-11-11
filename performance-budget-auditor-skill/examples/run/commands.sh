#!/usr/bin/env bash
# How examples/run/ was produced (bash, from performance-budget-auditor-skill/). Needs Playwright with
# Chromium and Firefox (pip install -r requirements.txt; python -m playwright install chromium firefox)
# and web-vitals in the cache (python scripts/fetch_assets.py). Lab numbers depend on the machine:
# a rerun gives different values, and the report records which machine produced these.
set -e
R=examples/run
start_site() { python examples/bakery/serve.py --site-version "$1" > /dev/null 2>&1 & SITE=$!; sleep 2; kill -0 $SITE || { echo "site did not start (port 8142 in use?)"; exit 1; }; }
stop_site() { kill $SITE 2>/dev/null || true; sleep 1; }

python scripts/fetch_assets.py --check

# Baseline: the previous release (1.0, a 290 KB hero), home and about only.
start_site 1.0
python scripts/measure.py examples/plan.json --out $R/baseline/measurements.json --artifacts $R/baseline/artifacts --templates home,about --no-diagnostic
stop_site
python scripts/attribute.py $R/baseline/measurements.json --budget examples/budget.json --out $R/baseline/candidates.json

# Current release (1.1): every page, both profiles, cold cache; then Firefox on one page for comparison.
start_site 1.1
python scripts/measure.py examples/plan.json --out $R/measurements.json --artifacts $R/artifacts
python scripts/measure.py examples/plan.json --out $R/measurements.json --artifacts $R/artifacts --append --browser firefox --profiles desktop --only about --no-diagnostic
stop_site
python scripts/attribute.py $R/measurements.json --budget examples/budget.json --out $R/candidates.json
python scripts/compare.py $R/baseline/candidates.json $R/candidates.json --out $R/regressions.json

python scripts/validate_report.py examples/perf_report.example.json --measurements $R/measurements.json --candidates $R/candidates.json --budget examples/budget.json --regressions $R/regressions.json --plan examples/plan.json
