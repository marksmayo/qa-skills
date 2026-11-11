#!/usr/bin/env bash
# How examples/run/ was produced (bash, from visual-regression-reviewer-skill/). Needs Playwright with
# Chromium (pip install -r requirements.txt; python -m playwright install chromium). Mode capture_both:
# v1 is the baseline site, v2 the current one, both on loopback.
set -e
R=examples/run
start() { python examples/storefront/serve.py $1 --port $2 > /dev/null 2>&1 & PIDS="$PIDS $!"; sleep 1; kill -0 $! || { echo "$1 did not start (port $2 in use?)"; exit 1; }; }
start v1 8143
start v2 8144
python scripts/capture.py examples/targets.json --set baseline --out $R/baseline
python scripts/capture.py examples/targets.json --set current --out $R/current       # two takes each
kill $PIDS
python scripts/diff_images.py --baseline $R/baseline --current $R/current --out $R/diff.json --diffs $R/diffs --ignore examples/targets.json
python scripts/group.py $R/diff.json --out $R/groups.json --crops $R/crops --review $R/review.md --targets examples/targets.json
python scripts/validate_review.py examples/review.example.json --groups $R/groups.json --diff $R/diff.json
