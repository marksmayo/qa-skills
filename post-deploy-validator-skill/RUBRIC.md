# Rubric: post-deploy validator

## Check results

| Status | When |
|---|---|
| `pass` | every assertion held (HTTP status, contains, not contains, JSON, headers, redirects) |
| `fail` | the application answered and an assertion didn't hold |
| `error` | no response (connection failed, timed out), or the target refused it: a 429, or a 403/503 with a WAF or rate-limit signature |
| `blocked` | not sent: not a read, or its credential set couldn't sign in |
| `skipped` | not sent: the run stopped (a refusal, or three 5xx answers in a row; a 4xx doesn't reset the count, a 2xx or 3xx does) |

Assertion kinds: `http_status` (`expect.status`), `body` (`contains`, `not_contains`), `json`, `header`
(`present`, `absent`, `equals`, `contains`), `redirect` (`no_redirect`, `final_path`, `not_login`: a check
without a credential set that lands on a sign-in page fails unless `allow_login_redirect`; `same_origin`:
a redirect off the site is not followed and fails).

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
| `not_comparable` | this run or the baseline timed a failing response (status not as expected, or not 2xx without an expectation); such a check is sent once, never sampled |

Defaults: tolerance 20%, minimum change 50 ms, 5 samples.

`baseline_relation`: `different_deployment` (or `different_version`) is a release comparison;
`same_deployment` (or `same_version` with no deployment id) is drift on one deployment, so its regressions
are findings but not `no_go` reasons; `unknown` when one side has neither.

## Findings

| Kind | Severity |
|---|---|
| `version_mismatch` | critical |
| `check_failed` on a critical check | critical |
| `check_failed` on another check | major |
| `regression` on a critical check | major |
| `regression` on another check | minor |
| `header_inconsistent`: page checks answered with different Content-Security-Policy values (nonces ignored; a missing policy counts as a value) | minor |

## Recommendation

1. `no_go`: version mismatch; a critical check failed; a critical check regressed against another deployment.
2. `not_established`: the version (and the deployment id) couldn't be read; a critical check errored, was
   blocked or was skipped; the target rate-limited or WAF-blocked the run.
3. `go`: otherwise, worded "no failed checks and no regressions in what was checked".

Non-critical failures and regressions are findings but don't change the recommendation. Refused writes are
notes and limitations. `decision` and `decided_by` stay empty.

## Wording the validator holds the report to

- The headline names the recommendation (`no-go`, `not established`, `go`) and says nothing that reads as
  another one ("ship it", "go-ish", "proceed", "looks good" over a `no_go`).
- Never "healthy", "safe to release", "good to go", "nothing (else) is wrong", "in good shape".
- A critical or major finding is never titled minor or cosmetic, a minor one never critical; a failed
  check's title keeps the HTTP status it answered when that wasn't 2xx. Evidence and notes are kept as
  `recommend.py` derives them.
