# Example: two suite runs

`suites.json` catalogues `checkout-regression`, a GitHub Actions workflow with `environment` and `tags`
inputs, and `smoke`, an Azure DevOps pipeline with the same parameters. Production is protected in both.
`preauth.json` holds grant PA-1, from the QA lead: both suites on staging and UAT, until 2026-11-30.

`ci-stub/server.py` answers the GitHub and Azure DevOps endpoints the runner uses, on loopback. When a run
is triggered, it runs `pytest -m "<tags>"` on `ci-stub/suites/` for real. The run moves from queued to in
progress to completed as it is polled, and the JUnit is served as a zipped artifact. The runner talks to it
over HTTP through `--api-base`, so the live code path is exercised. `runs/` holds the two records, their
JUnit and logs, and `stub_log.json`, every request the stub received. All of it was made by `commands.sh`.

## Answer key

| Request | Expected | Got |
|---|---|---|
| checkout-regression on staging, `checkout and not slow` | covered by PA-1: triggered without asking; one run | sent (preauth, PA-1); run 7001; queued, in progress, completed |
| its tests | 3 selected (the slow and payments tests excluded); the case-insensitive discount test fails | 2 of 3 passed; `test_discount_code_is_case_insensitive` failed; handed to ci-failure-triager |
| smoke on production, with the pre-authorisation | refused: production is protected | refused, with the exact request shown; nothing sent |
| the same, with the release manager's confirmation | triggered once | sent (confirmation); run 3301; 2 of 2 smoke tests passed |
| what the CI received | exactly two triggers | `stub_log.json`: one dispatch and one pipeline run |

```bash
python scripts/validate_suite_run.py examples/runs/checkout-staging/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
python scripts/validate_suite_run.py examples/runs/smoke-production/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
python scripts/render_report.py examples/runs/checkout-staging/suite_run.json examples/runs/smoke-production/suite_run.json
```

Run these from `ci-suite-runner-skill/`. Rerunning `commands.sh` gives new timestamps and the same results.
After a rerun, the grant's expiry still has to cover the trigger date, and the agent's notes are added
again.
