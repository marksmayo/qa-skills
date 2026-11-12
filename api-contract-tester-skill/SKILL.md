---
name: api-contract-tester
description: Check that a running HTTP API behaves the way its contract says. Read an OpenAPI 3.0/3.1 document or a GraphQL schema, generate requests from it (documented examples, boundary values, one-violation negatives, a seeded random sample, and requests without credentials), send them to a test instance in safe mode, and check every response for undocumented status codes, server errors, wrong content types, schema violations, missing headers, invalid input accepted, valid input rejected and auth not enforced. Every finding has a saved, redacted request and response and is re-sent once to separate repeatable from flaky. With a previous contract it also rates the breaking changes between versions. Uses Schemathesis and oasdiff when installed; works with the standard library alone. No findings never means the API conforms.
version: 1.2.0
---

# API contract tester

## Mission

Find where a running API breaks its own contract, with a recorded exchange for every claim. The
contract is the oracle: when the API and the contract disagree, say which side should change and
mark that as your judgment. Report what was exercised and what wasn't, so nobody reads "no
findings" as "conforms".

This skill differs from the code security auditor, which looks for exploitable weaknesses in
source code: this one looks for the API not doing what it promised. Server errors and missing auth
may be security relevant, and the report says so (and routes them to the auditor), but it doesn't
rate them with CVSS. It writes no tests by default; each finding carries a regression-test sketch.

## Non-negotiable rules

1. **No findings is not conformance.** The best verdict is "No contract violations found in N
   requests over M of K operations; conformance not established". Operations never called are
   `untested`, a check that couldn't be evaluated is `cantTell`, and neither is ever `passed`.
2. **Every finding has a saved exchange**: the exact request sent and the response received,
   under `exchanges/`. Each failing case is sent once more. A failure that doesn't happen again is
   kept as a flaky finding with low confidence, never dismissed.
3. **Safe mode is the default.** Only `GET`, `HEAD` and `OPTIONS` are sent (GraphQL queries go
   over POST but are reads; mutations are writes). Writes need `allow_writes`, `authorised_by` and
   the user's confirmation that the target is not production, even for operations the contract
   calls safe. A host on `production_hosts` never receives a write.
4. **Authorisation for the target.** Loopback needs nothing; any other host needs `authorised_by`
   (who confirmed testing it is allowed).
5. **Secrets are redacted before anything is written**: `Authorization`, cookies, API-key headers
   and query parameters, parameters named like token, secret or password, fields the schema marks
   `format: password` or `writeOnly`, and every occurrence of a configured secret value. Read
   credentials from environment variables, never from the command line or the report.
6. **Respect rate limits**: 5 requests per second by default, back off on `429` and `Retry-After`.
   Read the app's own limits first (middleware, a proxy, a gateway config) and set `--rate`,
   `--path-budget` and `--max-requests` under them. A run stopped by limits is `incomplete`, and
   every earlier attempt against the target is recorded, never hidden.
7. **The fix direction is judgment.** `fix_api` or `fix_contract`, always with
   `fix_direction_basis: agent_judgment`.
8. **Record every tool** as `ran`, `not_run` or `errored`, with versions.
9. **Not run is a valid answer.** When the API can't be run, write a `not_run` report from the
   static half (contract issues, breaking changes, the handler check). It claims nothing about
   behaviour: every operation is untested and there are no findings.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `contract` | required | OpenAPI 3.0/3.1 (JSON or YAML) or GraphQL SDL, as a file or URL. Swagger 2.0 needs Schemathesis to convert it; otherwise `not_assessed` |
