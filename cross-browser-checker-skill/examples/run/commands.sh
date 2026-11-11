#!/usr/bin/env bash
# How examples/run/ was produced (bash, from cross-browser-checker-skill/). Needs Playwright with Chromium,
# Firefox and WebKit (pip install -r requirements.txt; python -m playwright install chromium firefox webkit)
# and the feature data (python scripts/fetch_data.py).
set -e
R=examples/run
python scripts/fetch_data.py --check
python examples/travel-site/serve.py site --port 8145 > /dev/null 2>&1 & SITE=$!; sleep 1
kill -0 $SITE || { echo "site did not start (port 8145 in use?)"; exit 1; }
python scripts/capture.py examples/targets.json --out $R/capture
kill $SITE
python scripts/feature_scan.py --repo examples/travel-site --out $R/features.json
python scripts/compare_engines.py $R/capture/runs.json --features $R/features.json --out $R/candidates.json
python scripts/validate_report.py examples/report.example.json --runs $R/capture/runs.json --candidates $R/candidates.json --features $R/features.json --targets examples/targets.json
