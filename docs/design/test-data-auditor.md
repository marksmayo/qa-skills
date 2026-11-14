# Design: `test-data-auditor-skill`

Status: Built (v1.5.0), see "As built" at the end. Build order: 11.

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

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Candidates hold no values.**
  - **What `scan_data.py` writes:** the file, line, field, data type, why the candidate was
    flagged, and a 16-character SHA-256 fingerprint of the normalised value.
  - **Rule dismissals:** values on `data/reserved.json`'s lists, or with an invalid check digit,
    are dismissed by rule, and the rule is quoted.
  - **The validator's check:** it re-reads each remaining personal-data and secret location and
    fails when the report or any evidence file contains:
    - the value;
    - the number without its separators;
    - six consecutive digits of it.
  - **First catch:** on the first example report it caught my own dismissal reason quoting a
    placeholder name.
- **Secrets** come from a small set of patterns in `data/rules.json`: authorization headers,
  API key and password fields, AWS keys, private keys and live tokens. detect-secrets is
  optional and wasn't installed. Every secret must be handed to code-security-auditor; it can't
  be dismissed or made a finding.
- **Inventory mapping.** A data file's `loaded_by` comes from where its name appears:
  - a test function;
  - a test module;
  - a conftest fixture, followed through fixtures that request fixtures;
  - a source module, through the tests that import it.

  Mapping through a source module is module-level, so both tests in `test_patients.py` count
  as loading `seed.sql`. JavaScript tests are mapped per file.
- **YAML is a subset.** `datafiles.py` reads:
  - block mappings and sequences;
  - scalars;
  - inline lists;
  - comments.

  Anchors, aliases, tags, block scalars and multiple documents make a file `not_assessed`,
  never guessed at.
- **Time bombs.** A fixed future expiry date counts as a time bomb when a source or test
  compares that field with a now() call. The comparison operator decides the fail date: `>=`
  fails the day after, `>` on the day. Ages from fixed birth dates and "next Tuesday" logic
  aren't detected; the limitations say so.
- **`clock_shift.py`** is a stdlib pytest plugin. `TDA_CLOCK_SHIFT=+400d` or
  `TDA_CLOCK_DATE=2028-02-29` replaces `datetime.date`, `datetime.datetime`, `time.time` and
  `time.time_ns` before the tests import anything. Prefer the repo's own clock library, or
  libfaketime.
- **Locale on Windows.** Python's `setlocale(LC_ALL, "")` ignores `LANG` there, so the locale
  run was made in WSL with `C.UTF-8`. The report says that run also changed the platform.
- **Severity and priority are computed** by `--write-summary`, from the category, the audit
  date, the confidence and the `anonymised_by` marker.
  - Time bombs beyond a year are Moderate.
  - `suspected` findings are capped at P2.
  - Unconfirmed shared state is Moderate.
- **Example results:**
  - **Findings:** 7 findings from 16 candidates; 5 dismissed (3 by rule, 2 by review) and 2
    handed off.
  - **Personal data:** P0, four candidates in one finding.
  - **Time bomb:** fails 2026-12-02 and is Critical, but it is P1 because only two tests load
    the fixture.
  - **Shared state:** P1, confirmed by reverse order and passing in isolation.
  - **Schema drift:** P2.
  - **Locale (confirmed), magic id and unused file:** P3.
- **Found on a real repository, and fixed:**
  - Package and tool configuration in a test directory (`e2e/package.json`, `package-lock.json`,
    tsconfig, lint and CI configs) was inventoried as fixtures, and the near-duplicate check, which is
    quadratic, hung on the lockfiles. Those files are no longer test data. The duplicate check now applies
    the length bound and `quick_ratio` first, and compares very large files by their shared lines,
    recording `method: line_jaccard`.
  - The leak check matched an email's local part anywhere: a placeholder `user@` failed the report
    because "user" appears in file paths such as `src/user.js`. A local part now counts only when it is at
    least five characters and not a generic word, and values made only of letters must appear as a whole
    token. Values with digits, secrets included, still count anywhere. Every non-reserved flagged value in
    the example, planted in a sentence, at a sentence's end and inside a URL, is still caught (33 of 33).
  - The phone rule matched ISO dates: a test asserting a request's start and end dates (`"2026-11-12"`)
    produced two phone candidates. The phone and card rules now skip values shaped like dates and times
    (ISO and day-month-year forms, date ranges, `yyyymmdd`, date followed by a time), version strings,
    and epoch timestamps (13-digit milliseconds always, 10-digit seconds only in a field named as a time,
    because an Australian 1300 or 1800 number is also ten digits starting with 1). A card-shaped run that
    overlaps a date, or is part of a version or decimal, is skipped too. Of 26 synthetic values, the old
    rules flagged 9 dates, versions and timestamps as phones or cards; the new rules flag none of them and
    still catch every mobile, landline, international, 1300 and card number. The example's candidates
    did not change.
