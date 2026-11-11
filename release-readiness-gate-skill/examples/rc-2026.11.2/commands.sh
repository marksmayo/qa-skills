#!/usr/bin/env bash
# The gate run behind gate_report.json. Run from the repository root.
# HS is an empty directory for the sample repository (built with make_repo.py; the commit ids are fixed).
set -euo pipefail
G=release-readiness-gate-skill
RC=$G/examples/rc-2026.11.2
HS=${HS:?set HS to an empty directory for the sample repository}

python $RC/make_repo.py "$HS"

# 1. Inventory: hash every report in the manifest, run each skill's own validator, check provenance against the candidate
python $G/scripts/collect_reports.py --reports $RC/reports.json --candidate $RC/candidate.json --policy $RC/release_policy.json \
    --waivers $RC/waivers.json --repo "$HS" --out $RC/collection.json

# 2. Policy, waivers and verdict (waiver expiry is checked as of the gate's run date)
python $G/scripts/apply_policy.py $RC/collection.json --as-of 2026-11-12 --out $RC/gate_report.json

# 3. Validate and render
python $G/scripts/validate_gate.py $RC/gate_report.json --reports --recompute $RC/collection.json
python $G/scripts/render_report.py $RC/gate_report.json --markdown $RC/gate_report.md
