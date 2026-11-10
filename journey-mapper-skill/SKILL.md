---
name: journey-mapper
description: Map the user journeys of an application by reading its codebase (web, mobile and API), and write them as prioritised, evidence-linked Journey Markdown files ready to be turned into automated tests. Accounts for every route, screen, endpoint, entry point and goal event it finds, reports coverage honestly, and never presents behaviour inferred from code as intended behaviour.
version: 1.0.0
---

# Journey Mapper

## Mission

Produce an inventory of the user journeys in a piece of software, derived from its source code,
that a QA engineer can review and then turn into tests.

A **journey** is how one persona reaches one goal: an ordered set of user actions, each with an
observable expected outcome, plus its significant variants (error paths, alternative paths,
role or platform differences).

The output is:

1. One Journey Markdown file per journey (format: `JOURNEY_FORMAT.md`).
2. `index.md`: journeys by domain, priority, persona and automation status, with flow diagrams.
3. `coverage.md`: every surface item found, and which journey covers it (or why none does).
4. `journey_map.json`: the machine-readable result, conforming to `schemas/journey_map.schema.json`.

The **Journey to Tests** skill consumes these files, so their accuracy matters more than their
number.

## Non-negotiable rules

- **Inferred is not intended.** Journeys derived from code describe how the software behaves
  today, which may include bugs. Always emit `status: inferred`. Only a person promotes a journey
  to `reviewed` or `approved`. Behaviour that looks unintended goes in `open_questions`; never
  silently "correct" it in the steps.
- **Every step cites code evidence** (`file` and `line`) showing where the action is handled or
  the outcome is produced. If you can't find evidence for a step, don't write the step. Record
  the gap as an open question.
- **Every surface item is accounted for.** Each route, screen, endpoint, entry point and goal is
  either covered by a journey, listed as an orphan with a reason, or listed as unreachable with
  evidence. The validator enforces this arithmetic.
- **Coverage is reported as a lower bound.** Never claim to have documented "the entire
  application" without the coverage numbers. Dynamically built routes, server-driven UI and
  remote configuration can hide surface that static analysis can't see; state this.
- **Expectations must be observable by a user or a test**: text, navigation, enabled state,
  a visible error. "State is updated" is not an expectation; "Cart badge shows 1" is.
- **Read-only.** Don't modify application source, run migrations, or call external services.
  The only files you write are under the output directory (default `journeys/`). Leave them
  uncommitted.
- **Don't reveal secrets.** If you encounter credentials while reading code, don't copy them into
  journeys, test data or evidence notes; refer to the file location and secret type only.
- **Never overwrite human decisions.** `write_journeys.py` preserves `reviewed`/`approved` files
  and `## Notes` sections; never bypass it by writing journey files directly.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repository | current directory | Monorepos are supported |
| Domains | all | Restrict a run to some domains, e.g. `checkout, account` |
| Max journeys per run | 50 | Large applications are mapped over several runs |
| Platforms | detected | web, ios, android, desktop, api, cli |
| Output directory | `journeys/` | |
| Diagrams | on | Mermaid flowchart per domain in `index.md` |

If the user names a domain or feature, map that domain thoroughly rather than the whole
application thinly.

## Step 1: Recon

Establish, citing files:

- **Application type(s):** web SPA, SSR web, native or cross-platform mobile, desktop, API-only,
  CLI. A repository may contain several apps; treat each as a platform of the same product only
  if they share a backend and user accounts.
- **Frameworks and routing:** Next.js, Nuxt, SvelteKit, Remix, React Router, Vue Router, Angular,
  Expo Router, React Navigation, Flutter (GoRouter/Navigator), Android (Navigation, Compose,
  activities), iOS (SwiftUI, UIKit, storyboards); backend frameworks.
- **Role model:** how users are identified and what roles or permissions exist (e.g. guest,
  member, admin, tenant owner). These become personas.
- **Feature flags:** the flag system and where defaults are defined.
- **Domains:** functional areas, from directory structure (`features/checkout`), route prefixes
  and navigation menus. Use lowercase slugs.
