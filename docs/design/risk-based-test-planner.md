# Design: `risk-based-test-planner-skill`

Status: Built (v1.3.0), see "As built" at the end. Build order: 13.

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


## As built (v1.1.0)

Where the build differs from the design above, and what the example showed.

- **Percentiles are mid-rank.** p = (areas below + half the ties) / (areas - 1). The first
  version ranked tied areas at the bottom: payments got reach 1 while tied with three other
  areas on one journey each.
- **The history window ends at the HEAD commit's date** unless `--as-of` is given, so the same
  repository always scores the same.
- **Formatting-only commits** are left out of churn and listed. The subject must say style,
  format or reformat, and every file's added and deleted counts must be within 2 of each other.
- **Defect history** counts fix commits found by subject, plus Jira Bug tickets passed with
  `--defects` (`fetch_ticket.py --jql` output) whose component or label names the area. A bug
  already linked from a fix commit counts once. The example gives no Jira search, and the plan
  lists that as missing.
- **Surfaces describe actions.** `data/surfaces.json` matches calls like charge, refund, redeem,
  login, delete and export, not mere mentions: a catalogue showing a price is not a money
  surface. Gift-card redemption was added after the first run scored gift cards 1.
- **Test weakness:**
  - **Reach:** found by the tests that import an area.
  - **Score:** no test scores 5; otherwise it goes by the test-to-code ratio.
  - **Flaky tests:** tests from a flaky-test-hunter report don't count.
  - **The evaluator:** the test-suite-evaluator's scores are suite-wide, so they aren't used per
    area.
- **Inputs from other skills:**
  - **What is read:** security (CVSS severity), crash (ranking band), WCAG (findings with a file
    component) and flaky reports, through small adapters.
  - **How they count:** findings map to areas by file. Critical and high findings set the High
    floor, and each keeps its source confidence. The validator re-reads the source with
    `--input` to check that.
- **Effort.**
  - **Floors first:** each planned High area gets max(1, ceil(min(2, 0.1 x budget))), then the
    rest is shared by risk with largest remainders. The design's "budget x risk / sum of risk,
    with a floor" was ambiguous about rounding; this way the total always equals the budget.
  - **`sessions_short` counts every High area in scope**, planned or not. The first version
    counted only planned ones, so dropping a High area from the plan hid the shortfall; the
    one-session example caught it.
  - **The 4-session variant became 1 session:** with this floor rule, a 4-session budget covers
    two High areas.
- **Judged factors.** A person can change a factor's score by marking it `judged`, with
  `computed_score` kept and a reason. Every other factor must equal `risk.json`.
- **Example results:**

  | Area | Risk | Band | Note |
  |---|---|---|---|
  | payments | 16.6 | High | (17.9 in v1.0.0; its 6-line change is now capped at change in scope 2) |
  | auth | 7.4 | High | Through security finding F-1 |
  | giftcards | 13.8 | Medium | Novel; reaches the P0 checkout journey through the payments service's import (10.3 in v1.0.0) |
  | catalog | 8.9 | Medium | |
  | marketing | 5.8 | Low | The design's answer key had Medium; with no impact surface its likelihood alone isn't enough |
  | reports | 3.5 | Low | Not tested |

  The 16-session plan gives payments 6 sessions, auth 4, gift cards 3, catalog 2 and marketing 1
  (v1.0.0: gift cards, catalog and marketing 2 each).
- **Found on a real JavaScript repository, and fixed:**
  - Tests that import through an alias (`~/utils/...`, configured in jsconfig, tsconfig, vite or webpack)
    weren't linked to their areas, which overstated test weakness. Aliases are now resolved; on that
    repository it linked 18 more test files.
  - Collection and URL methods such as `url.searchParams.delete()` or `map.delete()` matched the
    "destroys data" surface. Surface classes now take `exclude` patterns, and the destructive class
    excludes those receivers.
