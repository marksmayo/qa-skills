# Design: `api-contract-tester-skill`

Status: Built (v1.0.0). Build order: 16.

## Mission

The skill checks that a running HTTP API behaves the way its contract says. It reads an OpenAPI
3.x document or a GraphQL schema, generates requests from it (examples, boundary values and
property-based cases), sends them to a test instance, and compares every response with the
contract. With two versions of a contract it also reports breaking changes between them.

How it differs from the existing skills:

- **code-security-auditor** looks for exploitable weaknesses in source code. This skill looks for
  the API not doing what it promised: undocumented status codes, responses that don't match the
  schema, wrong content types. Server errors it finds may be security-relevant, and the report
  says so, but it doesn't rate them with CVSS.
- **journey-to-tests** writes tests for user journeys. This skill writes no tests by default; it
  produces findings, each with a recorded request and response that reproduces it, and can emit a
  regression-test sketch in the repo's framework (ticket-to-tdd conventions) per finding.
- It needs only the contract and a base URL. Source code is optional and used only to point a
  finding at a handler.

## Decisions

- **Safe mode is the default:** only `GET`, `HEAD` and `OPTIONS` are sent. Other methods need
  `allow_writes: true`, `authorised_by` and a target the user confirms is not production, even
  for operations a spec marks as safe. The skill never
  sends writes to a host on the `production_hosts` list, even when allowed.
- **Schemathesis is optional.** When it's installed (MIT), the skill uses it for property-based
  generation and stateful links. Without it, `scripts/gen_cases.py` generates cases from the
  schema with stdlib Python: examples, `enum` values, boundaries (`minLength-1`, `maximum+1`),
  wrong types, missing required fields, and a seeded random sample. The fallback is weaker, and the
  report says which generator ran.
- **Breaking-change diff is stdlib-first.** `scripts/spec_diff.py` covers the common breaking
  changes. oasdiff (Apache-2.0) is used when installed, and the two are merged per change.
- **GraphQL in v1:** schema checks, introspection comparison, and generated queries against the
  schema's types. Mutations are writes, so the same rules apply.
- **No numeric score.** The headline is the finding counts, the share of operations exercised,
  and the verdict.

## Non-negotiable rules

- **No findings doesn't mean the API conforms.** The best verdict is "No contract violations
  found in N requests over M of K operations; conformance not established". Operations that were
  never called are `untested`.
- **Every finding is reproduced.** A finding needs the exact request sent and the response
  received, saved under `exchanges/`. A finding that didn't happen again on a second send is
  `flaky`, not dismissed.
- **Secrets are redacted** in every saved exchange: `Authorization`, cookies, API-key headers and
  query parameters, and any field the schema marks `format: password` or `writeOnly`. Redaction
  happens before writing to disk, and the validator scans the saved exchanges for the
  configured secret values (read from the environment) to check none leaked.
- **Rate limits are respected.** Requests are rate-limited by default (5 per second), and the
  skill backs off on `429` and `Retry-After`. A run stopped by limits is `incomplete`.
- **The contract is the oracle, not the implementation.** When the two disagree, the finding
  says which way to fix it (`fix_contract` or `fix_api`), and the agent's choice is
  `agent_judgment`.
- **Tools are recorded** as `ran`, `not_run` or `errored`, with versions.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `contract` | required | OpenAPI 3.0/3.1 (JSON or YAML) or GraphQL SDL, as a file or URL. Swagger 2.0 is converted when Schemathesis is present; otherwise `not_assessed` |
| `base_url` | required | The test instance |
| `authorised_by` | required for non-loopback | Who authorised testing this host |
| `auth` | optional | Header or bearer token from an env var, or an OAuth client-credentials recipe from env |
| `allow_writes` | `false` | Also needs a confirmed non-production target |
| `production_hosts` | `[]` | Hosts that never receive writes |
| `operations` | all | Include or exclude by `operationId`, tag or path |
| `max_cases_per_operation` | 50 | |
| `seed` | random, recorded | Makes generated cases repeatable |
| `previous_contract` | optional | Turns on the breaking-change diff |
| `repo` | optional | Used to locate the handler for each finding |

## Workflow