- **Testing:**
  - The validator catches 33 of 33 seeded violations.
  - 16 rule scenarios pass:
    - cards, specimen SSN and IBAN;
    - NI, NHS and TFN numbers;
    - the NHS 999 range;
    - production hosts, buckets and local paths;
    - unseeded random;
    - orphan snapshots and duplicates;
    - invalid enum values;
    - binary files and YAML anchors;
    - the `anonymised_by` marker;
    - `clock_shift.py` itself.

## As built (v1.1.0): a TypeScript suite

Run on a real Next.js and TypeScript repository: 402 Vitest files, 11 Playwright specs, Prisma.
Version 1.0.0 produced a valid report, but its most useful result, nine time bombs confirmed by
moved-clock runs, couldn't be reported. Every rule except personal data and secrets was
Python-only.

- **Findings no candidate covers.** A finding may set `source: "agent"` with no candidates and a
  `basis`:
  - `{file, line, excerpt}`: the validator re-reads the line with `--repo`;
  - `{evidence}`: a file in the run directory.

  An excerpt may not hold six or more digits or a non-reserved email address. A confirmed time
  bomb found this way names its `fails_on_date` and still needs a failing experiment. Date
  arithmetic now counts only where `scan_data.py` computed the fail date itself. The renderer
  labels these findings "found by review".
- **JavaScript and TypeScript rules.** `jsparse.py` tokenizes JS and TS: strings, template literals,
  comments and regex literals, so a quote inside `/can't/` doesn't open a string. It returns
  object literals as records with field names, and reads date calls (`new Date('...')`,
  `new Date(y, m, d)` with months counting from 0, `Date.UTC`, dayjs, moment, `parseISO`,
  `DateTime.fromISO`). Field patterns match camelCase through a snake_case conversion, and expiry
  fields may carry a prefix (`trialEndDate`, `certificationExpiresAt`). The JS rules cover:
  - time bombs, through comparisons with `Date.now()`, `new Date()` or a variable holding either,
    in product code;
  - future literal dates in tests that don't freeze the clock;
  - literal UUIDs, lookups by literal id and auto-increment assertions;
  - unseeded `Math.random`, faker, and generated ids in fixtures;
  - module-level mutable state that tests change and no hook resets;
  - machine-locale formatting compared with a literal. `locale_calls_js` in `rules.json` is now
    read.

  On the repository, all nine confirmed time bombs are now candidates at their lines: two
  `fixed_expiry` candidates carry the product comparison (`claim/[token]/route.ts:83`), and the
  rest are future literals in tests that read the clock. The JS scan takes 11 s for 430 sources.
- **False positives.** The phone rule now skips:
  - date-shaped strings, invalid dates included;
  - SSN-shaped strings;
  - values under identifier, code, password, amount and geometry fields (`meetingId`, `viewBox`).

  It dismisses by rule numbers whose last seven digits use one or two digit values, or run in
  sequence. The NANP fiction range matches without an area code (`+1-555-0123`). A UUID is never a
  card number. An iCalendar `UID:` value is not an email address.

  `reserved.json` records that New Zealand publishes no fiction range, so NZ 555 numbers stay a
  review call. On the repository, the open personal-data candidates went from 246 to 60, with 90
  phone numbers now dismissed by rule as placeholder patterns.
- **Placeholder secrets.** A secret whose value contains a placeholder word, names what it stands
  for (`google-access-token`, `pi_123_secret_abc`), or has too little entropy is dismissed by rule.
  Every other secret is still handed off and can't be dismissed by review. Mandatory handoffs on
  the repository went from 113 to 47; password-strength vectors remain handoffs. The example's one
  secret, `Bearer EXAMPLE-NOT-A-REAL-TOKEN-0000`, is now dismissed by that rule.
  `render_report.py --brief` summarises handoffs per recipient and file.
