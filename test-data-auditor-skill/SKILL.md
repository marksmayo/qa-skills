---
name: test-data-auditor
description: Audit the data tests depend on (fixtures, factories, seeds, snapshots, HTTP recordings and inline literals, in Python and JavaScript/TypeScript suites) for time bombs, real-looking personal data, magic ids, environment coupling, shared mutable fixtures, unseeded randomness, schema drift, snapshot rot, unused or duplicate files, locale assumptions and files tests leave behind. Every rule runs statically; time bombs, shared state and locale assumptions are then confirmed with real runs under a moved clock, reversed order or another locale. Personal data is never echoed (only file, line, field and data type), secrets are handed to code-security-auditor, and the report never calls test data "clean".
version: 1.2.0
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
   - `runs.json` and the JUnit files from the experiments, redacted with `redact_evidence.py`;
   - `residue.json` when a run left files in the working tree.
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
  location and type, and is never made a finding. The one exception is a value `scan_data.py`
  marks as an obvious placeholder: it contains a word such as test, fake, example or changeme,
  names what it stands for (`google-access-token`), or has too little entropy. That is dismissed
  by rule, quoting the rule. Every other secret is handed off, never dismissed.
- **Tools that didn't run are recorded** as `not_run` or `errored`, with what that leaves
  unchecked.
- **Fixes are proposed, not applied.** With `apply_fixes: true`, edit test data and test code
  only, rerun the affected tests, and list every changed file. Never commit, and never write
  back to Jira.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Paths | auto | Test directories (`tests/`, `fixtures/`, `testdata/`, `test-data/`, `sample-data/` and the like), seeds, `__snapshots__`, cassettes and recordings, mocks, mobile test assets |
| Data dirs | none | `--data-dir <dir>` (repeatable) for a tree of test data under a name the inventory doesn't know |
| Today | the real date | The date the time-bomb arithmetic counts from; record it |
| Clock shift | `+400d` | For confirming time bombs, plus a month-end and a 29 February run |
| Markers | none | `{"anonymised_by": {"path": "reference"}}`: lowers personal data one level (Critical to Serious), is cited in the finding, and never hides it |
| Critical tests | none | Tests on journey-mapper P0/P1 journeys; they give reach 3 |
| `apply_fixes` | false | |

## Step 1: Inventory

```bash
python scripts/inventory.py <repo> --out <run>/inventory.json [--markers markers.json] [--data-dir <dir> ...]
```

Every data source gets a kind (fixture, factory, seed, snapshot, recording, inline or support), a
format, and `loaded_by`: the tests that load it. `support` is setup and helper code the tests
share, scanned like test code but never counted as a test:

- a runner's setup file (`vitest.setup.ts`, `jest.setup.js`, `setupTests.ts`): loaded by every
  test file in its package;
- a Playwright setup project (`auth.setup.ts`): loaded by the specs beside it;
- helpers in a test area (`__tests__/helpers/`, `e2e/helpers.ts`, `test-support/`): loaded by the
  tests that import them.

A test loads a source when the source's file name appears in one of four places:
- **a test function:** that test;
- **a test module outside any function:** every test in the module;
- **a conftest fixture:** the tests requesting that fixture, directly or through other fixtures;
- **a source module:** the tests in modules that import it (a seed loaded by the app, for
  example).

How a mention is matched, so reach isn't overstated in a monorepo:

- the repo path, or its tail (`orders/export.csv`, also as `Path(DIR) / "orders" / "export.csv"`),
  always counts;
- a bare file name counts from code in the same package root (the nearest `pyproject.toml`,
  `package.json` and the like) when no other file there has that name, or when the module also
  names the directory. Data in an enclosing root (a repo-level `test-data/`) needs the directory
  named too. A file name in another service never counts;
- a glob (`DIR.glob("*.yaml")`) counts when the module names the directory, and so does code that
  names the directory alone (`seeds/families`) and none of its files: a loader of what is there;
- helper functions in a test module (a loader used by `parametrize`) count for every test in it.

Binary and encrypted files, and images, fonts and media (PNG, OTF, MP4), are `not_assessed`.
SVG is text, and is scanned. An `.xls` file is sniffed: report tools save HTML tables and XML
spreadsheets with that extension, and those are scanned as text (HTML tables as rows under their
header). `.xlsx` workbooks are read with stdlib `zipfile` (shared and inline strings); their line
numbers count lines of the text form that `python scripts/datafiles.py <file> --text` prints. A
data migration that seeds rows (`versions/007_seed_rooms.py`) is a seed, and is never "unused".

## Step 2: Static rules

```bash
python scripts/scan_data.py <repo> --inventory <run>/inventory.json --today <date> --out <run>/candidates.json
python scripts/schema_drift.py <repo> --inventory <run>/inventory.json --out <run>/drift.json   # also folded into candidates
```

