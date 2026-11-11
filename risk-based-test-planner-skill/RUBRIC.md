# Risk-based test planner rubric

Rules for `test_plan.json`. The validator enforces everything marked (computed) and every
"must".

## Factors

| Factor | Kind | Weight | Score |
|---|---|---|---|
| `churn` | likelihood | 2 | Mid-rank percentile of lines changed in the window (formatting-only commits excluded); 0 lines = 1 |
| `defects` | likelihood | 3 | Percentile of fix commits plus bug tickets by component or label (each bug counted once); 0 = 1 |
| `complexity` | likelihood | 1 | Percentile of branches per code file |
| `change_in_scope` | likelihood | 2 | Percentile of lines changed in the scope diff; 0 = 1 |
| `test_weakness` | likelihood | 2 | 5 when no test imports the area (flaky tests don't count). Otherwise by test-to-code ratio: < 0.2 = 4, < 0.5 = 3, < 1 = 2, else 1 |
| `journey_criticality` | impact | 3 | Highest linked journey: P0 = 5, P1 = 4, P2 = 3, P3 = 2; a journey map that cites the area nowhere = 1 |
| `surface` | impact | 3 | `data/surfaces.json`: money, access, personal data, destructive = 5; compliance = 4; external = 3; none = 1 |
| `reach` | impact | 1 | Usage share: ≥ 50% = 5, ≥ 25% = 4, ≥ 10% = 3, ≥ 2% = 2, else 1. Without usage data, the percentile of journeys citing the area |

Percentile: p = (areas below + half the ties) / (areas − 1); ≥ 0.9 = 5, ≥ 0.75 = 4, ≥ 0.5 = 3,
≥ 0.25 = 2, else 1.

Each factor has a status, and these rules apply:

| Status | Rule |
|---|---|
| `assessed` | Must have a score and evidence |
| `not_assessed` | Must have no score |
| `judged` | Keeps `computed_score` (the value in `risk.json`) and gives `judged_reason` (20+ characters) |

Every factor that isn't judged must equal `risk.json`.

The `not_assessed` cases:
- churn and defects for `novel` areas, and for shallow clones;
- journey criticality without a journey map;
- reach without usage data or a journey map;
- change in scope without a scope diff.

## Risk (computed)

- **Likelihood and impact:** each is the weighted mean of its assessed factors, to one decimal.
- **Risk:** likelihood × impact, to one decimal.

| Band | Risk | Floor |
|---|---|---|
| **High** | ≥ 15 | Also any area with an open critical or high finding from another skill (`open_findings`) |
| **Medium** | 8 to 14.9 | Also any `novel` area that would be Low |
| **Low** | < 8 | |

**Confidence:**

| Confidence | When |
|---|---|
| high | Every factor assessed |
| medium | Exactly one not assessed |
| low | Otherwise, or no impact factor assessed |

## Strategy

| Band | Needs |
|---|---|
| High | Two or more test types, including `exploratory`, and at least one charter |
| Medium | A test type plus `regression` |
| Novel | `exploratory`; never left untested |
| Low | Smoke or existing automation, or `not_tested` with the reason |

Every area is in exactly one of `strategy` and `not_tested`.

A `not_tested` entry:
- has the area's band as its `residual_band`;
- has `accepted_by` null until a named person accepts it.

A High area may be in `not_tested` only when `sessions_short` > 0, and its reason must mention
the budget or sessions.

## Effort (computed)

With a budget of B sessions:
1. **High floor:** each planned High area gets f = max(1, ceil(min(2, 0.1 × B))) sessions.
2. **Shortfall:** if f × (planned High areas) > B, the floors go in risk order until the budget
   runs out.
3. **The rest:** otherwise, B − floors is split across every planned area in proportion to risk,
   rounded by largest remainder, and added to the floors.

`sessions_short` = max(0, f × (all High areas in scope) − B). Without a budget, `effort_sessions`
is null, and `effort_share` = risk / Σ risk over the planned areas.

## Inputs from other skills

An open finding keeps the severity and confidence it has in its source report. With `--input`,
the validator re-reads the report and fails if the confidence was raised.

## Honesty rules

- **The headline:** it says "short" when `sessions_short` > 0.
- **No safety claims:** none of these may appear in the headline, reasons, exit criteria,
  assumptions or test-type reasons:
  - "safe to ship";
  - "is safe";
  - "fully tested";
  - "risk-free";
  - "guaranteed";
  - "no risk";
  - "100% covered".
