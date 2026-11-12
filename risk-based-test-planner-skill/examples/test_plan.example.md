# Test plan: larder 1.5 (since v1.4), 16 sessions

Payments (risk 16.6) and auth (High because of open security finding F-1) get most of the 16 sessions; gift cards are new and get an exploratory charter; reports is not tested beyond its existing test, with the reason.

## Scope
- Repository `larder` at `d834c7e13dc8`; scope: git since `v1.4`
- History: 2026-05-05 to 2026-11-01, 41 commits; 1 formatting-only commit(s) left out of churn
- Effort: 16 sessions of 90 minutes
- Inputs used: security (security_audit.json), journey-mapper (journey_map.json); missing: usage export, Jira bug search
- Areas: subdirectories of the main source directory larder/

## Risk register

| Area | Likelihood | Impact | Risk | Band | Confidence | Flags |
|---|---|---|---|---|---|---|
| payments | 3.6 | 4.6 | 16.6 | High | high | - |
| giftcards | 3.0 | 4.6 | 13.8 | Medium | low | novel |
| catalog | 2.7 | 3.3 | 8.9 | Medium | high | - |
| auth | 1.8 | 4.1 | 7.4 | High | high | open_findings |
| marketing | 2.9 | 2.0 | 5.8 | Low | high | - |
| reports | 2.2 | 1.6 | 3.5 | Low | high | - |

### Evidence per area
**payments** (`larder/payments`)
- Churn: 4 - 40 lines changed in 13 commits by 4 author(s), 2026-05-05 to 2026-11-01 (formatting-only commits excluded)
- Defect history: 5 - 6 fix commit(s) in the window; 048c970b0dec fix: regression in partial refunds (PAY-57); 229928e8cd68 hotfix: revert currency change, charge in the order currency
- Complexity: 3 - 3 branches in 2 file(s), 1.5 per file; 39 lines of code (complexity.py)
- Change in scope: 2 - 6 lines changed in the scope diff (percentile 3, capped at 2 for a change this size)
- Test weakness: 3 - 1 test file(s) import it (tests/test_payments.py); 8 test lines for 39 lines of code (ratio 0.21)
- Journey criticality: 5 - JRN-CHK-001 (P0) Customer pays for an order - evidence (larder/payments/gateway.py)
- Surface: 5 - moves money: larder/payments/gateway.py:13, larder/payments/gateway.py:17, larder/payments/service.py:6 (+5); external integration: larder/payments/gateway.py:2, larder/payments/gateway.py:8, larder/payments/gateway.py:9
- Reach: 2 - cited by 1 of 4 journeys (journey graph)
- Open finding: security F-2 Gateway timeout is long enough to hold worker threads (low from cvss.severity Low, confidence possible)

**giftcards** (`larder/giftcards`)
- Churn: - (not assessed) - no history before the scope: every file is new
- Defect history: - (not assessed) - no history before the scope: every file is new
- Complexity: 1 - 1 branches in 1 file(s), 1.0 per file; 10 lines of code (complexity.py)
- Change in scope: 2 - 15 lines changed in the scope diff (percentile 4, capped at 2 for a change this size)
- Test weakness: 5 - no test imports this area
- Journey criticality: 5 - JRN-CHK-001 (P0) Customer pays for an order - larder/giftcards/cards.py imported by larder/payments/service.py
- Surface: 5 - moves money: larder/giftcards/cards.py:6, larder/giftcards/cards.py:12
- Reach: 2 - cited by 1 of 4 journeys (journey graph)

**catalog** (`larder/catalog`)
- Churn: 3 - 8 lines changed in 3 commits by 2 author(s), 2026-05-05 to 2026-11-01 (formatting-only commits excluded)
- Defect history: 4 - 1 fix commit(s) in the window; db9dbcef59e2 fix: flour price was in dollars, not cents
- Complexity: 1 - 1 branches in 1 file(s), 1.0 per file; 10 lines of code (complexity.py)
- Change in scope: 1 - 0 lines changed in the scope diff
- Test weakness: 3 - 1 test file(s) import it (tests/test_catalog.py); 3 test lines for 10 lines of code (ratio 0.30)
- Journey criticality: 5 - JRN-CHK-001 (P0) Customer pays for an order - evidence (larder/catalog/products.py); JRN-BRW-001 (P2) Customer browses an aisle - evidence (larder/catalog/products.py)
- Surface: 1 - no surface pattern matched in data/surfaces.json
- Reach: 5 - cited by 2 of 4 journeys (journey graph)

**auth** (`larder/auth`)
- Churn: 1 - 2 lines changed in 1 commits by 1 author(s), 2026-05-05 to 2026-11-01 (formatting-only commits excluded)
- Defect history: 1 - 0 fix commit(s) in the window
- Complexity: 1 - 1 branches in 1 file(s), 1.0 per file; 12 lines of code (complexity.py)
- Change in scope: 1 - 0 lines changed in the scope diff
- Test weakness: 5 - no test imports this area
- Journey criticality: 4 - JRN-ACC-001 (P1) Customer signs in - evidence (larder/auth/session.py)
- Surface: 5 - controls access: larder/auth/session.py:8, larder/auth/session.py:12, larder/auth/session.py:13 (+1)
- Reach: 2 - cited by 1 of 4 journeys (journey graph)
- Open finding: security F-1 Session tokens never expire server-side (high from cvss.severity High, confidence likely)

