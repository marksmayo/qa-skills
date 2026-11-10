---
name: journey-to-tests
description: Turn user journeys written in Journey Markdown (from the Journey Mapper skill or by hand) into runnable end-to-end, UI or API tests inside the repository's existing test suite, using its own framework, page objects, fixtures and selector conventions. Every Expect becomes an assertion or is explicitly accounted for; product mismatches are reported as suspected bugs, never "fixed" by bending the test; new tests must pass repeated stability runs before they count.
version: 1.0.0
---

# Journey to Tests

## Mission

Add tests to an existing suite so that each selected user journey is checked automatically, and
report honestly how much of each journey is now verified.

Input: one or more journey files in the Journey Markdown format (`JOURNEY_FORMAT.md`). Output:

1. New or extended test files that follow the repository's conventions, each traceable to its
   journey ID.
2. `journey_tests_report.json`, conforming to `schemas/journey_tests_report.schema.json`: which
   Expect is asserted where, run results, suspected bugs, testability gaps and every file changed.
3. A rendered summary for the user.

## Non-negotiable rules

- **Every Expect becomes at least one assertion on something a user can observe** (text, URL,
  enabled state, visible element, response status for API journeys). The only exceptions are
  recorded: listed under the journey's `Not automatable`, blocked by a recorded testability gap,
  or deferred with a reason. A test with no assertion is never acceptable.
- **The journey is the specification; the product may be wrong.** When the app doesn't do what
  the Expect says, classify the failure `fail_product` and report a suspected bug. Never change
  the expected value, loosen the assertion, or edit the journey to make the test pass. Once an
  attempt is classified `fail_product`, the result stays `fail_product`.
- **Stable selectors only.** Use the repository's selector convention; otherwise role, label and
  accessible-name queries (`getByRole`, `getByLabel`), or accessibility IDs on mobile. When no
  stable selector exists, record a **testability gap** with a concrete suggestion instead of
  writing an XPath, `nth-child`, positional or styling-class selector.
- **No hard-coded waits** (`sleep`, `waitForTimeout`, `Thread.sleep`, `cy.wait(<ms>)`). Use the
  framework's auto-waiting assertions and explicit conditions.
- **Tests are independent.** Each test creates its own data the way the repository already does
  (API seeding, factories, fixtures), doesn't rely on other tests or their order, and cleans up
  if the suite does.
- **Reuse, don't bypass.** Extend existing page objects, screen objects, fixtures, login helpers
  and custom commands. Only create new ones where none exist, in the same style and location.
- **A test is done only when it passes every stability run.** A test that passes sometimes is
  `flaky` and is reported, not counted. Never add retries or waits to hide flakiness.
- **Ask before installing anything.** Never add a framework, dependency or browser download
  without the user's agreement. Never change application source code; propose changes (such as
  test IDs) as testability gaps.
- **Leave changes uncommitted** and list every file you changed.
- **Don't reveal secrets.** Use the repository's mechanism for credentials (environment
  variables, secret stores, seeded test users); never paste secret values into tests or reports.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Journeys | required | `.md` files or a directory (e.g. `journeys/`) |
| Filter | none | By priority, domain, platform or journey ID |
| Target | detected | Base URL, dev-server command, simulator/emulator or build path |
| Approval gate | on | The user approves the test plan before any file is written |
| Repeat | 3 | Stability runs per new test |
| Update journeys | off | When on, writes `automation` and `tests` back into journey front matter |

## Step 1: Load and lint the journeys

```bash
python scripts/journey_md.py lint journeys/            # add --strict for journey-mapper output
python scripts/plan_coverage.py journeys/ --repo . --out <scratch>/journey_tests_report.json \
    [--priority P1] [--domain checkout] [--platform web] [--id JRN-CHK-001]
```

Files written by journey-mapper (`status: inferred` with full Evidence) must lint clean with
`--strict`. Hand-written files only need to lint without errors; warnings are fine. A journey with
lint errors is skipped and reported.

`plan_coverage.py` writes the report skeleton with one `step_coverage` entry for every step and
variant Expect, all `pending`. Keep the report outside the repository (a scratch directory)
unless the user asks for it to be saved. Journeys with `status: inferred` describe how the code
appears to behave; mention to the user that they have not been reviewed by a person.

## Step 2: Fingerprint the conventions

```bash
python scripts/detect_conventions.py . --target <where the tests will go> --out <scratch>/conventions.json
```

From the fingerprint and your own reading, establish for the area that will hold the tests:

- **Framework.** Web: Playwright, Cypress, WebdriverIO, Selenium. Mobile: Detox, Appium, Maestro,
  XCUITest, Espresso/Compose UI test, Flutter `integration_test`/Patrol. API-only journeys become
  API tests in the repository's integration framework (e.g. pytest + httpx, supertest,
  REST Assured).
- **BDD.** If the repository uses Cucumber, pytest-bdd, behave or SpecFlow/Reqnroll, write
  `.feature` scenarios plus step definitions: Preconditions → `Given`, Action → `When`,
  Expect → `Then`. Reuse existing step definitions.
- **Run command**, preferring the one CI uses, and how to produce JUnit XML with it.
- **Layout and naming**: where e2e tests live, file naming, describe/tag style.
- **Building blocks**: page/screen objects, fixtures, login helpers (`storageState`, login
  fixtures), seeding APIs and factories, environment configuration, `baseURL`.
- **Selector convention**: test IDs, roles and labels, accessibility IDs.
- **Exemplars**: read the 2–3 exemplar tests closest to the target and model the new tests on
  them.

If no suitable framework exists for a platform (e.g. a mobile journey and no mobile UI test
framework), **stop for that platform**: propose the ecosystem default from the fingerprint's
`proposal`, and mark those Expects `deferred` with the reason. Don't install it yourself.

