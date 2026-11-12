# API contract tester rubric

`merge_findings.py` computes every rating here, and `validate_report.py` recomputes it from the run.

## Severity

| Severity | Weight | When |
|---|---|---|
| **Critical** | 4 | `auth_enforced` fails; a 5xx on a documented example |
| **High** | 3 | `schema_conformance` fails on a 2xx (clients will break); a 5xx on other valid input; `positive_rejected`; `graphql_errors` |
| **Medium** | 2 | An undocumented status code; `negative_accepted`; a 5xx on invalid input; `schema_conformance` on a non-2xx; a wrong content type with a body that isn't JSON |
| **Low** | 1 | A wrong content type with a parseable JSON body; a missing documented header |

"Valid input" means a case the contract says is valid: `valid`, `boundary`, `random`, `graphql`
or `added` (with `valid: true`). The most severe instance decides.

`status_documented` ignores 5xx, which `server_error` reports; a 5xx documented only through
`default` still fails `server_error`.

## Confidence

| Confidence | When |
|---|---|
| high | The failure happened again on the re-send, and no repeated instance is a write |
| medium | It happened again, but a repeated instance is a write (it depends on server state) |
| low | It didn't happen again on the re-send: flaky. Kept, never dismissed. Also an environment outage (below) |

**Environment outage.** When every answer of a run (first sends and re-sends) over two or more
operations is a 5xx, the server-error failures are one candidate (`environment_outage: true`,
cause "environment outage: every request answered 5xx", reach by the operations affected) with
confidence low: the run shows the environment failing, not N handlers.

## Reach

Operations affected by the same cause: 1 for one operation, 2 for two to four, 3 for five or more.
A cause is shared across operations for an undocumented status, a content type, missing auth, a
missing header, a fault in a named component schema, and a GraphQL field-type fault. Other causes
are per operation.

## Priority

`score = severity weight × reach × confidence (high 3, medium 2, low 1)`. P0 ≥ 24, P1 ≥ 12,
P2 ≥ 6, otherwise P3. A Critical finding is at least P1.

## Breaking changes

| Rating | Request side (narrowing breaks) | Response side (widening breaks) |
|---|---|---|
| breaking | new required parameter or property, parameter now required, removed enum value, tightened bound, new pattern or format, a type that accepts less, `additionalProperties: false` added, request body now required, media type removed, security added | removed property or status, a field no longer required, a type that can return more, required header removed, media type removed |
| potentially breaking | removed optional parameter, removed property (when extra properties are allowed), `oneOf`/`anyOf`/`allOf` changed | new enum value, loosened bound, changed pattern or format |
| compatible | everything else that changed | everything else that changed |

Removing an operation is breaking; a changed operationId is potentially breaking (breaking when
links use it). Severity: breaking without a major version bump is High, potentially breaking
without one Medium; with a major bump, breaking is Low (expected) and the rest have none.
Compatible changes are listed without severity.

## Verdict, completeness and overall confidence

- **Verdict:** `violations_found` when there is at least one finding, otherwise `not_established`.
  There is no "conforms".
- **Run status** (top level): `not_run` when no run was given (static only); `complete` when the
  run finished and wasn't an environment outage; otherwise `partial`, with a reason.
- **Evidence completeness:** `none` when not run; `incomplete` when the run stopped (rate limits,
  a request or path budget, a 429 on a path) or the instance was down; `partial` when an operation
  is untested or skipped for auth, a check is `cantTell`, a tool errored, or the run was an
  environment outage; otherwise `complete`.
- **Overall confidence:** low when not run, incomplete or an environment outage; medium when
  partial or any finding isn't high; otherwise high.