| Category | Rules |
|---|---|
| `personal_data` | A tabular extract (CSV, TSV, HTML or `.xlsx` sheet, JSON list) with 20 or more rows whose column is named for a person or customer (`customer_name`, `client`, `Cust Name`) and holds mostly names: judged by the header and the row count, reported by file, header line and column only (`customer_extract`; extend `person_columns` in `data/rules.json` for a repo's own abbreviations); email at a public or non-reserved domain; phone outside the reserved ranges; Luhn-valid card not on a test list (dates, times, version strings and epoch timestamps are never phones or cards); Medicare, TFN or NHS number with a valid check digit; SSN or National Insurance number in valid format; IBAN with a valid checksum; full name with a street address; date of birth beside a name |
| `time_bomb` | A fixed expiry date in the future that the code compares with now, giving the date it fails on; a future literal date in a test that also reads the clock |
| `magic_id` | A lookup by a literal id where a database or seed is in reach (the test loads a seed, has a database or integration marker, requests a database fixture, or sits in an integration tree), checked against the seed rows; not a large id in a "not found" test, nor an all-nines sentinel; a literal UUID; an equality assertion on an auto-increment id (`x.id == 3`, not `x.id > 0`) |
| `environment_coupling` | A production or staging host, a real bucket, an AWS account id, an absolute path on someone's machine (platform roots such as `/home/site/wwwroot` or a CI runner's home are the same everywhere, so they are not) |
| `shared_mutable` | A session, module, package or class-scoped fixture that returns a mutable value a test mutates; `importlib.reload(module)` in test code, with the module's import-time environment reads (`module_reload`): later tests see the reloaded state, so a test can pass only after it |
| `nondeterministic` | `random` or Faker with no seed in the file; `uuid4()` in test data |
| `schema_drift` | A fixture field the model no longer has; a missing required field; an enum value the model doesn't allow (dataclasses, pydantic, SQLAlchemy, TypedDict, JSON Schema, OpenAPI JSON). Only models from the fixture's own package root count, and a record matches a model only when half its keys are the model's fields |
| `snapshot_rot` | A snapshot with no test file; a snapshot over 500 lines |
| `unused_or_duplicate` | A data file no test loads; a file at least 90% similar to another one of the same format |
| `locale_assumption` | A test comparing a literal with a function that formats using the machine locale (`setlocale(..., "")` with `locale.currency`, `format_string` or locale-dependent `strftime`; in JS, `toLocaleString()`, `toLocaleDateString()`, `Intl.NumberFormat()` and the like with no locale argument) |
| `residue` | Files a test run leaves in the working tree (uploads, exports, databases), found with `residue.py` (Step 4) |
| `secret` | An authorization header; an API key or password field; an API key or subscription-key header (`x-api-key`, `Ocp-Apim-Subscription-Key`, `x-functions-key`); a 32-64 character hex value under a key, token or secret field; an AWS key, a private key, a live token: handed to code-security-auditor with location and type only, unless it is an obvious placeholder |

Rule settings are in `data/rules.json` and the reserved values in `data/reserved.json`.

**JavaScript and TypeScript.** `scripts/jsparse.py` tokenizes JS and TS: strings, template
literals, comments and regex literals. Values in object literals keep their field names, so
`expiresAt: new Date('2026-12-31')` is the `expiresAt` field holding a date. Object literals get
the same field-aware rules as JSON and Python records: a name with an address, a date of birth
beside a name, phone and email fields. The JS/TS rules:

- **`time_bomb`:**
  - an expiry-like field (`expiresAt`, `trialEndDate`, `certificationExpiresAt`) set to a future
    date, with the product line that compares that field with `Date.now()`, `new Date()` or a
    variable holding either;
  - a future literal date in a test file that doesn't freeze the clock (`vi.useFakeTimers`,
    `setSystemTime`, MockDate), when the file reads the clock or product code compares that field
    with now;
  - date calls count: `new Date('...')`, `new Date(2027, 4, 12)` (months count from 0), `Date.UTC`,
    dayjs, moment, `parseISO`, `DateTime.fromISO`.
- **`magic_id`:** literal UUIDs; `getById(42)`, `findOne(7)`, `where: { id: 7 }`; `expect(x.id).toBe(1)`.
- **`nondeterministic`:** `Math.random()` or faker with no seed or stub in the file; `randomUUID()`,
  uuid v4 and nanoid in fixtures and factories.
- **`shared_mutable`:** a module-level array, object, Map or Set that tests change and no
  `beforeEach` or `afterEach` resets.
- **`locale_assumption`:** a literal compared with one of these, when it formats with the machine
  locale: a direct call, a product function, or a rendered component.