1. **Load and lint the contract.** Resolve `$ref`s (local and same-host remote), and report
   contract problems (missing schemas for responses, examples that don't match their own
   schemas). Contract problems are a separate section: they aren't API failures.
2. **Plan.** List every operation with whether it will be exercised (`planned`, `excluded`,
   `skipped_write`, `skipped_auth`). This plan is the coverage denominator.
3. **Check tooling.** Schemathesis and oasdiff versions, or `not_run`.
4. **Probe the instance.** A single `GET` to a health or docs endpoint, then stop if it's down.
5. **Generate and send cases.** Per operation: documented examples first, then boundary and
   negative cases, then the property-based sample. Stateful links (OpenAPI `links`) only with
   writes allowed.
6. **Check every response** (see checks below), saving each exchange that fails a check.
7. **Re-send** each failing case once, to separate repeatable from flaky.
8. **Diff contracts** when `previous_contract` is given.
9. **Normalise and merge** into candidates: one per (operation, check, cause). Every candidate is
   triaged: promoted, `needs_review`, or dismissed with a checkable reason.
10. **Output, validate and render.**

## Checks

| Check | Fails when |
|---|---|
| `status_documented` | The status code isn't in the operation's responses and there's no `default` |
| `server_error` | Any `5xx` on any input. Rated higher on valid input |
| `schema_conformance` | The body doesn't validate against the response schema (JSON Schema 2020-12 for 3.1, the 3.0 dialect for 3.0) |
| `content_type` | The `Content-Type` isn't one documented for that status |
| `required_headers` | A documented response header is missing |
| `negative_accepted` | A request that violates the request schema got a `2xx` |
| `positive_rejected` | A valid request (from a documented example) got a `4xx` other than `401`/`403`/`429` |
| `auth_enforced` | An operation with a security requirement answered `2xx` without credentials |
| `graphql_errors` | A valid query returned `errors`, or `data` that doesn't match the field types |

The validator for JSON Schema is a stdlib subset in `scripts/jsonschema_lite.py` covering the
keywords OpenAPI uses (`type`, `properties`, `required`, `items`, `enum`, `format` for common
formats, `oneOf`/`anyOf`/`allOf`, `nullable`, `additionalProperties`, numeric and string bounds).
A keyword it doesn't support makes that check `cantTell` for the operation, never a pass.

## Breaking changes (contract diff)

Breaking: removed operation or response status; new required request parameter or property;
narrowed type or enum; removed response property; changed `operationId` used in links; tightened
bounds. Non-breaking changes are listed but not rated. Each is rated `breaking`, `potentially
breaking` (for example an added enum value in a response, which strict clients reject) or
`compatible`.

## Rating

| Severity | Examples |
|---|---|
| **Critical** | `auth_enforced` failure; `5xx` on a documented example |
| **High** | `schema_conformance` on a `2xx` (clients will break); `positive_rejected`; a breaking change without a version bump |
| **Medium** | Undocumented status code; `negative_accepted`; `5xx` on invalid input |
| **Low** | Wrong content type with a parseable body; missing documented header |

- **Confidence:** `high` when the failure repeated on the re-send and the check is
  deterministic; `medium` when it repeated but depends on server state; `low` when flaky.
- **Priority:** severity weight (4/3/2/1) x reach (operations affected: 1, 2 for 2–4, 3 for 5 or
  more) x confidence (high 3, medium 2, low 1). P0 >= 24, P1 >= 12, P2 >= 6, else P3. Every
  Critical is at least P1.

## Output (`api_contract_report.schema.json`)

```text
scope:        contract{kind, version, sha256}, base_url, authorised_by, writes_allowed, seed
metadata:     tools[{name, version, status}], generator: schemathesis|stdlib, requests_sent,
              rate_limited{events, backoff_seconds}
headline:     verdict: violations_found|not_established, severity_counts,
              operation_coverage{total, exercised, excluded, skipped_write, skipped_auth, untested},
              evidence_completeness, overall_confidence
operations[]: {operation_id, method, path, status, cases_sent, checks{name: passed|failed|cantTell|untested}}
findings[]:   {id, check, operation_ids[], severity, confidence, priority, repeatable,
               exchanges[{path, request_summary, status}], expected, actual, fix_direction,
               handler?{file, line}, remediation, verification_step, regression_test_sketch}
contract_issues[], breaking_changes[], needs_review[], triage[], limitations[]
```

