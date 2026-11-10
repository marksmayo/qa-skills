# Design: `test-data-auditor-skill`

Status: Designed; not built. Build order: 11.

## Mission

The skill audits the data tests depend on: fixtures, factories, seed scripts, recorded responses,
snapshots and inline literals. It finds data that will break on its own (time bombs), data that
ties tests to one environment (magic IDs, production URLs), data shared in ways that make tests
interfere, data that has drifted from the real schema, and real or real-looking personal data that
shouldn't be in a repository at all.

How it differs from the other skills:

- **test-suite-evaluator** comments on test data in passing as part of maintainability. This
  skill inventories every fixture and seed and checks each against concrete rules.
- **flaky-test-hunter** finds shared-state flakes by running tests. This skill finds the shared
  data statically, and hands suspected interference to the flake hunter to confirm.
- **code-security-auditor** owns secrets in test files. This skill hands any secret it sees to that
  skill's rules (location and type only) and doesn't rate it.
- **privacy-flow-auditor** traces personal data through product code. This skill covers the test
  side: personal data sitting in fixtures, dumps and recordings.

## Decisions

- **Static first, then experiments.** Every rule runs statically. Time bombs and interference are
  then confirmed by running the affected tests with a shifted clock or in random order.
- **Fixes are proposed, not applied, by default.** With `apply_fixes: true` the skill edits test
  data and test code only, and reruns the affected tests.
- **Synthetic replacements are proposed for personal data,** using reserved values (example.com
  addresses, reserved phone ranges, documented test card numbers) from `data/reserved.json`.
- **No commits, no Jira write-back.**

## Non-negotiable rules

