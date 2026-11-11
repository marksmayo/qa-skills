---
name: ci-suite-runner
description: Run a catalogued test suite in CI (GitHub Actions workflow_dispatch or an Azure DevOps pipeline run) on a chosen environment with chosen test tags, then poll it to completion and collect the results and artifacts - JUnit totals and failed tests, artifacts, failed jobs' logs - into one record. Every trigger is confirmed unless a pre-authorisation covers that suite and environment and hasn't expired; protected environments such as production always need a confirmation. Triggers once per request, never re-triggers on a timeout, accepts only the catalogue's suites, environments and tags, and hands failures to the CI failure triager.
version: 1.0.0
---

# CI suite runner

## Mission

Start a named suite in CI, on an environment, with a tag expression, and bring back what happened:
the run link, the status as it changed, the conclusion, the JUnit totals and failed tests, the artifacts,
and failed jobs' logs. ci-failure-triager takes a failed run from here; test-impact-selector can choose the
tags.

## Non-negotiable rules

1. **No trigger without authority.** Either a grant in `preauth.json` that names the suite and the
   environment and hasn't expired, or a confirmation of this exact request (who, how, when) passed as
   `--confirmed-by`. Without one, show the exact request and stop. Never treat silence as yes.
2. **Protected environments** (production by default) always need a confirmation for the run; a
   pre-authorisation never covers them, even if a grant lists them.
3. **Only the catalogue**: the suite, environment and tag markers must be in `suites.json`. Anything else is
   refused.
4. **Trigger once.** A record that was triggered is never triggered again; a timeout is `timed_out` with the
   link, and `poll` resumes later.
5. **Results come from JUnit**: no JUnit artifact is `not_collected`, never "passed"; the totals are
   counted from the files.
6. **Tokens stay out of files** (`GITHUB_TOKEN`/`GH_TOKEN`, `AZURE_DEVOPS_EXT_PAT`); secret inputs are
   redacted.
7. **No "all green"**: say "N of M tests passed in run R".
8. **Never approve** a pipeline's environment gate or a deployment on someone's behalf.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `catalogue` | `suites.json` | `schemas/suites.schema.json`: per suite the system, pipeline, input names, environments, protected environments, markers, JUnit artifact names |
| `suite`, `environment` | required | |
| `tags` | the suite's default | Markers with and/or/not and parentheses |
| `ref` | the suite's default | |
| `preauth` | none | `schemas/preauth.schema.json` |
| `confirmed_by` | none | From the user, for this request |

## Workflow

```bash
python scripts/run_suite.py plan --catalogue suites.json --suite checkout-regression --environment staging --tags "checkout and not slow" --out run/
python scripts/run_suite.py trigger --run run/ --preauth preauth.json          # or --confirmed-by "who, how, when"
python scripts/run_suite.py poll --run run/
python scripts/run_suite.py collect --run run/
python scripts/validate_suite_run.py run/suite_run.json --preauth preauth.json
python scripts/render_report.py run/suite_run.json
```

1. **Plan** and show the user the request when no grant covers it; ask for the confirmation, and pass their
   words as `--confirmed-by`.
2. **Trigger**, **poll** (backoff, timeout), **collect**.
3. **Write** the headline (totals and the conclusion), the next steps (failed tests to ci-failure-triager;
   what the tags excluded) and the limitations; **validate** and **render**.

## Rating

See `RUBRIC.md`: authority, outcomes and results.

## Output (`schemas/suite_run.schema.json`)

`request`, `plan` (the exact request), `authorisations` (each decision: refused with why, or sent with the
grant or the confirmation), `trigger`, `run`, `polling`, `outcome`, `results`, `artifacts`, `logs`, `jobs`,
`summary`, `next_steps`, `limitations`.

## Edge cases

- **No `workflow_dispatch`** on the workflow, or no parameters on the pipeline: refused; say what to add.
- **Approval gates**: the run waits; the record says so and who must approve.
- **Concurrency cancellations**: recorded as the conclusion; not retried.
- **A dispatch with no new run within 20 seconds**: `not_started`, with the reason; check the workflow's
  triggers and the ref.
