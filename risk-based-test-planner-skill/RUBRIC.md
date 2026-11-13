# Risk-based test planner rubric

Rules for `test_plan.json`. The validator enforces everything marked (computed) and every
"must".

## Factors

| Factor | Kind | Weight | Score |
|---|---|---|---|
| `churn` | likelihood | 2 | Mid-rank percentile of lines changed in the window (formatting-only commits excluded); 0 lines = 1 |
| `defects` | likelihood | 3 | Percentile of fix commits plus bug tickets by component or label (each bug counted once); 0 = 1 |
| `complexity` | likelihood | 1 | Percentile of branches per code file |
| `change_in_scope` | likelihood | 2 | Percentile of lines changed in the scope diff; 0 = 1. Capped by size: under 25 lines at most 2, under 100 at most 3, under 250 at most 4. With `--release`, the evidence also names the area's package's pending changesets and bump, and any breaking commit that touched it (the score is unchanged; a breaking change sets the `breaking_change` flag) |
| `test_weakness` | likelihood | 2 | 5 when no test imports the area or sits in it (flaky tests, live tests under `live/` that need real credentials, and helpers or fixtures in a test tree don't count; imports are resolved to files through relative paths and configured aliases such as `~/` from tsconfig, jsconfig, vite or webpack, so a top-level `src/*` area matches only files directly in `src/`; a mocked module is not a link; a Rust integration test `tests/**/test_<util>.rs` links to `src/uu/<util>/` by the filename convention, since it references the utility by name rather than by a resolvable path). Otherwise by test-to-code ratio: < 0.2 = 4, < 0.5 = 3, < 1 = 2, else 1 |
| `journey_criticality` | impact | 3 | Highest linked journey: P0 = 5, P1 = 4, P2 = 3, P3 = 2; a journey map that maps the area but no journey reaches it = 1. A journey reaches an area through its evidence files, the surface items it covers, the route handler behind a `METHOD /path` note, or one import hop from those |
| `surface` | impact | 3 | `data/surfaces.json` on code with comments and strings blanked (`.d.ts` and TypeScript member signatures without a body skipped): money, access, personal data, secrets or credentials (redaction, an Authorization header set, basic auth, storing or resolving a secret), destructive, and the systems/CLI pack (filesystem destruction, privilege) = 5; compliance = 4; public entry point, public API or CLI entry point (a file a package manifest's `main`/`module`/`exports`/`bin` or pyproject `[project.scripts]` names, a CLI command definition), external integration, filesystem write and raw syscall/unsafe = 3; none = 1. The money/access/personal-data classes are web-tuned and the systems pack is Rust-only. **Outside a web or app stack, a surface factor resting only on these patterns is low-trust: review it and mark it `judged` rather than presenting it as confidently assessed.** |
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
- journey criticality without a journey map, or when the map is partial (scoped to some
  domains, or with `out_of_scope` / `not_mapped_this_run` orphans) and doesn't map the area;
- reach without usage data or a journey map, or without usage data when a partial map doesn't
  map the area;
- change in scope without a scope diff.

## Risk (computed)

- **Likelihood and impact:** each is the weighted mean of its assessed factors, to one decimal.
- **Risk:** likelihood × impact, to one decimal.

| Band | Risk | Floor |
|---|---|---|
| **High** | ≥ 15 | Also any area with an open critical or high finding from another skill, at a confidence its report stands by (`open_findings`) |
| **Medium** | 8 to 14.9 | Also any `novel` area, any `breaking_change` area (its package has a major or "Breaking" pending changeset and the scope changed the area, or a breaking conventional commit touched it), or one whose critical or high findings are only assumed or possible (`unconfirmed_findings`), that would be Low |
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
| Breaking change | `regression` or `contract` (what callers rely on); left untested only with a reason that names the breaking change |
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
is null, and `effort_share` = risk / Σ risk over the planned areas, with the same High floor in
share form: each planned High area gets at least min(10%, 1 / planned areas), and the other
areas share the rest by risk. (A High area made High only by an open finding can have a small
risk; without the floor it would get 1%.) With a budget, the shares stay risk / Σ risk.

## Inputs from other skills

An open finding keeps the severity and confidence it has in its source report. Severity is read
by the adapters listed in SKILL.md (crash `ranking.priority` P0/P1 = critical/high, WCAG serious =
high, security CVSS severity) and recorded with its `source_rating`; a rating the adapter can't
read is `unknown` and sets no floor. With `--input`, the validator re-reads the report and fails
if the severity, the source rating or the confidence differ, and it checks the `open_findings`
flag is set exactly when a finding is critical or high at a confidence its report stands by.

- **Confidence decides the floor.** A critical or high finding whose own confidence is
  `assumed`, `possible`, `low`, `unconfirmed`, `suspected`, `speculative` or `unknown` doesn't
  make the area High: it sets `unconfirmed_findings`, which makes it at least Medium. (A
  migration reviewer caps a rating at P1 when table sizes are unknown; four assumed P1s are not
  a confirmed High.) A finding with no confidence field counts as confirmed.
- **Several reports of one kind** (two migration reviews) are all read; each carried finding
  records the report's file name in `source`, so equal ids in two reports are told apart.
- **A finding whose report cites the wrong file** can be moved with `score_risk.py
  --finding-area KIND:ID=AREA`. The finding records `reassigned_from`, `risk.json` warns, and
  the plan's assumptions say why.

Source files no area covers are scored as the `unassigned` area; they are never dropped. Every
other code file outside the areas (docs and examples, tool configuration, generated or vendored
code, fixtures and migrations outside an area, root tooling and scripts outside the workspace
packages) is listed in `excluded` with its reason, and root tooling also gets a warning.

A directory named `e2e`, `test`, `tests` or `spec` is test code unless it is a product package:
it holds a package manifest and a `src/` directory, or a `package.json` declaring `main`,
`module`, `exports` or `bin` (a workspace package named `e2e`).

## Honesty rules

- **The headline:** it says "short" when `sessions_short` > 0, and it doesn't contradict the
  register: no "no area reaches High" when one is High; "N High areas" (and Medium, Low) must
  match the counts; an area quoted with "(risk X)" or as High/Medium/Low must match its
  computed risk and band. A `not_tested` reason that quotes "<Band> risk (X)" must match too.
  When no area has journey criticality or reach assessed, impact is the surface factor alone,
  and the headline must say the ranking rests on surface patterns.
- **Judged factors:** a judged factor may keep its computed score with a new reason (a reviewed
  surface); the renderer's register shows every judged factor, with computed → judged score.
- **No safety claims:** none of these may appear in the headline, reasons, exit criteria,
  assumptions or test-type reasons:
  - "safe to ship";
  - "is safe";
  - "fully tested";
  - "risk-free";
  - "guaranteed";
  - "no risk";
  - "100% covered".
