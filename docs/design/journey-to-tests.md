# Design: `journey-to-tests-skill`

Status: Built (v1.0.0); see [Files](#files) for how the build differs from this design. Build order: 2.

## Mission

The skill takes user journeys written in the Journey Markdown format (see
[shared-components.md](shared-components.md)) and adds runnable tests to the existing test
suite. The journeys can come from journey-mapper or be written by hand. The tests use the
suite's own framework, structure and helpers, for both web and mobile.

## Non-negotiable rules

- **Every `Expect` becomes at least one assertion on an outcome the user can observe**, unless
  the journey file lists it under `Not automatable` with a reason. Tests with no assertions are
  rejected.
- **Selectors must be stable.** The skill follows the repo's convention, and otherwise uses
  role- and label-based queries (`getByRole`, accessibility IDs on mobile). If no stable selector
  exists, it records a **testability gap** (e.g. "add `data-testid` to the Pay button") instead
  of writing a brittle CSS, XPath or nth-child selector.
- **No hard-coded waits** (`sleep`, `waitForTimeout`, `Thread.sleep`). The skill uses each
  framework's built-in auto-waiting assertions.
- **When the product doesn't match the journey, the test isn't bent to match.** The failure is
  reported as a possible bug, or as a journey that's wrong.
- **Tests stay independent.** Each test sets up its own data, cleans up after itself, and doesn't
  depend on test order.
- **Changes are left uncommitted.** The skill never installs a framework or dependency without
  asking.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `journeys` | required | One or more `.md` files or a directory |
| `filter` | none | By priority, domain, platform or ID |
| `target` | auto | A local dev server command or a `baseURL`. For mobile: a simulator, emulator or build path |
| `approval_gate` | on | The user approves the test plan before files are written |
| `repeat` | 3 | Stability runs per new test |
| `update_journeys` | off | If on, sets `automation` and `tests` in each journey's front matter |

## Workflow

1. **Parse and lint the journeys.** Files from the mapper must be strict. Hand-written files get
   warnings, not errors.
2. **Fingerprint conventions** with the shared `detect_conventions.py`.
   - **Web:** Playwright, Cypress, WebdriverIO or Selenium.
   - **Mobile:** Detox, Appium, Maestro, XCUITest, Espresso, or Flutter `integration_test`.
   - **API-only journeys** become API-level tests in the repo's integration framework.
   - **Gherkin:** if the repo uses Cucumber or pytest-bdd, the skill writes `.feature` files and
     step definitions.
   - **No suitable framework:** the skill proposes one and stops.
3. **Inventory the building blocks** the tests will reuse: page and screen objects, fixtures,
   custom commands, login helpers (`storageState`, login fixtures), data factories or seeding
   APIs, and environment config. The repo's page objects are extended, never bypassed.
4. **Plan.**
   - Each journey becomes one spec file or `describe` block, and each variant becomes one test.
   - Each step is matched to an existing page object method or a new one.
   - Test data is set up the way the repo already does it (via API or factories, not by
     clicking through the UI).
   - The plan is shown for approval.
5. **Write the tests.** The journey ID goes in the test title and tags (e.g. `@JRN-CHK-001`) so
   each test can be traced back to its journey.
6. **Run and classify** using the shared `parse_results.py`:
   - `pass`
   - `fail_product`: the app's behaviour differs from the journey. This is a possible bug,
     reported with a screenshot, trace and the failing `Expect`.
   - `fail_test`: a problem in the test itself (selector, timing, setup), which the skill fixes
     and reruns.
   - `blocked_env`: the app, a service or the device isn't available.
7. **Stability check.** Each new test runs `repeat` times. A test that passes inconsistently is
   reported as `flaky` and is not counted as done.
8. **Report**, validate and render.

## Output (`journey_tests_report.json`)

```text
metadata:     repo, generated_at, framework(s), target, platforms
mapping[]:    {journey_id, journey_path, test_path, tests[{name, variant?, tags}]}
step_coverage[]: {journey_id, step, expect, assertions[{file, line}] | not_automatable_reason}
testability_gaps[]: {journey_id, step, element, suggestion}
runs[]:       shared run format
results[]:    {test, status: pass|fail_product|fail_test|blocked_env|flaky, evidence}
suspected_bugs[]: {journey_id, step, expected, actual, evidence}
files_changed[]: {path, change: added|modified}
```

## Validator fails when

- An `Expect` has no assertion and no `Not automatable` reason.
- The test lint finds a hard-coded wait, an assertion-free test, or a test missing its journey
  tag.
- A test is reported `pass` without a matching run.
- A `fail_product` result was "fixed" by editing the test's expected value.

## Files

Status: **built** (v1.0.0). The implementation differs from the original plan as follows:

- `parse_journeys.py` became the shared `journey_md.py` plus `plan_coverage.py`. The planner
  writes the report skeleton with one `pending` entry for every step and variant Expect, and a
  SHA-256 fingerprint of each journey's Expects. The validator rejects pending entries, and
  rejects journeys whose Expects changed during the run.
- `step_coverage[].coverage` is `asserted | not_automatable | blocked_by_gap | deferred`.
  `not_automatable` is only accepted when the journey lists it. `deferred` covers platforms with
  no framework in the repository.
- Results record every attempt (`run_id`, `case_id`, `outcome`, `classification`, `change`).
  `parse_results.py --hash` stores the SHA-256 of the test file and helpers for each run, which
  lets the validator check mechanically:
  - `pass` requires `repeat` passing attempts of the file now on disk.
  - Mixed outcomes on the same file must be `flaky`.
  - A changed test file needs a `change` category (`selector | timing | setup | test_data |
    navigation | structure`). There is deliberately no category for changing what the test
    expects.
  - Once any attempt is `fail_product`, the result stays `fail_product`.
- An extra status, `not_run`, covers tests that were written but couldn't be run.
- `update_journeys.py` implements the `update_journeys` option. It edits only `automation` and
  `tests`, and never downgrades `automation`.
- The example includes the sample repository (`examples/acme-shop/`), so `validate_report.py
  --repo` re-lints the tests and checks the assertion lines and hashes against real files.

```text
journey-to-tests-skill/
  SKILL.md  RUBRIC.md (selector strategy, assertion rules, classification, anti-flake)  README.md
  JOURNEY_FORMAT.md  schemas/journey.schema.json          # shared copies
  schemas/journey_tests_report.schema.json
  scripts/plan_coverage.py  lint_tests.py  update_journeys.py  validate_report.py  render_report.py
          journey_md.py  detect_conventions.py  parse_results.py   # shared copies
  examples/journey_tests_report.example.json  examples/acme-shop/…
```
