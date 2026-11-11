# Example: SHOP-310, loyalty points at checkout

`SHOP-310.issue.json` is a Jira issue (as a connector returns it) for the fictional shop of the ticket
readiness reviewer's examples. Each of its five criteria is planted to need a different technique.
`run/` holds the normalised ticket, the analysis and the generated rows, and `model.json` is the agent's
model. `test_cases.example.json` holds the cases, and `xray.json`, `cases.csv` and `cases.md` are exported
from them. Every step is listed in `commands.sh`.

## Answer key

| Criterion | Planted | Technique and rows | Cases |
|---|---|---|---|
| AC-1 | points from $10.00 to $500.00 inclusive, none below, 500 above | boundary (6 rows: $9.99, $10.00, $10.01, $499.99, $500.00, $500.01) and partition (3 classes) | 9; the six boundary cases and the "below $10" case are P0 (money) |
| AC-2 | Gold 2x, Silver 1.5x rounded down, Bronze 1x; DOUBLEUP +1 per dollar, never on an all-gift-card order | decision table: tier x DOUBLEUP x all gift card, the full 12 rows | 12; the three "promotion refused on gift card" cases are P0; the three DOUBLEUP-on card cases depend on Q-2 |
| AC-3 | pending when placed, available when delivered, removed if cancelled before shipping or returned; a shipped order can't be cancelled | state transition: 4 valid transitions and 16 refused events | 10: one per valid transition, "a shipped order can't be cancelled" on its own, and the other 15 refusals grouped by state |
| AC-4 | every payment method x every delivery option x every tier | pairwise: all 27 pairs in 10 rows | 10 |
| AC-5 | "the balance updates promptly after delivery" | scenario; "promptly" becomes Q-1 | 1, depending on Q-1 |

Questions and dismissals:

- **Q-1** (lexicon): "promptly" has no number.
- **Q-2** (agent judgment): is the DOUBLEUP extra point added after the tier multiplier, or multiplied by
  it? The analysis can't see this; the agent raised it, and the three cases it affects say so.
- **Decoy**: "available" in AC-3 matches the lexicon's non-functional terms ("high availability"), but here
  it is a points status, so it is dismissed with that reason. The analysis also suggests a decision table
  for AC-3's "when" and "if"; that suggestion is dismissed because the state model covers them.

All 52 rows are covered by the 42 cases (10 P0, 32 P1). The test data uses `@example.test` members, the
published test card 4111 1111 1111 1111, and made-up gift card codes.

```bash
python scripts/validate_cases.py examples/test_cases.example.json --ticket examples/run/ticket.json --model examples/model.json --rows examples/run/rows.json --xray examples/xray.json --csv examples/cases.csv
python scripts/render_report.py examples/test_cases.example.json --brief
```

Run these from `test-case-designer-skill/`. The cases fingerprint the normalised ticket, so editing
`SHOP-310.issue.json` or `model.json` invalidates them until they are rebuilt and re-exported.
