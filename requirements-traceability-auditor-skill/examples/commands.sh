#!/usr/bin/env bash
# The steps behind trace.example.json. Run from requirements-traceability-auditor-skill/.
# HL is an empty directory for the sample repository (make_repo.py; fixed commit ids).
set -euo pipefail
HL=${HL:?set HL to an empty directory for the sample repository}
python examples/make_repo.py "$HL"
# The JUnit in run/ is a real run: (cd "$HL" && python -m pytest --junitxml=junit.xml), hostname replaced, paths with "/".
python scripts/fetch_ticket.py --from-file examples/tickets --source connector --out examples/run/tickets.json
python scripts/load_requirements.py --tickets examples/run/tickets.json --doc examples/requirements.md --out examples/run/requirements.json
python scripts/collect_evidence.py --requirements examples/run/requirements.json --repo "$HL" --junit examples/run/junit.xml \
    --test-management examples/test-management.json --out examples/run/evidence.json
python scripts/build_matrix.py --requirements examples/run/requirements.json --evidence examples/run/evidence.json --out examples/run/matrix.json
# examples/triage.json is the agent's triage of the inferred links; trace.example.json adds it, the sources, the headline and the limitations
python scripts/build_matrix.py --requirements examples/run/requirements.json --evidence examples/run/evidence.json --triage examples/triage.json --out examples/run/matrix.json
python scripts/validate_trace.py examples/trace.example.json --requirements examples/run/requirements.json --evidence examples/run/evidence.json \
    --junit examples/run/junit.xml --repo "$HL"
python scripts/render_report.py examples/trace.example.json --csv examples/matrix.csv
