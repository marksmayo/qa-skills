# Test data auditor rubric

Rules for `test_data_report.json`. The validator enforces everything marked (computed) and every
"must".

## Triage

Every candidate in `candidates.json` must appear exactly once, in one of these places:

| Where | When |
|---|---|
| a finding's `candidates` | It is a real problem. Group the candidates that describe one problem, such as the fields of one record |
| `dismissed`, `by: rule` | `scan_data.py` set `auto_dismiss` (a reserved value, an invalid check digit, a placeholder digit pattern, or a placeholder secret). The reason quotes that rule |
| `dismissed`, `by: review` | Your judgement (a placeholder record, a date nothing compares with now), with a reason of 15+ characters that doesn't quote the value. `not_personal_data: true` (personal-data candidates only) says the value is not personal data at all, such as a dated run id; it lifts the leak check for that candidate |
| `handoffs` | Secrets go to code-security-auditor, unless `scan_data.py` marked the value an obvious placeholder (then they may be dismissed by rule). A secret is never dismissed by review. A confirmed `shared_mutable` finding must also be handed to flaky-test-hunter |

A finding's candidates must all be its category, and the finding's file must be one of their
files. A candidate dismissed by rule must never be promoted.

## Findings no candidate covers

A finding from the tools cites at least one candidate. A problem you found by reading or by an
experiment sets `source: "agent"`, may have no candidates, and must carry a `basis`:

| Basis | The validator checks |
|---|---|
| `{"file", "line", "excerpt"}` | With `--repo`: the file exists, has that line, and the excerpt (whitespace normalised) is on it. The excerpt never holds six or more digits, a quoted number of six or more digits that isn't a date, or a non-reserved email address |
| `{"evidence": "<path>"}` | The file exists under `--evidence` |

The finding's location must be one of its basis files (when it cites any). A confirmed time bomb
found this way names its `fails_on_date`, and still needs a failing experiment: date arithmetic
counts only where `scan_data.py` computed the fail date. A `residue` finding cites the
`residue.py` diff as evidence, and that diff is what confirms it.

## Severity (computed)

| Category | Severity |
|---|---|
| `personal_data` | Critical; Serious with an `anonymised_by` marker |
| `time_bomb` | Days from the audit date to `fails_on_date` (or the fixed date): ≤ 30 Critical, ≤ 365 Serious, otherwise Moderate |
| `environment_coupling` | Serious for a production or live host (by name, or a review finding with `live_endpoint: true` whose basis names the host the tests reach), otherwise Moderate |
| `shared_mutable` | Serious when confirmed, otherwise Moderate |
| `residue` | Serious when the files land in a served directory (`public/`, `static/`, `www/`, `wwwroot/`, `htdocs/`, `uploads/`), otherwise Moderate |
| `magic_id`, `nondeterministic`, `schema_drift`, `locale_assumption` | Moderate |
| `snapshot_rot`, `unused_or_duplicate` | Minor |

## Priority (computed)

`score = severity weight × reach`

- **Weights:** Critical 4, Serious 3, Moderate 2, Minor 1.
- **Reach:** 3 for 10 or more affected tests or any critical test, 2 for 2–9, and 1 otherwise.

| Priority | Score |
|---|---|
| **P0** | ≥ 9, and always for real-looking personal data |
| **P1** | ≥ 6 |
| P2 | ≥ 3 |
| P3 | < 3 |

A `suspected` finding is never above P2, except personal data: the personal-data rule wins, so a
personal-data finding is P0 whatever its confidence. Doubt about whether it is personal data at all
is settled by a review dismissal, not by a lower priority.

## Confidence

| Confidence | Needs |
|---|---|
| `confirmed` | An experiment with result `failed_as_predicted`: an affected test failed in its runs, passed in the named baseline runs, and every run is in `runs.json`. No cited run may be errored (no tests parsed, a timeout, or an exit code other than 0 or 1, such as a crashed interpreter or a pytest usage error): such an experiment's result is `errored`. The variation text must match what the runs recorded (reverse, shuffle, locale, time zone, isolation). With `cwd`, run case ids are made repo-relative before matching. A time bomb can also be confirmed by date arithmetic: the candidate found the comparison with now, and `fails_on_date` equals the date `scan_data.py` computed |
| `static` | A rule matched, and you checked how the data is used |
| `suspected` | Your judgement alone |

For `>=` and `<=` comparisons with today, the fail date is the day after the fixed date. For `>`
and `<` it is the fixed date itself.

## Personal data

- **Every finding:**
  - `real_looking: true`;
  - names what made it look real;
  - gives file, line, field and data type only.
- **What the validator re-reads:** every personal-data and secret candidate that no rule
  dismissed. It fails when either of these contains one of those values, or six consecutive
  digits of a number, or the number without its separators:
  - the report;
  - any evidence file (`--evidence`).
- **What counts as a value:** the span on the candidate's line whose fingerprint is the
  candidate's (never every number on that line); for a `customer_extract` column, its values of two
  or more words or eight or more characters. Known repository paths are blanked before matching, and
  `candidates.json` is not scanned. The whole email address always counts; its local part alone only when it
  reads as a person's name (`first.last`, `j_smith`), never a single word such as `staff` or
  `bookings`. Each error names the candidate whose value leaked, never the value.
  `redact_evidence.py` removes exactly these from runner output.

## Evidence completeness (computed)

The mean of three shares:
- the inventory files scanned (not `not_assessed`);
- the confirmable findings (time bomb, shared mutable, locale) whose experiment ran;
- the applicable tools that ran.

With no confirmable findings, the second share is left out and the mean is of the other two:
nothing to confirm is not evidence that experiments ran.

## Honesty rules

- **The headline:** it names the P0 findings when there are any. A finding count in it must
  equal the report's, and it never says "no findings" when there are some.
- **No clean-data claims:** the validator rejects these and similar:
  - "the test data is clean" or "safe";
  - "clean data";
  - "no personal data found";
  - "free of personal data";
  - "privacy-safe";
  - "GDPR-compliant";
  - "nothing sensitive".
- **Changed files:** `files_changed` is empty unless `apply_fixes` is true.
