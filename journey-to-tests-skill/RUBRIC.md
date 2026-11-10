# Journey to Tests rubric

Anchors for the judgement calls in `SKILL.md`: selectors, assertions, failure classification and
flakiness. The validator enforces the mechanical parts; this file covers what it can't check.

## 1. Selector strategy

Use the first option that works, in this order:

| Rank | Web | Mobile | Notes |
|---|---|---|---|
| 1 | The repository's own convention (e.g. `data-testid` via `getByTestId`) | Its convention (`testID`, accessibility identifier, `ValueKey`) | `detect_conventions.py` reports `selector_convention` |
| 2 | Role + accessible name: `getByRole('button', { name: 'Pay now' })` | Accessibility ID / label: `by.id`, `~label`, `withContentDescription`, `app.buttons["Pay now"]` | Also checks the element is accessible |
| 3 | Label / placeholder: `getByLabel('Email')` | Text: `by.text`, `withText` | For form fields |
| 4 | Visible text: `getByText('Order number')` | | For static content only; brittle to copy changes |
| 5 | Stable attribute CSS (`[name="email"]`, `#checkout-form`), or a provider-owned iframe by `title` | Resource ID | Only when it is a contract, not styling |

Never use:

- XPath, `nth-child`, `:eq()`, `.nth(n)`, or `.first()` used to pick among identical elements
- styling classes (`.btn-primary`, CSS-module or Tailwind classes)
- generated IDs, DOM depth or element order

If only these are available, record a **testability gap**:

```json
{"id": "GAP-001", "journey_id": "JRN-ACC-001", "ref": "step 3",
 "element": "Past orders list on /account",
 "problem": "Rows are nested divs with no role, heading or test id",
 "suggestion": "Render as <ul aria-label=\"Past orders\"> or add data-testid=\"order-history\"",
 "file": "web/src/app/account/page.tsx", "line": 31}
```

Assert what you can of that Expect with stable selectors, mark the entry `blocked_by_gap` with
the gap ID, and say in `reason` what is and isn't asserted.

## 2. Turning an Expect into assertions

| Expect says | Assert | Not enough |
|---|---|---|
| "Cart badge shows 1" | `toHaveText('1')` / `toContainText('1')` on the badge | the badge is visible |
| "Redirected to /account" | `toHaveURL('/account')` | a click succeeded |
| "Pay now becomes enabled" | `toBeEnabled()` | the button exists |
| "Inline error; user stays on checkout" | the error text **and** the URL is unchanged | either one |
| "Order created" (API side effect) | the confirmation the user sees; plus an API check if the suite already does that | a 200 from an internal call only |
| "Email and password fields are visible" | one assertion per field | one for the form |

Rules:

- **Every part of a compound Expect gets its own assertion.**
- **Assert the user-observable outcome first.** Add API or database checks only where the
  repository's tests already do so, and never instead of the visible outcome.
- **Exact text from the journey** in quotes ("Your card was declined") is asserted exactly.
  Paraphrased text ("an error is shown") is asserted with the most specific stable check (role
  `alert` visible with non-empty text) and noted in `reason`.
- **Negative Expects** ("user stays on /login") are asserted after the action has settled, with
  an auto-waiting assertion on something that proves it settled (the error message appeared).
- **Not automatable** applies only when the journey lists it. Typical legitimate reasons: visual
  judgement, real payment or SMS delivery, third-party CAPTCHA. If you think another Expect can't
  be automated, keep it as a gap or open question. Don't mark it `not_automatable` yourself.

## 3. Classifying failures

Read the failure message, trace or screenshot before deciding. `parse_results.py` gives
`failure_type` and `signals`, which help but don't decide.

| Evidence | Classification |
|---|---|
| The assertion ran, the element was found, and the observed value contradicts the Expect (e.g. expected "Out of stock", received "Sold out") | `fail_product` |
| A redirect, state or message the journey promises doesn't happen, and the test reached the right screen with the right data | `fail_product` |
| Element not found because the selector is wrong, inside an iframe or shadow root, or names the wrong role | `fail_test` (category `selector`) |
| An action happened before the app was ready, or there's a missing `await` | `fail_test` (category `timing`) |
| Wrong or colliding test data, missing login state, wrong starting URL | `fail_test` (category `test_data`, `setup` or `navigation`) |
| Connection refused, 5xx from the dev server, seeding endpoint disabled, simulator not booted, browser not installed, missing credentials | `blocked_env` |
| The element the journey names doesn't exist at all, and the code has no such feature | `fail_product` with `likely: journey` if the journey looks wrong, else `unclear`; add an open question |

When unsure between `fail_product` and `fail_test`, check the app by hand (a screenshot or trace
of the failing step) before changing anything. A fix that changes what is asserted is never a
`fail_test` fix.

`likely` on a suspected bug:

- `product`: the code contradicts other evidence of intent (copy elsewhere, specs, other screens).
- `journey`: the journey is inferred and the behaviour looks deliberate in code.
- `unclear`: either could be right; ask.

## 4. Stability

- `repeat` defaults to 3. Raise it for journeys touching timing-sensitive features (real-time
  updates, animations, debounced search).
- Run stability checks with the final test file. Any change restarts the count.
- A test is `flaky` if the same file both passed and failed for reasons other than a recorded
  `blocked_env` outage.
- Common causes, and fixes that are allowed:

| Cause | Allowed fix |
|---|---|
| Missing `await` or an action before the page is ready | Await the action; assert a readiness condition first |
| Shared or colliding test data | Unique data per test (factories, random suffixes) |
| Test order dependence | Own setup per test |
| Animation or transition | The framework's settle or animation-disabling option, if the suite already uses one |
| App race condition (e.g. cookie vs redirect) | None in the test: report as `flaky` with the suspected product cause |

Retries, longer timeouts and sleeps are not fixes.

## 5. Anti-flake checklist (before the first run)

- [ ] No `sleep`/`waitForTimeout`/`cy.wait(ms)`; every wait is an assertion or explicit condition
- [ ] Each test seeds its own data and does not depend on another test
- [ ] No `.only`, no conditional branches on page state
- [ ] Selectors from ranks 1–5 only
- [ ] Credentials come from the repository's mechanism (environment variables, fixtures)
- [ ] The journey ID is in every test title and tag
- [ ] Each Expect has its assertion(s), with a `Step N` comment before the step

## 6. Review checklist (before reporting)

- [ ] `validate_report.py --repo .` passes
- [ ] Every `fail_product` has a suspected bug with expected, actual and evidence
- [ ] Every flaky or blocked test has evidence and a suggested next step
- [ ] Every testability gap names the element, the problem and a concrete code change
- [ ] Deferred Expects say what framework or environment would unblock them
- [ ] The headline gives verified/total Expects and leads with problems, not successes
- [ ] Nothing committed; every changed file is listed
