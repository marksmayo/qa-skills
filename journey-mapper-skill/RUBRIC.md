# Journey Mapper Rubric

## 1. Priority model

Assign the **highest** priority whose criteria any part of the journey meets, then apply the
adjustments. Every journey records a one-line `priority_reason` naming the criterion.

### P1: critical

A failure would lose money, lock users out, lose or leak data, or break a legal obligation.

- **Revenue:** purchase, checkout, subscription, upgrade, payment method management, refunds.
- **Access:** sign-up, sign-in, sign-out, password reset, MFA, session expiry handling.
- **Data integrity:** creating, editing or deleting a user's primary records; irreversible
  actions (delete account, cancel order, bulk delete).
- **Compliance:** consent capture, privacy requests (export, deletion), age gates, accessibility
  of required flows.
- **Core value:** the journey ending in the product's primary goal event (the reason users come).

### P2: important

- Regularly used secondary features: search, filtering, profile editing, notifications settings,
  saved items.
- Admin or owner actions that affect other users or shared data.
- Onboarding steps after sign-up.
- Recovery paths for P1 journeys that are complex enough to be separate journeys.

### P3: nice to have

- Rarely used, cosmetic or informational features: theme switching, help pages, marketing pages
  without conversion goals.
- Internal tools used by staff only, unless they modify customer data (then P2).

### Adjustments

- **Up one level** when a journey is the only path to a P1 goal on some platform, e.g. the
  mobile-only checkout.
- **Never down** because a journey already has tests. Priority describes risk, not coverage.
  Coverage is reported separately.

## 2. Journey granularity

| Situation | Model as |
|---|---|
| Same persona, same goal, error or validation outcome | Variant |
| Same flow reached from a different entry point (deep link, notification) | Variant |
| Same flow with a platform-specific difference | Variant |
| Same goal, different persona, materially different steps | Separate journey |
| Different goal | Separate journey |
| A variant would need more than ~3 steps of its own | Separate journey |
| A sequence longer than ~12 steps | Split at a natural goal (e.g. "add to cart" vs "pay") and use preconditions to chain |

Good step: "Click 'Pay now'". Expect: "Confirmation page shows an order number".

Bad step: "Submit the form" with Expect "Order is processed". The action is vague, and the
outcome can't be observed.

## 3. Evidence rules

| Claim | Minimum evidence |
|---|---|
| A step's action exists | The handler, link or navigation call (file:line) |
| A step's expectation | Where the outcome is rendered or triggered (file:line) |
| A guard on an edge | The check itself (file:line) |
| `unreachable: dead` | The search you did, stated in the evidence note: "no references to /legacy-cart in links, redirects or navigation calls" |
| `unreachable: flag_off` | The flag's default value and where it is set for each environment |
| `automation: partial / automated` | Test file paths, and the reasoning about which Expects they assert |

Evidence is about **the code that implements the step**, not the test that checks it.

## 4. Goal detection heuristics

Strong signals:

- Analytics events with completion verbs: `*_completed`, `*_succeeded`, `*_submitted`,
  `*_created`, `*_purchased`, `*_exported`, `signed_up`, `subscribed`.
- Routes or screens named `success`, `confirmation`, `thank-you`, `done`, `complete`.
- Assignments of terminal statuses: `submitted`, `paid`, `shipped`, `approved`, `published`.

Weak signals (verify before using):

- Toast or snackbar success messages.
- Redirects after a POST.

Not goals:

- Page or screen views, clicks, impressions, errors.

## 5. Surface item buckets

Use the most specific truthful reason. If you are unsure whether something is dead or merely
unreached by your tracing, use orphan `no_user_path_found` or `needs_review`, not
`unreachable: dead`. "Dead" is a strong claim.

- `out_of_scope` only for items whose domain is outside `run_scope.domains`. The validator checks
  the domain (on the orphan, or on the surface item).
- `not_mapped_this_run` for an in-scope item whose user path you found but didn't map (time or
  budget). The note names the path, e.g. "called from `my-appointments/page.tsx:377`". Never
  hide it as `out_of_scope` or `no_user_path_found`.
- View and progress events the extractor reports as goals (`checkout_started`,
  `*_viewed`) stay in the surface. Cover them from the journey whose step fires them; otherwise
  orphan them with the reason that applies.

## 6. Automation status anchors

| Status | Anchor |
|---|---|
| `automated` | Every main-path step is exercised and every main-path Expect is asserted by at least one test. Variants may be untested |
| `partial` | Some steps or variants are exercised, or the path is exercised without asserting its outcomes |
| `none` | No test exercises the journey. Unit tests of components used by the journey don't count |

A test-client test (`kind: http-client`: Flask's or Django's test client, supertest) that requests
the journey's pages or endpoints and asserts the response text or status exercises those steps at
the HTTP level. It can make a journey `partial`, never `automated`: it runs no browser script,
clicks nothing and follows no client-side navigation.

What counts as asserting an Expect:

- A navigation wait (`waitForURL`, `toHaveURL`, `cy.url().should`) asserts an Expect only when
  that Expect is the navigation itself. It doesn't assert text, state or data on the new page.
- Steps run inside helper functions (`login()`, `registerAccount()`) count for the spec that
  calls the helper. List the spec in `tests`, not the helper file, and read the helper to see
  what it asserts.

## 7. Quality checklist before finishing

- [ ] Every journey's title is an outcome for a persona.
- [ ] Every step has an observable Expect and code evidence.
- [ ] Error paths for every form submission in a P1 journey are captured as variants, or noted as
      open questions if the code doesn't handle them.
- [ ] Personas match the application's real role model.
- [ ] Every surface item is covered, orphaned or unreachable, and the numbers add up.
- [ ] Unintended-looking behaviour is in open questions, not baked into steps.
- [ ] Validator passes; strict lint passes.