- **Testing:**
  - The validator catches 25 of 25 seeded violations.
  - 12 scorer scenarios pass:
    - no journey map;
    - a usage export;
    - a shallow clone;
    - formatting exclusion;
    - a low-severity finding setting no floor;
    - the novel floor;
    - floors, shares and shortfall in the allocation;
    - no budget;
    - monorepo areas;
    - no scope diff.

### v1.1.0: found planning a real Next.js App Router repository, and fixed

- **Crash severity was read from the wrong field.** The adapter read `ranking.band`; ui-crash-hunter
  writes `ranking.priority`, so two P1 crashes loaded as medium and never set the High floor.
  Adapters now read a documented list of fields per skill (P0/P1 = critical/high, WCAG serious =
  high), record `source_rating`, and mark an unreadable rating `unknown` with a warning. The
  validator re-reads the source and checks severity, rating and the `open_findings` flag.
- **A top-level `src/*` area matched every test.** The test matcher stripped `/*` and took every
  import under `src/`. Imports are now resolved to files (extensions, index files, aliases) and
  mapped with the same rule as source files; co-located tests count for their area.
- **Files outside `--areas` were dropped silently**, including the only source file the release
  changed. They are now scored as `unassigned` and named in a warning.
- **Journeys missed the API hops.** A journey linked to an area only through evidence files. It now
  also follows `covers` (surface item files), resolves `POST /api/x` notes to App Router or Pages
  Router handlers, and follows one hop of imports, so a payment route and the lib modules behind
  it are on the booking journey.
- **A partial journey map was read as complete.** Areas a scoped map didn't map scored journey
  criticality 1 with high confidence. They are now `not_assessed` (and so is reach), which lowers
  confidence; `out_of_scope` and `not_mapped_this_run` orphans both count as unmapped.
- **Default areas for Next.js App Router**: one per API resource, route segment or group,
  `app (root)`, `middleware`, and `lib/`, `components/` and other directories split by
  subdirectory and file-name prefix, instead of one `app` area holding 256 files.
- **Surface patterns** were Python-centric and matched mentions: `logInfo(` as login,
  `Sentry.captureMessage(` as a payment capture, CSP strings and marketing copy as Stripe,
  `useSession()` as access control, `someMap.delete(key)` and `.d.ts` declarations. Patterns now
  run on code with comments and strings blanked, camelCase verbs are matched case-sensitively,
  receivers and in-memory maps are excluded, `.d.ts` is skipped, and JS/TS idioms (Stripe
  resource calls, bcrypt, getServerSession, Prisma deletes, route handlers, server actions,
  middleware matchers) were added. A new `entry` class (3) marks route handlers and server
  actions.
- **Fix commits** were any subject containing "fix": merges, `feat: ... fixes` and a UI redesign
  counted. Conventional-commit types now decide on their own, other subjects must start with a fix
  verb or name a bug, merges never count and duplicates count once. On that repository 18 "fixes"
  became 8. A note warns when `--as-of` ends the window long after HEAD.
- **Change in scope** ranked an 18-line change as 5 when every other area tied at 0. The
  percentile is now capped by absolute size (under 25 lines at most 2, under 100 at most 3, under
  250 at most 4).
- **complexity.py walked node_modules** and filtered afterwards (3 min 20 s cold on that
  repository). It now prunes dependency, build and cache directories before descending: 10.1 s
  warm before, 0.55 s after, the same 875 files.
- **Renderer and validator.** The register is sorted by risk after judged factors, `--brief`
  includes what is not tested, and the validator checks the headline against the register (no
  "no area reaches High" when one does; band counts, quoted risks and bands must match). The
  example's headline quoted payments at 17.9 after re-scoring, and the new check caught it.

### v1.2.0: found planning a real Python API monorepo and a Vite React app (Azure DevOps), and fixed

