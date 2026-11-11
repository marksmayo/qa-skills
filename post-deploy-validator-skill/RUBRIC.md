# Rubric: post-deploy validator

## Check results

| Status | When |
|---|---|
| `pass` | every assertion held (status, contains, not contains, JSON) |
| `fail` | the response arrived and an assertion didn't hold |
| `error` | no response (connection failed, timed out) |
| `blocked` | not sent: not a read, or its credential set couldn't sign in |
| `skipped` | not sent: the run stopped after three checks in a row answered 5xx |

JSON assertions: `exists`, `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `len_ge`, `len_le`, `all_gt`, `all_ge`,
`all_lt`, `all_le` (over `items[*].field`), `unique`, `matches` (regex), `fresh_within_minutes` (a timestamp
no older than N minutes at the time of the request).

## Performance

Median of `samples` requests per timed check, against the same check's median in the baseline run (the
previous run with the same plan SHA-256 and base URL).

| Status | When |
|---|---|
| `regressed` | median > baseline x (1 + tolerance) **and** median - baseline > minimum change |
| `improved` | median < baseline x (1 - tolerance) **and** baseline - median > minimum change |
| `ok` | otherwise |
| `no_baseline` | no baseline run, or the baseline has no timing for the check |

Defaults: tolerance 20%, minimum change 50 ms, 5 samples.

## Findings

| Kind | Severity |
|---|---|
| `version_mismatch` | critical |
| `check_failed` on a critical check | critical |
| `check_failed` on another check | major |
| `regression` on a critical check | major |
| `regression` on another check | minor |

## Recommendation

1. `no_go`: version mismatch; a critical check failed; a critical check regressed.
2. `not_established`: the version couldn't be read; a critical check errored, was blocked or was skipped.
3. `go`: otherwise, worded "no failed checks and no regressions in what was checked".

Non-critical failures and regressions are findings but don't change the recommendation. Refused writes are
notes and limitations. `decision` and `decided_by` stay empty.
