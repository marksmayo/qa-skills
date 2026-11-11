#!/usr/bin/env bash
# How examples/run/ was produced (bash, from i18n-checker-skill/). The static steps are stdlib; the runtime
# step needs Playwright with Chromium (pip install -r requirements.txt; python -m playwright install chromium).
set -e
R=examples/run
python scripts/fetch_cldr.py --check
python scripts/discover.py --repo examples/bookclub --out $R/resources.json
python scripts/check_keys.py $R/resources.json --repo examples/bookclub --out $R/keys.json
python scripts/scan_code.py --repo examples/bookclub --out $R/code.json
python examples/bookclub/serve.py site --port 8146 > /dev/null 2>&1 & SITE=$!; sleep 1
kill -0 $SITE || { echo "site did not start (port 8146 in use?)"; exit 1; }
python scripts/render_locales.py examples/targets.json --out $R/runtime
kill $SITE
python scripts/merge_findings.py --resources $R/resources.json --keys $R/keys.json --code $R/code.json --runtime $R/runtime/runtime.json --repo examples/bookclub --out $R/candidates.json --patch $R/missing_keys.patch
python scripts/validate_report.py examples/report.example.json --candidates $R/candidates.json --keys $R/keys.json --resources $R/resources.json --repo examples/bookclub --runtime $R/runtime/runtime.json
