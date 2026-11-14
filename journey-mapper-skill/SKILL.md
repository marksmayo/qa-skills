---
name: journey-mapper
description: Map the user journeys of an application by reading its codebase (web, mobile and API), and write them as prioritised, evidence-linked Journey Markdown files ready to be turned into automated tests. Accounts for every route, screen, endpoint, entry point and goal event it finds, reports coverage honestly, and never presents behaviour inferred from code as intended behaviour.
version: 1.3.0
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
  member, admin, tenant owner). These become personas. Role or group names often encode several
  dimensions, e.g. `<app>-<env>-<region>-<site>-<READ|WRITE>` such as `bakery-prod-north-harbour-write`,
  with a special site value that grants every site. Split them: the access level gives the
  personas (READ is a viewer, WRITE an editor), and the other dimensions become preconditions
  ("signed in as an editor of one site") and variants (one site versus every site), not one
  persona per combination. Check whether every screen honours the access level: a write action
  that a read-only user can still reach is an open question.
- **Feature flags:** the flag system and where defaults are defined.
- **Domains:** functional areas, from directory structure (`features/checkout`), route prefixes
  and navigation menus. Use lowercase slugs.
- **Existing end-to-end and UI tests:** frameworks and locations. Note helper modules
  (`e2e/helpers.ts`) whose functions run whole flows for the specs that call them.
- **Next.js App Router:** the guard file is `middleware.ts`, or `proxy.ts` from Next 16, at the
  project root or in `src/`. Layouts (`layout.tsx`) often hold auth checks for every page below
  them. Server components may read the database directly, which is where ownership checks belong.
  Server actions (`'use server'`) are endpoints. Imports go through `tsconfig.json` `paths`
  aliases such as `@/*`.
- **React Router (or Vue Router, Angular) single-page app:** routes are usually one
  `createBrowserRouter([...])` config or `<Routes>` in `App.tsx`, with feature modules that
  render their own `<Routes>` under a splat route (`path: 'shop/*'`). Guards are wrapper
  components (`<ProtectedRoute>`, a layout that redirects to sign-in) rather than middleware;
  check whether the sign-in redirect keeps the page the user asked for. API calls go through a
  client module (`api/*.ts`, generated clients, React Query hooks), so trace each screen's calls
  to that module and from there to the endpoint.
- **Several repositories** (an SPA and a separate API repository, or several services): put
  them side by side under one parent directory, run the extractor on each, and cite evidence as
  `<repo>/<path>` relative to that parent. Validate with `--repo <parent>`, so one run re-reads
  citations in every repository. Merge the surfaces into one map, prefixing each item's `file`
  with its repository directory.
- **Multi-service backends:** when endpoints come from several apps or services in one
  repository, the extractor labels each item with `service` (from the nearest project manifest,
  e.g. `services/<name>/pyproject.toml`) and starts its proposed domain with it
  (`orders-loaves`). The same path in two services is two items; name the service in the
  journey's steps (`POST /v1/loaves on the orders service`).
- **Existing journeys:** if the output directory already exists, read `index.md` and note which
  journeys are `reviewed` or `approved`.

## Step 2: Extract the surface

Run the extractor:

```bash
python scripts/extract_surface.py <repo> --out surface.json
```

It finds candidate routes, screens, API endpoints, entry points (deep links, URL schemes) and
goal events (analytics calls). Write `surface.json` to a scratch location, not into the
repository. Details worth knowing:

- **Endpoints are per HTTP method.** A Next.js `route.ts` that exports `GET`, `PATCH` and
  `DELETE` gives three items (`GET /api/x/:id`, ...), each at its export line. `ANY /path` means
  no exported method was found; check how the file exports its handlers. Server actions appear as
  `ACTION <function name>`.
- **Intercepting routes** (`(.)x`, `(..)x`, `(...)x`) are reported under the route they
  intercept, with detector `next-intercepting-route` and a note. They are a second way to render
  that route (usually a modal on client-side navigation), so cover them with a variant.
- **Goals** include calls to tracking wrappers: functions that forward their first parameter to a
  known analytics call (e.g. `trackMixpanelEvent('sign_up_completed')`), found automatically and
  listed in `context.goal_wrappers` with where they are defined. If the app tracks through a
  wrapper the extractor misses, rerun with `--goal-wrapper <name>` (repeatable). Events passed as
  constants (`EVENTS.SIGNUP`) are not resolved; add them as agent items.
- **Python routes** (Flask, FastAPI, Azure Functions) are read by parsing the code, so
  decorators in docstrings and usage examples never count. FastAPI paths include the
  `APIRouter(prefix=...)` (also on subclasses of `APIRouter`) and every
  `include_router(..., prefix=...)` above it; Flask paths include the blueprint's `url_prefix`. A
  note says when a router was never seen being included (a prefix may be missing) or when part of
  a path is computed. `context.python_mounts` lists includes through variables or loops.