- **Leak check.**
  - An email's local part alone counts only when it reads as a name (`first.last`), so a role word
    used in test names no longer fails every JUnit file.
  - Errors name the candidate whose value leaked.
  - Six-digit windows are a set lookup.
  - `redact_evidence.py` removes exactly what the validator would reject from runner output. It
    parses JSON and rewrites strings only, so durations stay numbers. The hand-written redactor
    used on the repository took 6 min 19 s; this takes about 2 s for 18 MB.
- **Inventory.**
  - Images, fonts and media in a test area are listed as `not_assessed`, and SVG is scanned as
    text.
  - Setup and helper files are a new kind, `support`: `vitest.setup.ts`, `auth.setup.ts`,
    `__tests__/helpers/`, `test-support/`. They are scanned but not counted as tests, and are
    loaded by their package, their directory's specs, or their importers.
  - Walks prune `.turbo`, `.cache` and virtualenvs too.
- **Residue.** A new category for files a run leaves in the working tree. `residue.py snapshot` and
  `diff` record them, and a run that wrote uploads into `public/` is Serious. The evidence file
  confirms the finding.
- **Shared helpers.** `clock_shift.mjs` (shared) moves `Date` for Node runners, so the clock
  experiments that needed a hand-written setup file on the repository now use the skill's own tool.
  The shared `run_tests.py` gained `--npm-script` (the repo's own test command and environment),
  `--runner-args`, `--env` in any position, and Vitest re-runs by id. Its `vitest` preset shuffles
  but can't reverse; SKILL.md says so.
- **Not changed here.**
  - Prisma `schema.prisma` is still not a drift source.
  - JS `loaded_by` is still per file.

## As built (v1.2.0): a Python monorepo with production extracts

Run on a Python monorepo: two FastAPI services and a shared library, each with its own
`pyproject.toml`, about 200 data sources, a 37 MB repo-level `test-data/` tree of CSV extracts,
`.xlsx` workbooks and an HTML table saved as `.xls`, and pandas in the code under test. Version
1.1.0 produced a valid report, but it missed the whole `test-data/` tree, read dated run ids as
cards and phones, and those false positives then made every path in the report "leak".

- **Inventory.**
  - `test-data/`, `testData/`, `test-fixtures/`, `sample-data/` and similar names are test data
    trees, and `--data-dir` names any other (recorded in `inventory.json`, read back by
    `scan_data.py`).
  - An `.xls` is sniffed: HTML or XML text is scanned as text, and HTML tables become rows under
    their header. `.xlsx` workbooks are read with stdlib `zipfile` (shared and inline strings);
    findings cite lines of their text form (`datafiles.py <file> --text`).
  - A factory by file name counts only in a test area, so a product reader factory is not test
    data. A data migration that seeds rows is a seed, and is never "unused".
  - **Matching a file to its tests.** A bare file name counted anywhere in the repo, so one test
    naming a file in another service was credited with loading it. Now a bare name counts only in
    the same package root (and only if unique there, unless the directory is named too); data in
    an enclosing root needs the directory named; a path tail always counts. Globs over a named
    directory, code that names a directory and none of its files, and helper functions used by
    `parametrize` count as loading. Unused files on the repository went from 69 to 46. Those 46
    were not all checked by hand; each stays a candidate for the agent to confirm unread before
    it is reported.
- **New rules.**
  - `customer_extract`: a table of 20 or more rows whose column is named for a person or customer
    and holds mostly names. It is judged by the header and the row count and reports file, header
    line and column only; the leak check protects the column's multi-word values. It found 7
    extracts on the repository. A repo's own abbreviated headers need `person_columns` in
    `rules.json` extended.
  - Secrets: API key and subscription-key headers (`x-api-key`, `Ocp-Apim-Subscription-Key`,
    `x-functions-key`) and 32-64 character hex values under key, token or secret fields. The one
    missed gateway key is now a handoff.
  - `module_reload`: `importlib.reload` in test code, with the module's import-time environment
    reads. The confirmed order dependence on the repository is now a candidate.
- **False positives.**
  - Compact date-times and dated run ids (`yyyymmdd-hhmmss`, `yyyymmdd-NNN`), digit runs inside a
    path or identifier, and one- or two-digit repeats are never cards; dated run ids and bare
    digit strings with no phone field are never phones. Card and phone candidates on the
    repository went from 33 to 0.
  - `literal_lookup` needs a database or seed in reach and skips large ids in "not found" tests;
    `auto_increment` needs an equality. Magic-id candidates went from 181 to 33.
  - `/home/site/wwwroot` and CI runners' homes are reserved paths, not someone's machine.
  - Schema drift compares a fixture only with models in its own package root, needs half the
    record's keys to be the model's, keeps same-named models from two services apart, and counts
    unmatched records per file. Drift candidates went from 68 to 0.
  - Near-duplicate checks compare large pairs by line sets: character matching on large extracts
    took over ten minutes.
- **Leak check.** Each candidate's value is the span on its line whose fingerprint matches, not
  every number on the line. Known repository paths are blanked before matching, both in the
  validator and in `redact_evidence.py`, which no longer rewrites paths. The validator skips
  `candidates.json` as the redactor does. A review dismissal may say `not_personal_data: true`,
  which lifts the check for that candidate only.
- **Runs.** The validator rejects an experiment citing a run with no parsed tests, a timeout, or
  an exit code other than 0 or 1 (a crashed interpreter recorded as "0 passed, 0 failed"); such an
  experiment is `errored`. It checks the variation text against what `runs.json` recorded, and
  `experiment.cwd` makes case ids from a package-directory run repo-relative. SKILL.md covers the
  `clock_shift.py` crash when pandas is imported after it, and running each package with its own
  environment.
- **Rubric and summary.** The personal-data P0 rule wins over the suspected cap. Evidence
  completeness leaves out the experiment share when nothing is confirmable. The headline's finding
  count is checked.
- **Not changed here** (shared files, owned elsewhere): `clock_shift.py` still patches the
  classes at load; `run_tests.py` still records a crashed run with zero totals, builds reverse-order
  ids from pytest's rootdir, and records case ids relative to `--cwd`.
- **Example.** `clinic-api` gains a customer extract with dated batch ids, a module-reload pair of
  tests (confirmed by a reverse-order run), an HTML table saved as `.xls`, and a placeholder
  `x-api-key` header.

## As built (v1.3.0): a TypeScript pnpm monorepo

Found on a workspace of about 850 data sources, with a package named `e2e` that ships code:

- **Speed.** `inventory.py` re-read every JavaScript test once per data file (about 140,000
  reads); it now reads each once and counts lines in chunks. Whole repository: 2 min 10 s to
  34 s. `scan_data.py` tokenized every JS/TS product file to look for comparisons with now; it
  now tokenizes a product file only when its raw text mentions the clock or a locale call,
  deduplicates candidates with a set, and the tokenizer uses compiled patterns for whitespace,
  strings and operators (identical tokens on 1,039 files). Whole repository: 22 min to 16 min,
  still slow; the remaining time was not profiled to the end (the run was cut short), so it is a
  known gap.
- **Product packages named like test areas.** A directory named `e2e`, `tests`, `spec`... is a
  test area only when it is not a product package (a manifest that ships code), the rule
  `diff_scope.is_test_path` uses; `inventory.json` lists them under `product_dirs`.
- **False positives.** The email rule no longer matches package specs (`name@1.2.3`), IP hosts
  or file names such as `icon@2x.png`.
- **False negatives.** A module-level `let`/`var` reassigned in a test body is
  `shared_mutable` (`reassigned_module_let`); template literal expressions are code, so
  `Math.random()` inside `${...}` is seen.
- **Guidance.** SKILL.md covers the shipped network guards, `run_tests.py --repo-root` for
  workspace packages, and `clock_shift.mjs` moving `fs.stat` times.
- **Not changed:** the other review gaps (unused-file rule and computed names, placeholder
  secret words, magic ids for opaque UUIDs, near-duplicate device captures, TypeScript models
  for schema drift, an exploratory-experiment slot).
- **Example.** Unchanged: the rescan gives the same candidates.

## As built (v1.4.0): a Go command-line tool

Found on a Go module of about 340 `*_test.go` files, with vendored Swift and TypeScript driver code:

- **Go rules.** `scan_data.py` reads Go test files with comments and strings masked: a fixed
  `time.Date` or ISO date on or after the audit date in a test that reads `time.Now`, `Since` or
  `Until` (`future_literal_with_now`); `os.Setenv`/`Unsetenv`/`Clearenv` and `os.Chdir` in a test
  function (`go_os_setenv`, `go_os_chdir`, one candidate per function; `TestMain` and `init` are
  setup); a package-level `var` reassigned with no `defer` or `t.Cleanup` restore, whether direct,
  through a pointer or in a helper called first (`go_package_var_reassigned`, also when restored
  but the test calls `t.Parallel()`); `math/rand` with no seeded source (`unseeded_random`). On the
  repository: 15 environment, 4 working-directory and 2 package-variable candidates (a global set
  and cleared under a mutex with nothing restoring it).
- **Loud about the rest.** `candidates.json` has `language_coverage` (`code_rules`, and `text_only`
  per language) and a note plus a stderr warning for test code in languages with no code rules (the
  29 Swift test files). `schema_drift.py` reports `language_coverage` from the shared `lang_census.py` and warns when the
  repository is mostly a language whose models it does not read. `net_guard_proxy.py` (shared) is the guard for Go runs.
- **False positives.** URL user-info (`scheme://user:key@host`) was read as an email address (19 of
  50 personal-data candidates): it is now a `url_credentials` secret, dismissed by rule when the
  password is a placeholder or a variable, or the host is reserved. The card rule matched
  space-separated small numbers (swipe coordinates, process-listing columns; 6 candidates, now 0):
  a card is an unbroken run or the groups card schemes print (4-4-4-4, 4-6-5, ...), and still
  Luhn-valid. The stock placeholder password and a few like it are placeholder words.
- **Validator.** A review `environment_coupling` finding may carry `host` and `live_endpoint: true`;
  its basis must name the host (the calling line, or a proxy log under `--evidence`), and it is then
  Serious rather than always Moderate.
- **Residue outside the tree.** `residue.py --also home=<dir>` snapshots an isolated home folder too,
  listed as `home:<path>`. SKILL.md says to run tests with HOME pointed at an empty folder, gives a
  proxy-based network-isolation recipe for runtimes with no guard, Go run notes, and says why
  libfaketime does not move Go binaries.
- **Self-test.** `scripts/self_test.py` builds synthetic repositories for each of the above.
- **Not changed:** an assignment in a `describe` body read as a test's change, absolute paths
  passed to path parsers flagged as `local_path`, a device-identifier data type, Go struct models
  for schema drift, Go test-data conventions (`testdata/` golden files, `//go:embed`).
- **Example.** Unchanged: the rescan of `clinic-api` gives the same candidates.

## As built (v1.5.0): a server-rendered Python site with a large data tree

Found on a Flask site with a data directory of thousands of JSON records (tens of MB) that a
parametrised pytest suite globs:

- **Inventory scale.** `inventory.py --data-dir` never finished: each data file re-searched every
  module's strings one by one, re-walked every test module for importers of a loader module, and
  re-resolved every test's fixtures. Each node's strings are now joined and searched once per file
  (cached per node), and importers, visible fixtures, requested fixtures and fixture requesters are
  cached, so a few hundred templated files inventory in well under a second (self-test).
- **Never silently skipped.** A directory outside the test areas with 50+ data files that test code
  names is listed under `unlisted_data_dirs`, warned about on stderr and repeated in the scan's
  notes as NOT scanned. `--max-files-per-dir N` scans an evenly spaced sample and records the rest
  as `not_assessed` ("not sampled") with `sampled_dirs`. Both scripts print progress.
- **Duplicates bounded.** A directory with more than `duplicate_max_files` (200) files of one format
  is checked for exact copies only (a templated tree is near-duplicate by design), and the notes
  say so; the pairwise check was quadratic.
- **Review findings leak-checked.** The values on an agent `personal_data` or `secret` finding's
  basis lines are protected like a candidate's (reserved-domain addresses excepted), so the
  address it found cannot be pasted into `why_flagged`; the leak check runs with `--repo` even
  without `--candidates`.
- **Guidance.** A clock experiment that fails because a moved `now()` is compared with a real file
  modification time is skew, not a time bomb; `TEMP`/`TMP`/`TMPDIR` go to a scratch folder with
  HOME.
- **cwd-relative paths.** `environment_coupling` / `cwd_relative_path`: a test in a subdirectory
  that opens a top-level repository directory by a relative path (`open("data/x.json")`,
  `os.listdir("data")`) passes only when run from the root; a test that changes directory itself
  is left alone.
- **Not changed:** the `magic_id` rule still needs a database or seed in reach (a JSON data tree
  is not treated as a seed), and the inventory does
  not read pytest's `testpaths`/`python_files` (a root `*_test.py` script still counts as a test).
  `residue.py` and `run_tests.py` still hash the whole tree per call (shared files).
- **Example.** Unchanged: the rescan of `clinic-api` gives the same inventory and candidates.
