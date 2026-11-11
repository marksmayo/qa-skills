# Shared components and the Journey Markdown format

Status: Partly built; skills 7–25 add more shared files (see "Planned for skills 7–24"). Done: the Journey Markdown format, `journey_md.py`, `fetch_ticket.py` (shared since skill 7), `run_tests.py` and `stats.py` (since skill 8), `diff_scope.py` (since skill 9),
`detect_conventions.py`, `parse_results.py`, `sarif.py` and `tools/check_shared.py`. The
rendering helpers were not shared: each skill's renderer is small and report-specific, so each keeps
its own.

## Why shared pieces are duplicated, not imported

Each skill must be self-contained: it gets installed by copying one directory into an agent's
skills folder. So shared files are **copied** into every skill that needs them.

A repo-level check, `tools/check_shared.py`, fails if the copies drift apart. The canonical copy
of each shared file lives in `shared/`, and the check compares every skill's copy byte-for-byte
against it.

| Shared file | Used by |
|---|---|
| `JOURNEY_FORMAT.md` + `schemas/journey.schema.json` | journey-mapper, journey-to-tests |
| `scripts/detect_conventions.py` | ticket-to-tdd, journey-to-tests |
| `scripts/parse_results.py` (JUnit XML) | ticket-to-tdd, journey-to-tests |
| `scripts/sarif.py` (SARIF read/write) | code-security-auditor, ui-crash-hunter |
| `scripts/_render_common.py` (colour/Markdown rendering helpers) | all skills |
| Finding core (fields below) | all finding-producing skills |

## Core finding format

Every skill that produces findings includes these fields, and adds its own fields on top:

```text
id, title, evidence_type: tool | probe | trace | agent_judgment | reproduced,
confidence: high | medium | low (or skill-specific ordinal),
locations[{file?, line?, url?, selector?}], evidence[{kind, path_or_excerpt}],
sources[{tool, version, rule_id}], remediation, verification_step
```

Shared rule: an `agent_judgment` finding can never be rated `high` confidence.

## `detect_conventions.py`

This script does static, stdlib-only detection of a repo's testing conventions. It returns one
fingerprint per area of the repo, because monorepos and mixed stacks can use different
conventions in different places.

| Aspect | Detects |
|---|---|
| Framework | pytest/unittest, jest/vitest/mocha, JUnit 4/5/TestNG, Go `testing`, RSpec/minitest, xUnit/NUnit/MSTest, PHPUnit, cargo test |
| E2E and mobile | Playwright, Cypress, WebdriverIO, Selenium, Detox, Appium, Maestro, XCUITest, Espresso, Flutter `integration_test` |
| BDD | pytest-bdd, behave, Cucumber (any language) |
| Run command | Read from CI config first, then `package.json` scripts, `Makefile`, `tox.ini`/`noxfile`, `pyproject.toml`, Gradle/Maven. CI is treated as the source of truth |
| Layout and naming | `tests/` dir vs colocated, `test_*.py` vs `*_test.py`, `*.spec.ts` vs `*.test.ts`, whether tests mirror the source tree |
| Style | Assertion library, fixtures/`conftest`/factories/builders, mocking library, parametrisation, markers/tags, async handling, test-data location |
| E2E building blocks | Page and screen objects, custom commands, login helpers (`storageState`), seeding APIs, `baseURL` and environment config, selector conventions (`data-testid`, role-based) |
| Levels | Unit, integration and e2e, and how the repo separates them (directories, markers, configs) |

It also selects **2–3 exemplar tests**: the existing tests closest to a given target path. New
tests are modelled on these exemplars.

If the repo has no tests, the fingerprint says so. Skills then stop and propose the ecosystem
default rather than silently picking one.

## `parse_results.py`

This script reads JUnit XML, which almost every runner can emit:

- pytest `--junitxml`
- jest-junit
- vitest's junit reporter
- Maven Surefire
- go-junit-report
- Playwright's junit reporter

It normalises the results to:

```text
runs[{id, command, exit_code, junit_path, totals}], cases[{id, name, file, status, failure_type,
message_excerpt}]
```

`failure_type` separates `assertion` failures from `error` failures (import, collection, fixture
and setup errors). ticket-to-tdd needs this split to decide whether a red is valid.

**As built:**

- Cases also carry `run_id`, `classname` and `signals`. Signals are `timeout`,
  `selector_not_found`, `network`, `crash` and `environment`.
- Action timeouts (`locator.click: Timeout`) count as `error`, because no expectation was
  evaluated.
- Repeated cases within one run (`--repeat-each`) get `#2` and `#3` suffixes.
- Runs record `file_hashes`, the SHA-256 of the files passed with `--hash`. This is the test-file
  lock shared by ticket-to-tdd and journey-to-tests.
