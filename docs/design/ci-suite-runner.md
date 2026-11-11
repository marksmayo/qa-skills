# Design: `ci-suite-runner-skill`

Status: Built (v1.0.0); see "As built" at the end. Build order: 31.

## Mission

The skill runs a named test suite in CI, on a chosen environment and with chosen test tags. It triggers
the pipeline, polls it to completion, and collects the results and artifacts into one report:

- **trigger**: a GitHub Actions `workflow_dispatch` or an Azure DevOps pipeline run, with the
  environment, tags and ref mapped to the pipeline's inputs by a suite catalogue;
- **poll** with backoff until the run completes or the time limit passes;
- **collect**: job conclusions, JUnit results (totals and failed tests), and the artifacts listed and
  downloaded if asked; failed jobs' logs are fetched so the CI failure triager can take over.

How it differs from the other skills:

- **ci-failure-triager** (26) reads a finished failed run and classifies its failures. This skill starts
  runs; a failed run's report is the triager's input.
- **test-impact-selector** picks which tests to run; its selection can be the tags passed here.
- **flaky-test-hunter** reruns tests locally under variations. This skill runs a suite once in CI.

## Decisions

Agreed with the user (see `decisions-26-32.md`):

- **GitHub Actions and Azure DevOps**, as for the triager.
- **Each trigger is confirmed unless pre-authorised.** A pre-authorisation file names the suites and
  environments it covers, who granted it, and when it expires. Without one that covers the request, the
  skill shows the exact request it would send and stops. Protected environments (production by default)
  always need confirmation for the specific run; a pre-authorisation never covers them.
- **It polls to completion**, then collects the results and artifacts.

## Non-negotiable rules

- **No trigger without authority**: a covering, unexpired pre-authorisation, or a confirmation for this
  request (who, when, how), recorded in the report. A protected environment needs the confirmation.
- **One trigger per request**: before triggering, look for a run of the same suite, environment and ref
  already in progress that the skill started; reuse it rather than start a second. Never re-trigger on a
  timeout; report `timed_out` with the run link.
- **Only the catalogue's pipelines and inputs**: a suite, environment or tag outside the catalogue's
  allowed values is refused.
- **Results come from the run**: totals and failures from the JUnit artifacts, conclusions from the CI
  API. A run without JUnit is `results: not_collected`, never "passed".
- **Secrets never output**: tokens come from the environment (`GITHUB_TOKEN`/`GH_TOKEN`,
  `AZURE_DEVOPS_EXT_PAT`) and are never written; inputs marked secret in the catalogue are redacted.
- **No "all green"** beyond what the results show: "N of M tests passed in run R".

## Inputs

| Input | Default | Notes |
|---|---|---|
| `suite` | required | A name in `suites.json` (the catalogue: system, pipeline, input names, allowed environments and tags, protected environments) |
| `environment` | required | One of the suite's allowed environments |
| `tags` | the suite's default | A tag expression passed to the pipeline's tags input |
| `ref` | the default branch | Branch, tag or commit |
| `preauth` | none | `preauth.json`: scopes (suites, environments), granted_by, expires |
| `confirmed_by` | none | Who confirmed this request, and how (chat, ticket) |
| `timeout_minutes`, `poll_seconds` | 60, 20 (backoff to 120) | |
| `download` | JUnit only | Artifact names or patterns to download |

## Workflow

```bash
python scripts/run_suite.py plan --catalogue suites.json --suite checkout-regression --environment staging --tags "checkout and not slow" --out run/
python scripts/run_suite.py trigger --run run/ [--preauth preauth.json | --confirmed-by "who, how"]
python scripts/run_suite.py poll --run run/
python scripts/run_suite.py collect --run run/ [--download "reports-*"]
python scripts/validate_suite_run.py run/suite_run.json [--preauth preauth.json]
python scripts/render_report.py run/suite_run.json [--brief]
```

1. **Plan**: resolve the suite in the catalogue, check the environment and tags, build the exact request
   (method, URL, body), and decide whether it is authorised.
2. **Confirm**: if not authorised, show the request and ask; record the answer as `confirmed_by`.
3. **Trigger** once; find the run it created (GitHub dispatches return no id: a unique `run_label` input,
   when the workflow accepts one, or the newest dispatch run on the ref created after the trigger).
