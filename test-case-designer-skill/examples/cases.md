# Test cases for SHOP-310: Members earn loyalty points at checkout

42 cases for 5 of 5 criteria; design rows 52 of 52 covered; 2 open question(s). By priority: P0 10, P1 32.

## Coverage

| Criterion | Techniques | Cases | Status |
|---|---|---|---|
| AC-1 | boundary, partition | 9 | covered |
| AC-2 | decision table | 12 | covered with questions |
| AC-3 | state transition | 10 | covered |
| AC-4 | pairwise | 10 | covered |
| AC-5 | scenario | 1 | covered with questions |

## Open questions

- **Q-1** (AC-5, "promptly"): "Promptly" needs a number and a unit: within how many seconds or minutes of delivery must the balance show the new points? Blocks TC-042.
- **Q-2** (AC-2, "1 extra point per whole dollar"): Is the DOUBLEUP extra point added after the tier multiplier (Gold: 2 + 1 = 3 points per dollar) or multiplied by it (Gold: 2 x (1 + 1) = 4)? The cases assume added after. Blocks TC-011, TC-015, TC-019.

## AC-1

> Members earn 1 point per whole dollar of the order subtotal for orders from $10.00 to $500.00 inclusive. Orders below $10.00 earn no points, and orders above $500.00 earn 500 points.

- **boundary**: A points range on the subtotal, $10.00 to $500.00 inclusive, with different outcomes either side of each limit.
- **partition**: Three classes with three outcomes: no points below $10.00, 1 per whole dollar within, a cap of 500 above $500.00.

### TC-001 Subtotal $9.99 earns 0 points (below the $10.00 limit)

boundary, P0; rows AC-1-BVA-01; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $9.99 | The basket shows a subtotal of $9.99 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 0 pending points |

### TC-002 Subtotal $10.00 earns 10 points (on the $10.00 limit)

boundary, P0; rows AC-1-BVA-02; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $10.00 | The basket shows a subtotal of $10.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 10 pending points |

### TC-003 Subtotal $10.01 earns 10 points (above the $10.00 limit)

boundary, P0; rows AC-1-BVA-03; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $10.01 | The basket shows a subtotal of $10.01 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 10 pending points |

### TC-004 Subtotal $499.99 earns 499 points (below the $500.00 limit)

boundary, P0; rows AC-1-BVA-04; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $499.99 | The basket shows a subtotal of $499.99 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 499 pending points |

### TC-005 Subtotal $500.00 earns 500 points (on the $500.00 limit)

boundary, P0; rows AC-1-BVA-05; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $500.00 | The basket shows a subtotal of $500.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 500 pending points |

### TC-006 Subtotal $500.01 earns 500 points (above the $500.00 limit)

boundary, P0; rows AC-1-BVA-06; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $500.01 | The basket shows a subtotal of $500.01 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 500 pending points |

### TC-007 Subtotal below $10.00: $4.50 earns 0 points

negative, P0; rows AC-1-EP-01; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $4.50 | The basket shows a subtotal of $4.50 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 0 pending points |

### TC-008 Subtotal from $10.00 to $500.00: $123.45 earns 123 points

positive, P1; rows AC-1-EP-02; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $123.45 | The basket shows a subtotal of $123.45 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 123 pending points |

### TC-009 Subtotal above $500.00: $842.10 earns 500 points

positive, P1; rows AC-1-EP-03; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $842.10 | The basket shows a subtotal of $842.10 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 500 pending points |

## AC-2

> Gold members earn double points and Silver members earn 1.5 times the points, rounded down; Bronze members earn the base points. When the DOUBLEUP promotion is active, every tier earns 1 extra point per whole dollar, but the promotion never applies to an order paid entirely with a gift card.

- **decision table**: The points depend on three conditions together: the tier, whether DOUBLEUP is active, and whether a gift card paid the whole order (which switches the promotion off).

### TC-010 Gold, DOUBLEUP on, all gift card: $100.00 earns 200 points

