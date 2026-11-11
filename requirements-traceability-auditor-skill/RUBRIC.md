# Rubric: requirements traceability auditor

## Evidence and strength

| Evidence | Strength | Reference |
|---|---|---|
| The test's name, docstring, comment or tag names the key | explicit | `file:line` |
| A ticket "is tested by" link to a test key the export maps to this test | explicit | the ticket field and the export entry |
| A test-management export entry lists the key | explicit | the export entry |
| A commit whose message names the key added the test | inferred | the commit |
| A ticket comment names the test or its file | inferred | the comment |
| Code or a commit names the key | implementation (not a test link) | `file:line` or the commit |

A key matches only as a whole key: not preceded by a letter, digit, `_` or `-`, and not followed by one or
by `-` and a digit. A link's strength is its strongest evidence.

## Criteria

A test covers a criterion when its evidence names it (`SHOP-401 AC-2`, `SHOP-401/AC-2`), or when the
requirement has one criterion and the link is explicit. The agent may judge a criterion covered (with a
reason); that coverage is inferred.

## Results

A linked test's latest result from the JUnit; no result is `not_run`. Fresh: the run is not older than the
latest commit naming the key or touching the linked tests' files.

## Status

| Status | When |
|---|---|
| `failing` | any kept linked test failed or errored |
| `verified` | every criterion has an explicit test that passed in a fresh run, and every kept linked test passed and is fresh |
| `partially_verified` | linked tests, but not verified (an uncovered criterion, only inferred links, a test not run or stale) |
| `untested` | no kept linked test, but code or commits name the requirement |
| `not_traceable` | nothing names the requirement |

Gaps: criteria with no explicit test, tests not run, failed or stale, and tested requirements with no
implementation evidence.

## Orphans

Tests linked to no requirement in scope, noting any out-of-scope keys they name.