## Validator fails when

- A finding has no saved exchange, or an exchange file contains a configured secret value.
- A write request appears in the exchanges with `writes_allowed: false`, or any write targets a
  `production_hosts` entry.
- An operation is marked `passed` with zero cases sent, or a check used an unsupported schema
  keyword and still reports `passed`.
- The verdict claims conformance, or coverage numbers don't add up to the plan.
- A candidate is missing from `triage`.

## Edge cases

- **Pagination and huge responses:** bodies over 2 MB are truncated in the exchange file, with
  the hash of the full body kept.
- **Non-deterministic fields** (timestamps, ids) are validated by schema only, never compared
  between sends.
- **Rate limits on login:** the OAuth token is fetched once per run.
- **Spec served by the API itself:** the fetched copy is hashed and recorded so the report says
  which contract it judged.
- **Mobile backends:** the same skill applies to the APIs mobile apps call; there is no separate
  mobile mode.

## Files

```text
api-contract-tester-skill/
  SKILL.md  RUBRIC.md  README.md
  schemas/api_contract_report.schema.json
  scripts/load_contract.py     # OpenAPI/GraphQL loading, $ref resolution, contract lint
          gen_cases.py         # stdlib case generator (examples, boundaries, negatives, seeded random)
          send_cases.py        # rate-limited sender, safe mode, redaction, exchanges/
          jsonschema_lite.py   # stdlib JSON Schema subset
          spec_diff.py         # stdlib breaking-change diff; merges oasdiff output when present
          run_schemathesis.py  # optional wrapper, records version or not_run
          merge_findings.py  validate_report.py  render_report.py
          sarif.py             # shared copy
  examples/
```

## Example

`examples/clinic-api/` is a tiny stdlib `http.server` API (an appointment booking service, about
200 lines) with `openapi.yaml` and an older `openapi.v1.yaml`, served on `127.0.0.1:8140`. Planted
problems, with an answer key in its README: a `500` on an unknown `date` format, an
undocumented `404` body shape, a `2xx` response missing a required field, `text/html` on an error
response, an endpoint that ignores its bearer requirement, a request accepted with a too-long
name, and between versions a removed response property and a new required parameter.
`examples/run/` is a real run, and `examples/report.example.json` the triaged report.

## Resolved questions

Agreed with the user before the build.

1. **Allow `POST` to operations marked safe in safe mode?** No: write methods need `authorised_by` and a non-production target.
2. **Regression-test sketch per finding in v1?** Yes.
3. **gRPC or AsyncAPI?** Later, not v1.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Two more scripts.** `yaml_lite.py` parses the YAML subset contracts use, so YAML works
  without PyYAML (it matched PyYAML on both example contracts). `run_schemathesis.py` replaces
  "Schemathesis when installed" with a wrapper that enforces the same safe mode.
- **Schemathesis's defaults are not safe.** Its coverage phase sends PUT, POST, DELETE, PATCH and
  TRACE to every path ("unexpected methods"), whatever `--include-method` says; the first trial
  run sent TRACE requests. In safe mode the wrapper writes a config file that turns this off, and
  also disables the stateful phase and the checks that create resources. With it, all 81 requests
  in the example were GETs.
- **Schemathesis failures are candidates without exchanges.** They are never promoted directly:
  the agent reproduces one with an added case (`gen_cases.py --add-cases`), which then becomes a
  stdlib candidate with a saved exchange, or lists it as needs_review.
- **Candidates merge across operations** for a shared cause: an undocumented status, a content
  type, missing auth, a missing header, a fault in a named component schema (found by walking
  the error path to the last `$ref`), and a GraphQL field-type fault.
- **`status_documented` ignores 5xx**, which `server_error` reports, so one 500 isn't two
  candidates.
- **Confidence** is high when the failure repeated on the re-send, medium when a repeated
  instance is a write, low when it didn't repeat. Severity of a 5xx on valid input other than a
  documented example is High (the design named only the example and invalid-input cases).
