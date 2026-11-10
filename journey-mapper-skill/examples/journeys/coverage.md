# Journey coverage of the application surface

Run scope: all domains. Generated 2026-11-09T10:00:00+00:00.

| Measure | Count |
|---|---|
| Surface items found | 20 |
| In at least one journey | 15 (75%) |
| Orphans (no journey) | 3 |
| Unreachable | 2 |

> The surface was found by static analysis plus agent review. Items the analysis could not see (e.g. dynamically built routes) are not counted, so this is a lower bound on the real surface.

## Routes

| ID | Name | Location | Status |
|---|---|---|---|
| R-001 | `/` | `web/src/app/page.tsx:1` | JRN-CHK-001 |
| R-002 | `/products/:slug` | `web/src/app/products/[slug]/page.tsx:1` | JRN-CHK-001 |
| R-003 | `/checkout` | `web/src/app/(shop)/checkout/page.tsx:1` | JRN-CHK-001 |
| R-004 | `/checkout/confirmation` | `web/src/app/(shop)/checkout/confirmation/page.tsx:1` | JRN-CHK-001 |
| R-005 | `/login` | `web/src/app/login/page.tsx:1` | JRN-ACC-001 |
| R-006 | `/account` | `web/src/app/account/page.tsx:1` | JRN-ACC-001 |
| R-007 | `/admin/reports` | `web/src/app/admin/reports/page.tsx:1` | unreachable (flag_off) |
| R-008 | `/legacy-cart` | `web/src/app/legacy-cart/page.tsx:1` | unreachable (dead) |

## Screens

| ID | Name | Location | Status |
|---|---|---|---|
| S-001 | `/login` | `mobile/app/login.tsx:1` | JRN-ACC-001 |
| S-002 | `/product/:id` | `mobile/app/product/[id].tsx:1` | JRN-CHK-001 |
| S-003 | `/cart` | `mobile/app/cart.tsx:1` | JRN-CHK-001 |

## API endpoints

| ID | Name | Location | Status |
|---|---|---|---|
| E-001 | `POST /api/cart` | `web/src/app/api/cart/route.ts:12` | JRN-CHK-001 |
| E-002 | `POST /api/orders` | `web/src/app/api/orders/route.ts:9` | JRN-CHK-001 |
| E-003 | `POST /api/session` | `web/src/app/api/session/route.ts:7` | JRN-ACC-001 |
| E-004 | `GET /api/health` | `web/src/app/api/health/route.ts:3` | **orphan** (not_user_facing) — Load balancer health check |
| E-005 | `POST /api/webhooks/stripe` | `web/src/app/api/webhooks/stripe/route.ts:15` | **orphan** (not_user_facing) — Payment provider webhook; covered indirectly by order confirmation |

## Entry points

| ID | Name | Location | Status |
|---|---|---|---|
| N-001 | `acme://product/` | `mobile/app.json:14` | JRN-CHK-001 |

## Goal events

| ID | Name | Location | Status |
|---|---|---|---|
| G-001 | `order_completed` | `web/src/app/(shop)/checkout/confirmation/page.tsx:21` | JRN-CHK-001 |
| G-002 | `login_succeeded` | `web/src/features/auth/useLogin.ts:34` | JRN-ACC-001 |
| G-003 | `newsletter_subscribed` | `web/src/components/NewsletterForm.tsx:18` | **orphan** (needs_review) — NewsletterForm is defined but no page renders it |

## Open questions

- **Q-001:** Is the admin reports page (R-007) expected to ship? It is behind admin_reports, which is off everywhere.
- **JRN-CHK-001:** Confirmation page does not clear the cart badge until reload (CheckoutForm.tsx:130). Intended?