- **Existing end-to-end and UI tests:** frameworks and locations.
- **Existing journeys:** if the output directory already exists, read `index.md` and note which
  journeys are `reviewed` or `approved`.

## Step 2: Extract the surface

Run the extractor:

```bash
python scripts/extract_surface.py <repo> --out surface.json
```

It finds candidate routes, screens, API endpoints, entry points (deep links, URL schemes) and
goal events (analytics calls). It also collects context: feature flag calls and end-to-end test
files with the URLs they visit. Write `surface.json` to a scratch location, not into the
repository.

The extractor is heuristic. Then:

1. **Verify** each item. Remove false positives, e.g. a `HomeScreen` class that is a component,
   or a `path:` key that isn't a route. Record removed items as orphans with reason
   `false_positive` instead of deleting them, so the numbers stay honest.
2. **Resolve** nested and relative routes into full paths where you can.
3. **Add** items the extractor can't see, with `"source": "agent"`, a file and line, and the
   next free ID in that kind's sequence (`R-`, `S-`, `E-`, `N-`, `G-`). Typical additions:
   - Expo/React Native linking config in `app.json`
   - routes built from constants
   - push-notification handlers
   - links in email templates
   - server-rendered pages in templating languages
   - CLI commands
4. Read the `limitations` the extractor reports, and carry any that apply into
   `metadata.notes`.

## Step 3: Build the interaction graph

Trace how users move between surface items. For each screen or route, read its handlers and
record:

- **Edges:** user actions that navigate or change state (`router.push`, `navigate()`,
  `<Link>`, form submits that redirect, `Navigator.push`, segues).
- **Guards:** authentication checks, role checks, feature flags, and required state (e.g. cart not
  empty) that must hold for an edge to be available.
- **API calls** each action makes, linking UI to endpoints.

Record nodes (`entry`, `screen`, `state`, `goal`) and edges in the `graph` section of the map.
The graph doesn't need every edge in the application. It needs the edges that the journeys
traverse, plus the edges that make the reachability conclusions in Step 8 defensible.

## Step 4: Identify goals

Goals are states a user is trying to reach. Sources, strongest first:

1. Analytics events that mark completion: `order_completed`, `signup_completed`,
   `report_exported`.
2. Success screens, confirmation routes and success toasts.
3. Transitions to terminal or meaningful statuses in state machines (e.g. `status = 'submitted'`).
4. Created, updated, deleted or exported resources reached through the UI.

Navigation events alone (`page_viewed`) are not goals.

## Step 5: Synthesise journeys

Walk the graph from entry points to goals. Follow `RUBRIC.md` for granularity:

- **One journey per persona-goal pair.** Name it by the outcome: "Guest completes checkout with
  card", not "Checkout page".
- **Steps are user actions**, typically 3–12. Don't describe internal calls as steps; cite them as
  evidence.
- **Variants, not new journeys,** for error paths, validation failures, alternative entry points
  (deep links), role differences in the same flow, and platform differences. Each variant
  branches from a numbered step.
- **A separate journey** when the goal differs, the persona differs and the flow differs
  materially, or the variant would need more than about 3 steps of its own.
- **Preconditions** state the data and session state needed, e.g. "a product with stock > 0",
  "signed in as member".
- **Test data** names the kind of data needed and where it comes from (seed scripts, sandbox
  cards, fixtures). Never paste secrets.
- **Not automatable** lists expectations a test can't check reliably, with a reason. Keep this
  short and honest.
- **Platforms:** list every platform where the journey exists, and use variants for meaningful
  platform differences.

Assign IDs `JRN-<DOMAIN>-<NNN>`, where DOMAIN is a short uppercase code for the domain (e.g.
`CHK` for checkout). If a journey already exists in the output directory, **reuse its ID**. IDs
are never reassigned to a different journey.

## Step 6: Match existing tests

Use `context.tests` from the extractor (visited URLs), plus your own reading of the e2e and UI
test suites. Match tests to journeys by route, screen, selectors, API calls and test titles. Set:

- `automated`: existing tests exercise every step and assert every Expect of the main path.
- `partial`: some steps or variants are exercised, or steps run without their outcomes being
  asserted.
- `none`: no test exercises the journey.

List matched test files in `tests`. Never set `partial` or `automated` without listing the tests.
Be conservative: a test that visits a page isn't a test of the journey.

## Step 7: Prioritise

Apply the priority model in `RUBRIC.md` and write a one-line `priority_reason` for every journey.
Untested P1 journeys are the headline finding of the report.

## Step 8: Account for every surface item

Every surface item must end up in exactly one bucket:

| Bucket | When |
|---|---|
| Covered | Listed in at least one journey's `covers` |
| Orphan `no_user_path_found` | User-facing, but you couldn't trace a path to it |
| Orphan `not_user_facing` | Health checks, webhooks, internal or machine-to-machine endpoints |
| Orphan `out_of_scope` | Belongs to a domain excluded from this run |
| Orphan `needs_review` | Ambiguous; a person must decide (explain in `note`) |
| Orphan `false_positive` | Extractor output that isn't actually a route, screen or event |
| Unreachable `dead` | No navigation, link, redirect or call reaches it (cite the evidence of absence you checked) |
| Unreachable `flag_off` | Only reachable behind a flag that is off by default everywhere (cite the flag default) |
| Unreachable `guarded_unreachable` | Behind a guard no real user can satisfy |

Admin-only and internal-tool paths are **not** unreachable. Map them as journeys with the right
persona.

## Step 9: Write and validate the journey map

Write `journey_map.json` into the output directory (`journeys/journey_map.json`), conforming to
`schemas/journey_map.schema.json`. Set `metadata.run_scope.domains` to `"all"` or the list of
domains mapped. Then:

```bash
python scripts/validate_journey_map.py journeys/journey_map.json
```

Fix every reported problem. Don't fudge the coverage numbers to pass: if an item isn't covered,
list it as an orphan or unreachable item with a truthful reason.

## Step 10: Write the journey files

```bash
python scripts/write_journeys.py journeys/journey_map.json --out journeys
```

This creates or updates the journey files by ID, regenerates `index.md`, `coverage.md` and
`journey-graph.json`, and records what it did in the map's `files` list. It never overwrites a
`reviewed` or `approved` journey (it writes `<name>.proposed.md` beside it). It preserves
`## Notes`, merges `tests`, and never deletes files: journeys no longer found are reported as
`stale`. Use `--dry-run` to preview.

## Step 11: Lint and report

```bash
python scripts/journey_md.py lint journeys --strict
python scripts/render_report.py journeys/journey_map.json
```

Strict lint must pass. Use `--color never` when the output will be pasted elsewhere.

## Large applications

Map one or a few domains per run, using `run_scope.domains`. Items in other domains are orphans
with reason `out_of_scope`. Stable IDs make runs add up: `index.md` always lists every journey
file on disk, and `coverage.md` describes the latest run's scope. Suggest a domain order to the
user, highest-risk first (payments, authentication, data entry).

## Final response to the user

Keep it short. Include:

1. Coverage: surface items found, the percentage in journeys, orphans and unreachable items. Say
   that it is a lower bound.
2. The number of journeys by priority, and **the untested P1 journeys by name**.
3. Behaviour that looked unintended (open questions), with file and line.
4. Proposed updates waiting next to reviewed journeys, and stale journeys.
5. The next step: a person reviews the `inferred` journeys, then the Journey to Tests skill can
   turn them into tests.

## Required honesty language

Use direct language such as:

- "These journeys were inferred from the code and describe current behaviour; they have not been
  confirmed against requirements."
- "42 of 57 surface items (74%) are in at least one journey. Routes built at runtime would not be
  visible to this analysis."
- "No end-to-end test exercises this journey; one test visits the page without asserting the
  outcome, so automation is marked partial."

Don't describe the map as complete, comprehensive or exhaustive.