- Runs add notes when the exit code and the parsed cases disagree, or when no cases were
  parsed.
- `--phase` stores a label on the run (ticket-to-tdd uses `red`, `green`, `regression` and
  `re_red`).
- Classification uses the failure's `type` and `message` attributes first, when they decide it,
  because pytest's failure body quotes the test source (`assert ...`) even for import errors.
  `NotImplementedError`, `AttributeError` and "not implemented" are errors, so a stub that raises
  doesn't produce a valid red. "DID NOT RAISE" is an assertion. Playwright's
  `file:line:col title` messages are skipped.

`sarif.py` as built (shared by ui-crash-hunter and code-security-auditor):

- `read(path, root)` normalises every result of a SARIF 2.1.0 log to `{tool, version, rule_id,
  level, message, file, line, column, end_line, snippet, fingerprint, tags, cwe}`. It resolves
  `ruleIndex`, `uriBaseId` and `file://` URIs, and fills message templates from `messageStrings`.
  `cwe` collects CWE ids from rule and result tags, a `cwe` property, or CWE taxonomy relationships
  (gosec), because tools disagree on where they put them.
- `build(tool, version, results)` writes a minimal valid SARIF log for code-scanning dashboards.
  A result's optional `rule_properties` (tags, `security-severity` for GitHub code scanning) go on
  its rule.
- CLI: `python sarif.py read a.sarif b.sarif --root <repo>`.

`detect_conventions.py` as built:

- Emits `run_commands` with their source. CI commands are scoped to an area by
  `working-directory`, by a path in the command, or by framework.
- Picks a `selector_convention` (ties go to role, then label, then test id).
- Adds a `proposal` for areas with no tests.
- Scores `exemplars` against `--target` by path, name and test level.

## Planned for skills 7–24 (not built)

The second batch of skills needs three more shared scripts. Like the others, they are extracted
from the first skill that needs them, then copied into the rest by `tools/check_shared.py`.

| Planned shared file | Extracted from | Used by |
|---|---|---|
| `scripts/run_tests.py` | new (wraps `parse_results.py`) | flaky-test-hunter, mutation-tester, test-impact-selector, dead-test-finder, bug-reproducer |
| `scripts/diff_scope.py` | new | mutation-tester, test-impact-selector, db-migration-reviewer, risk-based-test-planner |
| `scripts/browser_harness.py` | wcag-auditor's `run_audit.py` | performance-budget-auditor, visual-regression-reviewer, cross-browser-checker, i18n-checker, resilience-prober, privacy-flow-auditor, bug-reproducer, api-contract-tester (request guard only) |
| `scripts/fetch_ticket.py` | ticket-to-tdd | ticket-readiness-reviewer, bug-reproducer, risk-based-test-planner |

**`run_tests.py`** runs a test command once or N times and records each run in
`parse_results.py`'s format. Every run records its command, exit code, duration, JUnit path and
the variation it applied. Variations:

- `seed`: sets `PYTHONHASHSEED` and the framework's random-seed flag.
- `order`: reverse or shuffled order, through pytest-randomly, `jest --randomize` or JUnit's
  `MethodOrderer.Random`, where the repo has them.
- `parallel`: worker count.
- `env`: time zone, locale or a fixed clock.
- `isolate`: one test per process.

A per-run timeout kills the process tree and records `timed_out`. The script never edits test
files.

