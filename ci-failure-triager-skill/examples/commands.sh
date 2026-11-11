#!/usr/bin/env bash
# The run behind triage.example.json. Run from ci-failure-triager-skill/.
# TW is an empty directory for the sample repository (make_repo.py; the commit ids are fixed).
set -euo pipefail
TW=${TW:?set TW to an empty directory for the sample repository}
python examples/make_repo.py "$TW"

# Recorded API responses from real local runs (pytest and pip) at the failing commit; no CI is called.
# Rerunning this changes the timings, the flaky test's picks and the log line numbers, and invalidates the triage.
# python examples/record_example_run.py "$TW" --out examples/recorded

# GitHub Actions run 109: fetch (recorded), failures of attempt 1, the recorded re-run, owning changes, candidates
python scripts/fetch_run.py --system github --from-dir examples/recorded/github --out examples/run
python scripts/extract_failures.py --run examples/run --out examples/run/failures.json
python scripts/rerun.py --run examples/run --authorised-by "Repository owner, allow_rerun in the task" --recorded
python scripts/owning_change.py --run examples/run --repo "$TW" --out examples/run/changes.json
python scripts/classify.py --run examples/run --out examples/run/candidates.json

# Azure DevOps build 2207 of the same commit (no re-run): the same failures through the other normaliser
python scripts/fetch_run.py --system azure --from-dir examples/recorded/azure --out examples/run-azure
python scripts/extract_failures.py --run examples/run-azure --out examples/run-azure/failures.json
python scripts/owning_change.py --run examples/run-azure --repo "$TW" --out examples/run-azure/changes.json
python scripts/classify.py --run examples/run-azure --out examples/run-azure/candidates.json

# The triage (the agent's judgment on the candidates) is examples/triage.example.json
python scripts/validate_triage.py examples/triage.example.json --run examples/run
python scripts/render_report.py examples/triage.example.json --defects-md examples/drafts.md
