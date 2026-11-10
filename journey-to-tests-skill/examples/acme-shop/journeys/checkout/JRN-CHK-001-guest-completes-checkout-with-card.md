---
id: JRN-CHK-001
title: Guest completes checkout with card
domain: checkout
platforms: [web, ios, android]
persona: guest
priority: P1
priority_reason: Revenue path; ends in the primary goal event order_completed
status: inferred
entry_point: /
goal: Confirmation page shows an order number and an order is created
flags: []
automation: partial
tests: [web/e2e/checkout/JRN-CHK-001-guest-checkout.spec.ts]
---

## Preconditions
- At least one product has stock > 0
- User is not signed in

## Steps
1. **Action:** Open the home page and select a product
   **Expect:** Product page shows title, price and an enabled "Add to cart" button
2. **Action:** Click "Add to cart"
   **Expect:** Cart badge shows 1
3. **Action:** Open the cart and continue to checkout
   **Expect:** Checkout form shows shipping and card fields; "Pay now" is disabled
4. **Action:** Fill in valid shipping and card details
   **Expect:** "Pay now" becomes enabled
5. **Action:** Click "Pay now"
   **Expect:** Confirmation page shows an order number

## Variants
- **V1 – card declined** (from step 5): **Action:** Pay with a card the provider declines **Expect:** Inline "Your card was declined" error; user stays on the checkout page
- **V2 – out of stock** (from step 1): **Expect:** "Add to cart" is disabled and shows "Out of stock"
- **V3 – deep link on mobile** (from step 1): **Action:** Open acme://product/<id> on a device **Expect:** App opens directly on the product screen

## Test data
- Stripe test cards: 4242 4242 4242 4242 (success), 4000 0000 0000 0002 (declined)

## Not automatable
- (none)

## Open questions
- Confirmation page does not clear the cart badge until reload (CheckoutForm.tsx:130). Intended?

## Evidence
- Step 1: `web/src/app/page.tsx:22` — ProductCard links to /products/[slug]
- Step 1: `mobile/app/product/[id].tsx:1`
- Step 2: `web/src/features/cart/useAddToCart.ts:30` — POST /api/cart then updates badge count
- Step 3: `web/src/app/(shop)/checkout/page.tsx:40`
- Step 3: `mobile/app/cart.tsx:55`
- Step 4: `web/src/features/checkout/CheckoutForm.tsx:88` — isValid gates the submit button
- Step 5: `web/src/features/checkout/CheckoutForm.tsx:112` — POST /api/orders
- Step 5: `web/src/app/(shop)/checkout/confirmation/page.tsx:21` — track('order_completed')
- V1: `web/src/features/checkout/CheckoutForm.tsx:120`
- V2: `web/src/components/AddToCartButton.tsx:14`
- V3: `mobile/app.json:14`