**Numbers that aren't personal data.**

- **Skipped by the phone and card rules:**
  - dates and times, including invalid dates in date-validation tests;
  - version strings and epoch timestamps;
  - SSN-shaped strings, which the SSN rule handles;
  - values under identifier, code, password, amount or geometry fields (`meetingId`, `pin`,
    `viewBox`);
  - UUIDs: a UUID is never a card number, even when it passes Luhn;
  - compact date-times and dated run ids (`20261103-142530`, `20261103-001`), digit runs inside
    a path or identifier (`runs/20261103-142530/`, `key-0000...`), and runs of one or two
    repeated digits;
  - a bare string of digits with no phone field and no separators or `+`: an order number or an
    id far more often than a phone. Under a phone field it is still checked.
- **Dismissed by rule as a placeholder pattern:** a phone whose last seven digits use one or two
  digit values (`1111112`, `0000004`), or run in sequence (`1234567`, `9876543`).
- **Fiction ranges:**
  - The NANP fiction range 555-0100 to 555-0199 is reserved with or without an area code.
  - New Zealand has no published fiction range, and `reserved.json` records that check. A NZ
    number with 555 in it is dismissed by review only, with your reason.

## Step 3: Review every candidate

Each candidate ends up in exactly one of three places:

- **A finding.** Group the candidates that describe one problem, for example the fields of a
  single real-looking record. Check how the data is used: is the date really compared with now?
  Is the fixture really read by an assertion?
- **`dismissed`.** Use `by: rule` for a reserved-value dismissal, quoting the rule. Use
  `by: review` for your own judgement, with a reason, for example "a placeholder record made of
  placeholder words". Never quote the value. When a personal-data candidate is not personal data
  at all (a dated run id read as a phone, an order number), add `"not_personal_data": true`: the
  validator then stops protecting its value, so a path or id that contains it doesn't fail the
  leak check. A placeholder name is still personal-data shaped: leave the flag off for it.
- **`handoffs`.** Secrets go to code-security-auditor. Confirmed shared state goes to
  flaky-test-hunter.

**Problems no candidate covers.** You may find a problem by reading, or by an experiment: a time
bomb a moved clock exposed, or files a run left behind. Report it as a finding with
`source: "agent"` and no candidates. It carries a `basis`:

- `{"file", "line", "excerpt"}` for code you read. With `--repo`, the validator re-reads the line and
  checks the excerpt is on it.
- `{"evidence": "<path in the run dir>"}` for a run artefact, such as `residue.json` or a JUnit file.

An excerpt quotes the code around a value, never the value. The validator rejects an excerpt that
holds six or more digits or a non-reserved email address. A confirmed time bomb found this way:

- names its `fails_on_date`, bracketed with dated runs;
- cites those runs in its experiment.

The renderer labels these findings "found by review".

## Step 4: Experiments

Run the affected tests with `run_tests.py`, and compare each experiment with green baseline runs
(label `baseline`). Confirm on the affected test files only, with two baselines. A whole-suite run
on a busy machine times out and makes a poor baseline.