- **Personal data is never echoed.** A finding gives the file, line, field and data type ("an email
  address at a public mail domain", "a Luhn-valid card number not on any published test list"),
  never the value or a partial value. Rendered output and evidence files are checked for this.
- **"Looks real" is not "is real".** The skill can't know whether a person exists. Findings say
  `real_looking` and say what made it look real. Severity treats it as real, because the
  repository can't tell either.
- **A time bomb is `confirmed` only by a run with a shifted clock that fails,** or by arithmetic on a
  literal date the code compares against now (with the date it will fail on). Otherwise it's
  `suspected`.
- **No "clean test data" claims.** The report lists which files and formats were scanned, and data
  it couldn't parse (binary dumps, encrypted fixtures) is `not_assessed`.
- **Tools that didn't run are recorded** as `not_run` or `errored`, with what that leaves unchecked.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | |
| `paths` | auto | Test directories, fixtures, seeds, `__snapshots__`, cassettes (VCR, Polly, WireMock, MSW handlers), Storybook mocks |
| `schemas` | auto | Models, OpenAPI, GraphQL, JSON Schema, migrations; used for drift checks |
| `clock_shift` | `+400d` | Shift used to confirm time bombs; plus month-end and leap-day runs |
| `apply_fixes` | false | |

## Workflow

1. **Inventory.** Find every data source tests use: fixture files (JSON, YAML, CSV, SQL, XML),
   factories (factory_boy, Fishery, FactoryBot, Kotlin and Swift builders), seed scripts, snapshot
   files, HTTP recordings, inline literals in tests, mobile fixture assets. Map each to the tests
   that load it.
2. **Static rules** (`scan_data.py`, from `data/rules.json`):

   | Category | What it finds |
   |---|---|
   | `time_bomb` | Literal dates compared with now; expiry fields (tokens, certificates, cards, subscriptions) set to a fixed date; ages computed from fixed birth dates; "next Tuesday" logic without a frozen clock |
   | `personal_data` | Emails at public mail domains, phone numbers outside reserved ranges, Luhn-valid card numbers not on test lists, government ID formats (TFN, Medicare, SSN, UK National Insurance number, NHS number) with valid check digits, IBANs with valid checksums, full name plus street address pairs, real-looking dates of birth beside names |
   | `magic_id` | Hard-coded primary keys and UUIDs that match a seed dump or an environment, tests that assume an auto-increment value |
   | `environment_coupling` | Production or staging hostnames, real bucket names, account ids, absolute local paths |
   | `shared_mutable` | Module or session-scoped fixtures mutated by tests, class-level fixture dicts, factories with global sequences that tests assert on |
   | `nondeterministic` | Faker or random without a seed in fixtures used by assertions, `uuid4()` in snapshots, generated data asserted by exact value |
   | `schema_drift` | Fixture fields the model no longer has, missing required fields, enum values no longer valid, recorded responses that no longer match the API schema |
   | `snapshot_rot` | Snapshots no test references, very large snapshots (over 500 lines) asserted wholesale |
   | `unused_or_duplicate` | Fixtures no test loads; near-identical fixtures (similarity ≥ 0.9) that could be one factory |
   | `locale_assumption` | Formatted numbers, currencies and dates as literals that depend on the machine locale |

3. **Agent review** of each candidate: is it actually used in an assertion, is the date really
   compared with now, is the email domain really public. Dismissals need a reason.
4. **Experiments** on the tests that load each confirmed-candidate fixture, via `run_tests.py`:
   - clock shifted by `clock_shift`, to the last day of the month and to 29 February (libfaketime,
     or the repo's freezegun, time-machine, Jest fake timers or `java.time.Clock` injection);
   - random order and isolation for `shared_mutable` candidates (hand off to flaky-test-hunter for
     a full investigation);
   - a different locale and time zone for `locale_assumption`.
5. **Propose fixes:** relative dates from a frozen clock, factories in place of shared dicts,
   reserved synthetic values for personal data, schema-generated fixtures, lookups by natural key
   instead of id.
6. **Rank, report, validate and render.**

## Rating

| Severity | When |
|---|---|
| **Critical** | `personal_data` that looks real (a privacy incident whatever the tests do); a `time_bomb` that will fail within 30 days |
| **Serious** | A `time_bomb` failing within a year; `environment_coupling` to production; `shared_mutable` confirmed by an order run |
| **Moderate** | `magic_id`, `nondeterministic`, `schema_drift`, `locale_assumption` |
| **Minor** | `snapshot_rot`, `unused_or_duplicate` |

Confidence: `confirmed` (an experiment failed as predicted, or exact date arithmetic), `static` (a
rule matched and the agent checked the usage), `suspected` (agent judgment alone, never high).

Priority: `score = severity weight (4/3/2/1) x reach`, where reach is 3 when the data feeds 10 or
more tests or a journey-mapper P0/P1 journey, 2 for 2–9 tests, 1 for one. Bands: P0 ≥ 9, P1 ≥ 6,
P2 ≥ 3, otherwise P3. Real-looking personal data is always P0, whatever its reach.

## Output (`test_data_report.json`)

```text
metadata:    repo, commit, generated_at, tool versions, clock_shift
inventory[]: {path, kind: fixture|factory|seed|snapshot|recording|inline, format, loaded_by[],
              status: scanned|not_assessed, reason?}
tools[]:     {name, version, status, reason}
findings[]:  {id, category, title, location{file, line, field}, data_type?, why_flagged,
              severity, confidence, experiment?{run_ids, shift_or_variation, result},
              fails_on_date?, affected_tests[], fix, verification_step, score, priority}
dismissed[]: {candidate_id, reason}
handoffs[]:  {to: flaky-test-hunter|code-security-auditor, location, reason}
summary:     {files_scanned, not_assessed, findings_by_category{}, by_priority{},
              earliest_time_bomb_date, evidence_completeness}
limitations[], files_changed[]
```

## Validator fails when

- Any personal-data value, or a substring of one, appears in the report or evidence (the validator
  re-reads the flagged locations to check, as the security auditor does for secrets).
- A `time_bomb` is `confirmed` without a failing shifted-clock run or a `fails_on_date`.
- A candidate is neither promoted nor dismissed, or a dismissal has no reason.
- Real-looking personal data is ranked below P0.
- A secret is rated here rather than handed off.
- Report text says the test data is "clean" or "safe".

## Edge cases

- **Large SQL or JSON dumps.** Scanned in a streaming pass; a dump over 50 MB is sampled and the
  report says so.
- **Production data copied into fixtures on purpose** ("anonymised"). Still flagged when values look
  real; the user can mark the file `anonymised_by` with a reference, which lowers severity by
  one level (Critical to Serious) and is cited in the finding, but never hides it.
- **Recorded HTTP cassettes** containing auth headers go to the security auditor; personal data in
  bodies stays here.
- **Mobile fixture assets** (JSON in `androidTest/assets`, iOS bundles) are scanned the same way.
- **Binary or encrypted fixtures** are `not_assessed`.

## Tool stack

Stdlib Python for the scan, date arithmetic, Luhn and checksum checks, and similarity. Optional and
open source: libfaketime (GPL-2.0, run as a separate tool, not linked), the repo's own clock library,
detect-secrets (Apache-2.0) only to route secrets to the security auditor. Versions are recorded.

## Files

```text
test-data-auditor-skill/
  SKILL.md  RUBRIC.md  README.md
  data/rules.json  reserved.json     # reserved domains, phone ranges, test card numbers, ID formats
  schemas/test_data_report.schema.json
  scripts/inventory.py  scan_data.py  schema_drift.py  validate_test_data_report.py  render_report.py
          run_tests.py  parse_results.py  detect_conventions.py   # shared copies
  examples/clinic-api/  run/  test_data_report.example.json
```

## Example

`examples/clinic-api/` is a small Flask-style API with a pytest suite and JSON, YAML and SQL
fixtures. All planted personal data is invented for the example; none of it belongs to a real
person, and the README says so. The answer key:

| Planted | Expected |
|---|---|
| A patient fixture with a public-mail email and a valid-format Medicare number | `personal_data`, Critical, P0, value never shown |
| A membership expiry of a fixed date 20 days after the example's run date | `time_bomb`, Critical, confirmed by the shifted run |
| Tests that look up "patient 42" from the seed dump | `magic_id` |
| A session-scoped fixture dict that one test appends to | `shared_mutable`, confirmed in random order, handed off |
| An appointment fixture with a `room` field the model dropped | `schema_drift` |
| A fee formatted as `"1.234,50"` | `locale_assumption` |
| An `example.com` address and a reserved phone number | Not flagged (the dismissal shows the rule) |

## Resolved questions

Agreed with the user before the build.

1. **Horizon for a Critical time bomb?** 30 days.
2. **May an `anonymised_by` marker lower severity?** Yes, by one level, and the finding cites the marker.
3. **Which national ID formats?** TFN, Medicare, SSN, UK National Insurance number and NHS number.
