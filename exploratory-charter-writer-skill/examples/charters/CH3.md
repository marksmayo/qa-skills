# CH3: Explore gift card codes from an attacker's point of view

**with** a proxy to edit requests, two customer accounts **to discover** whether codes can be guessed, reused or redeemed by someone else

| | |
|---|---|
| Priority | Do first (impact 3 x uncertainty 3 = 9) |
| Time box | 90 minutes |
| Personas | Adversarial user |
| Heuristics | Sequence variations, Starve |
| Oracles | Statutes and standards, Familiar problems |

## Areas
- guessing codes
- replaying a redemption request
- redeeming another customer's card

## Setup
- Proxy (for example OWASP ZAP) between the browser and staging

## Risks to look for
- Free money through replay or guessing

## Out of scope
- Card payments

## Why this charter exists
- `risk:giftcards`: "surfaces money"
- `diff:larder/giftcards/cards.py`: "added larder/giftcards/cards.py"