- **Breaking-change severity**: High for breaking and Medium for potentially breaking without a
  major version bump; with one, breaking is Low and the rest have none.
- **The example has two reports**, REST and GraphQL, because a report covers one contract.
  GraphQL support is queries and mutations with scalar and enum arguments, one level of nested
  selection, and introspection comparison (`--introspect-url`).
- **Not built:** OAuth client-credentials (bearer tokens and API keys from environment variables
  only), stateful OpenAPI links in the stdlib sender (Schemathesis covers them with writes
  allowed), and Swagger 2.0 conversion without Schemathesis.
- **Example results** (`examples/run/`, seed 1611, 159 requests; Schemathesis 4.33.2 in a
  throwaway venv, 75 cases, safe mode):
  - 10 findings: 1 Critical, 2 High, 6 Medium, 1 Low, one of them flaky. All 7 planted API
    faults were found, plus 3 real ones the sample wasn't designed to have (0 accepted for
    `clinic_id` and `clinician_id`, and an extra property accepted).
  - Schemathesis's 8 failures were all the same as stdlib candidates; one (getClinician id 3)
    the seeded sample had missed, reproduced with an added case.
  - 27 contract changes against 1.4.0: 10 breaking, 1 potentially breaking.
  - GraphQL: 2 findings and the served-schema mismatch.
- **The example servers refuse a port that is in use.** On Windows, Python's `HTTPServer` sets
  `SO_REUSEADDR`, which lets a second server bind a busy port while the first keeps answering. The
  first published run of this example reached a server left over from trial runs (its appointment
  ids showed it); it was rerun against a fresh server with the same findings, and `server.py` now
  refuses a busy port while `commands.sh` checks the server started.
- **Testing:** 36 of 36 seeded report violations are caught. The sender refuses a non-loopback
  host without `authorised_by` and writes without authorisation or to a production host, blocks
  every write case in safe mode, and backs off on 429.

## As built (v1.1.0): gaps found on a real Next.js app

A run against a mid-size Next.js 16 App Router app (a code-built OpenAPI contract with no
operationIds and no security, behind a proxy with per-prefix rate limits, and a staging deployment
whose database was down) found these gaps. What changed:

- **Case ids are filesystem-safe.** With no operationId the default id is `"GET /api/x"`, and the
  sender crashed writing `exchanges/GET /api/x-001.json` after requests had gone out. `gen_cases.py`
  now makes unsafe ids a slug plus a short hash (`GET_api_x-1a2b3c4d-001`); safe ids are unchanged,
  so the example's cases are byte-identical. The sender applies the same function to file names.
