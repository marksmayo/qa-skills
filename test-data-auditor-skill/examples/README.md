# Test data auditor example

`clinic-api/` is a small booking package for a physiotherapy clinic, with a pytest suite and
JSON, YAML, CSV and SQL fixtures. **Every personal detail in it is invented.** The
"real-looking" patient record was made up to trip the rules and belongs to no real person.

`run/` holds the real run, step by step in `run/commands.sh`:

| File | From |
|---|---|
| `inventory.json` | `inventory.py` |
| `candidates.json`, `drift.json` | `scan_data.py --today 2026-11-11`, `schema_drift.py` |
| `runs.json`, `junit/` | `run_tests.py`: 2 baselines, clock +400 days, 2027-01-31 and 2028-02-29 (`clock_shift.py`), reverse order, isolation (Windows, Python 3.14), and a `C.UTF-8` locale run (WSL Ubuntu, Python 3.12) |

## Answer key

| Planted | Result |
|---|---|
| Patient 1: email at a public mail domain, Medicare number with a valid check digit, name with a street address, date of birth | F1 `personal_data`, Critical, P0, four candidates in one finding, no value in the report |
| Membership expiry fixed at 2026-12-01, compared with `date.today()` using `>=` | F2 `time_bomb`, Critical (fails 2026-12-02, 21 days after the audit), confirmed by the +400d, month-end and leap-day runs; P1 (reach 2) |
| Session-scoped `waitlist` list appended to by one test, expected empty by another | F3 `shared_mutable`, Serious, confirmed by the reverse-order run (each test passes alone), handed to flaky-test-hunter |
| `appointments.yaml` keeps a `room` field the model dropped | F6 `schema_drift`, P2 |
| A test expects `"$1,234.50"` from `locale.currency` after `setlocale(LC_ALL, "")` | F4 `locale_assumption`, confirmed: the `C.UTF-8` run fails |
| `get_patient(42)`, which only exists in `db/seed.sql` | F5 `magic_id` |
| `legacy_rooms.csv`, which nothing loads | F7 `unused_or_duplicate` |
| A bearer token in the recorded insurer request (`EXAMPLE-NOT-A-REAL-TOKEN-0000`, deliberately fake and low-entropy so secret scanners on this repository don't flag it) | dismissed by rule as a placeholder secret (the value says it is an example); a secret that isn't an obvious placeholder is handed to code-security-auditor, with no value shown |
| Patient 2: an `example.com` address and two ACMA fiction numbers | dismissed by rule, naming the rule |
| Patient 2's placeholder name, address and date of birth | dismissed by review |

The factory uses `random.Random(7)`, so the nondeterminism rule doesn't flag it.

Everything in `run/` is real output. Hostnames in the JUnit files were replaced with `host`.
Editing `clinic-api/` changes the candidates' lines and invalidates the example until the run
is repeated. The time-bomb figures count from the audit date recorded in the report
(2026-11-11), not from the day you read this.
