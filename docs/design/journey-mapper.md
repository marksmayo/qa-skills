# Design: `journey-mapper-skill`

Status: Built (v1.0.0); see [Files](#files) for how the build differs from this design. Build order: 1 (after shared components).

## Mission

The skill reads a codebase and documents the user journeys across the whole application, for
both web and mobile. The journeys are written in the Journey Markdown format (see
[shared-components.md](shared-components.md)). Each one is prioritised, linked to evidence in
the code, and marked with whether tests already cover it, so it can be turned into tests.

## Non-negotiable rules

- **Journeys inferred from code describe how the software behaves now, not how it should
  behave.** Every journey starts as `status: inferred`. Only a person can promote one to
  `reviewed` or `approved`.
- **Every step cites code evidence** (file and line). Steps with no evidence aren't written;
  the gaps go in `Open questions`.
- **Coverage figures are always reported**, e.g. "routes found 142, routes in at least one
  journey 118, orphans listed". "Documented the entire software" is never claimed without these
  numbers.
- **Unreachable code is flagged, not turned into a journey.** This covers dead routes, screens
  behind flags that are off by default, and unreachable screens. Admin-only and internal paths
  are documented but tagged with their persona.
- **Source code is read-only.** The only files written are under `journeys/`, and they are left
  uncommitted.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | cwd | |
| `domains` | all | Limits a run to some domains, for large apps |
| `max_journeys` | 50 per run | Larger apps are mapped over several runs. Stable IDs make the runs add up |
| `platforms` | auto | web, ios, android, cross-platform (React Native, Flutter) |
| `output_dir` | `journeys/` | |
| `diagrams` | on | Mermaid flow diagram per domain in `index.md` |

## Workflow

1. **Recon.**
   - Identify the app type: web SPA or SSR, mobile (native or cross-platform), desktop,
     API-only, or CLI. A repo may be several of these.
   - Identify the frameworks, the role and permission model, feature flags and i18n.
2. **Extract the surface.**

   | Surface | Sources |
   |---|---|
   | Web routes | React Router, Vue Router, Angular routes, and file-based routing in Next.js, Nuxt, SvelteKit and Remix |
   | Mobile screens | React Navigation, Expo Router, Flutter `GoRouter`/`Navigator`, Android nav graphs and `Activity`/`Fragment` manifests, iOS storyboards, SwiftUI `NavigationStack`, coordinators |
   | Entry points | Deep links and universal links, push-notification handlers, links in email templates, landing pages |
   | Actions | Forms, buttons and gestures, their handlers, and the API calls they make |
   | Backend | Route definitions and OpenAPI or GraphQL schemas |
   | State | Status enums and their transitions (state machines) |
   | Guards | Authentication, role checks, feature flags, required state |

3. **Build the interaction graph.**
   - Nodes are screens and states. Edges are user actions, annotated with their guards.
   - The graph is saved to `journey-graph.json`.
4. **Find goals.** Goals are the "value" states a user is trying to reach:
   - analytics events (`track('order_completed')`)
   - success screens and toasts
   - transitions to terminal statuses
   - created or exported resources
5. **Synthesise journeys.** The skill walks the graph from entry points to goals.
   - Each journey is named by the user's goal.
   - The happy path becomes the steps.
   - Error paths, alternative paths, role differences and platform differences become
     **variants**, so the files don't multiply.
6. **Add coverage from existing tests.** The skill matches existing e2e, integration and UI
   tests to journeys by route, screen, selector, endpoint and test title, then sets
   `automation: none | partial | automated` and `tests:`.
7. **Prioritise.** RUBRIC.md defines the model:
   - criticality: revenue, authentication and account, data loss, compliance
   - how central the journey is in the graph, used as a proxy for how often it's used
   - step count and how many variants it has
   - inverse coverage: critical journeys with no tests rank highest
8. **Write the output** idempotently: existing files are merged, and fields a person has set are
   preserved. Then lint every journey file.

## Output

The output is the `journeys/` directory described in the shared format, plus a machine-readable
run summary in `journey_map.json`:

```text
metadata:  repo, generated_at, platforms, frameworks, domains_mapped, run_scope
surface:   {routes, screens, endpoints, entry_points, goals} with counts
coverage:  {surface_items_total, in_journey, orphans[{item, reason}]}
journeys[]: {id, path, priority, persona, platforms, automation, status, steps, variants}
open_questions[], unreachable[{item, reason: dead|flag_off|guarded_unreachable}]
```

## Validator (`validate_journey_map.py`) fails when

- A journey has a step with no evidence reference.
- A journey written by the mapper has any `status` other than `inferred`, unless the file had
  already been promoted by a person.
- The coverage numbers are missing, or don't add up.
- Two files share the same journey ID.

## Files

Status: **built** (v1.0.0). The implementation differs from the original plan as follows:

- `extract_routes.py` became `extract_surface.py`. It also extracts endpoints, entry points, goal
  events, flag calls and e2e test visits.
- `build_graph.py` was dropped. Tracing edges and guards needs code reading, so the agent writes
  the `graph` section of `journey_map.json` directly.
- `lint_journeys.py` is the shared `journey_md.py lint`.
- `journey_map.json` lives in the output directory (`journeys/journey_map.json`).
  `write_journeys.py` records the files it wrote in the map's `files` list.
- Coverage is enforced arithmetically. Each journey lists the surface item IDs it `covers`. Every
  surface item must be covered, orphaned (with a reason) or unreachable (with evidence), and the
  validator checks that the totals match.
- Reviewed or approved journeys are never overwritten. Updates go to `<name>.proposed.md`.

Version 1.1.0, after a run on a real Next.js 16 App Router application:

- The extractor reports one endpoint per exported HTTP method of a Next.js `route.ts` (including
  `export { handler as GET }` and `export const { GET, POST } = handlers`), Express
  `router.route(path).get().post()` chains, and server actions (`ACTION <name>`). Intercepting
  routes resolve to the route they intercept instead of being treated as route groups.
- Goals include calls to tracking wrappers: functions that forward their first parameter to a
  known analytics call, followed through wrappers of wrappers, plus names given with
  `--goal-wrapper`. `context.goal_wrappers` lists them with their definitions.
- New context: `guards` (matchers, path lists, path checks and redirects in `middleware.ts` or
  `proxy.ts`), `layouts` (App Router layouts with guard-like lines) and `path_aliases`
  (`tsconfig`/`jsconfig` `paths`, read as JSON with comments). Routes and endpoints carry a
  proposed `domain` from their first path segment, and test-only surface carries a note.
- The validator takes `--repo` and re-reads every cited file and line (missing file, line past
  the end, blank step evidence, optional `excerpt` mismatch) and checks journeys' `tests` exist.
  `out_of_scope` orphans must have a domain outside the run scope, journeys must be inside it,
  and the new orphan reason `not_mapped_this_run` (in scope, path found, not mapped) needs a note.
  The map must carry `graph` or `graph_declined`.
- The report's coverage headline uses the run's scope in scoped runs, with the whole-repository
  figure second; `--brief` prints the headline only. `coverage.md` shows both figures. Journey
  file slugs are cut at a word boundary.

Version 1.2.0, after a run on a React Router SPA with a separate multi-service FastAPI repository:

- Python routes are read with `ast` (`py_routes.py`), so decorators inside docstrings no longer
  count. FastAPI paths include `APIRouter(prefix=...)`, on subclasses of `APIRouter` too, and
  every `include_router(..., prefix=...)` above it, across modules; Flask paths include blueprint
  prefixes. A router never seen being included, or a computed prefix, is noted on the item.
- Client routes come from `js_routes.py`: `createBrowserRouter` objects and `children`, `<Route>`
  elements, index and lazy routes, and a component's own `<Routes>` joined to the splat route that
  renders it from another file. Paths built from constant objects (`ROUTES.X`, template literals)
  are resolved.
- When routes or endpoints come from several apps in one repository, each item carries `service`
  (from the nearest project manifest) and its proposed domain starts with it.
- Entry points include command lines (argparse sub-commands, click/typer, console scripts,
  `package.json` `bin`) and Azure Functions triggers; Azure HTTP functions are endpoints.
- `context.unit_tests` counts unit and component test files and cases, with their runners.
  Success toasts and messages are low-confidence candidate goals.
- The validator warns (errors with `--strict`) when a journey covers an item whose file none of
  its evidence cites, and when a map-level open question repeats a journey's. The renderer and
  `coverage.md` list each open question once.
- `extract_surface.py --self-test` checks the extractor against a built-in sample repository.
- Server-rendered Flask sites (1.3.0): a view that calls `render_template` is a web route with its
  template; `context.templates` lists each template's links (`url_for` resolved to paths), forms,
  `fetch()` calls and extends/includes by line. `/api/*` endpoints of an app that also serves
  pages share the domain `api`; root files (`/robots.txt`) are `site-files`. `context.tests`
  entries have a `kind`: `browser` (now including Python Playwright and Selenium) or
  `http-client` (framework test clients; at most `partial` per RUBRIC section 6).
- Data trees (1.3.0): JSON is opened only by name, and a directory of 200+ files that are at least
  90% data and hold no code is pruned before it is walked (`stats.skipped_data_dirs`, `--exclude`).
  A site with thousands of JSON records took minutes to extract before this.
- The validator checks padded coverage per item when several items share a file (a span runs to
  the next item's line), reports entries of the wrong shape instead of crashing, and has a
  `--self-test`.

```text
journey-mapper-skill/
  SKILL.md  RUBRIC.md  README.md
  JOURNEY_FORMAT.md                      # shared copy
  schemas/journey.schema.json            # shared copy
  schemas/journey_map.schema.json
  scripts/extract_surface.py             # heuristic surface + context extraction (stdlib)
          py_routes.py  js_routes.py     # Python routes/CLIs/triggers (ast); client route trees
          write_journeys.py              # idempotent merge, preserves human edits
          journey_md.py                  # shared copy: parse / lint / render
          validate_journey_map.py  render_report.py
  examples/journeys/…                    # complete sample output incl. journey_map.json
```
