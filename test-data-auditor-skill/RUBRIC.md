# Test data auditor rubric

Rules for `test_data_report.json`. The validator enforces everything marked (computed) and every
"must".

## Triage

Every candidate in `candidates.json` must appear exactly once, in one of these places:

| Where | When |
|---|---|
| a finding's `candidates` | It is a real problem. Group the candidates that describe one problem, such as the fields of one record |
| `dismissed`, `by: rule` | `scan_data.py` set `auto_dismiss` (a reserved value, an invalid check digit, a placeholder digit pattern, or a placeholder secret). The reason quotes that rule |
| `dismissed`, `by: review` | Your judgement (a placeholder record, a date nothing compares with now), with a reason of 15+ characters that doesn't quote the value |
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
| `environment_coupling` | Serious for a production or live host, otherwise Moderate |
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

A `suspected` finding is never above P2.

## Confidence

| Confidence | Needs |
|---|---|
| `confirmed` | An experiment with result `failed_as_predicted`: an affected test failed in its runs, passed in the named baseline runs, and every run is in `runs.json`. A time bomb can also be confirmed by date arithmetic: the candidate found the comparison with now, and `fails_on_date` equals the date `scan_data.py` computed |
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
- **What counts as a value:** the whole email address always; its local part alone only when it
  reads as a person's name (`first.last`, `j_smith`), never a single word such as `staff` or
  `bookings`. Each error names the candidate whose value leaked, never the value.
  `redact_evidence.py` removes exactly these from runner output.

## Evidence completeness (computed)

The mean of three shares:
- the inventory files scanned (not `not_assessed`);
- the confirmable findings (time bomb, shared mutable, locale) whose experiment ran;
- the applicable tools that ran.

## Honesty rules

- **The headline:** it names the P0 findings when there are any.
- **No clean-data claims:** the validator rejects these and similar:
  - "the test data is clean" or "safe";
  - "clean data";
  - "no personal data found";
  - "free of personal data";
  - "privacy-safe";
  - "GDPR-compliant";
  - "nothing sensitive".
- **Changed files:** `files_changed` is empty unless `apply_fixes` is true.