- **Server-rendered pages** (Flask): a view whose function calls `render_template(...)` is a
  web **route** (detector `flask-view`) with its `methods`, `view` and the `template` it renders,
  not an API endpoint; JSON views stay endpoints. `context.templates` lists every server-side
  template (`.html` under a `templates/` directory, `.jinja`, `.j2`) with the routes that render
  it, its links (`href="/..."` and `url_for('view')`, resolved to paths), forms (action, method),
  `fetch()` calls and the templates it extends or includes, each by line: these are the
  navigation and API-call edges for Step 3. Django views and computed template names are not
  followed; trace them by hand.
- **Client-side routes** (React Router, Vue Router, Angular) are joined to their parents:
  `children` arrays, nested `<Route>` elements, and a component's own `<Routes>` mounted by a
  splat route in another file (`/shop/*` rendering `<ShopModule/>` makes its `cakes` route
  `/shop/cakes`). Paths built from constant objects (`ROUTES.LOGIN`, `` `${ROUTES.SHOP}/*` ``) are
  resolved. Index routes carry `index: true` and render at the parent's path; lazy routes and
  redirects say so in a note. A path that could not be resolved keeps its expression in braces.
- **Entry points** include command-line commands (`cli: <program> <command>` from argparse
  sub-commands, click and typer commands, `[project.scripts]` and `package.json` `bin`) and
  Azure Functions triggers (`blob trigger: <path>`, `timer trigger: <schedule>`). A trigger is
  not user-facing by itself: find the user action that causes it (an upload that writes the
  blob), cover it from that journey, or orphan it as `not_user_facing`. Operator command lines
  are journeys for an operator persona when they change what users see.
- **Domains** on routes and endpoints are proposals from the first path segment (`/api/orders/:id`
  becomes `orders`), prefixed with the service in multi-service repositories. In an app that serves
  pages and a JSON API under `/api/`, the API's endpoints share the domain `api`; files served at
  the site root (`/robots.txt`, `/sitemap.xml`) get `site-files`. Correct them during review: they
  decide what `out_of_scope` may hold.
- **Data directories are not walked.** JSON files are opened only by name (`package.json`,
  `host.json`, `function.json`, `tsconfig`/`jsconfig`, `openapi*`/`swagger*`), and a directory
  holding 200 or more files, at least 90% of them data files (JSON, CSV, XML, YAML, ...) and none
  code, is skipped and listed in `stats.skipped_data_dirs`. Pass `--exclude <dir>` (repeatable) to
  skip any other directory.
- **Success messages** (`toast.success('Order placed')`, `setSuccessMessage(...)`) are reported
  as low-confidence goals with detector `success-message`. Confirm each marks completion.
- **Test-only surface** (`/test-support/...`, Sentry example pages) carries a note. Confirm it,
  then orphan it as `not_user_facing`.
- **Context:** `flags`, `tests` (test files and the URLs they visit, by `kind`: `browser` for
  Playwright, Cypress, WebdriverIO, Selenium and Python Playwright tests; `http-client` for tests
  that request a URL through a framework test client, such as Flask's `client.get('/x')` or
  supertest, and assert the response), `unit_tests` (how many unit
  and component test files and cases exist, and their runners: not journey tests, so they never
  make a journey `automated`), `services`, `guards` (each Next.js
  `middleware.ts`/`proxy.ts`: its `matcher` values, lists of path prefixes such as
  `AUTH_REQUIRED`, path checks and redirects, by line), `layouts` (each App Router layout with
  lines that look like guards), `path_aliases` (`tsconfig`/`jsconfig` `paths`) and
  `goal_wrappers`.

The extractor is heuristic. Then:

1. **Verify** each item. Remove false positives, e.g. a `HomeScreen` class that is a component,
   or a `path:` key that isn't a route. Record removed items as orphans with reason
   `false_positive` instead of deleting them, so the numbers stay honest.
2. **Resolve** nested and relative routes into full paths where you can.
3. **Add** items the extractor can't see, with `"source": "agent"`, a file and line, and the
   next free ID in that kind's sequence (`R-`, `S-`, `E-`, `N-`, `G-`). Typical additions:
   - Expo/React Native linking config in `app.json`
   - routes built at runtime (from data or a loop)
   - push-notification handlers
   - links in email templates
   - server-rendered pages in templating languages
   - command lines the extractor doesn't recognise (hand-parsed `sys.argv`, shell scripts)
4. Read the `limitations` the extractor reports, and carry any that apply into
   `metadata.notes`.

## Step 3: Build the interaction graph

Trace how users move between surface items. For each screen or route, read its handlers and
record:

- **Edges:** user actions that navigate or change state (`router.push`, `navigate()`,
  `<Link>`, form submits that redirect, `Navigator.push`, segues).
- **Guards:** authentication checks, role checks, feature flags, and required state (e.g. cart not
  empty) that must hold for an edge to be available. Start from `context.guards` and
  `context.layouts`: a matcher says which requests reach the guard file, the path lists and
  checks say what it enforces, and the redirects show where a refused user goes (check whether the
  redirect keeps the query string). Confirm each one by reading the lines cited.
- **API calls** each action makes, linking UI to endpoints. Name the method and path
  (`POST /api/orders`) so the call resolves to one endpoint item. Follow imports through
  `context.path_aliases` (e.g. `@/lib/x` is `src/lib/x`).

Record nodes (`entry`, `screen`, `state`, `goal`) and edges in the `graph` section of the map.
The graph doesn't need every edge in the application. It needs the edges that the journeys
traverse, plus the edges that make the reachability conclusions in Step 8 defensible. If you
don't build it, set `graph_declined` to the reason instead; the validator requires one or the
other, and `journey-graph.json` and the diagrams are then not written.

## Step 4: Identify goals

Goals are states a user is trying to reach. Sources, strongest first:

1. Analytics events that mark completion: `order_completed`, `signup_completed`,
   `report_exported`.
2. Success screens, confirmation routes and success toasts.
3. Transitions to terminal or meaningful statuses in state machines (e.g. `status = 'submitted'`).
4. Created, updated, deleted or exported resources reached through the UI.

Navigation events alone (`page_viewed`) are not goals. When the extractor reports view or
progress events (`checkout_started`, `dashboard_viewed`), keep them in the surface: cover them
from the journey whose step fires them, or orphan them with the reason that applies. Don't drop
them, or the coverage arithmetic stops describing the code.

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
test suites. A `http-client` test exercises a page or endpoint at the HTTP level: it can make a
journey `partial`, never `automated` (RUBRIC section 6). Match tests to journeys by route, screen, selectors, API calls and test titles. Set:

- `automated`: existing tests exercise every step and assert every Expect of the main path.
- `partial`: some steps or variants are exercised, or steps run without their outcomes being
  asserted.
- `none`: no test exercises the journey.

List matched test files in `tests`. Never set `partial` or `automated` without listing the tests.
Be conservative: a test that visits a page isn't a test of the journey. A URL wait
(`waitForURL`, `toHaveURL`) asserts only a navigation Expect, not what the page shows. Flows run
in helper functions count for the spec that calls them: list the spec, and read the helper to see
what it asserts (`RUBRIC.md` section 6).

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
| Orphan `out_of_scope` | Belongs to a domain excluded from this run. The item's `domain` (or the orphan's own `domain`, when the item's proposed one is wrong) must be outside `run_scope.domains` |
| Orphan `not_mapped_this_run` | In scope, and you found its user path, but didn't map it in this run. The `note` names the path (file and line) |
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
python scripts/validate_journey_map.py journeys/journey_map.json --repo <repo>
```

With `--repo` the validator re-reads every cited file and line: a missing file, a line past the
end of the file, or a blank line cited as step evidence is an error. If an evidence entry has an
`excerpt`, it must be on the cited line. It also checks that each journey's `tests` exist. It
checks, with or without `--repo`, that journeys and `not_mapped_this_run` orphans are inside the
run scope and `out_of_scope` orphans are outside it, and that the graph is present or declined.

It also warns when a journey `covers` an item whose file none of the journey's evidence cites
(padded coverage: cite the line where the journey reaches the item, or drop it from `covers`),
and when a map-level open question repeats a journey's. Map-level `open_questions` are for
questions that span journeys; a question about one journey goes in that journey only. Run with
`--strict` to make the warnings errors, and fix them before handing the map over.

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

Strict lint must pass. Use `--color never` when the output will be pasted elsewhere, and
`--brief` for the headline only. In a scoped run the coverage headline counts the run's domains
(every item except `out_of_scope` orphans), and the whole-repository figure follows it.

## Large applications

Map one or a few domains per run, using `run_scope.domains`. Items in other domains are orphans
with reason `out_of_scope`; in-scope items you traced but had no time to map are
`not_mapped_this_run`. Stable IDs make runs add up: `index.md` always lists every journey
file on disk, and `coverage.md` describes the latest run's scope. Suggest a domain order to the
user, highest-risk first (payments, authentication, data entry).

## Final response to the user

Keep it short. Include:

1. Coverage: surface items found, the percentage in journeys, orphans and unreachable items. Say
   that it is a lower bound. In a scoped run, give the in-scope figure first and the whole
   repository second, and say how many in-scope items were `not_mapped_this_run`.
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
