#!/usr/bin/env bash
# The run behind examples/state/. Run from the repository root: the validator commands use paths from there.
# The reports are other skills' examples, standing in for one change's evidence; the "people" are the roles
# in the config. In a real run the state goes to a temporary folder (omit --state-dir).
set -euo pipefail
S=qa-orchestrator-skill/examples/state
O="python qa-orchestrator-skill/scripts/orchestrate.py"
rm -rf "$S"
$O init --config qa-orchestrator-skill/examples/orchestrator.example.json --run-id example-2026.12 --skills-root . --state-dir "$S" \
   --subject "Loyalty points at checkout, release 2026.12 (example: other skills' example reports stand in for one change's evidence)"
$O next --state "$S"

# G1 Ready: the readiness review (SHOP-201 is ready)
$O record --state "$S" --gate G1 --step readiness --report ticket-readiness-reviewer-skill/examples/batch.example.json
$O evaluate --state "$S" --gate G1
$O approve --state "$S" --gate G1 --by "QA lead" --how "in chat, after reading the SHOP-201 review"

# G2 Designed: the test cases (every row covered; two open questions)
$O record --state "$S" --gate G2 --step cases --report test-case-designer-skill/examples/test_cases.example.json
$O evaluate --state "$S" --gate G2
$O approve --state "$S" --gate G2 --by "QA lead" --how "in chat; Q-1 and Q-2 go to the product owner before G3"

# G3 Verified in CI: one failing test, triaged; the criteria fail, so an approval needs an override
$O record --state "$S" --gate G3 --step suite --report ci-suite-runner-skill/examples/runs/checkout-staging/suite_run.json
$O record --state "$S" --gate G3 --step triage --report ci-failure-triager-skill/examples/triage.example.json
$O evaluate --state "$S" --gate G3
$O approve --state "$S" --gate G3 --by "QA lead" --how "in chat" || echo "(refused: failed criteria need --override)"
$O approve --state "$S" --gate G3 --by "QA lead" --how "in chat, 2026-11-12" \
   --override "The failing test and the real failure are drafted as defects in the triage report; the QA lead accepts them into G4, where the release gate decides"

# G4 Release ready: the release gate says no_go; the release manager rejects, and the run stops
$O record --state "$S" --gate G4 --step gate --report release-readiness-gate-skill/examples/rc-2026.11.2/gate_report.json
$O evaluate --state "$S" --gate G4
$O reject --state "$S" --gate G4 --by "Release manager" --how "release meeting, 2026-11-12" \
   --reason "Seven blockers, including three security P0s and the stale crash report; no release until a fresh gate report shows them fixed"
$O status --state "$S"
