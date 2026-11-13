# QA run example-2026.12

Subject: Loyalty points at checkout, release 2026.12 (example: other skills' example reports stand in for one change's evidence)
Status: **stopped**. State: `qa-orchestrator-skill/examples/state`.

| Gate | Status | Decision | By | Evidence |
|---|---|---|---|---|
| G1 Ready | approved | approved | QA lead | ticket-readiness-reviewer (validator 0) |
| G2 Designed | approved | approved | QA lead | test-case-designer (validator 0) |
| G3 Verified in CI | approved | approved with override | QA lead | ci-suite-runner (validator 0), ci-failure-triager (validator 0) |
| G4 Release ready | rejected | rejected | Release manager | release-readiness-gate (validator 0) |
| G5 Deployed | pending | - | - | - |
| G6 Hypercare | pending | - | - | - |

## G1 Ready (approved)

- ticket-readiness-reviewer: `ticket-readiness-reviewer-skill/examples/batch.example.json` (SHA-256 07f3417353b9..., validator exit 0: VALID)
- criterion readiness-valid: **pass** (validator exited 0 and the report is unchanged)
- criterion ticket-ready: **pass** (reviews[ticket.key=SHOP-201].verdict.value = 'ready' (allowed ['ready', 'ready_with_questions']))
- decision: approved by QA lead (in chat, after reading the SHOP-201 review) at 2026-11-13T20:44:31Z

## G2 Designed (approved)

- test-case-designer: `test-case-designer-skill/examples/test_cases.example.json` (SHA-256 50edec126467..., validator exit 0: test-case-designer-skill\examples\test_cases.example.json: valid. 42 case(s) for 5 of 5 criteria; rows 52/52 covered, 0 infeasible; 2 question(s))
- criterion cases-valid: **pass** (validator exited 0 and the report is unchanged)
- criterion all-rows-covered: **pass** (summary.rows_covered = 52, summary.rows_total = 52)
- decision: approved by QA lead (in chat; Q-1 and Q-2 go to the product owner before G3) at 2026-11-13T20:44:35Z

## G3 Verified in CI (approved)

- ci-suite-runner: `ci-suite-runner-skill/examples/runs/checkout-staging/suite_run.json` (SHA-256 f2a78e49af10..., validator exit 0: examples\runs\checkout-staging\suite_run.json: valid. checkout-regression on staging: triggered (preauth), completed failure; 2 of 3 passed)
- ci-failure-triager: `ci-failure-triager-skill/examples/triage.example.json` (SHA-256 fb36f293e921..., validator exit 0: ci-failure-triager-skill\examples\triage.example.json: valid. 4 failure(s): real 1, flaky 1, environment 1, data 1; 2 draft(s), 1 re-run(s), evidence completeness 1.0)
- criterion suite-valid: **pass** (validator exited 0 and the report is unchanged)
- criterion triage-valid: **pass** (validator exited 0 and the report is unchanged)
- criterion no-failed-tests: **fail** (results.totals.failed = 1, needs == 0)
- criterion no-real-failures: **fail** (summary.by_classification.real = 1, needs == 0)
- decision: approved with override by QA lead (in chat, 2026-11-12) at 2026-11-13T20:44:39Z; override: The failing test and the real failure are drafted as defects in the triage report; the QA lead accepts them into G4, where the release gate decides

## G4 Release ready (rejected)

- release-readiness-gate: `release-readiness-gate-skill/examples/rc-2026.11.2/gate_report.json` (SHA-256 84c9ea3cf685..., validator exit 0: release-readiness-gate-skill\examples\rc-2026.11.2\gate_report.json: valid. verdict no_go; 7 blocker(s), 1 waived, 2 missing, 1 warning(s); completeness 0.71, confidence medium)
- criterion gate-valid: **pass** (validator exited 0 and the report is unchanged)
- criterion gate-go: **fail** (headline.verdict = 'no_go' (allowed ['go']))
- decision: rejected by Release manager (release meeting, 2026-11-12) at 2026-11-13T20:44:42Z; reason: Seven blockers, including three security P0s and the stale crash report; no release until a fresh gate report shows them fixed
