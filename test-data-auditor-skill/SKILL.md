---
name: test-data-auditor
description: Audit the data tests depend on (fixtures, factories, seeds, snapshots, HTTP recordings and inline literals) for time bombs, real-looking personal data, magic ids, environment coupling, shared mutable fixtures, unseeded randomness, schema drift, snapshot rot, unused or duplicate files and locale assumptions. Every rule runs statically; time bombs, shared state and locale assumptions are then confirmed with real runs under a moved clock, reversed order or another locale. Personal data is never echoed (only file, line, field and data type), secrets are handed to code-security-auditor, and the report never calls test data "clean".
version: 1.0.0
---

# Test data auditor

## Mission

Find the test data that will break on its own, ties tests to one machine or environment, lets
tests interfere with each other, has drifted from the real schema, or shouldn't be in a
repository at all. Every finding gives the location, why it was flagged, the tests it affects, a
fix and how to check the fix.

How this differs from the other skills:

- **test-suite-evaluator** mentions test data in passing. This skill inventories every data
  source and checks each one against concrete rules.
- **flaky-test-hunter** finds shared-state flakes by running tests many times. This skill finds
  the shared data statically, confirms it with one order run, and hands it over.
- **code-security-auditor** owns secrets, so any secret found here is handed off, with its
  location and type only, and isn't rated here.
- **privacy-flow-auditor** traces personal data through product code. This skill covers the
  test side: fixtures, dumps and recordings.

Output:

1. `test_data_report.json`, conforming to `schemas/test_data_report.schema.json`.
2. The run directory, kept with the report as its evidence:
   - `inventory.json`, `candidates.json` and `drift.json`;
   - `runs.json` and the JUnit files from the experiments.
3. Proposed fixes. Fixes are applied only with `apply_fixes: true`, and then only to test data
   and test code.
4. A rendered summary for the user.

## Non-negotiable rules

- **Personal data is never echoed.**
  - **What a finding gives:** the file, line, field and data type, for example "an email
    address at a public mail domain" or "a Medicare number with a valid check digit". Never the
    value, a partial value, a masked value or a value with the separators removed.
  - **What the validator checks:** it re-reads every flagged location, then checks the report
    and every evidence file for those values.
  - **Your own text too:** the same applies to quoting a placeholder name in a dismissal.
- **"Looks real" is not "is real", and severity treats it as real.**
  - A personal-data finding is `real_looking: true`, says what made it look real, and is always
    P0.
  - A value on a reserved list (example.com, ACMA or Ofcom fiction numbers, published test
    cards, specimen IDs) or with an invalid check digit is dismissed by rule, and the dismissal
    names the rule.
  - Anything else you judge to be a placeholder is dismissed by review, with your reason.
- **A time bomb is `confirmed` only by evidence.** That means either a failing run with a moved
  clock, or date arithmetic: a literal date that the code compares with now, with the date it
  will fail on. Otherwise it is `static` or `suspected`.
- **No "clean test data" claims.** The report lists the files and formats that were scanned.
  Data that couldn't be parsed (binary, compressed or encrypted files, unsupported YAML) is
  `not_assessed`. The validator rejects "clean", "safe", "no personal data found" and similar.
- **Secrets are handed off, never rated.** A secret goes to code-security-auditor with its
  location and type. It is never dismissed and never made a finding.
- **Tools that didn't run are recorded** as `not_run` or `errored`, with what that leaves
  unchecked.
- **Fixes are proposed, not applied.** With `apply_fixes: true`, edit test data and test code
  only, rerun the affected tests, and list every changed file. Never commit, and never write
  back to Jira.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Paths | auto | Test directories, fixtures, seeds, `__snapshots__`, cassettes and recordings, mocks, mobile test assets |
| Today | the real date | The date the time-bomb arithmetic counts from; record it |
| Clock shift | `+400d` | For confirming time bombs, plus a month-end and a 29 February run |
| Markers | none | `{"anonymised_by": {"path": "reference"}}`: lowers personal data one level (Critical to Serious), is cited in the finding, and never hides it |
| Critical tests | none | Tests on journey-mapper P0/P1 journeys; they give reach 3 |
| `apply_fixes` | false | |

## Step 1: Inventory

```bash
python scripts/inventory.py <repo> --out <run>/inventory.json [--markers markers.json]
```

Every data source gets a kind (fixture, factory, seed, snapshot, recording or inline), a format,
and `loaded_by`: the tests that load it.

A test loads a source when the source's file name appears in one of four places:
- **a test function:** that test;
- **a test module outside any function:** every test in the module;
- **a conftest fixture:** the tests requesting that fixture, directly or through other fixtures;
- **a source module:** the tests in modules that import it (a seed loaded by the app, for
  example).

Binary and encrypted files are `not_assessed`.

## Step 2: Static rules

```bash
python scripts/scan_data.py <repo> --inventory <run>/inventory.json --today <date> --out <run>/candidates.json
python scripts/schema_drift.py <repo> --inventory <run>/inventory.json --out <run>/drift.json   # also folded into candidates
```

