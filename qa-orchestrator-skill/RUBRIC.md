# Rubric: QA orchestrator

## Gate statuses

| Status | Means |
|---|---|
| `pending` | not started |
| `in_progress` | the current gate; steps being recorded |
| `criteria_met` | evaluated; every criterion passed; waiting for the approver |
| `criteria_failed` | evaluated; a criterion failed; the approver may reject, or approve with an override |
| `approved` | decided by a person (or auto-approved, if the config allows it); the next gate starts |
| `rejected` | decided by a person, with a reason; the run stops until the gate is reopened |
| `skipped` | not run in this orchestration, with a reason (started later with `--start-gate`) |

## Criteria

| Kind | Passes when |
|---|---|
| `report_valid` | the step's report is recorded, unchanged since, and its validator exited 0 |
| `field_in` | the value at a path in the step's report is one of the allowed values |
| `field_compare` | the value at a path compares as configured with a number (`==`, `!=`, `<=`, `>=`, `<`, `>`) |
| `field_equals_field` | the values at two paths are equal |

Paths are dotted, with `[n]` indexes, `[field=value]` filters (`reviews[ticket.key=SHOP-201].verdict.value`) and `.length` of a list (`drafts.length`).

## Decisions

| Decision | Needs |
|---|---|
| `approved` | evaluated criteria, all passing; who and how |
| `approved_with_override` | evaluated criteria with failures; who, how, and an override reason (20+ characters); the failed criteria are recorded |
| `auto_approved` | the gate's `auto_approve` on in the config; all criteria passing |
| `rejected` | who and a reason (20+ characters) |
| `skipped` | a reason |