| `base_url` | required | The test instance |
| `authorised_by` | required for non-loopback | Who authorised testing this host |
| `auth` | none | A bearer token, API key or session cookie in an environment variable (`--bearer-env`, `--api-key-env`, `--cookie-env`) |
| `auth_required` | none | Operations the contract leaves unsecured that need credentials (`--auth-required`, agent judgment from the handlers) |
| `request_budget` | 5000 | `--max-requests` per attempt, `--path-budget PREFIX=N` per path prefix (for app rate limits) |
| `liveness_probe` | first parameterless GET | `--probe` path and `--probe-expect` statuses; use a health endpoint when there is one |
| `allow_writes` | `false` | Also needs `authorised_by` and a confirmed non-production target |
| `production_hosts` | `[]` | Hosts that never receive writes |
| `operations` | all | `--include`/`--exclude` by operationId, `tag:T` or `path:/prefix` |
| `max_cases_per_operation` | 50 | |
| `seed` | random, recorded | Makes the generated cases repeatable |
| `previous_contract` | none | Turns on the breaking-change diff |
| `repo` | none | Points each finding at its handler, and feeds the contract-vs-handler check |

## Workflow

Work in a run directory (`<run>/`). All scripts are stdlib Python 3.9+ (PyYAML is used for YAML
when installed, otherwise the bundled `yaml_lite.py`). Schemathesis and oasdiff are optional; ask
before installing them.

### 1. Load and lint the contract

```bash
python scripts/load_contract.py openapi.yaml --out <run>/contract.json
python scripts/load_contract.py schema.graphql --introspect-url http://127.0.0.1:8140/graphql --out <run>/contract.json   # GraphQL
```

It resolves `$ref`s (local, and external files or same-host URLs when the fragment has no local
refs of its own), lists the operations with their line in the contract, and reports **contract
issues**: a missing operationId, a 2xx response without a schema, an empty response schema
(`{}`, which every body matches, so `schema_conformance` can't fail: generators write it for a
handler that returns a raw `Response`, a stream or a file), an example that doesn't match its own schema, a keyword the checker can't evaluate, an unresolved `$ref`, or (GraphQL) a served
schema that differs from the SDL. Contract issues are a separate section: they are not API failures. `no_security_declared` means no
operation requires credentials, so `auth_enforced` can't be evaluated from the contract: read the
handlers and mark the operations that do need a session or token with `--auth-required` (step 2).

**A contract built in code** (a TypeScript object served by a route, zod-to-openapi, a framework's
generator, JSDoc) has no file to load. Produce the file the app itself would serve: run the
project's own generator or export script, or import the builder with the project's toolchain
(`node`/`tsx`, `python -c`), or fetch the route from a running local instance; save the JSON and
load that. Record in `limitations` how it was produced and which source file builds it, since
contract lines then point into the generated file. For a previous contract, extract the same
builder at an earlier commit (list the commits that touched the builder file and show the file
at one of them).

**A contract generated from the handlers** (FastAPI's `app.openapi()`, NestJS Swagger, springdoc)
documents every routed handler by construction, so the contract-vs-handler check (step 6b) is
close to a tautology: an empty result shows the generator ran, not that the contract is right.
Say so in `limitations`, and spend the static effort where generated contracts go wrong:

- what each handler actually returns against its declared response model and examples (a dict
  built by hand, a raw `Response`, a stream or file documented as JSON, which handler_map reports
  as `response_class_mismatch`, an example that names other fields than the code returns);
- routes added outside the framework's router (mounted sub-apps, middleware that answers
  requests, static files), which the generated contract never shows;
- serialisation the schema describes correctly but clients don't expect: Pydantic sends
  `Decimal` as a string (`type: string`), so a client or a recorded payload that uses numbers is
  stale, not the contract; and a naive `datetime` is serialised without an offset, which fails
  `format: date-time` (RFC 3339 needs one), so once run it is a `schema_conformance` failure on a
  2xx, rated High. Find these by reading the models (`Decimal`, `datetime` without a timezone or
  `AwareDatetime`) and, when the repository has recorded payloads (frontend fixtures, mocks, saved
  responses), checking a few against the contract's schemas with `jsonschema_lite.py`.

Record what you find by reading as contract issues with `"source": "agent"` and a `basis` (the
file, line and an excerpt of the line that shows it), after the loader's issues, with ids of your
own (`CA1`, ...). They are leads, not findings: a finding needs a saved exchange. The validator
re-reads each basis under `--repo`.

### 2. Plan and generate