- **429 policy.** `--max-retries-429` (default 3) and `--max-retry-after` (default 60 s): a
  Retry-After longer than the maximum is not retried early. `--on-429 stop-path` stops only that path
  (its `--path-budget` prefix, else the operation's path) and carries on; `--path-budget PREFIX=N`
  caps requests per prefix (a first send needs room for its re-send); `--max-requests` caps the attempt.
  Cases not sent are in `blocked` and the run is `incomplete`.
- **Resumable, crash-safe runs.** `--resume earlier/run.json` reuses answered cases (not 429s),
  copies their exchanges and records the earlier attempt in `resumed_from`; `requests_sent` counts
  both. If the sender aborts, the partial run.json is still written. The report's
  `metadata.aborted_attempts` lists earlier attempts and the validator requires every resumed one.
- **Liveness.** `--probe` takes a path with or without its leading slash (Git Bash rewrote `/` to
  `C:/Program Files/Git/`; a rewritten path is now refused with a hint about `MSYS_NO_PATHCONV=1`),
  and `--probe-expect` sets what "up" means. Without `--probe`, the default probe is an API
  operation, and its 5xx no longer marks the whole target down.
- **Environment outage.** When every answer over two or more operations is a 5xx, the server-error
  failures merge into one candidate (`environment_outage`, confidence low) instead of one Critical
  per operation; the headline's confidence is low, run_status `partial`, and the summary must say
  "outage". On the staging run this would have been 1 candidate, not 11.
- **Session auth.** `--cookie-env` sends a Cookie header (redacted, each cookie value scrubbed).
  `load_contract.py` raises a `no_security_declared` contract issue, and `gen_cases.py --auth-required`
  marks operations the contract leaves unsecured as needing credentials (agent judgment, recorded in
  cases.json), so they get a no_auth case that expects 401/403.
- **Static-only reports.** Top-level `run_status` (`complete`, `partial`, `not_run`) with
  `run_status_reason`. `merge_findings.py` and `validate_report.py` work without a run: every
  operation untested, no findings, evidence `none`, confidence low, and the summary must say the API
  was not run and may not claim anything a run would show. `examples/report.static.example.json` is
  one. Missing files are a usage message, not a traceback.
- **Contract vs handlers.** `handler_map.py` reads Next.js App Router `route.ts` exports (including
  `export { handler as GET }`), Pages Router API files, Express, Flask and FastAPI routes, and reports
  undocumented handlers, documented operations without a handler, methods not handled and
  path-parameter name differences, in the report's `handler_check`; the validator compares it with
  handlers.json and re-reads each cited line. On the app above it found the same 190 handlers,
  148 undocumented and the documented-but-unhandled `POST .../reschedule` as a hand-written script,
  in 0.5 s. Mount prefixes (Express `app.use`, Flask blueprints, FastAPI routers) are not resolved.
- **spec_diff** reports a removed required property once (as removed), not also as "no longer required".
- **Validator.** A finding's `actual` must state a status its exchanges show; generic triage reasons
  ("not a real issue") and three or more triage entries sharing one reason that names no candidate
  are rejected; placeholder summaries are rejected; limitations may not be empty when the run was
  partial, not run, had excluded or untested operations, or earlier attempts.
- **Not built:** recording CDN cache headers, a re-send sample option, grouping the
  missing-operationId lint, mapping code-built contracts' lines back to source, and checking that
  cases.json equals what `gen_cases.py` would generate for the recorded seed.

## As built (v1.2.0): gaps found on a FastAPI monorepo with generated contracts

A static-only run against two FastAPI services whose contracts came from `app.openapi()` found
these gaps. What changed:

- **Python handlers are parsed, not pattern-matched.** `handler_map.py` reads Flask, FastAPI and
  Azure Functions routes with `ast` (`py_routes.py`, shared in content with the journey mapper), so
  decorators inside docstrings no longer count. FastAPI paths include `APIRouter(prefix=...)`, also
  on subclasses of `APIRouter`, and every `include_router(..., prefix=...)` above it across modules;
  Flask paths include blueprint prefixes. On the services above the check went from 35 and 37
  false issues to none. `--handler-prefix` prepends a prefix to handler paths, and
  `unresolved_mounts` counts handlers whose router was never seen being included.
- **`response_class_mismatch`.** A handler that streams or returns a file, text or HTML where the
  contract documents only JSON is a handler-check issue. **`empty_response_schema`** is a new
  contract lint: `{}` matches every body, so `schema_conformance` can't fail.
- **Contract issues found by review.** `contract_issues` may end with entries the agent found by
  reading (`source: agent`, a `basis` file, line and optional excerpt), re-read under `--repo`; the
  loader's issues must still equal contract.json's. The static example has one.
- **spec_diff** unwraps `anyOf`/`oneOf` [X, `{type: null}`] (Pydantic's Optional) before comparing,
  so a field that became nullable is one `now_nullable` change instead of "anyOf changed" plus a
  removed bound that was only moved into the branch. It notes when `info.version` is unchanged (a
  framework default such as `0.1.0`); the ratings stand and the agent says so in limitations.
- **Report.** `scope.contract.title` names the API, and the rendered heading uses it, so two
  services' reports can be told apart. `scope.contract.source` is compared with contract.json's
  as a path (relative, `./`, backslashes or absolute).
- **SKILL.md** says that a contract generated from the handlers makes the contract-vs-handler check
  close to a tautology and where to look instead (actual returns against the declared model,
  routes outside the router, `Decimal` serialised as a string, naive datetimes against
  `format: date-time`, recorded payloads checked with `jsonschema_lite.py`).
- **Not built:** dismissing individual handler-check issues in the report, and a `--samples` step
  that checks recorded client payloads against the contract.
