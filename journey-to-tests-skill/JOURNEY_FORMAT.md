# Journey Markdown format

Version 1.0. This file is the contract between skills that **write** user journeys (Journey
Mapper) and skills that **read** them (Journey to Tests). People may also write journeys by
hand.

A journey is one Markdown file describing how one persona reaches one goal. The file has YAML
front matter, then a fixed set of `##` sections.

`scripts/journey_md.py` parses, lints and renders this format:

```bash
python scripts/journey_md.py lint journeys/ --strict    # generated journeys
python scripts/journey_md.py lint journeys/              # hand-written journeys (warnings allowed)
python scripts/journey_md.py parse journeys/checkout/JRN-CHK-001-guest-checkout.md
```

## Directory layout

```text
journeys/
  index.md               # generated: journeys by domain, priority, automation status
  coverage.md            # generated: surface items ↔ journeys, orphans, open questions
  journey-graph.json     # generated: screen/state interaction graph
  journey_map.json       # generated: machine-readable report of the last mapping run
  <domain>/JRN-<DOMAIN>-<NNN>-<slug>.md
```

When the linter is given a directory, it lints only the files whose first line is `---`, so
`index.md` and `coverage.md` are skipped.

## Example

```markdown
---
id: JRN-CHK-001
title: Guest completes checkout with card
domain: checkout
platforms: [web, ios, android]
persona: guest
priority: P1
priority_reason: Revenue path; primary goal event order_completed
status: inferred
entry_point: /products/:slug
goal: Order confirmation shown and order created
flags: [new_checkout]
automation: none
tests: []
---

## Preconditions
- Product with stock > 0 exists
- User is not signed in

## Steps
1. **Action:** Open a product page
   **Expect:** Product title, price and "Add to cart" are visible
2. **Action:** Click "Add to cart"
   **Expect:** Cart count shows 1
3. **Action:** Go to checkout and fill shipping and card details
   **Expect:** "Pay now" becomes enabled
4. **Action:** Click "Pay now"
   **Expect:** Confirmation page shows an order number

## Variants
- **V1 – card declined** (from step 4): **Action:** Pay with a card the provider declines **Expect:** Inline error; user stays on the payment step
- **V2 – out of stock** (from step 2): **Expect:** "Add to cart" is disabled with an out-of-stock message

## Test data
- Card: payment provider sandbox test card (declined card for V1)

## Not automatable
- (none)

## Open questions
- (none)

## Evidence
- Step 1: `src/app/products/[slug]/page.tsx:12` — product page route
- Step 2: `src/features/cart/useAddToCart.ts:30` — POST /api/cart
- Step 3: `src/app/checkout/page.tsx:8` — checkout form
- Step 4: `src/features/checkout/Confirm.tsx:44` — track('order_completed')
```

## Front matter

The front matter is deliberately a **restricted subset of YAML**, so it can be parsed without
dependencies. Each line is either `key: value` or `key: [a, b, c]`. Values may be quoted with
`"` or `'`, and a `#` preceded by whitespace starts a comment. Block lists, nested maps and
multi-line strings are **not** supported.

| Key | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | `JRN-<DOMAIN>-<NNN>` | Stable identifier. Never reused for a different journey |
| `title` | yes | text | The user goal, phrased as an outcome |
| `domain` | recommended | slug | Functional area, e.g. `checkout`, `account`. Also the subdirectory name |
| `platforms` | recommended | list | Any of `web`, `ios`, `android`, `desktop`, `api`, `cli` |
| `persona` | yes | slug | A role from the application's role model, e.g. `guest`, `member`, `admin` |
| `priority` | yes | `P1` \| `P2` \| `P3` | P1 critical, P2 important, P3 nice to have |
| `priority_reason` | recommended | text | Why this priority was chosen |
| `status` | yes | `inferred` \| `reviewed` \| `approved` | `inferred` = derived from code by a tool or agent. Only a person promotes it |
| `entry_point` | yes | text | Route, screen name or deep link where the journey starts |
| `goal` | yes | text | The observable end state that means the user succeeded |
| `flags` | no | list | Feature flags the path depends on |
| `automation` | no | `none` \| `partial` \| `automated` | Existing automated test coverage of this journey |
| `tests` | no | list | Test file paths that exercise this journey. Required when `automation` is not `none` |

## Sections

The sections appear in this order. Write `- (none)` in a section that has no entries.

| Section | Format |
|---|---|
| `## Preconditions` | Bullet list of states that must hold before step 1 |
| `## Steps` | Numbered list starting at 1. Each item is `N. **Action:** …` followed by an indented `**Expect:** …` line (or both on one line). **Every step must have an Expect** — it becomes the test assertion |
| `## Variants` | One bullet per variant: `**V<n> – <name>** (from step <N>): [**Action:** …] **Expect:** …`. A variant branches from an existing step. Single line |
| `## Test data` | Free text or bullets describing data the journey needs |
| `## Not automatable` | Bullets of `step <N>: <reason>` or `V<n>: <reason>` for expectations a test can't check (e.g. visual judgement of imagery) |
| `## Open questions` | Bullets. Gaps in the evidence, or behaviour that looks unintended |
| `## Notes` | Optional. **Owned by people**: tools must preserve this section verbatim when they rewrite a file |
| `## Evidence` | Bullets of `Step <N>: \`path:line\` — note`. A bullet without `Step <N>:` is journey-level evidence |

## Rules

1. **Status belongs to people.** Tools always write `status: inferred`. They never overwrite a
   file whose status is `reviewed` or `approved`. Instead they write their proposed version
   next to it as `<file>.proposed.md`.
2. **IDs are stable.** Re-running a tool updates the file with the same ID in place. It never
   creates a duplicate, and it never deletes a journey file. Journeys that a tool no longer finds
   are reported as stale.
3. **Tests are additive.** Tools merge `tests` with the existing list; they don't replace it.
4. **Evidence is required for generated journeys.** In strict lint, every step must have at
   least one evidence bullet containing a `` `path:line` `` reference.
5. **Gherkin mapping.** Preconditions map to `Given`, each Action to `When`, and each Expect to
   `Then`.

## Lint levels

| Problem | Default lint | `--strict` |
|---|---|---|
| Unparseable front matter, missing required key, invalid enum value, bad ID | error | error |
| No steps, step numbering gap, step without Action or Expect | error | error |
| Variant from an unknown step, duplicate variant ID, unparseable variant | error | error |
| `Not automatable` or `Evidence` entry pointing at an unknown step or variant | error | error |
| `automation` is `partial` or `automated` but `tests` is empty | error | error |
| Duplicate journey ID across files (`.proposed.md` files excepted) | error | error |
| Missing evidence, step without evidence, evidence without `path:line` | warning | error |
| Unknown front matter key or section, unknown platform, missing domain | warning | error |
| File name doesn't start with the ID, unstructured `Not automatable` entry | warning | error |