- **Squash merges hid every fix.** Azure DevOps squash merges have subjects like `Merged PR 123:
  fix(x): ...`, and the conventional-commit and fix-verb patterns are anchored at the start, so
  one repository showed no fixes at all and the other a fraction of them. `mine_history.py`
  strips the merge tool's prefix before matching and records `merge_prefix` on the commit.
- **Python tests through sys.path roots were not linked.** Tests import `<pkg>.x` from a
  `libs/<lib>/src` layout and `<package>.x` from a service directory; only relative paths and
  `src/` were tried, so most areas read "no test imports this area" although many test files
  imported them. Imports now also resolve relative imports and through the importer's ancestor
  directories, the parent of every top-level package, project-file directories and their
  `src/`, pytest `pythonpath` and `sys.path.insert` targets (stdlib names are not looked up
  there).
- **SQL in Python strings** (`op.execute("ALTER TABLE ... DROP COLUMN ...")`, an f-string
  `TRUNCATE {t} ... CASCADE`) didn't match: the raw patterns only knew `DROP TABLE`, `DELETE
  FROM` and `TRUNCATE TABLE`. Upper-case-only raw patterns for `TRUNCATE`, `ALTER TABLE ... DROP
  COLUMN` and `DROP SCHEMA/DATABASE/COLUMN/INDEX/VIEW` were added.
- **Entry points and integrations outside Next.js**: decorated Python handlers (Flask, FastAPI,
  Azure Functions `route`/`*_trigger`, DRF) are entry points; API client wrappers
  (`apiClient.postJson(`, `this.http.get(`) and cloud SDK clients are external integrations.
- **React false positives**: `deleteRow(index)` (local form state), `Plotly.purge(el)` (chart
  cleanup) and a sign-out button's `logout()` each made an area impact 5. Index-argument deletes
  and chart purges are excluded, and log-out/sign-out no longer count as access control.
- **UI impact without a journey map** collapsed to surface alone, so no frontend area was High.
  A `ui_route` class (3) now marks page files and route definitions (by path for page modules),
  and `risk.json` warns when UI areas have neither a journey map nor usage; the validator wants
  the plan to say so.
- **Default areas.** Workspace packages with more than 40 source files are split by
  subdirectory below their source root; packages with no files (an uninitialised submodule) are
  not areas. The default layout goes down from the main source directory while one
  subdirectory holds nearly all the code (`app/` -> `app/src`) and splits container directories
  (`modules/`, `common/`, ...), so `src/modules/<module>` are the areas. Tool configuration
  written as code (`.prettierrc.js`) and files under `doc/` are excluded and listed, not scored.
- **Inputs.** Two reports of one kind (two migration reviews) overwrote each other in the
  validator; carried findings now record their `source` report and are matched by it. A
  finding the source report only assumes (`assumed`, `possible`, ...) sets
  `unconfirmed_findings` (at least Medium) instead of the High floor. The migration reviewer's
  `generated` date is read. `--finding-area KIND:ID=AREA` moves a finding whose report cites
  the wrong file; it records `reassigned_from` and the plan must explain it.
- **Effort without a budget.** A High area made High only by a finding (a very low risk) got a
  1% share. Without a budget, each planned High area now gets at least 10%.
- **"The last N merged PRs"** as a scope base is described in SKILL.md (the parent of the
  Nth-newest squash merge on the first-parent line).
- The example was re-scored: the scores, bands and areas are unchanged; carried findings gained
  `source`.

### v1.3.0: found planning a TypeScript pnpm monorepo (an SDK and CLI released with changesets), and fixed

- **Surface patterns misfired on SDK and UI-automation idioms**, and without a journey map they
  decided the bands: `settle<Something>(` helpers that wait until the UI is stable,
  `capture<Something>(` helpers and `capture({...})` that take screenshots (money), a
  `truncate<Encoding>(` string helper and a stream's `.destroy()` (destroys data), `seen?.delete(k)`, a `ledger`
  field, `requireSession()`, a `matcher:` type annotation, a React `consent` toggle, and
  interface members (`delete(id: string): Promise<boolean>;`). Bare capture/settle/transfer now
  need a money noun or a money argument; truncate needs a table or data noun; stream, socket and
  client `.destroy()`, optional-chained and call-result map deletes, the `delete (x)[k]` operator
  and terminal `erase()` are excluded; ledger needs a posting call; `matcher:` must be a literal
  (`config = { matcher: [...] }`); compliance needs a call or a named policy, not a mention; and a
  TypeScript member signature with no body matches nothing. `score_risk.py --self-test` holds the
  idioms, with the real surfaces next to them.
- **No surface class for a library's public API or its secret handling.** A `secrets` class (5)
  covers defining a redactor, resolving or storing a secret, setting an Authorization header and
  basic auth (calls to a redactor don't count: a reporter calls it everywhere). A `public_api`
  class (3) scores the files package.json `main`/`module`/`exports`/`bin` name (a `dist/` target
  mapped back to `src/`) and pyproject `[project.scripts]`, and CLI command definitions. On the
  review repository, of the four Highs that rested on false positives, the CLI dropped to Medium
  (public API, 3), two now rest on real secret handling (download redaction, masking secure
  fields), and one on a cache store's own `delete` method, which is left for a person to judge.
- **Root tooling was dropped silently.** In the default layout, code outside the workspace
  packages (43 files there, including the release tooling) was in neither `excluded` nor
  `unassigned`. Every code file outside the areas is now in `excluded` with its reason, and root
  tooling gets a warning with its count. (The report's other claim, that files untouched in the
  window are in no list, was not the case: `mine_history.py` adds every tracked file.)
- **Changesets were not a release signal.** `release_scope.py` resolves the release base (the
  nearest `name@x.y.z` or `vX.Y.Z` tag merged into HEAD, `--package` for one package's own),
  reads the pending `.changeset/*.md` bumps per package and the breaking conventional commits.
  `score_risk.py --release` names them in the change-in-scope evidence and flags
  `breaking_change` areas (at least Medium; the plan needs regression or contract testing for
  them). A minor bump with a `**Breaking:**` note counts as breaking, as 0.x releases use it.
  Changing a shared `diff_scope.py` (`--since-release`) was left to the shared files.
- **A workspace package named `e2e`** was not source to `is_source()` (an `e2e/` directory meant
  test code). Directories named `e2e`, `test`, `tests` or `spec` are test code unless they are a
  product package (a manifest plus `src/`, or a package.json with `main`/`module`/`exports`/`bin`),
  the rule the skills share.
- **Test weakness didn't discriminate.** Live tests (`live/`, credential-gated) and helpers or
  fixtures in a test tree counted as tests; they no longer do and are listed in
  `tests_not_counted`.
- **Two-file areas** from file-name prefixes (`read*`, `cdp*`) in a large flat package directory:
  prefix grouping is now for the Next.js `lib/` layout only; elsewhere the flat files are one
  `(top level)` area.
- **Ranking by surface alone was easy to miss.** When no area has journey criticality or reach
  assessed, `risk.json` warns and the validator wants the headline to say the ranking rests on
  surface patterns; SKILL.md says to give `--usage` (downloads per package, calls per entry point)
  for a library or CLI, where journey-mapper does not fit.
- **Judged factors were invisible in `--brief`.** The register has a Judged column (computed →
  judged score). Judging a factor to its computed score with a new reason stays allowed, to
  record a review.
- **Speed.** Profiling showed 7 million regex searches (one per pattern per line). Each class's
  patterns and excludes are now one alternation, searched once per line: the review repository
  scores in 18 s (default areas) and 14 s (`--areas`); the review measured 23 s and 51 s, and
  the unmerged patterns took 48 s on the same loaded machine, with identical output. Windows antivirus time in `complexity.py` is documented in the README, not changed.
- The example was re-scored with `release_scope.py` added to `commands.sh` (base `v1.4`, no
  changesets): scores, bands and areas are unchanged.
