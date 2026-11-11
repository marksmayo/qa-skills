---
name: qa-orchestrator
description: Take a change through the QA gates - Ready, Designed, Built and tested, Verified in CI, Release ready, Deployed, Hypercare (configurable) - by running the right skill at each step, recording each report with its SHA-256 and its own validator's result, evaluating the gate's exit criteria, and asking the named person to approve, approve with an override, or reject before moving on. Run state lives in a temporary folder; a script keeps the state and refuses an approval without evaluated criteria, a person, or (when a criterion failed) an override reason. Never auto-approves unless the configuration says so, never overrides a step skill's own rules, and never calls QA complete.
version: 1.0.0
---

# QA orchestrator

## Mission

Sequence the QA skills across a change's gates and keep a person in the loop:

| Gate | Steps (default) | Exit criteria (default) |
|---|---|---|
| G1 Ready | ticket-readiness-reviewer | review valid; verdict ready or ready with questions |
| G2 Designed | test-case-designer (risk-based-test-planner optional) | cases valid; every design row covered |
| G3 Built and tested | ticket-to-tdd, code-security-auditor | reports valid; no P0 security finding |
| G4 Verified in CI | ci-suite-runner (ci-failure-triager optional) | run valid; no failed tests |
| G5 Release ready | release-readiness-gate | gate report valid; verdict go |
| G6 Deployed | post-deploy-validator | report valid; recommendation go |
| G7 Hypercare | hypercare-anomaly-watcher | watch valid; no incident drafts |

The gates, steps, criteria and approvers are in `data/orchestrator.json` and can be changed per team.

## Non-negotiable rules

1. **A person approves each gate** (who and how), unless the config turns on `auto_approve` for that gate;
   an auto-approval is recorded as such. Off by default everywhere.
2. **Criteria before approval.** Evaluate first. A failed criterion needs an override reason to approve;
   a rejection needs a reason and stops the run until someone reopens the gate.
3. **In order.** A gate starts only after the previous one is approved or skipped (with a reason).
4. **Reports are evidence**: a step counts only with its report recorded (SHA-256) and its validator
   exiting 0; a report changed afterwards must be recorded again.
5. **Step skills keep their own rules.** Run each step by its skill's `SKILL.md`: the CI suite runner still
   needs its trigger confirmation, the post-deploy validator stays read-only, the release gate never calls a
   release safe. A gate approval is not any of those confirmations.
6. **State in a temporary folder** (`<temp>/qa-orchestrator/<run id>/`), not the repository; export a summary
   only when asked.
7. **No "QA complete" or "approved by QA"**: the summary lists each gate's decision, who made it, and on what
   evidence.

## Workflow

```bash
python scripts/orchestrate.py init --subject "SHOP-310 for release 2026.12" [--config orchestrator.json]
python scripts/orchestrate.py next --state <dir>
#   run the step's skill (its SKILL.md), then:
python scripts/orchestrate.py record --state <dir> --gate G1 --step readiness --report review.json
python scripts/orchestrate.py evaluate --state <dir> --gate G1
#   ask the gate's approver; record their words:
python scripts/orchestrate.py approve --state <dir> --gate G1 --by "QA lead" --how "in chat" [--override "why"]
python scripts/orchestrate.py reject --state <dir> --gate G5 --by "Release manager" --how "release meeting" --reason "..."
python scripts/validate_run.py <dir>
python scripts/render_report.py <dir> [--markdown summary.md]
```

1. **Init**: the subject (ticket, release, repository, commit) and the config; start later with
   `--start-gate` and `--skip-reason` when earlier gates were done elsewhere.
2. **For each gate**: `next`; run each step's skill; `record` its report; `evaluate`; show the person the
   criteria results and the reports' headlines; record their decision exactly as given. Never decide for
   them, and never treat silence or a general "carry on" as an approval of a gate with failed criteria.
3. **On rejection**: stop; say what would reopen the gate (a fresh report, a fix).
4. **Summarise** when asked or at the end.

## Rating

See `RUBRIC.md`: gate statuses, criteria kinds, decisions.

## Output

`<state>/state.json` (`schemas/run_state.schema.json`): subject, config and its hash, gates (status, steps
with report, hash and validator result, criteria results, decision, history) and the event log.

## Edge cases

- **A step's report comes from CI** or another person: record it the same way; its validator still runs.
- **A validator needs its skill's folder**: set the step's `cwd` in the config.
- **The person approves a gate with failed criteria**: ask for the reason and pass it as `--override`.
- **Reopening**: `reopen` with who and why; the earlier decision stays in the gate's history.