```bash
python scripts/gen_cases.py <run>/contract.json --out <run>/cases.json --seed 1611 [--exclude cancelAppointment] \
  [--allow-writes] [--auth-configured] [--auth-required path:api/appointments] [--add-cases extra.json]
```

Selectors are an operationId, `tag:T` or `path:/prefix` (the leading slash is optional, because
Git Bash rewrites a leading `/` into a Windows path; or set `MSYS_NO_PATHCONV=1`). Case ids are
`<operationId>-NNN`; an operationId that isn't filesystem-safe (the default `GET /api/x`) becomes a
slug plus a short hash.

The plan lists every operation as `planned`, `excluded`, `skipped_write` or `skipped_auth`; it is
the coverage denominator. Cases per operation, in order: the documented example; boundaries (each
bound and enum value); negatives (one violation at a time); a seeded random sample; and the
example without credentials for secured operations. A sample the contract says is invalid is never
sent labelled valid. GraphQL: each query field with every enum value and a few values per scalar
argument, plus a query missing each required argument.

### 3. Check tooling

Look for Schemathesis (`st`) and oasdiff. Record each as `ran`, `not_run` (not installed, or not
used) or `errored`. Without Schemathesis the stdlib generator is the only one, and the report says so.

### 4. Optional: Schemathesis

```bash
python scripts/run_schemathesis.py openapi.yaml --base-url http://127.0.0.1:8140 --out <run>/schemathesis.json \
  --seed 1611 --rate 5 --bearer-env API_TOKEN [--exclude-operation-id cancelAppointment]
```

The wrapper enforces the same safe mode. Schemathesis's coverage phase sends PUT, POST, DELETE,
PATCH and TRACE to every path by default ("unexpected methods"), whatever its method filter says,
so in safe mode the wrapper turns that off in a config file, and also turns off the stateful phase
and the checks that create resources. Never run `st` directly against a shared instance without
those settings. Schemathesis also writes a `.schemathesis/` cache in the working directory; keep it
out of version control.

Its failures have no exchange of ours. For each one the stdlib run didn't also find, write a case
that reproduces it (`extra_cases.json`: `operation_id`, `description`, `path_params`, `query`,
`body`, `source`) and regenerate with `--add-cases`; or list it as `needs_review`.

### 5. Probe, send and check

```bash
python scripts/send_cases.py <run>/contract.json <run>/cases.json --base-url http://127.0.0.1:8140 --out <run>/run.json \
  --bearer-env API_TOKEN [--allow-writes --authorised-by "who" --confirm-non-production] [--production-host api.example.com]
```

```bash
# a deployed target with app-level rate limits and session auth
python scripts/send_cases.py <run>/contract.json <run>/cases.json --base-url https://staging.example.com --out <run>/run.json \
  --authorised-by "who" --cookie-env SESSION_COOKIE --rate 1 --probe api/health --probe-expect 200 \
  --path-budget api/appointments=15 --max-requests 150 --max-retries-429 0 --on-429 stop-path
python scripts/send_cases.py ... --out <run>/run.2.json --resume <run>/run.json     # finish a stopped run
```

It probes first and stops if the instance is down: the probe can't connect or misses
`--probe-expect` (default: any status below 500). Without `--probe` it uses the first parameterless
GET operation, and a 5xx from that alone doesn't mark the target down (it is an API answer, sent as
a case). A 429 is retried after its `Retry-After` up to `--max-retries-429` times when the wait is
at most `--max-retry-after` seconds; when it persists, `--on-429 stop-run` (default) stops the run
and `stop-path` stops only that path and carries on. Cases over a budget or on a stopped path are
listed in `blocked`. `--resume` reuses the answered cases of an earlier run.json and sends the rest;
if the sender aborts, it still writes the partial run.json. List every earlier attempt in
`metadata.aborted_attempts`. Every response is checked:

| Check | Fails when |
|---|---|
| `status_documented` | A 2xx/3xx/4xx status isn't in the operation's responses and there's no `default` (5xx is `server_error`'s) |
| `server_error` | Any 5xx |
| `content_type` | The `Content-Type` isn't one documented for that status |
| `schema_conformance` | The body doesn't validate against the response schema (3.0 or 3.1 dialect); `cantTell` when it uses a keyword the stdlib checker doesn't support and nothing else failed |
| `required_headers` | A documented required header is missing |
| `negative_accepted` | A request that violates the request schema got a 2xx |
| `positive_rejected` | A documented example got a 4xx other than 401, 403 or 429 |
| `auth_enforced` | A secured operation answered 2xx without credentials |
| `graphql_errors` | A valid query returned `errors`, or data that doesn't match the field types |

Failing exchanges are saved, redacted, and each failing case is re-sent once. Bodies over 2 MB are
truncated with the full body's SHA-256 kept. Timestamps and ids are checked by schema only, never
compared between sends.

**An environment that is down looks like N defects.** When every answer over two or more operations
is a 5xx, `merge_findings.py` makes one `environment_outage` candidate (confidence low) instead of
one per operation, the headline confidence is low and the run is `partial`. Say "outage" in the
summary, check a health endpoint, and treat any other failures seen only on those 5xx responses as
the same cause. Nothing about the handlers' contract behaviour has been shown.

### 6. Diff contracts (with a previous contract)

```bash
python scripts/load_contract.py openapi.v1.yaml --out <run>/contract.v1.json
python scripts/spec_diff.py <run>/contract.v1.json <run>/contract.json --out <run>/diff.json [--run-oasdiff old.yaml new.yaml]
```