4. **Poll** with backoff; record each status change.
5. **Collect** conclusions, JUnit (shared `parse_results.py`), artifacts and failed jobs' logs.
6. **Report**; hand failures to the CI failure triager.

## Output (`suite_run.schema.json`)

```text
request{suite, system, pipeline, environment, tags, ref, inputs}
authorisation{kind: preauth|confirmation|none, by, at, scope?, expires?}
trigger{sent: bool, method, url, body (secrets redacted), status, at, refused_reason?}
run{id, url, attempt}
polling[{at, status}], outcome{status: completed|timed_out|not_started, conclusion}
results{totals{tests, passed, failed, errors, skipped}, failed_tests[], junit[]}
artifacts[{name, size, downloaded_to?}], logs[{job, path}]
next_steps[], limitations[]
```

## Validator fails when

- A trigger was sent without authority, with an expired or non-covering pre-authorisation, or to a
  protected environment without a confirmation.
- The request uses a suite, environment, tag or input outside the catalogue.
- More than one trigger was sent for one request.
- The outcome or results differ from the polled status and the JUnit; results are claimed without JUnit.
- A token or a secret input value appears in the report.
- Text says "all green" or "everything passed" beyond the totals.

## Edge cases

- **Workflows without `workflow_dispatch`** or pipelines without parameters: refused, with what to add.
- **Concurrency groups** that cancel older runs: the cancellation is recorded, not retried.
- **Approval gates in the pipeline** (environment protection rules): the run waits; the poll records
  `waiting` and the report says who must approve. The skill never approves.
- **Runs longer than the timeout**: `timed_out`, with the link; a later `poll` resumes.

## Files

```text
ci-suite-runner-skill/
  SKILL.md  RUBRIC.md  README.md
  schemas/suites.schema.json  preauth.schema.json  suite_run.schema.json
  scripts/run_suite.py  validate_suite_run.py  render_report.py  parse_results.py (shared)
  examples/
```

## Example

`examples/ci-stub/server.py` is a loopback stand-in for both CI APIs. It accepts a GitHub
`workflow_dispatch` and an Azure DevOps pipeline run, runs the requested tests for real with pytest (the
tags become a `-m` expression), moves the run through queued, in progress and completed as it is polled,
and serves the JUnit as an artifact. The skill's live code path talks to it through `--api-base`. There
are two runs:

- `checkout-regression` on staging with tags `checkout and not slow`, pre-authorised by the QA lead:
  triggered, polled to completion, one test fails;
- `smoke` on production (protected) through Azure DevOps: first refused (the pre-authorisation doesn't
  cover production), then triggered with the release manager's confirmation, and passes.

## Resolved questions

Agreed with the user before the build (`decisions-26-32.md`).

1. **CI systems?** GitHub Actions and Azure DevOps.
2. **Confirmation?** Each trigger, unless pre-authorised.
3. **Collection?** Poll to completion, then collect results and artifacts.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **The example exercises the live code path** against a loopback stub of both APIs
  (`examples/ci-stub/server.py`) through `--api-base`. The stub runs the requested tests for real with
  pytest and logs every request it receives, so the validator can check (`--stub-log`) that the CI got
  exactly the triggers the records say were sent. No real CI was called.
- **Finding a GitHub run after a dispatch** uses the newest `workflow_dispatch` run on the ref, created at or
  after the trigger, for the catalogued workflow. The optional `run_label` input from the design is not
  built.
- **Every authorisation decision is kept** in `authorisations` (refused, then sent), so a record shows what
  was asked, the refusal and the confirmation that followed.
- **Approval-gate waiting** (`waiting`) and artifact patterns beyond JUnit work through `--download`, but
  the example doesn't have a gated pipeline.
- **Example results:** `checkout-regression` on staging ran under the QA lead's grant: 2 of 3 passed, one
  failure handed to the triager. `smoke` on production was refused under the grant, then ran with the
  release manager's confirmation: 2 of 2 passed. The stub received exactly two triggers.
- **Testing:** 19 of 19 seeded violations are caught, including an expired or non-covering grant, a
  production run under a grant, a second trigger, a refused request that still reached the CI, edited
  totals, and a token in the record.