## Step 3: Plan and get approval

For each journey, plan:

- **One spec file or describe block per journey**, titled with the journey ID and title. Put the
  ID in the test titles and in the framework's tag mechanism (Playwright `tag`, Cypress/WDIO
  title tags, pytest markers, JUnit `@Tag`, Gherkin `@JRN-…` tags, Maestro `tags`).
- **One test for the main path, one test per variant.** A variant test sets up its own state and
  goes straight to the branching step where the framework allows it (seeded data, direct URL),
  rather than replaying the whole main path.
- **Each step mapped** to an existing page-object method or a new one.
- **Each Expect mapped** to the assertion(s) that will check it, with the selector strategy.
- **Test data** from the journey's `Test data` section, created the way the repository does it.
- **Gaps and deferrals** you already know about.

Show the plan to the user in a compact table (journey, test, Expects covered, selectors, data
setup, gaps). With the approval gate on, write nothing until the user approves; record the
approval in `metadata.approval`. Fill in `mapping` and the planned `step_coverage`.

## Step 4: Write the tests

- Follow the exemplars: imports, fixtures, naming, file location, formatting, comment density.
- Every Expect gets an assertion in the test that covers it. Put a short `// Step N` or
  `# Step N` comment before each step's actions so the mapping is easy to review.
- Use web-first/auto-waiting assertions (`await expect(locator).toHaveText(…)`,
  `cy.contains(…).should(…)`, Espresso idling resources, XCUITest `waitForExistence` followed by
  an assertion).
- Assert the specific observable outcome the Expect describes. "Cart badge shows 1" is
  `toHaveText('1')` on the badge, not "no error was thrown".
- When an Expect has several parts ("title, price and an enabled Add to cart button"), assert each
  part.
- Don't add retries, `force: true` clicks, or conditional logic that branches on page state.

Then lint what you wrote and fix every error:

```bash
python scripts/lint_tests.py <each test, page object and helper you added or changed>
```

## Step 5: Run and classify

Run each new spec with the repository's runner, producing JUnit XML, and record every run:

```bash
python scripts/parse_results.py <scratch>/runs.json --junit <junit.xml> --run-id R1 \
    --command "<exact command>" --exit-code <code> --hash <test file> <page objects/helpers you changed>
```

Always pass `--hash` with the test file and every helper you changed; the validator uses these
hashes to prove which version of a test passed. Copy `runs` and `cases` into the report.

Classify each failed or errored attempt using `RUBRIC.md`:

| Classification | Meaning | What you do |
|---|---|---|
| `fail_product` | The app ran and its observable behaviour contradicts the Expect | Stop changing the test. Record a suspected bug with expected, actual and evidence |
| `fail_test` | The test is wrong: selector, timing, setup, navigation, data | Fix the test (not its expectations), record a `change` with category and detail, re-run |
| `blocked_env` | App, service, device, seed data or credentials unavailable | Record evidence and what would unblock it. Don't fake the environment |

The `change` categories are `selector`, `timing`, `setup`, `test_data`, `navigation` and
`structure`. There is no category for changing what a test expects, on purpose. If you believe
the journey itself is wrong, keep the test as the journey says, classify `fail_product` with
`likely: journey`, and raise it as an open question.

Limit fix attempts: after 3 `fail_test` iterations on one test without progress, stop, keep
status `fail_test`, and report what you tried.

## Step 6: Stability check

Run each test that passed `repeat` times (default 3) with the same test file, e.g.
`--repeat-each=3` (Playwright), a loop over the runner, or `--count` (Go). Record these runs
with `parse_results.py --hash` too.

- All `repeat` runs pass with the final file → `pass`.
- Mixed results with the same file → `flaky`. Report it with evidence and a likely cause; don't
  "fix" flakiness with waits or retries. Investigate only if the cause is clearly in the test
  (shared state, missing await, data collision), and then re-run the full stability check.

## Step 7: Update journeys (optional)

Only if the user turned on `update_journeys`:

```bash
python scripts/update_journeys.py <scratch>/journey_tests_report.json --repo .
```

It changes only `automation` and `tests` in front matter, never downgrades automation, and
records the edits in the report.

## Step 8: Validate and report

Fill in `testability_gaps`, `suspected_bugs`, `files_changed` (every file, with its kind),
`lint` (from `lint_tests.py --json`), `open_questions` and a one-sentence `summary.headline`.
Then:

```bash
python scripts/validate_report.py <scratch>/journey_tests_report.json --repo . --write-summary
python scripts/render_report.py <scratch>/journey_tests_report.json
```

`--write-summary` recomputes the counts; fix every validation error rather than editing counts by
hand. Show the rendered report. Lead with suspected bugs, flaky tests and anything blocked; then
the number of Expects verified by passing, stable tests out of the total; then the files changed,
which are left uncommitted.

Never describe the result as "fully tested", "complete coverage" or "bug-free". Say what is
verified, and what isn't and why.

## Report fields

| Field | Content |
|---|---|
| `journeys[]` | Every journey read, whether in scope, and the fingerprint of its Expects |
| `mapping[]` | Journey → test file → tests (`test_id`, title, variant, tags) |
| `step_coverage[]` | Every Expect: `asserted` (file:line), `not_automatable`, `blocked_by_gap`, `deferred` |
| `testability_gaps[]` | Elements with no stable selector and the change that would fix it |
| `runs[]`, `cases[]` | Normalised JUnit results from `parse_results.py` |
| `results[]` | Per test: status and every attempt (run, case, outcome, classification, change) |
| `suspected_bugs[]` | Expected vs actual for each `fail_product` |
| `files_changed[]` | Every file added or modified, with its kind |
| `summary` | Counts recomputed by the validator, plus the headline |