`X | None` written as `anyOf` [X, `{type: null}`] (Pydantic v2's Optional) is unwrapped, so a field
that became optional is one `now_nullable` change, not a removed bound. Severity assumes the API
bumps `info.version` for a break. Many never do (FastAPI's default `0.1.0` stays in every
release, and spec_diff prints a note when the version is unchanged): keep the ratings, and say in
`limitations` that the API doesn't version its contract, so consumers can't tell a breaking release
from the version.

### 6b. Optional: compare the contract with the handlers (with a repo)

```bash
python scripts/handler_map.py <repo> --contract <run>/contract.json --out <run>/handlers.json [--contract-prefix /api] [--handler-prefix /api]
```

Reads the route handlers (Next.js App Router `route.ts` exports including `export { h as GET }`,
Pages Router API files, Express, Flask, FastAPI, Azure Functions) and lists undocumented handlers,
documented operations with no handler, documented methods the handler doesn't export,
path-parameter name differences, and handlers that stream or return a file, text or HTML where the
contract documents only JSON (`response_class_mismatch`). It works without a running API. Python
is parsed, not pattern-matched: decorators in docstrings don't count, and FastAPI `APIRouter`
prefixes (also on subclasses), `include_router(prefix=...)` and Flask blueprint prefixes are
applied. `unresolved_mounts` counts handlers whose router was never seen being included, or whose
prefix is computed. Express `app.use` mounts are not resolved: use `--contract-prefix` when the
contract's paths lack a prefix the code has, `--handler-prefix` when the code's lack one. Copy its
summary and issues into the report's `handler_check`. They are review leads, not findings: a
handler registered through a wrapper can be missed. Coverage is still the contract's operations;
say in `limitations` how many handlers the contract doesn't document. For a contract generated
from the handlers, read step 1 first: this check can't disagree with it.

### 7. Merge into candidates

```bash
python scripts/merge_findings.py <run>/contract.json <run>/cases.json <run>/run.json --out <run>/candidates.json \
  [--diff <run>/diff.json] [--schemathesis <run>/schemathesis.json]
python scripts/merge_findings.py <run>/contract.json [<run>/cases.json] --out <run>/candidates.json    # not run: static only
```

One candidate per (check, cause); a cause shared by several operations (an undocumented status, a
fault in a shared component schema, missing auth) is one candidate with a wider reach. Severity,
confidence, reach, score and priority are computed (`RUBRIC.md`).

### 8. Triage

Every candidate is `promoted` (to a finding), `needs_review`, or `dismissed` with a checkable
reason (for a Schemathesis candidate: "same failure as K2"). A generic reason ("not a real issue")
is rejected; candidates dismissed for one shared cause name it by id ("same outage as K1"). A Schemathesis candidate is promoted
only through its reproduced stdlib candidate. For each finding write:

- `title`, `expected` (what the contract says) and `actual` (what the exchange shows, including
  its status code);
- `fix_direction` (`fix_api` or `fix_contract`) and the reason, as `agent_judgment`;
- `handler` (file and line) when the repo is available; `null` otherwise;
- `remediation`, a `verification_step` (rerun with the same seed and name the case that should
  pass), and the `regression_test_sketch` from the candidate, adapted to the repo's framework when
  one is known;
- `security_relevant`: always for `auth_enforced`; for a 5xx that leaks internals; route those to
  the code security auditor.

### 9. Write, validate, render

Write `<run>/report.json` (`schemas/api_contract_report.schema.json`), then:

```bash
python scripts/validate_report.py <run>/report.json --run <run>/run.json --candidates <run>/candidates.json \
  --contract <run>/contract.json --cases <run>/cases.json [--diff <run>/diff.json] [--handlers <run>/handlers.json] \
  [--repo <repo>] [--write-summary]
python scripts/validate_report.py <run>/report.json --contract <run>/contract.json [--cases ...] [--diff ...] \
  [--handlers ...] [--repo <repo>]                                   # a not_run report
python scripts/render_report.py <run>/report.json [--brief] [--sarif findings.sarif --contract <run>/contract.json]
```

Fix every validator error; never edit the run or the exchanges to make it pass. Set the secret
environment variables before validating, so the exchanges are scanned for their values.

## Output fields

- `run_status`: `complete`, `partial` (stopped early, down, or an environment outage) or `not_run`
  (static only), with `run_status_reason` whenever it isn't complete.
- `scope`: the contract (kind, version, title, API version, source, SHA-256; copy them from
  contract.json, whose `source` is the path as load_contract.py was given it), the previous contract,
  base URL, `authorised_by`, writes allowed, production hosts, seed, filter and case limit (base
  URL, seed and limit are null when nothing was run or planned).
- `metadata`: tools, generator (`stdlib`, `schemathesis` or `both`), requests sent, rate-limit
  events and backoff, run status, the secret environment variable names, the run file, and
  `aborted_attempts` (earlier attempts against the target: requests sent, why they stopped, their
  record).
- `headline`: verdict (`violations_found` or `not_established`), a summary, severity counts, flaky
  findings, breaking-change counts, operation coverage, evidence completeness and overall confidence.
- `operations[]`: every operation with status, cases sent and the result of each check.
- `findings[]`, `needs_review[]`, `triage[]`, `contract_issues[]` (the loader's, then any found by
  reading, with `source: agent` and a `basis`), `handler_check` (null when not run),
  `breaking_changes[]`, `limitations[]`, `next_steps[]`.

## What the validator enforces

The operations, coverage, candidates, ratings and headline are recomputed from the contract, the
cases and the run. Every finding needs a saved exchange belonging to its candidate. No configured
secret, credential header, API key or password field may appear unredacted. No write may appear
when writes weren't allowed, and none may go to a production host. A non-loopback target needs
`authorised_by`. Every candidate is triaged exactly once, with a specific reason. A finding's
`actual` states a status its exchanges show. The summary may not claim conformance or be a
placeholder; after an outage it must say so. Limitations may not be empty when the run was partial
or not run, operations were excluded or untested, or there were earlier attempts, and every attempt
the run resumed must be in `aborted_attempts`. A `not_run` report has no run, findings or triage,
and its summary says the API was not run and claims nothing a run would show. The loader's contract
issues must equal contract.json's; issues found by review need a `basis`, re-read with `--repo`
(the line must exist and hold the excerpt). The contract's source is compared as a path, so a
relative and an absolute path to the same file match. The handler check must equal handlers.json,
and with `--repo` each cited line is re-read.
