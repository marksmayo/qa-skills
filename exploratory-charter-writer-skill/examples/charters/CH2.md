# CH2: Explore the gift card balance page

**with** cards with known balances and their redemption history **to discover** whether the balance shown matches what was redeemed

| | |
|---|---|
| Priority | Next (impact 2 x uncertainty 2 = 4) |
| Time box | 90 minutes |
| Personas | First-time customer |
| Heuristics | Follow the data, Zero, one, many |
| Oracles | Claims, Product |

## Areas
- balance after a redemption
- unknown code
- a card used on several orders

## Setup
- Redeem a $50 card across two orders first

## Risks to look for
- Balance not updated, or updated twice

## Out of scope
- Checkout

## Why this charter exists
- `ticket:GIFT-7#AC3`: "The balance shown on the Gift cards page goes down by the amount redeemed."
- `journey:JRN-GFT-001#step1`: "The remaining balance is shown"
- `diff:larder/web/giftcards_page.py`: "modified larder/web/giftcards_page.py"
