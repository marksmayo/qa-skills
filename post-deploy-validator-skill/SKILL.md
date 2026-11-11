---
name: post-deploy-validator
description: Check a release right after it is deployed, read-only - the deployed version is the expected one, key pages and API endpoints answer correctly (signed in with a named set of test credentials where needed), data the release depends on is sane through the API, and response times have not regressed against the skill's previous run - then recommend go, no_go or not_established for a person to decide. Sends only GET and HEAD plus the named sign-in; anything else in the plan is refused and recorded. Credentials, tokens and cookies never reach a file; response values are recorded only where the plan says so. Never decides and never calls a release healthy.
version: 1.0.0
---

# Post-deploy validator

## Mission

Minutes after a deploy, check the live environment and recommend whether the release stays:

- **Version**: the deployed version (endpoint, header or meta tag) is the expected one.
- **Smoke**: key pages and endpoints answer with the right status and content.
- **Signed-in flows**: with a named credential set (for example `smoke-reader`), from the environment.
- **Data**: through the API only: counts, ranges, uniqueness, relationships, freshness.
- **Performance**: each timed check's median against the same check in the skill's previous run.

The skill recommends `go`, `no_go` or `not_established`; a person decides. performance-budget-auditor
measures depth in a lab; api-contract-tester checks contracts on test environments; release-readiness-gate
decides before the deploy; hypercare-anomaly-watcher watches the days after.

## Non-negotiable rules

1. **Read-only.** GET and HEAD, plus the sign-in request of a named credential set. Any other method in
   the plan is refused before it is sent and recorded as `blocked`. Never create, change or delete.
2. **Production needs `authorised_by`** in the plan: who allowed this run and this credential set.
3. **Secrets stay in memory.** Credential values come from the environment variables the plan names;
   they, tokens, Authorization, Cookie and Set-Cookie values are never printed or written. A response value
   is recorded only for an assertion marked `record: true` (personal data such as an email never is).
4. **Missing evidence is not a pass.** An errored, blocked or skipped critical check, a failed sign-in or
   an unreadable version is `not_established`, never `go`. No baseline: performance is `no_baseline`.
5. **The skill recommends; a person decides.** `decision` and `decided_by` stay empty for the named
   decision owner.
6. **Gentle on production**: paced requests (`rate_per_second`), a few samples, and stop after three
   checks in a row answer 5xx.
7. **Never "healthy" or "safe".** The strongest wording is "no failed checks and no regressions in what was
   checked".

## Inputs

| Input | Default | Notes |
|---|---|---|
| `plan` | required | `pvt_plan.json` (`schemas/pvt_plan.schema.json`) |
| `expect_version` | required | The version the deploy should have put live |
| `baseline` | the previous run for this target and plan | Same plan SHA-256 and base URL, or it isn't compared |
| `decision_owner` | the release manager | Named in the report |

## Workflow

```bash
python scripts/run_pvt.py pvt_plan.json --expect-version 1.9.0 --out runs/<stamp>/ --baseline runs/<previous>/run.json
python scripts/recommend.py runs/<stamp>/run.json --decision-owner "Release manager" --out pvt_report.json
python scripts/validate_pvt.py pvt_report.json --run runs/<stamp>/run.json --baseline runs/<previous>/run.json
python scripts/render_report.py pvt_report.json [--brief]
```

1. **Plan**: check the target, `authorised_by`, the credential sets, and that every check is a read (the
   runner refuses the rest anyway). Ask before adding checks to a production plan.
2. **Run**: version, sign-in, checks in order, samples for timed checks, comparison with the baseline.
3. **Recommend**: findings (version mismatch, failed checks, regressions) and the recommendation.
4. **Write** the headline, finding titles and limitations (what wasn't checked: refused checks, client-side
   behaviour, the sample size); keep the findings and recommendation as computed.
5. **Validate and render**; hand the page to the decision owner.

## Rating

See `RUBRIC.md`. `no_go`: version mismatch, a critical check failed, or a critical check regressed.
`not_established`: otherwise, a critical check errored, was blocked or skipped, or the version couldn't be
read. `go`: otherwise. A regression exceeds both the tolerance (20%) and the minimum change (50 ms).

## Output (`schemas/pvt_report.schema.json`)

`target`, `plan` (path, SHA-256), `deployed`, `window` (requests sent, refused), `credential_sets` (signed in
or why not), `checks`, `performance` (baseline run, comparisons), `findings`, `recommendation` (value,
reasons, notes, decision owner, empty decision), `summary`, `limitations`. `run.json` keeps every request
(method, path, status, time; no headers or bodies) and each assertion's result.

## Edge cases

- **Canary or blue/green**: the plan targets the new pool (host or header); a response with the old version
  header is an `error` for that check.
- **WAF or rate limiting**: stop and report `not_established`.
- **A check that must write**: refused; suggest a read-only check or a synthetic monitor owned by the team.
- **First run on an environment**: performance is `no_baseline`; this run becomes the next baseline.