**A crashed or empty run is not a result.** A run that parsed no tests, timed out, or exited with
a code other than 0 or 1 (pytest 2 to 5 for an interrupted, internal, usage or no-tests error; a
crashed interpreter's 3221225725 or a negative signal code) can be recorded as "0 passed, 0
failed". It is errored: the validator rejects an experiment that cites one as a baseline or as
evidence, so set the experiment's result to `errored`, find out why, and rerun. Check also that
the option you meant reached the runner: the validator compares the experiment's `variation` text
(reverse, shuffle, locale, time zone, isolation) with what `runs.json` recorded.

**Monorepos and packages.** Run each package from its own directory with its own environment
(`--env PYTHONPATH=...`, one `run_tests.py` call per package): two services that each define a
top-level `shared` package collide when installed together. Run case ids are relative to `--cwd`;
when that is a subdirectory, give the experiment `"cwd": "<repo-relative dir>"` so the validator
matches its cases to the repo-relative ids in `affected_tests`. With `--order reverse`, start
pytest in its rootdir (the directory of `pytest.ini`) and pass test ids relative to it: ids built
from an ancestor rootdir don't resolve from a package directory, and the run exits 4.

Before validating, redact the evidence:

```bash
python scripts/redact_evidence.py <run> --candidates <run>/candidates.json --repo <repo> [--report <run>/test_data_report.json]
```

Runners copy test data into their output: Vitest and Jest put it in test names and failure
messages. The validator rejects any evidence file that holds a flagged value. The script uses the
validator's own matching, and prints only counts and candidate ids. Both take each candidate's
value from the span on its line whose fingerprint is the candidate's (not every number on the
line), leave repository paths alone, and skip `candidates.json`, which holds fingerprints and
never values.

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
  - Prefer the repo's own clock: freezegun, time-machine or an injected `Clock`. Fake timers in Jest
    and Vitest are per test, so they can't shift a whole suite. Otherwise use one of these:
    - libfaketime, for whole processes;
    - `clock_shift.py`, which moves Python's `date`, `datetime` and `time`, and must sit in the
      directory pytest starts from. It replaces the `date` and `datetime` classes when it loads,
      and a C-extension library imported after that (pandas with numpy) can crash the
      interpreter with no traceback (Windows exit 3221225725). If the package imports pandas,
      load a small wrapper plugin that imports pandas first and then `clock_shift`, and treat a
      run with no parsed tests as errored;
    - for Node runners (Vitest, Jest), `scripts/clock_shift.mjs`, which replaces `Date` for the whole
      run, with the same `TDA_CLOCK_SHIFT` and `TDA_CLOCK_DATE` settings. Load it with
      `--env NODE_OPTIONS=--import=file:///<abs path>/clock_shift.mjs` (it reaches Vitest's workers),
      or first in `setupFiles` of a scratch config that extends the repo's. Check it with
      `node scripts/clock_shift.mjs --self-test`. It doesn't change the time zone (use `--tz`), and a
      test that installs fake timers replaces it while they are on.
  - Bracket the first failing day with dated runs, and report that date as `fails_on_date`.
  - Where the repo's test command sets flags or a time zone (a `package.json` script such as
    `TZ=... vitest --configLoader native`), run it with `--npm-script <name>` so the experiment
    uses the same environment, and pass extra flags with `--runner-args`. The `vitest` preset
    re-runs single tests by id (file plus `-t`).
- **Shared state:** reverse or shuffle the order, and run each test in isolation. The `vitest`
  preset shuffles (`--order shuffle --seed N`, with two seeds) but can't reverse.
- **Residue:** snapshot the copy before the runs, and diff it after:

  ```bash
  python scripts/residue.py snapshot <copy> --out <run>/tree.before.json
  python scripts/residue.py diff <copy> --before <run>/tree.before.json --out <run>/residue.json
  ```

  Files added or changed outside the ignored build and dependency directories are a `residue`
  finding, with `source: agent` and basis `{"evidence": "residue.json"}`.
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
| **Serious** | A time bomb failing within a year; environment coupling to production; shared mutable state confirmed by an order run; residue in a served directory (`public/`, `static/`, `uploads/`) |
| **Moderate** | Magic ids, nondeterminism, schema drift, locale assumptions, unconfirmed shared state, time bombs further out, residue outside a served directory |
| **Minor** | Snapshot rot, unused or duplicate files |

Each finding's priority is computed:
- **Score:** severity weight (4, 3, 2 or 1) × reach. Reach is 3 for 10 or more tests or a
  critical test, 2 for 2–9 tests, and 1 otherwise.
- **Bands:** P0 at 9 or more, P1 at 6, P2 at 3, otherwise P3.
- **Overrides:** real-looking personal data is always P0, and a `suspected` finding is never
  above P2. The personal-data rule wins: a personal-data finding is P0 whatever its confidence.
  If you doubt the data is personal at all, dismiss it by review with your reason instead.

```bash
python scripts/validate_test_data_report.py <run>/test_data_report.json --candidates <run>/candidates.json --repo <repo> \
    --runs <run>/runs.json --evidence <run> --write-summary
python scripts/render_report.py <run>/test_data_report.json [--brief]
```

`--brief` prints the findings, and one line per handoff recipient with the files that hold the
most handoffs. A leak error names the candidate whose value leaked, never the value.

Write the headline yourself, naming the P0 findings. Fix every error rather than editing
computed fields. The validator checks a finding count in the headline against the report. With
no findings, say what was read rather than what wasn't found, for example: "No findings from the
rules or the runs: 16 inline test files scanned, no fixture files; no experiment was needed."
Evidence completeness then leaves out the experiment share (there was nothing to confirm) rather
than counting it as complete.

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
| `findings[]` | Category, candidates (or `source: agent` with a `basis`), location, data type, why flagged, `real_looking`, confidence, experiment, fails-on date, affected tests, fix, verification step; severity, reach, score and priority are computed |
| `dismissed[]` | Each candidate set aside, by rule or by review, with the reason |
| `handoffs[]` | To code-security-auditor (secrets), flaky-test-hunter (shared state) or privacy-flow-auditor |
| `summary` | Headline, files scanned and not assessed, counts by category, priority and severity, earliest time bomb, evidence completeness |
| `limitations[]`, `files_changed[]` | What the rules and experiments can't see; files changed with `apply_fixes` |