**`diff_scope.py`** (built with skill 9; see the mutation tester's "As built") turns `git diff <base>...HEAD` (or a patch file) into changed files and
changed line ranges per file. It follows renames (`-M`), separates added, modified, deleted and
renamed files, and can map line ranges to the enclosing function. For Python this uses `ast`;
other languages use a brace and indentation heuristic, and the output records which method was
used.

**`browser_harness.py`** holds the parts of the WCAG runner that aren't specific to
accessibility:

- launches Chromium, Firefox or WebKit;
- requires `authorised_by` for non-loopback targets;
- runs the non-GET route guard with `allow_form_submission` and logs `blocked_requests`;
- handles auth via a storage state or a login recipe whose credentials come from environment
  variables;
- applies `rate_limit_seconds`;
- applies named `states` and `viewports`;
- dismisses the consent banner;
- saves screenshots and accessibility-tree snapshots;
- includes the stdlib PNG decoder (`png_pixels`, `changed_pixels`).

The WCAG auditor keeps its own copy until it is migrated to the shared file; that migration must
leave its example valid.

**`fetch_ticket.py`** moves to `shared/`, so every skill that reads Jira normalises tickets the
same way. It gains a read-only `--jql` search mode, which batch readiness reviews, defect
history and duplicate search all need.

The design docs add these needs:

- **`diff_scope.py --diff <file>`** reads a unified diff instead of a git range. Examples ship
  `.diff` files because a nested git repository can't be committed. The impact selector also
  uses it to replay past changes.
- **`run_tests.py --check-tree`** hashes the working tree before and after a run and fails if
  anything outside the run's output changed. The mutation tester and dead test finder need it
  because they modify code temporarily.
- **`stats.py`** (new shared file) holds the Wilson score interval and the rate bands. The
  flaky test hunter and test impact selector use it; the release readiness gate uses its flake
  thresholds.
- **`neutralise.py`** is owned by the dead test finder. It disables the code under test to see
  whether a test can still fail. The flaky test hunter uses it to check that a fixed test can
  still fail, so it becomes shared if both keep that step.
- **`browser_harness.py`** also needs:
  - a PNG encoder, for visual regression crops and diff images;
  - targeted fault responses for write requests, answered locally without reaching the server
    (resilience);
  - service workers off by default (resilience);
  - hostname mapping, so stub third parties can run on loopback ports (privacy).
- **`data/wcag-sc.json` and `data/en301549-map.json`** become shared between the WCAG auditor
  and the mobile app auditor, along with the WCAG validator's outcome rules.
- **`jsonschema_lite.py`**, a cut-down JSON Schema checker, starts in the API contract tester.
  It becomes shared when a second skill needs one.

---

# Journey Markdown format (`JOURNEY_FORMAT.md`)

This is the contract between journey-mapper (which writes these files) and journey-to-tests
(which reads them). It must be pleasant to write by hand and strict enough to parse.

## File layout

```text
journeys/
  index.md                     # domains → journeys table
  coverage.md                  # routes/screens/endpoints/goals ↔ journeys, orphans, open questions
  journey-graph.json           # interaction graph (journey-mapper only)
  <domain>/JRN-<DOMAIN>-<NNN>-<slug>.md
```

## Example

```markdown
---
id: JRN-CHK-001
title: Guest completes checkout with card
domain: checkout
platforms: [web, ios, android]   # where this journey exists
persona: guest                   # from the app's role model
priority: P1                     # P1 critical / P2 important / P3 nice-to-have
status: inferred                 # inferred | reviewed | approved
entry_point: /products/:slug     # route, screen name or deep link
goal: Order confirmation shown and order created
flags: [new_checkout]            # feature flags this path depends on
automation: none                 # none | partial | automated
tests: []                        # test paths, filled by journey-to-tests
---

## Preconditions
- Product with stock > 0 exists
- User is not signed in

## Steps
1. **Action:** Open a product page
   **Expect:** Product title, price and "Add to cart" are visible
2. **Action:** Click "Add to cart"
   **Expect:** Cart count shows 1
3. **Action:** Go to checkout and fill shipping + card details
   **Expect:** "Pay now" becomes enabled
4. **Action:** Click "Pay now"
   **Expect:** Confirmation page shows an order number

## Variants
- **V1 – card declined** (from step 4): **Expect:** inline error, user stays on payment step
- **V2 – out of stock** (from step 2): **Expect:** "Add to cart" disabled with message

## Test data
- Card: payment provider sandbox test card (declined card for V1)

## Not automatable
- (none)

## Open questions
- (none)

## Evidence
- Route: `src/app/products/[slug]/page.tsx:12`
- Handler: `src/features/cart/useAddToCart.ts:30` → `POST /api/cart`
- Goal event: `track('order_completed')` in `src/features/checkout/Confirm.tsx:44`
```

## Rules

- **Strict front matter, validated by `journey.schema.json`.** Required fields: `id`, `title`,
  `persona`, `priority`, `status`, `entry_point` and `goal`.
- **Every step has both an `Action` and an `Expect`.** The `Expect` is what becomes the
  assertion.
- **A variant names the step it branches from** and has its own `Expect`.
- **Anything a test can't check goes under `## Not automatable`**, with a reason, e.g. "visual
  judgement of brand imagery".
- **IDs are stable.** Re-runs update a file in place and never duplicate it. Fields a person
  has set are preserved: `status: reviewed|approved`, edited prose, `Open questions` answers.
- **Lint strictness depends on where the file came from:**
  - Files from journey-mapper must lint clean.
  - Hand-written files get warnings, not errors, wherever the intent is still parseable.
- **Gherkin mapping.** Preconditions map to `Given`, `Action` to `When`, and `Expect` to
  `Then`. journey-to-tests uses this mapping when the repo already uses Cucumber or pytest-bdd.