negative, P0; rows AC-2-DT-01; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion active in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0100, balance $100.00) | gift card (test gift card GC-TEST-0100, balance $100.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 200 pending points (the promotion does not apply to an order paid entirely by gift card) |

### TC-011 Gold, DOUBLEUP on, card: $100.00 earns 300 points

positive, P1; rows AC-2-DT-02; depends on Q-2; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion active in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 300 pending points (Gold rate plus 1 DOUBLEUP point per dollar) |

### TC-012 Gold, DOUBLEUP off, all gift card: $100.00 earns 200 points

positive, P1; rows AC-2-DT-03; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion switched off in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0100, balance $100.00) | gift card (test gift card GC-TEST-0100, balance $100.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 200 pending points (Gold rate) |

### TC-013 Gold, DOUBLEUP off, card: $100.00 earns 200 points

positive, P1; rows AC-2-DT-04; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion switched off in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 200 pending points (Gold rate) |

### TC-014 Silver, DOUBLEUP on, all gift card: $100.00 earns 150 points

negative, P0; rows AC-2-DT-05; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion active in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0100, balance $100.00) | gift card (test gift card GC-TEST-0100, balance $100.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 150 pending points (the promotion does not apply to an order paid entirely by gift card) |

### TC-015 Silver, DOUBLEUP on, card: $100.00 earns 250 points

positive, P1; rows AC-2-DT-06; depends on Q-2; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion active in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 250 pending points (Silver rate plus 1 DOUBLEUP point per dollar) |

### TC-016 Silver, DOUBLEUP off, all gift card: $100.00 earns 150 points

positive, P1; rows AC-2-DT-07; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion switched off in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0100, balance $100.00) | gift card (test gift card GC-TEST-0100, balance $100.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 150 pending points (Silver rate) |

### TC-017 Silver, DOUBLEUP off, card: $100.00 earns 150 points

positive, P1; rows AC-2-DT-08; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion switched off in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 150 pending points (Silver rate) |

### TC-018 Bronze, DOUBLEUP on, all gift card: $100.00 earns 100 points

negative, P0; rows AC-2-DT-09; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion active in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0100, balance $100.00) | gift card (test gift card GC-TEST-0100, balance $100.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 100 pending points (the promotion does not apply to an order paid entirely by gift card) |

### TC-019 Bronze, DOUBLEUP on, card: $100.00 earns 200 points

positive, P1; rows AC-2-DT-10; depends on Q-2; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion active in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 200 pending points (Bronze rate plus 1 DOUBLEUP point per dollar) |

### TC-020 Bronze, DOUBLEUP off, all gift card: $100.00 earns 100 points

positive, P1; rows AC-2-DT-11; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion switched off in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0100, balance $100.00) | gift card (test gift card GC-TEST-0100, balance $100.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 100 pending points (Bronze rate) |

### TC-021 Bronze, DOUBLEUP off, card: $100.00 earns 100 points

positive, P1; rows AC-2-DT-12; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points; DOUBLEUP promotion switched off in the admin console

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $100.00 | The basket shows a subtotal of $100.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 100 pending points (Bronze rate) |

## AC-3

> Points are pending when the order is placed, become available when it is delivered, and are removed if the order is cancelled before it ships or returned after delivery. A shipped order can't be cancelled.

- **state transition**: Points follow the order's lifecycle (placed, shipped, delivered, cancelled, returned), and one transition is forbidden (cancelling a shipped order).

### TC-022 Placed order, ship: points become pending

positive, P1; rows AC-3-ST-01; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped; the account page shows the 80 points as pending; the points balance is 0 |

### TC-023 Shipped order, deliver: points become available

positive, P1; rows AC-3-ST-02; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped and its 80 points are pending |
| 3 | Mark the order as delivered in the admin console |  | The order status is delivered; the account page shows the 80 points as available; the points balance is 80 |

### TC-024 Placed order, cancel: points become removed

positive, P1; rows AC-3-ST-03; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Cancel the order from the account page |  | The order status is cancelled; the account page shows the 80 points as removed; the points balance is 0 |

### TC-025 Delivered order, return: points become removed

positive, P1; rows AC-3-ST-04; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped and its 80 points are pending |
| 3 | Mark the order as delivered in the admin console |  | The order status is delivered and its 80 points are available |
| 4 | Record a return for the order in the admin console |  | The order status is returned; the account page shows the 80 points as removed; the points balance is 0 |

### TC-026 A shipped order can't be cancelled, and its points stay pending

negative, P1; rows AC-3-ST-08; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped and its 80 points are pending |
| 3 | Try to cancel the order from the account page |  | There is no Cancel button, or cancelling shows "This order has shipped and can't be cancelled"; the status stays shipped and the 80 points stay pending |

### TC-027 Placed order refuses deliver, return

negative, P1; rows AC-3-ST-05, AC-3-ST-06

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | In the admin console and the account page, look for deliver, return on the order |  | None of deliver, return is offered (or each is refused with a message); the status stays placed and the points stay pending |

### TC-028 Shipped order refuses ship, return

negative, P1; rows AC-3-ST-07, AC-3-ST-09

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped and its 80 points are pending |
| 3 | In the admin console and the account page, look for ship, return on the order |  | None of ship, return is offered (or each is refused with a message); the status stays shipped and the points stay pending |

### TC-029 Delivered order refuses ship, deliver, cancel

negative, P1; rows AC-3-ST-10, AC-3-ST-11, AC-3-ST-12

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped and its 80 points are pending |
| 3 | Mark the order as delivered in the admin console |  | The order status is delivered and its 80 points are available |
| 4 | In the admin console and the account page, look for ship, deliver, cancel on the order |  | None of ship, deliver, cancel is offered (or each is refused with a message); the status stays delivered and the points stay available |

### TC-030 Cancelled order refuses ship, deliver, cancel, return

negative, P1; rows AC-3-ST-13, AC-3-ST-14, AC-3-ST-15, AC-3-ST-16

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Cancel the order from the account page |  | The order status is cancelled and its 80 points are removed |
| 3 | In the admin console and the account page, look for ship, deliver, cancel, return on the order |  | None of ship, deliver, cancel, return is offered (or each is refused with a message); the status stays cancelled and the points stay removed |

### TC-031 Returned order refuses ship, deliver, cancel, return

negative, P1; rows AC-3-ST-17, AC-3-ST-18, AC-3-ST-19, AC-3-ST-20

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test and place an order with a subtotal of $80.00 | subtotal $80.00 | The order is placed and shows 80 pending points |
| 2 | Mark the order as shipped in the admin console |  | The order status is shipped and its 80 points are pending |
| 3 | Mark the order as delivered in the admin console |  | The order status is delivered and its 80 points are available |
| 4 | Record a return for the order in the admin console |  | The order status is returned and its 80 points are removed |
| 5 | In the admin console and the account page, look for ship, deliver, cancel, return on the order |  | None of ship, deliver, cancel, return is offered (or each is refused with a message); the status stays returned and the points stay removed |

## AC-4

> Points are earned with every payment method (card, PayPal, gift card), every delivery option (standard, express, click and collect) and every member tier (Gold, Silver, Bronze).

- **pairwise**: Three independent parameters of three values each (27 combinations); all pairs of values are covered by 10 orders (greedy all-pairs).

### TC-032 Gold member, card, standard: $120.00 earns 240 points

positive, P1; rows AC-4-PW-01; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with standard delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 240 pending points |

### TC-033 Silver member, card, express: $120.00 earns 180 points

positive, P1; rows AC-4-PW-02; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with express delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 180 pending points |

### TC-034 Bronze member, card, click and collect: $120.00 earns 120 points

positive, P1; rows AC-4-PW-03; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with click and collect delivery and pay by card (test card 4111 1111 1111 1111) | card (test card 4111 1111 1111 1111) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 120 pending points |

### TC-035 Silver member, PayPal, standard: $120.00 earns 180 points

positive, P1; rows AC-4-PW-04; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with standard delivery and pay by PayPal (sandbox buyer paypal.buyer@example.test) | PayPal (sandbox buyer paypal.buyer@example.test) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 180 pending points |

### TC-036 Gold member, PayPal, express: $120.00 earns 240 points

positive, P1; rows AC-4-PW-05; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with express delivery and pay by PayPal (sandbox buyer paypal.buyer@example.test) | PayPal (sandbox buyer paypal.buyer@example.test) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 240 pending points |

### TC-037 Bronze member, gift card, standard: $120.00 earns 120 points

positive, P1; rows AC-4-PW-06; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with standard delivery and pay by gift card (test gift card GC-TEST-0120, balance $120.00) | gift card (test gift card GC-TEST-0120, balance $120.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 120 pending points |

### TC-038 Gold member, gift card, click and collect: $120.00 earns 240 points

positive, P1; rows AC-4-PW-07; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as gold.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with click and collect delivery and pay by gift card (test gift card GC-TEST-0120, balance $120.00) | gift card (test gift card GC-TEST-0120, balance $120.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 240 pending points |

### TC-039 Bronze member, PayPal, express: $120.00 earns 120 points

positive, P1; rows AC-4-PW-08; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with express delivery and pay by PayPal (sandbox buyer paypal.buyer@example.test) | PayPal (sandbox buyer paypal.buyer@example.test) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 120 pending points |

### TC-040 Silver member, PayPal, click and collect: $120.00 earns 180 points

positive, P1; rows AC-4-PW-09; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with click and collect delivery and pay by PayPal (sandbox buyer paypal.buyer@example.test) | PayPal (sandbox buyer paypal.buyer@example.test) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 180 pending points |

### TC-041 Silver member, gift card, express: $120.00 earns 180 points

positive, P1; rows AC-4-PW-10; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as silver.member@example.test |  | The account page shows the member's tier and a points balance of 0 |
| 2 | Add items to the basket until the subtotal is exact | subtotal $120.00 | The basket shows a subtotal of $120.00 |
| 3 | Check out with express delivery and pay by gift card (test gift card GC-TEST-0120, balance $120.00) | gift card (test gift card GC-TEST-0120, balance $120.00) | The order confirmation page shows an order number |
| 4 | Open the order in the account page |  | The order shows 180 pending points |

## AC-5

> The points balance on the account page updates promptly after delivery.

- **scenario**: One observable flow (deliver an order, then look at the account page); the timing has no number yet, so the case depends on Q-1.

### TC-042 Balance shows the new points after delivery, within the time agreed in Q-1

positive, P1; rows AC-5-SC-01; depends on Q-1; automation candidate

Preconditions: Staging shop with the loyalty programme on; Test members gold.member@example.test, silver.member@example.test and bronze.member@example.test, each with 0 points

| # | Action | Data | Expected |
|---|---|---|---|
| 1 | Sign in as bronze.member@example.test; note the points balance |  | The account page shows the current balance, B |
| 2 | Place an order with a subtotal of $60.00 and mark it delivered in the admin console; note the time | subtotal $60.00 | The order status is delivered |
| 3 | Reload the account page until the balance changes, noting the time |  | The balance is B + 60, and the time since delivery is within the limit agreed in Q-1 |

## Limitations

- The cases were designed from the ticket alone; no running shop was used, so the screens and messages named in the steps are as the ticket and the comment describe them.
- Cases for the DOUBLEUP rows assume the extra point is added after the tier multiplier (Q-2); if the answer is the other reading, those 3 cases change their expected points.
- The balance timing in AC-5 has no number yet (Q-1); its case can be run, but not passed or failed, until it is answered.
- Pairwise covers every pair of payment, delivery and tier, not every triple (27 combinations, 10 cases).
