#!/usr/bin/env bash
# The run behind examples/privacy_audit.example.json. Run from the repository root.
# Needs Playwright with Chromium (pip install -r privacy-flow-auditor-skill/requirements.txt).
set -euo pipefail
S=privacy-flow-auditor-skill
E=$S/examples
LOG=$(mktemp -d)/server.log

# The sample app and its stub vendors on loopback; the browser maps *.test to 127.0.0.1
python $E/clinic-signup/serve.py --log "$LOG" &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
sleep 1
kill -0 $PID || exit 1

# 1. A fresh synthetic canary set
python $S/scripts/match_canaries.py new --out $E/run/canaries.json --id 7f3a

# 2. Static scan (the stub vendors stand in for third-party code, so they are excluded)
python $S/scripts/scan_privacy.py $E/clinic-signup --exclude stub-vendors --out $E/run/static.json

# 3. Live capture in three consent states, typing the canaries; the server log is read afterwards
python $S/scripts/capture_flows.py $E/flows.json --canaries $E/run/canaries.json --out $E/run/capture.json \
  --server-log "$LOG" --screenshots $E/run/screenshots

# 4. Candidates, recipients and the consent matrix
python $S/scripts/merge_candidates.py --capture $E/run/capture.json --static $E/run/static.json --flows $E/flows.json \
  --lists $E/lists.example.json --declared $E/clinic-signup/declared_processors.json --regions AU,EU --out $E/run/candidates.json

# 5. Validate the audit against the run
python $S/scripts/validate_privacy_audit.py $E/privacy_audit.example.json --candidates $E/run/candidates.json \
  --capture $E/run/capture.json --canaries $E/run/canaries.json --evidence $E/run --repo $E/clinic-signup --recompute

# 6. The static-only variant: merge and audit from the static scan alone (no capture)
python $S/scripts/merge_candidates.py --static $E/run/static.json --declared $E/clinic-signup/declared_processors.json --regions AU,EU \
  --run-status-reason "static-only example: the live capture was not run (no browser in the environment), so only the code was read" \
  --out $E/static-only/candidates.json
python $S/scripts/validate_privacy_audit.py $E/privacy_audit.static-only.example.json --candidates $E/static-only/candidates.json \
  --repo $E/clinic-signup --recompute
