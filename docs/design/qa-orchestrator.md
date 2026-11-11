# Design: `qa-orchestrator-skill`

Status: Built (v1.0.0); see "As built" at the end. Build order: 32.

## Mission

The skill chains the other QA skills across the gates a change goes through, from a ticket being ready to
the days after its release, and keeps a person in the loop at every gate:

```text
G1 Ready ──► G2 Designed ──► G3 Built and tested ──► G4 Verified in CI ──► G5 Release ready ──► G6 Deployed ──► G7 Hypercare
readiness    test cases,      ticket-to-tdd,          ci-suite-runner,       release-readiness    post-deploy     hypercare
reviewer     risk plan,       journey tests,          ci-failure-triager,    gate                 validator       watcher
             charters         security, crash,        flaky hunter
                              wcag ...
```

At each gate it says which skill runs next and with what. It records each report the agent produces, runs
that report's own validator, and checks the gate's exit criteria against the reports. Then it asks a person
to approve, approve with an override, or reject. It never moves past a gate a person hasn't approved.

How it differs from the other skills:

- **release-readiness-gate** is one gate's decision (G5), from reports. The orchestrator runs all the
  gates, and the release gate is one of its steps.
- Every other skill is a step. The orchestrator doesn't redo their work. It sequences them, checks their
  outputs and records the human decisions between them.

## Decisions

Agreed with the user (see `decisions-26-32.md`):

- **All gates, configurable**: the gates, their steps (skill, inputs, report, validator command), their
  exit criteria and their approver roles come from `orchestrator.json`; the skill ships a default with the
  seven gates above.
- **A person approves at each gate, initially.** `auto_approve` exists per gate but is off by default;
  turning it on is a configuration change a person makes, and an auto-approval is recorded as such.
- **Run state is kept in a temporary folder** (`<temp>/qa-orchestrator/<run id>/`), not in the repository;
  the summary can be exported when asked.
- **Instructions plus a script**: `SKILL.md` tells the agent how to run each step's skill; `orchestrate.py`
  keeps the state, runs validators, evaluates criteria and records approvals.

## Non-negotiable rules

