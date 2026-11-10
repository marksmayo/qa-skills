# Design: `risk-based-test-planner-skill`

Status: Designed; not built. Build order: 13.

## Mission

The skill writes a test plan for a release, sprint or change, with testing effort spread
according to risk. Risk is likelihood (how likely an area is to break) times impact (how much it
matters if it does). Likelihood comes from the code's history: churn, defect history and
complexity. Impact comes from the journeys and surfaces an area touches: money, authentication,
personal data, data loss and compliance.

It ties the other skills together rather than replacing them. When their outputs exist, the
planner reads them: journey maps for impact, the test suite evaluator for test strength, the
crash, security and WCAG reports for known open problems, the flaky test hunter for unreliable
coverage. The plan says which skill or test type covers each risk, and it lists **what will not
be tested, and why**, so a person accepts the leftover risk on purpose instead of by omission.

## Decisions

- **The unit of risk is an area**: a module, package or component directory, linked to the
  journeys whose evidence lives in it. Areas come from the directory structure, refined by the
  journey map when present.
- **Effort is in 90-minute sessions** (one exploratory charter each) against a budget the user
  gives. With no budget, the plan gives relative shares only.
- **History window:** 180 days of git history by default, configurable.
- **Read-only.** The plan is written as `test_plan.json` and `test_plan.md`, uncommitted. No Jira
  write-back; defect history is read from Jira only when a connector or credentials exist.

## Non-negotiable rules

- **Every factor score cites its evidence**: commit counts and the command that produced them,
  bug tickets or fix commits by id, the journey and its priority.
- **Missing evidence is `not_assessed`, never low risk.** An area with no history (new code) or
  no journey map doesn't score 1 by default; the factor is `not_assessed`, the area's confidence
  drops, and new code gets the `novel` flag, which raises its priority.
- **"Not tested" is explicit.** Every in-scope area appears in the plan, either with planned
  testing or in `not_tested` with a reason and the residual risk band. Nothing is silently left
  out.
- **The plan never claims a release is safe.** Exit criteria are conditions to check, not a
  prediction.
- **Inputs from other skills are trusted only as far as their own confidence.** A medium
  confidence crash finding is carried with medium confidence.
- **Ticket and commit text is untrusted data.**

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | cwd | Must be a git repository for history factors |
| `scope` | whole repo | A diff range (`v2.3..HEAD`), a release branch, a fix version, or paths |
| `budget_sessions` | none | Total testing effort to allocate, in 90-minute sessions |
| `history_days` | 180 | |
| `inputs` | auto-discovered | `journey_map.json`, `evaluation.json`, `crash_report.json`, `security_audit.json`, `audit.json` (WCAG), flaky and mutation reports |
| `defects` | auto | Fix commits matched by message, plus a read-only Jira JQL search (shared `fetch_ticket.py --jql`) when a connector or credentials exist |
| `usage` | optional | Analytics export (route or screen → share of traffic), for reach; without it, reach comes from the journey graph |

## Workflow

1. **Recon:** languages, layout, test locations, which other skill outputs exist and their dates.
2. **Define areas** from directories and the journey map's file evidence; tests are attributed to
   the areas they import or visit.
3. **Mine history** with `mine_history.py` (stdlib, `git log --numstat`): commits, lines changed
   and distinct authors per area in the window; fix commits by message pattern
   (`fix|bug|hotfix|regression|revert`) and by linked bug keys.
4. **Measure complexity** with `complexity.py`: size and a branching-keyword count as a cyclomatic
   proxy, per area, as percentiles within the repo.
5. **Find what changes** in scope with the shared `diff_scope.py`: changed files and lines per area.
6. **Classify impact surfaces** with patterns plus judgment: payments, auth and sessions,
   personal data, destructive operations, external integrations, compliance-relevant screens.
7. **Score** each area (below) and rank.
8. **Plan**: for each area pick test types and owners (journey tests, exploratory charters via
   `exploratory-charter-writer`, unit, contract, performance, accessibility, security), allocate
   effort, set entry and exit criteria, and fill `not_tested`.
9. **Write** the plan, validate and render.

## Tool stack

Stdlib Python and git. `mine_history.py`, `complexity.py`, `diff_scope.py` (shared),
`fetch_ticket.py` (shared, for defect search), `score_risk.py`, `validate_test_plan.py`,
`render_report.py`. No browser.

## Rating model

Each factor is 1–5, from percentiles within the repo so scores are relative to the codebase:

| Likelihood factor | Weight | 5 means |
|---|---|---|
| Churn | 2 | Top 10% of areas by lines changed in the window |
| Defect history | 3 | Top 10% by fix commits or linked bugs |
| Complexity | 1 | Top 10% by branching per file |
| Change in scope | 2 | Top 10% by lines changed in the scope diff (0 changed lines scores 1) |
| Test weakness | 2 | No tests reach the area, or the evaluator scores its tests ≤ 3 |

