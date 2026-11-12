# Design: `post-deploy-validator-skill`

Status: Built (v1.1.0); see "As built" at the end. Build order: 28.

## Mission

Right after a deploy, the skill checks the live environment, read-only, and recommends go or no-go:

- **the deployed version** is the one expected (a version endpoint, a header or a meta tag);
- **smoke checks**: key pages and API endpoints answer with the right status and content, signed in
  where needed, with a named set of test credentials;
- **data checks** through the API: the data the release depends on is there and sane (counts, ranges,
  relationships, freshness);
- **performance against the previous run**: each check's response time compared with the same check in
  the skill's last run on that environment.

The skill recommends; a person decides. The report has a place for the decision and who made it, and the
skill never fills it.

How it differs from the other skills:

- **performance-budget-auditor** measures lab performance against budgets on a test site, in depth. This
  skill takes a few timings in production against its own last run, to catch a regression the deploy
  introduced.
- **api-contract-tester** checks an API against its contract with generated requests, on a test
  environment. This skill sends only the reads the plan lists, in production.
- **release-readiness-gate** decides before the deploy from reports. This skill checks after it.
- **hypercare-anomaly-watcher** (29) watches metrics for days after the release. This skill is the
  minutes-after check.

## Decisions

Agreed with the user (see `decisions-26-32.md`):

- **Read-only in production.** Only GET and HEAD requests, plus the sign-in request the plan names for a
  credential set. Any other method in the plan is refused and recorded as `blocked`; nothing is created,
  changed or deleted.
- **Authenticated flows use a named set of credentials**, such as `smoke-reader`, read from environment
  variables the plan names. The values are never printed, logged or written; tokens and cookies are
  redacted from the evidence.
- **Data checks go through the API**, never the database.
- **The performance baseline is the skill's previous run** on the same environment and plan. A first run
  establishes the baseline and says so.
- **The skill recommends `go`, `no_go` or `not_established`; a person decides.**

## Non-negotiable rules

- **Production targets need `authorised_by`** in the plan, naming who allowed the run and the credential set.
- **Never a write.** The request guard allows GET, HEAD and the named sign-in; everything else is refused
  before it is sent.
- **Secrets never leave memory**: credentials, tokens, cookies and set-cookie headers are redacted; a
  response value is recorded only when the plan marks its path `record: true` (otherwise the result
  records only whether the check passed, the type and length).
- **Missing evidence is not a pass.** A check that errored, was blocked or couldn't sign in is
  `not_established` for its part, never `pass`; a missing baseline makes the performance part
  `not_established`.
- **No "the release is healthy".** The strongest wording is "no failed checks and no regressions in what
  was checked".
- **Gentle**: a request rate limit (default 2 per second), a few samples per timing, and stop on repeated
  5xx from the same host.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `plan` | required | `pvt_plan.json`: target, expected version, credential sets, checks, performance settings |
| `baseline` | the previous run for this target and plan | Found in the runs folder by target and plan hash, or given |
| `samples` | 5 per timed check | |
| `tolerance` | 20% and at least 50 ms | A regression must exceed both |
| `rate_per_second` | 2 | |

## Workflow

```bash
python scripts/run_pvt.py pvt_plan.json --out runs/<timestamp>/ [--baseline runs/<previous>/run.json]
python scripts/recommend.py runs/<timestamp>/run.json --out pvt_report.json
python scripts/validate_pvt.py pvt_report.json --run runs/<timestamp>/run.json [--baseline runs/<previous>/run.json]
python scripts/render_report.py pvt_report.json [--brief]
```

1. **Check the plan**: production needs `authorised_by`; every check's method is allowed.
2. **Version**: read the deployed version; compare with the expected one.
3. **Sign in** for each credential set the checks need (credentials from the environment).
4. **Run the checks** in plan order through the request guard: status, content, JSON assertions; timed
   checks take several samples.
5. **Compare timings** with the baseline run (median against median).
6. **Recommend**: findings for failed checks, a version mismatch and regressions; the recommendation with
   its reasons; an empty decision for a person.

## Rating

| Recommendation | When |
|---|---|
| `no_go` | the version doesn't match, a `critical` check failed, or a `critical` check regressed beyond tolerance |
| `not_established` | no `no_go` reason, but a critical check errored, was blocked or couldn't sign in, or the version couldn't be read |
| `go` | otherwise: no failed checks and no regressions in what was checked |

Finding severity: a failed critical check or a version mismatch is `critical`; a failed non-critical check
`major`; a regression on a critical check `major`, otherwise `minor`.

## Output (`pvt_report.schema.json`)

```text
target{base_url, environment, authorised_by}, plan{path, sha256}
deployed{version, expected, matches, source}
run{started_at, finished_at, requests_sent, blocked[], credential_sets[{name, available, signed_in}]}
checks[{id, kind, critical, method, path, status: pass|fail|error|blocked|skipped, http_status, assertions[{path, op, ok, detail}], timing{samples_ms[], median_ms}}]
performance{baseline_run, comparisons[{check_id, median_ms, baseline_median_ms, change_pct, status}]}
findings[{id, kind, severity, check_ids[], title, evidence}]
recommendation{value, reasons[], decided_by: null, decision: null}
limitations[]
```

