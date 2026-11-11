#!/usr/bin/env bash
# The steps behind test_cases.example.json. Run from test-case-designer-skill/.
set -euo pipefail
python scripts/fetch_ticket.py --from-file examples/SHOP-310.issue.json --source connector --out examples/run/ticket.json
python scripts/analyse_criteria.py examples/run/ticket.json --out examples/run/analysis.json
# examples/model.json is the agent's model: techniques and reasons per criterion, questions, dismissed signals
python scripts/design_rows.py examples/model.json --out examples/run/rows.json
# examples/test_cases.example.json is the agent's cases, one per row or group of rows
python scripts/export_cases.py examples/test_cases.example.json --xray examples/xray.json --csv examples/cases.csv
python scripts/validate_cases.py examples/test_cases.example.json --ticket examples/run/ticket.json --model examples/model.json \
    --rows examples/run/rows.json --xray examples/xray.json --csv examples/cases.csv
python scripts/render_report.py examples/test_cases.example.json --markdown examples/cases.md
