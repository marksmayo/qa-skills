# CH1: Explore redeeming a gift card at checkout

**with** $5, $50 and $500 test cards, two browser tabs and the gateway sandbox **to discover** whether the amount charged and the remaining balance are always right

| | |
|---|---|
| Priority | Do first (impact 3 x uncertainty 3 = 9) |
| Time box | 90 minutes |
| Personas | First-time customer, Adversarial user, Power user |
| Heuristics | Goldilocks, Sequence variations, Starve, Follow the data |
| Oracles | Claims, User expectations |

## Areas
- part of the order paid by gift card
- whole order paid by gift card
- unknown or used-up code
- two tabs redeeming the same card
- refund of an order paid partly by gift card
- a $500 card on a $1 order

## Setup
- Create $5, $50 and $500 gift cards in the back office
- Log in as the test customer with a sandbox card saved

## Risks to look for
- Charging the card for money the gift card covered
- A balance that goes below zero or never goes down
- Rounding at the limits

## Out of scope
- Buying gift cards
- Gift card emails

## Why this charter exists
- `ticket:GIFT-7#AC1`: "reduces the amount charged to the card by up to its balance"
- `ticket:GIFT-7#AC2`: "If the gift card covers the whole order, no card payment is taken."
- `ticket:GIFT-7#AC4`: "An unknown or used-up code shows "This gift card can't be used" and nothing is charged."
- `risk:giftcards`: "flags novel, surfaces money, 0 test file(s)"
- `risk:payments`: "payments: High risk 17.9"
- `journey:JRN-CHK-001#step2`: "Order confirmed and the card charged once"
- `ticket:GIFT-7#AC1`: "up to its balance"
