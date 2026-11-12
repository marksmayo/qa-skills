---
name: post-deploy-validator
description: Check a release right after it is deployed, read-only - the deployed version is the expected one, key pages and API endpoints answer correctly (signed in with a named set of test credentials where needed), data the release depends on is sane through the API, and response times have not regressed against the skill's previous run - then recommend go, no_go or not_established for a person to decide. Sends only GET and HEAD plus the named sign-in; anything else in the plan is refused and recorded. Credentials, tokens and cookies never reach a file; response values are recorded only where the plan says so. Never decides and never calls a release healthy.
version: 1.1.0
---

# Post-deploy validator

## Mission

Minutes after a deploy, check the live environment and recommend whether the release stays:

- **Version**: the deployed version (a JSON field, a header, an HTML meta tag, or a pattern over the body
  such as `data-dpl-id="..."`) and, optionally, the deployment id, match the expected value.
- **Smoke**: key pages and endpoints answer with the right status, content, headers (security headers
  present, absent, equal to or containing a value) and no unexpected redirect.
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
6. **Gentle on production**: paced requests (`rate_per_second`), a few samples, and only for checks that
   answer as expected (a failing check is sent once). Stop at once on a 429 or a WAF/bot challenge (the
   target refused: `error`, and `not_established`); stop after three 5xx answers in a row (a 4xx between
   them doesn't reset the count) as an application outage.
7. **Never "healthy" or "safe".** The strongest wording is "no failed checks and no regressions in what was
   checked". The headline names the recommendation and never contradicts it ("ship it" over a `no_go`);
   titles keep the severity and the HTTP status the check answered.
8. **A failing response is not a timing.** A slower 500 is not a regression; its time is `not_comparable`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `plan` | required | `pvt_plan.json` (`schemas/pvt_plan.schema.json`); checked against the schema before anything is sent, unknown fields are errors |
| `expect_version` | required | The version, deployment id or commit SHA the deploy should have put live |
| `baseline` | the previous run for this target and plan | See "Choosing the baseline" |
| `decision_owner` | the release manager | Named in the report |

## Workflow

```bash
python scripts/run_pvt.py pvt_plan.json --expect-version 1.9.0 --out runs/<stamp>/ --baseline runs/<previous>/run.json
python scripts/recommend.py runs/<stamp>/run.json --decision-owner "Release manager" --out pvt_report.json
python scripts/validate_pvt.py pvt_report.json --run runs/<stamp>/run.json --baseline runs/<previous>/run.json
python scripts/render_report.py pvt_report.json [--brief]
```

1. **Plan**: check the target, `authorised_by`, the credential sets, and that every check is a read (the
   runner refuses the rest anyway). Ask before adding checks to a production plan. Name where the build
   shows itself: `version` and `deployment` each take `path` plus `json_path`, `header`, `meta` (a meta
   tag's name) or `regex` (first group, over the body). A framework that hides its version from the public
   (common on Next.js) usually still puts a deployment id in the HTML (`data-dpl-id`) or a header.
   Security headers go in `expect.headers`; a public page that may redirect states it (`no_redirect`,
   `final_path`, or `allow_login_redirect` when a redirect to sign-in is the intended behaviour).
2. **Run**: version, sign-in, checks in order, samples for timed checks, comparison with the baseline.
3. **Recommend**: findings (version mismatch, failed checks, regressions) and the recommendation.
4. **Write** the headline, finding titles and limitations (what wasn't checked: refused checks, client-side
   behaviour, the sample size); keep the findings and recommendation as computed.
5. **Validate and render**; hand the page to the decision owner.

## Choosing the baseline

The baseline is the most recent earlier `run.json` in the runs folder with the same plan SHA-256 and base
URL (the runner compares nothing otherwise). Prefer the last run of the **previous release**: each run
records the deployment id (`deployment`) or version, and `performance.baseline_relation` says what the
comparison is. `same_deployment` (or `same_version` with no deployment id) means two runs of one
deployment: the timings show drift, not a release change, so a regression is a finding but not a `no_go`
reason, and the notes say so. A first run on an environment has no baseline (`no_baseline`) and becomes the
next one.

## Rating

See `RUBRIC.md`. `no_go`: version mismatch, a critical check failed, or a critical check regressed against
another deployment. `not_established`: otherwise, a critical check errored, was blocked or skipped, the
version couldn't be read, or the target rate-limited or WAF-blocked the run. `go`: otherwise. A regression
exceeds both the tolerance (20%) and the minimum change (50 ms). A Content-Security-Policy that differs
between page checks is a minor finding.

## Output (`schemas/pvt_report.schema.json`)

`target`, `plan` (path, SHA-256), `deployed` (version, deployment id, sources, matches), `window` (requests
sent, refused, `stopped`), `credential_sets` (signed in or why not), `checks` (with `final_path`),
`performance` (baseline run and relation, comparisons), `findings`, `recommendation` (value, reasons,
notes, decision owner, empty decision), `summary`, `limitations`. `run.json` keeps every request (method,
path, status, time; no bodies), each check's redirect hops, its safe headers (content type, cache and CDN
headers such as `age`, `x-vercel-cache`, `cf-cache-status`, and security headers with CSP nonces
redacted; never cookies or authorization), timing samples with their cache status, and each assertion's
result with its `kind` (`http_status`, `body`, `json`, `header`, `redirect`).

## Edge cases

- **Canary or blue/green**: the plan targets the new pool (host or header); a response with the old version
  header is an `error` for that check.
- **WAF or rate limiting** (429, or 403/503 with a challenge page, `cf-mitigated`, `x-vercel-mitigated` or
  Retry-After): the run stops, the check is `error`, and the recommendation is at best `not_established`.
  Three 500s are different: the application is down, and the failed checks are the findings.
- **Redirect to sign-in**: a public check that lands on a sign-in page fails unless it allows it; a redirect
  off the site is never followed.
- **CDN caches**: a page can pass from the edge cache rather than the new deployment; `age`,
  `x-vercel-cache` and `cf-cache-status` are recorded per sample so a reader can tell.
- **A check that must write**: refused; suggest a read-only check or a synthetic monitor owned by the team.
- **First run on an environment**: performance is `no_baseline`; this run becomes the next baseline.
