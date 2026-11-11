#!/usr/bin/env bash
# How examples/run/ was produced (bash, from resilience-prober-skill/). Needs Playwright with Chromium
# (pip install -r requirements.txt; python -m playwright install chromium). Every fault is injected in the browser.
set -e
R=examples/run
python scripts/plan_scenarios.py examples/flows.json --out $R/scenarios.json
python examples/parcel-tracker/server.py --port 8147 > /dev/null 2>&1 & SITE=$!; sleep 1
kill -0 $SITE || { echo "server did not start (port 8147 in use?)"; exit 1; }
python scripts/run_resilience.py examples/flows.json $R/scenarios.json --out $R --runs 2
kill $SITE
python scripts/classify.py $R/run.json $R/scenarios.json examples/flows.json --out $R/candidates.json
python scripts/validate_resilience.py examples/resilience_report.example.json --run $R/run.json --scenarios $R/scenarios.json --candidates $R/candidates.json