- **No gate without a person** (unless that gate's `auto_approve` is configured on): approval names who and
  how (chat, ticket, meeting), and is recorded with the time.
- **Criteria first**: a gate can be approved only after its criteria are evaluated. When a criterion
  failed, an approval needs an override reason, recorded beside the failed criteria. A rejection needs a
  reason.
- **In order**: a gate starts only when the previous gate is approved (or skipped as configured, with a
  reason). A rejected gate stops the run until a person reopens it.
- **Reports are evidence, not claims**: a step counts only with a report file, its SHA-256 and its
  validator's exit code 0. A report changed after it was recorded is stale.
- **The orchestrator never weakens a step**: each skill's own non-negotiable rules apply inside its step
  (for example the CI suite runner's trigger confirmation and the post-deploy validator's read-only
  production rule). An orchestrator approval is not that confirmation.
- **No "QA complete" or "release approved by QA"** language: the summary lists each gate's decision, who
  made it, and on what evidence.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `config` | the shipped `orchestrator.json` | gates, steps, criteria, approver roles, `auto_approve` |
| `subject` | required | What is being taken through the gates: ticket key, release, repository, commit |
| `state_dir` | `<temp>/qa-orchestrator/<run id>/` | |
| `start_gate` | G1 | A run can start later (for example at G4 for a change already designed) with the earlier gates skipped and the reason recorded |

## Workflow

```bash
python scripts/orchestrate.py init --config orchestrator.json --subject "SHOP-310 for release 2026.12" [--state-dir <dir>]
python scripts/orchestrate.py next --state <dir>                                   # what to run now
python scripts/orchestrate.py record --state <dir> --gate G2 --step cases --report test_cases.json
python scripts/orchestrate.py evaluate --state <dir> --gate G2
python scripts/orchestrate.py approve --state <dir> --gate G2 --by "QA lead, in chat" [--override "why"]
python scripts/orchestrate.py reject --state <dir> --gate G4 --by "Release manager, in the release meeting" --reason "..."
python scripts/validate_run.py <dir>
python scripts/render_report.py <dir> [--markdown summary.md]
```

1. **Init** the run with its subject and config; the state goes to a temporary folder.
2. **Next**: the current gate, the steps still to run, each with its skill and the suggested command.
3. **Run the step's skill** by its own `SKILL.md`, then **record** its report: the hash and the validator
   result.
4. **Evaluate** the gate's criteria.
5. **Ask the person** named by the gate's approver role; record approve, override or reject.
6. **Advance** to the next gate; stop on a rejection; summarise at the end or when asked.

## Exit criteria kinds

| Kind | Passes when |
|---|---|
| `report_valid` | the step's report is recorded and its validator exited 0 |
| `field_in` | a JSON path in the step's report has one of the allowed values (a verdict) |
| `field_compare` | a JSON path compared with a number (`==`, `<=`, `>=`), for example failed tests `== 0` |
| `field_equals_field` | two paths are equal (rows covered equals rows total) |

## Output (state directory)

```text
state.json    config, subject, gates[{id, name, status, steps[{id, skill, report, sha256, validator{command, exit_code, output}}],
              criteria[{id, kind, result, detail}], decision{kind: approved|approved_with_override|rejected|auto_approved,
              by, how, at, reason?}}], events[]
```

Statuses: `pending`, `in_progress`, `criteria_met`, `criteria_failed`, `approved`, `rejected`,
`skipped`.

## Validator fails when

- A gate was approved before its criteria were evaluated, or with a failed criterion and no override
  reason; a rejection has no reason; an approval has no person; an auto-approval on a gate whose config
  doesn't allow it.
- A gate started before the previous one was approved or skipped.
- A recorded report's hash no longer matches, or its validator didn't exit 0 while the step counts.
- A criterion's result differs from re-evaluating it on the recorded report.
- Text claims QA is complete or the release is approved by QA.

## Edge cases

- **A step that needs another confirmation** (a CI trigger, a production check): the step's own skill
  asks; the orchestrator records the step only when its report exists.
- **A report produced outside the run** (by CI): record it like any other; its validator still runs.
- **Reopening a rejected gate**: `reopen` with who and why; the earlier decision stays in the history.
- **Several subjects in one release**: one run per subject; G5 to G7 can share a release run.

## Files

```text
qa-orchestrator-skill/
  SKILL.md  RUBRIC.md  README.md
  data/orchestrator.json          # the default seven gates
  schemas/orchestrator.schema.json  run_state.schema.json
  scripts/orchestrate.py  validate_run.py  render_report.py
  examples/
```

## Example

A run for one change, through four gates of a shortened configuration. The reports are other skills'
examples, standing in for one change's evidence:

- G1 Ready: the readiness reviewer's review (criteria met), approved by the QA lead;
- G2 Designed: the test case designer's cases (all rows covered, 2 open questions), approved;
- G3 Verified in CI: the CI suite runner's checkout run (one failing test) and the CI failure triager's
  triage. The criteria fail, and the QA lead approves with an override naming the reason;
- G4 Release ready: the release readiness gate's `no_go`. The criteria fail, the release manager rejects,
  and the run stops there.

## Resolved questions

Agreed with the user before the build (`decisions-26-32.md`).

1. **Which gates?** All, configurable.
2. **Human in the loop?** A person at each gate, initially.
3. **Where is run state kept?** A temporary folder.
4. **Form?** Instructions plus a script.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **A step can set its validator's working directory** (`cwd`), because some skills' validators resolve
  their inputs relative to the skill folder (the CI suite runner's records name its catalogue that way).
  `{report}` and `{skills}` are then given relative to it.
- **Criteria paths** take `[n]`, `[field=value]` filters and `.length`.
- **The script refuses rather than warns**: approving before evaluating, approving failed criteria without an
  override reason, recording or evaluating a gate that isn't current, and rejecting without a reason all
  exit non-zero and leave the state unchanged.
- **The example's state is committed** (`--state-dir examples/state`), so it can be validated; a real run
  defaults to `<temp>/qa-orchestrator/<run id>/`.
- **Example results:**
  - G1 and G2 were approved.
  - G3 was approved with an override: one failing test, one real failure.
  - G4 was rejected on the release gate's `no_go`, which stopped the run.
  - G5 and G6 never started.
- **Testing:** 16 of 16 seeded violations are caught. They include an approval without evaluated criteria
  or without an override, an auto-approval that the config doesn't allow, a gate started out of order, an
  edited report hash or criterion, and "QA complete" language. Six refusals of the script itself were also
  checked.