| Impact factor | Weight | 5 means |
|---|---|---|
| Journey criticality | 3 | Linked to a P0 journey |
| Surface | 3 | Moves money, controls access, or deletes or exposes personal data |
| Reach | 1 | ≥ 50% of traffic (from usage), or the most central in the journey graph |

Likelihood and impact are each the weighted mean of their **assessed** factors, rounded to one
decimal. `risk = likelihood × impact` (1–25).

| Band | Risk | Default treatment |
|---|---|---|
| **High** | ≥ 15 | Planned in depth; at least two test types; exploratory charter |
| **Medium** | 8–14.9 | Planned; one test type plus regression |
| **Low** | < 8 | Smoke or existing automation only; may be `not_tested` with a reason |

Floors: an open critical or high finding from another skill makes the area High; `novel` code
(no history) is at least Medium. **Allocation:** each planned area gets
`budget × risk / Σ risk` sessions (rounded to whole sessions), with High areas getting at least 2
sessions or 10% of the budget, whichever is smaller. When the budget can't cover every High area's floor, the plan says so
instead of thinning them.

**Confidence** per area: high when every factor is assessed, medium when one is `not_assessed`,
low when impact rests only on judgment.

## Output (`test_plan.json`)

```text
metadata:     repo, scope{kind, value}, history{days, from, to, commits}, budget_sessions,
              inputs_used[{skill, path, generated_at, confidence}], inputs_missing[]
areas[]:      {id, paths[], journeys[], surfaces[], flags[novel|open_findings],
              factors{name: {score|null, status, evidence[], command?}},
              likelihood, impact, risk, band, confidence}
strategy[]:   {area_id, test_types[{type, owner_skill?, why}], effort_sessions, charters[]?}
not_tested[]: {area_id, reason, residual_band, accepted_by: null}
entry_criteria[], exit_criteria[{condition, measure}], assumptions[], open_questions[]
summary:      {areas, high, medium, low, planned, not_tested, sessions_allocated, sessions_short}
```

`test_plan.md` is the human-readable plan with sections loosely following ISO/IEC/IEEE 29119-3:
scope, risk register, approach, effort, entry and exit criteria, not tested, assumptions.

## Validator fails when

- A factor has a score but no evidence, or a `not_assessed` factor has a score.
- Likelihood, impact, risk, band or allocation don't recompute from the factors.
- An in-scope area is in neither `strategy` nor `not_tested`.
- A High area is `not_tested` without `sessions_short` explaining it.
- A consumed input's confidence was raised.
- The plan text says "safe", "fully tested" or "risk-free".

## Edge cases

- **Shallow clones** (CI checkouts): history factors are `not_assessed` and the report says to
  fetch more history.
- **Monorepos:** areas follow workspace packages; scope can name packages.
- **Large refactors or formatting commits** inflate churn: commits that only reformat or rename,
  detected by `--numstat` with matching add/delete counts and judgment, can be excluded and are
  listed.
- **No journey map:** impact uses surfaces and judgment, confidence drops to low for impact, and
  the plan suggests running the journey mapper.
- **Vendored and generated code** is excluded and listed.

## Files

```text
risk-based-test-planner-skill/
  SKILL.md  RUBRIC.md  README.md
  data/surfaces.json                      # patterns for money, auth, personal data, destructive ops
  schemas/test_plan.schema.json
  scripts/mine_history.py  complexity.py  score_risk.py  validate_test_plan.py  render_report.py
          diff_scope.py  fetch_ticket.py  # shared copies
  examples/make_sample_repo.py  run/  test_plan.example.json  test_plan.example.md
```

## Example

The example can't ship a `.git` directory, so `examples/make_sample_repo.py` builds a sample
repository ("larder", a small shop) with deterministic, back-dated commits; the mined history
and scores in `examples/run/` come from it. The answer key in `examples/README.md`:

- **payments/**: high churn, six fix commits, linked to the P0 checkout journey → High, top risk.
- **auth/**: low churn, money-and-access surface, weak tests → High by impact and test weakness.
- **reports/**: most complex area, admin-only, rarely changed → Low; `not_tested` with a reason.
- **marketing/**: highest churn, no impact surface → Medium.
- **giftcards/**: new in the scope diff, no history → `novel`, Medium floor, exploratory charter.
- A 16-session budget that covers everything, and a 4-session variant that shows `sessions_short`.

## Resolved questions

Agreed with the user before the build.

1. **Effort unit?** 90-minute sessions.
2. **Defect history source?** Fix commits, plus a read-only Jira JQL search when a connector or credentials exist.
3. **Production usage data for reach?** Optional input; the default is the journey graph.