| Category | Rules |
|---|---|
| `personal_data` | Email at a public or non-reserved domain; phone outside the reserved ranges; Luhn-valid card not on a test list; Medicare, TFN or NHS number with a valid check digit; SSN or National Insurance number in valid format; IBAN with a valid checksum; full name with a street address; date of birth beside a name |
| `time_bomb` | A fixed expiry date in the future that the code compares with now, giving the date it fails on; a future literal date in a test that also reads the clock |
| `magic_id` | A lookup by a literal id, checked against the seed rows; a literal UUID; an assertion on an auto-increment id |
| `environment_coupling` | A production or staging host, a real bucket, an AWS account id, an absolute path on someone's machine |
| `shared_mutable` | A session, module, package or class-scoped fixture that returns a mutable value a test mutates |
| `nondeterministic` | `random` or Faker with no seed in the file; `uuid4()` in test data |
| `schema_drift` | A fixture field the model no longer has; a missing required field; an enum value the model doesn't allow (dataclasses, pydantic, SQLAlchemy, TypedDict, JSON Schema, OpenAPI JSON) |
| `snapshot_rot` | A snapshot with no test file; a snapshot over 500 lines |
| `unused_or_duplicate` | A data file no test loads; a file at least 90% similar to another one of the same format |
| `locale_assumption` | A test comparing a literal with a function that formats using the machine locale (`setlocale(..., "")` with `locale.currency`, `format_string` or locale-dependent `strftime`) |
| `secret` | An authorization header, an API key or password field, an AWS key, a private key, a live token: handed to code-security-auditor |

Rule settings are in `data/rules.json` and the reserved values in `data/reserved.json`.

## Step 3: Review every candidate

Each candidate ends up in exactly one of three places:

- **A finding.** Group the candidates that describe one problem, for example the fields of a
  single real-looking record. Check how the data is used: is the date really compared with now?
  Is the fixture really read by an assertion?
- **`dismissed`.** Use `by: rule` for a reserved-value dismissal, quoting the rule. Use
  `by: review` for your own judgement, with a reason, for example "a placeholder record made of
  placeholder words". Never quote the value.
- **`handoffs`.** Secrets go to code-security-auditor. Confirmed shared state goes to
  flaky-test-hunter.

## Step 4: Experiments

Run the affected tests with `run_tests.py`, and compare each experiment with green baseline runs
(label `baseline`).

```bash
python scripts/run_tests.py --preset pytest --cwd <copy> --out <run>/runs.json --label baseline --run-prefix B --repeat 2
python scripts/run_tests.py --preset custom --cmd "python -m pytest -q -p no:cacheprovider -p clock_shift --junitxml={junit}" \
    --cwd <copy> --env TDA_CLOCK_SHIFT=+400d --out <run>/runs.json --label "clock +400d" --run-prefix S
python scripts/run_tests.py --preset pytest --cwd <copy> --out <run>/runs.json --label "order reverse" --order reverse --tests <ids>
python scripts/run_tests.py --preset pytest --cwd <copy> --out <run>/runs.json --label "locale C.UTF-8" --locale C.UTF-8 --tests <ids>
```

- **Time bombs:**
  - Move the clock by the shift, to the last day of a month, and to 29 February
    (`TDA_CLOCK_DATE=2028-02-29`).
  - Prefer the repo's own clock: freezegun, time-machine, Jest fake timers or an injected
    `Clock`. Otherwise use libfaketime for whole processes, or `clock_shift.py`, which moves
    Python's `date`, `datetime` and `time` and must sit in the directory pytest starts from.
- **Shared state:** reverse or shuffle the order, and run each test in isolation.
- **Locale:** use another locale and time zone. Many runtimes on Windows ignore `LANG`, so run
  it on Linux or macOS when you can, and say when the platform changed too.

Run experiments in a scratch copy of the repo. An experiment is `failed_as_predicted` only when
both are true:
- an affected test failed in its runs;
- that test passed in the baseline runs.

## Step 5: Rank, report, validate

| Severity (computed) | When |
|---|---|
| **Critical** | Real-looking personal data (Serious with an `anonymised_by` marker); a time bomb failing within 30 days |
| **Serious** | A time bomb failing within a year; environment coupling to production; shared mutable state confirmed by an order run |
| **Moderate** | Magic ids, nondeterminism, schema drift, locale assumptions, unconfirmed shared state, time bombs further out |
| **Minor** | Snapshot rot, unused or duplicate files |

Each finding's priority is computed:
- **Score:** severity weight (4, 3, 2 or 1) × reach. Reach is 3 for 10 or more tests or a
  critical test, 2 for 2–9 tests, and 1 otherwise.
- **Bands:** P0 at 9 or more, P1 at 6, P2 at 3, otherwise P3.
- **Overrides:** real-looking personal data is always P0, and a `suspected` finding is never
  above P2.

```bash
python scripts/validate_test_data_report.py <run>/test_data_report.json --candidates <run>/candidates.json --repo <repo> \
    --runs <run>/runs.json --evidence <run> --write-summary
python scripts/render_report.py <run>/test_data_report.json [--brief]
```

Write the headline yourself, naming the P0 findings. Fix every error rather than editing
computed fields.

Show the user:
1. the headline and the earliest time-bomb date;
2. the ranked findings, each with its location, data type, why it was flagged, the experiment
   and the fix;
3. the handoffs;
4. what wasn't assessed and which tools didn't run.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, date the audit counts from, clock shift, `apply_fixes`, candidates and runs files, critical tests, platform |
| `inventory[]` | Every data source: kind, format, the tests that load it, scanned or not assessed, any `anonymised_by` |
| `tools[]` | Each tool ran, not run, errored or not applicable, and what that leaves unchecked |
| `findings[]` | Category, candidates, location, data type, why flagged, `real_looking`, confidence, experiment, fails-on date, affected tests, fix, verification step; severity, reach, score and priority are computed |
| `dismissed[]` | Each candidate set aside, by rule or by review, with the reason |
| `handoffs[]` | To code-security-auditor (secrets), flaky-test-hunter (shared state) or privacy-flow-auditor |
| `summary` | Headline, files scanned and not assessed, counts by category, priority and severity, earliest time bomb, evidence completeness |
| `limitations[]`, `files_changed[]` | What the rules and experiments can't see; files changed with `apply_fixes` |