## Validator fails when

- A check's status, timing or assertion results differ from `run.json`; a regression is recomputed
  differently from the baseline.
- A request in the run used a method other than GET, HEAD or the named sign-in.
- A credential, token or cookie value appears anywhere in the run or the report.
- The recommendation doesn't follow the rules, or `decision` / `decided_by` are filled by the skill.
- A production target has no `authorised_by`.
- Text says the release is healthy, safe or fine.

## Edge cases

- **The version can't be read**: `not_established`, never assumed.
- **Blue/green or canary**: the plan names the host or header that reaches the new version; checks that
  reached the old one (by version header) are `error`.
- **Rate limits or a WAF block**: stop, record, `not_established`.
- **A check that must write** (place an order): refused; suggest a test-only endpoint or a synthetic
  monitor owned by the team.

## Files

```text
post-deploy-validator-skill/
  SKILL.md  RUBRIC.md  README.md
  schemas/pvt_plan.schema.json  pvt_report.schema.json
  scripts/run_pvt.py  recommend.py  validate_pvt.py  render_report.py
  examples/
```

## Example

`examples/harbour-tickets/server.py` is a small stdlib ferry-ticket site and API standing in for
production on loopback, in two releases. `examples/runs/` holds a real run of 1.8.0 (the baseline) and a
real run of 1.9.0 against it. Planted in 1.9.0: the timetable page answers 500; the fares API returns a
negative price; the fares endpoint got slower by about 300 ms; and the plan lists a POST to bookings,
which the guard refuses. Decoys: a page slower within tolerance, and an authenticated account page that
passes with the `smoke-reader` set.

## Resolved questions

Agreed with the user before the build (`decisions-26-32.md`).

1. **Production access?** Read-only.
2. **Authentication?** A named set of credentials.
3. **Data checks?** Through the API.
4. **Performance baseline?** The skill's previous run.
5. **Who decides?** The skill recommends; a person decides.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **The expected version is a command-line argument**, not part of the plan. The plan's SHA-256 then
  stays the same from release to release, and the next run can find its baseline by plan and target.
- **Belt and braces on secrets**: before writing `run.json`, the runner checks that no password or token
  it handled appears in the record, and writes nothing if one does. A username is not treated as secret,
  because the example asserts it. The validator also scans the run and the report for bearer tokens,
  cookie values and 32-hex tokens.
- **HTTP only**: pages are checked by status and content, not in a browser, so client-side errors and
  rendering are out of scope in v1 and listed as a limitation.
- **The minimum change matters as much as the tolerance**: in the example the about page rose 193%, but by
  29 ms, so it is `ok`.
- **Example results:** `no_go` with three findings (the timetable 500, the negative child fare, the fares
  API about 9 times slower); the bookings POST refused and never sent; the signed-in check passed without
  recording the email.
- **Testing:** 22 of 22 seeded violations are caught, including a POST or DELETE in the request log, a
  write check not recorded as blocked, a set-cookie value, the password in the run, a softened
  recommendation, and the skill filling in the decision.

### v1.1.0: after a run on a real Next.js staging deployment

A run on a Next.js App Router site on Vercel, with its database layer down, showed these gaps; v1.1.0
closes them.

- **Failing checks were timed.** Three 500s were sampled three times each, and one came back slower than
  minutes earlier: a "critical regression" of an error page. A check that doesn't answer as expected is now
  sent once, its timing is `comparable: false`, and the comparison is `not_comparable`; sampling also stops
  when a sample changes status.
- **The version could not be read.** The app exposes its build only in the HTML (`data-dpl-id`). Readers
  now take `meta` and `regex` over the body as well as JSON and headers, a separate `deployment` reader
  records a deployment id, and `--expect-version` may be either.
- **Baselines minutes apart on one deployment looked like evidence.** Each run records the deployment id;
  `baseline_relation` says whether the comparison is between releases, and a same-deployment regression is
  a finding but not a `no_go` reason. The baseline rule is written down in SKILL.md.
- **No header checks.** `expect.headers` asserts present, absent, equals, contains; cache, CDN and security
  headers are recorded (CSP nonces redacted), and differing CSPs between pages are a minor finding.
- **A JSON field `status` collided with the HTTP status** ("answered None"). Assertions carry a `kind`.
- **The stop rule depended on plan order** (a 404 reset the 5xx count) and treated an outage like a WAF.
  Now a 4xx doesn't reset the count, and a 429 or a WAF/bot challenge stops at once as `error`,
  `not_established`.
- **Redirects were followed silently.** Hops are recorded, only same-origin hops are followed, a public
  check landing on a sign-in page fails by default, and `no_redirect` / `final_path` can be asserted.
- **The plan was never schema-checked**; a typo such as `"critcal"` silently made a check non-critical.
  The runner and the validator now refuse it.
- **The validator checked numbers, not words.** It now also requires the headline to name the
  recommendation without contradicting it ("ship it", "go-ish" over a `no_go`), the derived notes and
  evidence to be kept, titles to keep severity and HTTP status, and checks the report against its schema.
- **Example:** 1.9.0 now also redirects the public help page to sign-in and sends an older CSP on the about
  page; the timetable 500 is `not_comparable`. `no_go` with five findings.