**marketing** (`larder/marketing`)
- Churn: 5 - 420 lines changed in 21 commits by 4 author(s), 2026-05-05 to 2026-11-01 (formatting-only commits excluded)
- Defect history: 1 - 0 fix commit(s) in the window
- Complexity: 4 - 2 branches in 1 file(s), 2.0 per file; 17 lines of code (complexity.py)
- Change in scope: 2 - 20 lines changed in the scope diff (percentile 5, capped at 2 for a change this size)
- Test weakness: 4 - 1 test file(s) import it (tests/test_marketing.py); 3 test lines for 17 lines of code (ratio 0.18)
- Journey criticality: 3 - JRN-BRW-001 (P2) Customer browses an aisle - evidence (larder/marketing/banners.py)
- Surface: 1 - no surface pattern matched in data/surfaces.json
- Reach: 2 - cited by 1 of 4 journeys (journey graph)

**reports** (`larder/reports`)
- Churn: 2 - 4 lines changed in 1 commits by 1 author(s), 2026-05-05 to 2026-11-01 (formatting-only commits excluded)
- Defect history: 1 - 0 fix commit(s) in the window
- Complexity: 5 - 18 branches in 1 file(s), 18.0 per file; 36 lines of code (complexity.py)
- Change in scope: 1 - 0 lines changed in the scope diff
- Test weakness: 4 - 1 test file(s) import it (tests/test_reports.py); 3 test lines for 36 lines of code (ratio 0.08)
- Journey criticality: 2 - JRN-ADM-001 (P3) Store manager reads the weekly stock report - evidence (larder/reports/stock.py)
- Surface: 1 - no surface pattern matched in data/surfaces.json
- Reach: 2 - cited by 1 of 4 journeys (journey graph)

## Approach and effort

| Area | Band | Sessions | Test types |
|---|---|---|---|
| payments | High | 6 | journey, exploratory, contract, mutation |
| auth | High | 4 | security, unit, exploratory |
| giftcards | Medium | 3 | exploratory, unit, regression |
| catalog | Medium | 2 | regression, journey |
| marketing | Low | 1 | smoke |

**payments**
- journey (journey-to-tests): Checkout and refund are the P0 journey; automate the card and refund paths end to end.
- exploratory (exploratory-charter-writer): Six fix commits in six months, five of them on refunds or currency: explore around them.
- contract (api-contract-tester): The gateway is an external HTTP API; pin the charge and refund request shapes.
- mutation (mutation-tester): The two payments tests only check rejections; measure what the refund and fee logic tests miss.
- Charter: Explore refunds across every reason, partial amounts and currencies to discover where the six recent fixes left gaps
- Charter: Explore paying with a gift card plus a card, including a card that covers the whole order

**auth**
- security (code-security-auditor): Retest security finding F-1 (session tokens never expire server-side) once fixed.
- unit (ticket-to-tdd): No test imports auth at all: add unit tests for login, password hashing and permissions.
- exploratory (exploratory-charter-writer): Sign-in is the P1 journey and the area has no tests to lean on.
- Charter: Explore sign-in, session length and permission checks with expired, reused and tampered tokens

**giftcards**
- exploratory (exploratory-charter-writer): New in this release with no history and no tests; it sits on the P0 checkout journey because payments now imports it.
- unit (ticket-to-tdd): Issue and redeem limits ($5 to $500, partial redemption) need unit tests.
- regression: Payments now calls redeem(): run the payments regression suite with gift cards.
- Charter: Explore issuing and redeeming gift cards at the $5 and $500 limits and redeeming more than the balance

**catalog**
- regression: Existing catalog test plus the browse journey after the autumn price changes.
- journey (journey-to-tests): Catalog sits on the P0 checkout and P2 browse journeys; cover search and price display.

**marketing**
- smoke: Banner copy changes weekly but moves nothing: a smoke check that every aisle gets a banner.

## Not tested
- **reports** (residual risk Low): Low risk (3.5): the most complex area, but one 4-line change in six months, no fixes, a P3 admin journey and an existing unit test. Its existing test keeps running in CI. Accepted by: nobody yet.

## Entry criteria
- The release candidate is deployed to the staging environment with the gateway sandbox.
- Security finding F-1 has a fix in the release candidate, or an accepted exception.
- Gift card issue and redeem are feature-complete and behind no flag in staging.

## Exit criteria
- Every High-risk area's planned sessions are done and their charters debriefed (session sheets for payments and auth)
- No open critical or high defect in payments, auth or gift cards (defect tracker query for this release)
- The checkout and sign-in journeys pass in staging (journey test run, linked)
- F-1 retested (code-security-auditor retest report)

## Assumptions
- Fix commits are found by message (a fix:, hotfix: or revert: type, or a subject starting with fix, bugfix, hotfix, revert or regression, or naming a bug); merges are not counted, and a fix with another message is missed.
- Reach comes from the journey graph, because no usage export was given.
- The journey map predates gift cards; they reach the P0 checkout journey only through the payments service's import, so their journey criticality rests on that one link until the map is refreshed.

## Open questions
- Should gift cards get a journey in the journey map before release? Run journey-mapper.
- Is the stock report used outside head office? If so, reports may deserve more than its existing test.
