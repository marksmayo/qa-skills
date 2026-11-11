# Example: one change through four gates

`orchestrator.example.json` is a six-gate configuration: Ready, Designed, Verified in CI, Release ready,
Deployed and Hypercare, with each validator command's arguments for the example reports. The reports are
other skills' examples, standing in for one change's evidence. The readiness review covers SHOP-201 and
the test cases cover SHOP-310; the CI run, the triage and the release gate are from their own samples.
`commands.sh` runs the orchestration from the repository root. Because the state goes to `state/` here
(`--state-dir`), the example can be committed; a real run uses a temporary folder. The approvers are the
roles named in the config.

## Answer key

| Gate | Evidence | Criteria | Decision |
|---|---|---|---|
| G1 Ready | the readiness review (validator VALID) | review valid; SHOP-201 `ready` | approved by the QA lead |
| G2 Designed | the test cases (42 cases, 52 of 52 rows) | cases valid; rows covered equals rows total | approved, with the two open questions noted |
| G3 Verified in CI | the checkout run (2 of 3 passed) and its triage (1 real failure) | both valid; **no failed tests: fail**; **no real failures: fail** | a plain approval is refused; approved with an override that names why |
| G4 Release ready | the release gate report (`no_go`, 7 blockers) | report valid; **verdict go: fail** | rejected by the release manager, with the reason; the run stops |
| G5, G6 | - | - | pending: never started |

```bash
python qa-orchestrator-skill/scripts/validate_run.py qa-orchestrator-skill/examples/state --rerun
python qa-orchestrator-skill/scripts/render_report.py qa-orchestrator-skill/examples/state
```

Run these from the repository root. The run records the SHA-256 of each report, so editing any of those
example reports makes the validator report it as changed until `commands.sh` is rerun.
