---
name: ui-crash-hunter
description: Hunt for code paths in web and mobile UI code that are likely to crash the app (B1, app-fatal) or a whole screen (B2, screen-fatal), rank them by real-world severity, and write reproduction steps for each. Uses static analysis, an API-contract diff and the error-boundary map, then confirms crashes in the running app where possible. Every finding has a file and line, a concrete trigger and a confidence level (reproduced, traced or pattern); degradations are listed separately and never ranked as crashes.
version: 1.5.0
---

# UI Crash Hunter

## Mission

Find the places where realistic data, environments or user actions make the UI crash, and say how
bad each one is and how to reproduce it. A crash here means:

- **B1, app-fatal:** the app terminates (native crash), the whole single-page app goes blank, or the
  app crashes on every launch because of stored state (a **crash loop**).
- **B2, screen-fatal:** an error boundary or fallback replaces the whole screen or route, or the
  screen freezes (render loop, hang, Android "Application Not Responding").

Smaller failures are **degradations**, reported in their own list and never ranked with crashes:

- **B3:** one component is broken or replaced by a fallback; the rest of the screen works.
- **B4:** a console error or unhandled rejection with no visible effect.

Output:

1. `crash_report.json`, conforming to `schemas/crash_report.schema.json`: recon (routes, screens,
   the error-boundary map), tool status, candidate triage, probes, ranked crashes, degradations,
   coverage and limitations.
2. Evidence and repro specs in an output directory **outside** the repository.
3. A rendered summary for the user (and SARIF, if they want it in a code-scanning dashboard).

## Non-negotiable rules

- **"Likely crash" is not "crash".** Every finding has a confidence:
  - `reproduced`: forced in the running app, with a screenshot and either a stack trace or, for a
    silent or non-script failure, the probe's observed effect (see step 5).
  - `traced`: you followed a realistic input through the code to an unguarded throw, hop by hop.
  - `pattern`: a lint rule or heuristic matched and you did not trace it further.
- **Every finding cites a file and line, and a concrete trigger**: the exact data shape, stored
  value, environment or user action, and why it is realistic (the contract, the changelog, the
  declared targets, telemetry). "This could be null" with no trigger is not a finding.
- **The tier comes from the boundary map, not from the throw.** The same `TypeError` is B1 with
  nothing above it, B2 under a route boundary and B3 under a component boundary. Name the boundary
  that catches it, or say that nothing does.
- **Never modify source or config.** Stricter compiler and lint configs live only in a scratch
  directory outside the repository. Evidence, probe scenarios and repro specs go to the output
  directory, also outside the repository, unless the user asks for them elsewhere.
- **Don't install anything without asking**, and never commit, push or create branches. A tool
  that isn't available is recorded as `not_run` with the reason.
- **"No findings" is not "crash-free".** The report states which routes, screens and components
  were analysed, which tools ran, and what wasn't covered. Never write "crash-free", "stable",
  "safe" or "no crashes".
- **Probe only local or test environments** the user has approved, with mocked or test data.
  Never point probes at production or at real user accounts. A target that isn't loopback needs the
  user's own approval (`--authorised-by`, recorded as `app_run.authorised_by`) and runs read-only:
  no request other than GET, HEAD or OPTIONS leaves the browser.
- **UI code is the scope.** Tests, API route handlers (a throw there is an HTTP 500, not a UI
  crash) and modules no page or entry point imports are out of scope; the scripts exclude them and
  count what they excluded. Say so in the coverage.
- **Don't reveal secrets.** Never copy tokens, keys or personal data from code, storage or
  telemetry into the report; give the location and the kind of secret only.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | current directory | The UI code to analyse (web, Android, iOS, Flutter, React Native) |
| `scope` | whole app | Paths, routes or screens to focus on |
| `app` | none | How to run it: a local URL, a dev-server command, or an emulator/simulator build. Without it the report is static only |
| `journey_map` | none | `journey_map.json` from journey-mapper, for route priorities (the reach factor) |
| `telemetry` | none | A Sentry, Crashlytics or Bugsnag export; raises likelihood and confirms production crashes |
| `out_dir` | `<repo>-crash-hunt/` beside the repo | Evidence, probe scenarios, repro specs, the report. Must be outside the repo |
| `scratch` | a temp directory | Strict overlay configs |

## Workflow

### 1. Recon

```bash
python scripts/recon.py <repo> [--journey-map journey_map.json] --out <out_dir>/recon.json
```

Read the output, then confirm and complete it by reading the code:

- **The site model and blast radius** (`site.kind`, `blast_radius`). A single-page app or a
  server-rendered framework has one render tree: an uncaught render error above every boundary
  blanks the app (`per_root`). A static multi-page site (HTML pages that each load their own scripts,
  no SPA framework) is `per_script`: the HTML renders without script, so an uncaught error stops
  only the script (or event handler) that threw and breaks that feature (B3) or only logs (B4). It is
  B1 only on pages whose content is rendered by script (`content_rendered_by_js`) or when the script
  hides the content. For static sites recon lists the pages (one route per page, or one per set of
  scripts when there are many), the scripts each loads, scripts that inject others, pages with
  inline scripts (read those yourself; the pattern scan skips HTML), and error pages such as
  `404.html` whose relative asset URLs break when served at a nested path.
  A site that a Node server renders itself (`node:http` `createServer`, Express, Fastify or Koa, with
  pages built from HTML template literals) is `multi_page` / `per_script` too, with
  `site.served_by: "node_server"`: recon follows the server's relative imports and lists the routes
  from pathname comparisons, `case '/x':`, `app.get('/x')` and `'/path': handler` tables, keeping
  `/api/*`, JSON and non-GET handlers apart in `site.server.api_endpoints`. The browser code is then
  inline `<script>` blocks inside template strings (`site.server.templates`), which no static tool
  reads: read them, and probe the pages. The server modules and Node command-line scripts are server
  code (a throw is an HTTP 500 or a failed command), and `scan_pitfalls.py` excludes them.
  A directory named like a test folder (`e2e/`, `tests/`) that holds its own package manifest is a
  product package and is analysed. Xcode test targets (`<Name>Tests`, `<Name>UITests`,
  `<Name>Tests_<platform>`) are tests.
- **Language coverage and vendored trees** (`language_coverage`, `warnings`). Vendored and bundled
  third-party trees (`vendor/`, `third_party/`, a bundled driver such as WebDriverAgent, a nested
  directory with its own LICENSE and a manifest of another ecosystem) are left out of everything.
  When the dominant language is one no pack reads (Go, Rust, Python, ...), recon and the scan say so
  in `warnings`, and with under a quarter of the source in an analysed language recon sets `site.kind`
  and `blast_radius` to `unknown` rather than model the app from incidental files. Say in the report
  that the dominant language was not analysed (limitations, coverage); never present the empty static
  result as evidence.
- **HTML held in string constants** (`site.embedded_templates`): a page a Go `html/template` constant,
  a Python or Java/Kotlin triple-quoted string, a Rust raw string or a template file (`.tmpl`,
  `.gohtml`, `.jinja`, `.erb`) holds. `scan_pitfalls.py` scans their inline scripts at the host file's
  real line (candidates marked `embedded`); read the rest of the template yourself, and probe a
  rendered page. A generated report file is probed as a local file (see the probe step). A page whose
  list is rendered by the server and whose detail is rendered by its script is `per_script`: a throw
  in the script empties the script-rendered panel (B3), not the server-rendered list; mark the route
  `content_rendered_by_js` when the script renders the main content.
- **Platforms and frameworks**, and the supported targets (`browserslist`, `minSdkVersion`, iOS
  deployment target). Targets decide whether an API is "missing in a supported browser or OS".
- **Routes and screens.** Add the app shell (header, navigation, layout) as an item: it renders on
  every screen and is often outside every boundary. For Next.js recon adds the root layout as a
  `shell` item itself (`(root layout)`, at the line of its default export).
- **Next.js** (`recon.nextjs`): the layouts (the root layout and everything it renders, including
  providers and `next/dynamic` widgets, is outside every `error.tsx`: only `global-error` catches a
  throw there, and it replaces the whole document), `template`/`not-found`/`loading` files,
  `middleware.ts`/`proxy.ts` with its matcher (it runs before every matched page: auth redirects and
  rate limits change what a probe sees), `next/dynamic` imports (`ssr: false` runs only in the browser,
  after hydration), and the route handlers with their HTTP methods. Route handlers are API code: they
  are not UI routes and never count as analysed routes. Server components run during SSR; their
  triggers are server data (a seeded test database), not browser mocks.
  The Next.js file conventions apply only when the project's `package.json` depends on `next`, and only
  to `app/` and `pages/` at its root or under `src/`: a `pages/` folder inside a plain React app is
  just a folder, not routes. A directory holding `next.config.*` is the project root even without a
  `package.json` of its own (`next build app`, with `next` in the package above), and its own
  tsconfig `paths` resolve the app's imports.
- **Module-scope configuration** entries for a module no UI entry point imports carry
  `reachable_from_ui: false`: they cannot blank the app unless loaded some way the graph can't follow.
- **Code-built contracts.** Recon lists OpenAPI documents built in code (`openapi-in-code`) and
  Prisma schemas. To use a code-built OpenAPI document, run its own builder or export endpoint into
  the output directory and pass that file to `contract_diff.py --contract`. A FastAPI app builds its
  document at runtime; recon names the module and gives the export command
  (`python -c "import json, main as m; print(json.dumps(m.app.openapi()))" > <out_dir>/openapi.json`,
  run from the API's directory; it imports the app, so ask first). A repository that is only a FastAPI
  service is `site.kind: "api"`: hunt crashes in the UI that calls it, with its document as the contract.
- **The error-boundary map.** For each boundary, record its `scope` (`app`, `route`/`screen`,
  `component`, `global`) and what it covers. Check what is *outside* every boundary: the root
  render, the shell, providers, and code that runs before the first render. Recon finds router
  boundaries too (`kind: route_prop`/`route_module`): a route's `errorElement` or `ErrorBoundary`
  (React Router 6.4+ data routers and 7, Remix), a route module's exported `ErrorBoundary`, TanStack
  Router's `errorComponent`, and `withErrorBoundary` from `react-error-boundary`. A throw under a
  route's `errorElement` (render, loader or a failed `lazy()` import) is B2, not B1.
- **Module-scope configuration** (`module_scope_config`): modules that read `import.meta.env` (Vite)
  or `process.env` and throw at module scope fail on import, before any boundary renders, so a
  missing or invalid value blanks the app (B1). This is the Vite analogue of configuration read in a
  Next.js root layout.
  Framework specifics are in `RUBRIC.md` (React, Next.js, Vue, Angular, React Native, Flutter,
  Android, iOS).
- **The data layer** (fetch, axios, React Query, Apollo, Retrofit, URLSession, Dio) and whether
  responses are validated at the boundary (zod, kotlinx.serialization, Codable).
- **API contracts** (OpenAPI, GraphQL, generated types and models).
- **Persisted state** (localStorage, IndexedDB, AsyncStorage, SharedPreferences, DataStore,
  UserDefaults, Core Data, Room, Hive) and how its format changed over versions. A format change
  without migration is the classic crash loop.
- **Reach** for each route or screen: `critical_path` (entry, auth, checkout, onboarding, the shell),
  `high_traffic`, `rare`, or `unknown`. Use journey priorities or analytics when given; otherwise
  say it is your reading of the app.

### 2. Static analysis

Run what applies; record every tool's status, including the ones that couldn't run.

```bash
python scripts/strict_overlay.py tsc <repo> --scratch <scratch> --out <out_dir>/tsc.json
python scripts/strict_overlay.py eslint <repo> --scratch <scratch> --out <out_dir>/eslint.json
python scripts/contract_diff.py <repo> --out <out_dir>/contract.json
python scripts/scan_pitfalls.py <repo> --sarif <semgrep/detekt/swiftlint .sarif files> \
    --extra <out_dir>/tsc.json <out_dir>/eslint.json <out_dir>/contract.json --out <out_dir>/candidates.json
```

| Stack | Tools (only if already installed, or the user agrees to install) |
|---|---|
| TypeScript / JavaScript | `strict_overlay.py tsc` (strictNullChecks, noUncheckedIndexedAccess; plain JavaScript without a tsconfig is checked with `checkJs` through a config generated in the scratch directory, using the repo's or the PATH's `tsc`, and is `not_applicable` only when there is no `tsc`) and `eslint` (react-hooks/exhaustive-deps, no-floating-promises, no-non-null-assertion, no-unnecessary-condition) using the repo's own installs. Both compile or lint only what the UI entry points reach (`--scope ui`, the default; `--scope all` for everything), and the baseline and strict compiles run side by side through scratch configs, so nothing (not even `tsconfig.tsbuildinfo`) is written into the repo. The ESLint step asks ESLint which plugins the config really loads (`--print-config`), so plugins that come in through `eslint-config-next` count (it asks about a source file under `src/`, never `eslint.config.js`, which a flat config for `**/*.{ts,tsx}` does not cover); type-aware rules run only when the config has type information. A solution-style `tsconfig.json` (`"files": []` plus `references`, the create-vite layout) compiles nothing, so the overlay compiles the referenced project whose `include` covers `src/` (`tsconfig.app.json`) and says so, or is `errored` when none does. A compile that covered no source file is `errored`, never "0 new errors"; a baseline with more than 50 errors carries a `warning` that the tsconfig is probably not the one the build uses (pass `--tsconfig`). The output always names the `tsconfig` it compiled, its `compiled_files` and `ui_files_compiled`, and lists `warnings` when it covers under half of the UI's files, when the baseline has errors of its own, when `workspace:*` dependencies have no built output (build the workspace first) and when the pages' browser code is inline scripts in template literals that no type checker sees: read them before trusting "0 new errors". Without `--tsconfig`, a Next.js or Vite app nested in a subdirectory is compiled with its own tsconfig (the one nearest most UI entry points). oxlint (`.oxlintrc.json`) is not run: the ESLint overlay is then `not_run` |
| Android | Android Lint (`./gradlew lint`, SARIF via `lint { sarifReport = true }`), detekt (`--report sarif:...`) |
| iOS | SwiftLint (`swiftlint lint --reporter sarif`) with force_unwrapping, force_cast, force_try enabled in a scratch config |
| Flutter | `dart analyze` / `flutter analyze` (null-check and `use_build_context_synchronously` diagnostics) |
| All | Semgrep with rules written to the scratch directory, `--sarif` output |
| All | `scan_pitfalls.py`: the stdlib pattern scan over `data/pitfalls.json`, which always runs |

The **contract diff** (`contract_diff.py`) is the biggest single source of real crashes: fields the
contract marks nullable or optional, dereferenced in the UI without a guard. It reads OpenAPI,
GraphQL, Prisma schemas (`?` fields are nullable; required relations are not), exported TypeScript
types and mobile models, but not a route handler's private row types. A field name that is non-null
in another type is skipped as a name collision and counted (`--include-collisions` lists them), and
`prisma.model.findMany()` delegates are not field reads. A call such as `.filter(` or `.trim(` is a
method on something else, not a read of a field with that name, and is skipped (counted in
`skipped.method_call`) unless a contract field of that name is a function type that is not a built-in
method name. In a FastAPI-generated OpenAPI document a property with a non-null default is always
serialised, so it is not "optional" (`--defaults-present` forces this for other documents), and a
Pydantic `X-Input` schema is skipped when `X-Output` exists. Access sites are looked for only in files
the UI entry points reach (`--scope all` for every file), never in tool configs (`vite.config.ts`,
`eslint.config.js`). With `--contract`, only the listed files are read, so the UI's own TypeScript
types drop out; add `--discover` to read both. The strict `tsc` overlay finds the same class
of bug through the types, including accesses after destructuring that name matching misses.

`scan_pitfalls.py` scans UI code only: it follows imports from the UI entry points (resolving
`tsconfig`/`jsconfig` `paths` such as `@/*`, including those of the projects a solution tsconfig
references, and a literal Vite `resolve.alias`) and excludes tests, route handlers and unreachable
modules from its own scan and from the `--extra`/`--sarif` candidates, counting each in
`scope.excluded` with examples. `--all-files` scans route handlers and unreachable modules too; tool
configs stay out either way. Read `scope.warnings`: imports that did not resolve (an `@/` alias the
graph doesn't know), or under 30% of the UI files reachable, mean the scope is wrong, not that the app
is small; fix the alias or use `--all-files`, and say so in the coverage.

### 3. Triage every candidate

`candidates.json` holds leads, not findings. For each candidate, decide:

- **Promote** it to a finding (or merge it into one) when a realistic input reaches it unguarded.
- **Dismiss** it with a specific reason: the guard and its line, the invariant that makes it
  unreachable, or why the trigger can't happen.

Every candidate ends up in `triage.promoted`, `triage.dismissed` or `triage.dismissed_groups`; the
validator checks the count against `candidates.json`. On a large app, dismiss a class of candidates
that one checkable rule covers as a group: `rule_id`, a `match` (`pitfall_id`, `path` glob, `tool`,
source `rule`, `snippet` regex), the specific reason, and the ids; the validator checks every listed
candidate matches. Don't paste one sentence onto many individual dismissals: more than three
identical reasons are rejected. Then read the code for crash classes no tool finds (unsupported APIs in
the declared targets, render loops, stale responses, lifecycle misuse, deep links, permission
denial, process death, configuration read at module scope in the root layout or, in a Vite app, from
`import.meta.env` in a module that throws when a value is missing): see the catalogue in
`RUBRIC.md`. Those findings set `"source": "agent"`, `sources: [{"tool": "agent"}]`, a `basis` (file,
line and an excerpt of that line, re-read by the validator) and no candidate ids.

### 4. Trace each finding

For every finding, write the trace from the input to the throw as `file:line` hops, and answer:

- Can a realistic input reach this code? What is the exact data, state, environment or action?
- What guards are in the way? (If one works, dismiss the candidate.)
- Which boundary catches the throw? That decides the tier and the `effect`.
- Does the bad input persist (storage, cache, server state)? Then it can be a crash loop.

### 5. Dynamic confirmation (when the app can run)

Ask before starting servers or emulators. Confirm crashes in priority order (P0 and P1 first).

**Web** (Playwright for Python; see `requirements.txt`; ask before installing):

```bash
python scripts/probe_web.py <out_dir>/probes/C-1.json --out-dir <out_dir>/evidence/C-1 \
    --emit-spec <out_dir>/repro/repro_C-1.py
```

A scenario mocks API responses, seeds storage once (so reloads see what the app itself wrote), sets
locale, time zone and viewport, and runs steps (`goto`, `click`, `fill`, `type` (key by key),
`press`, `reload`, `back`, `offline`, `unroute` (stop a mock, so the next request reaches the real
server), `expect`, `wait_for`, `wait_ms`). Each step is an object with one action:

```json
"steps": [
  {"goto": "/checkout"},
  {"fill": ["#email", "test@example.test"]},
  {"fill": "#card", "value": "4111"},
  {"type": ["#search", "lamp"]},
  {"click": "button[type=submit]"},
  {"press": "Enter"},
  {"expect": [{"text_visible": "Order placed"}]},
  {"wait_ms": 500}
]
```

`fill` and `type` take a selector and a text, as a pair or with `"value"` (the form the WCAG and
cross-browser skills use). `probe_web.py` validates every step and assertion before a browser starts
and exits 2 naming the bad one; `--check` validates only. If a step still fails at run time, the
evidence has `step_error` and `expectations_skipped: true`: the final assertions were never evaluated,
which is neither a reproduction nor a clean baseline. The evidence also records each step's `ms` and a
`timing` block (launch, steps, total), so a slow probe can be explained. `--emit-spec` writes nothing
for a baseline probe (a baseline never reproduces). The evidence file records uncaught page errors with
stacks, console errors (a boundary that catches an error usually logs it), every request with its
status, failed requests, whether the root is empty (white screen) and which fallback texts are
visible, plus a screenshot. Console errors keep their arguments: an Error that a boundary logs with
`console.error(error)` is recorded as `{name, message, stack}` (Firefox's text for it is only
`JSHandle@object`), and `console_error` assertions match against it. `--emit-spec` writes a
standalone script that exits 1 while the problem reproduces and 0 once it is fixed; it carries the
same browser, launch and context options, init scripts and guards. Run `probe_web.py` rather than
importing it; if a wrapper must import it, run that with `python -B` so nothing is written into the
skill folder.

Environment options, for triggers a mock can't produce:

| Option | Effect |
|---|---|
| `"storage_blocked": true` (or `{"except": [stack substrings]}`) | `window.localStorage`/`sessionStorage` getters throw `SecurityError`, as when the browser blocks site data. `except` spares callers whose stack contains a substring, to confirm each of several throwing sites on its own |
| `"init_scripts": ["js", {"file": "x.js"}]` | Scripts run before the page's own (files relative to the scenario) |
| `"browser_prefs": {...}` | Firefox preferences, e.g. `{"network.cookie.cookieBehavior": 2}` (the real "block cookies and site data") |
| `"launch_args": [...]` | Chromium switches |
| `"bypass_csp": true` | Ignore the page's CSP (on by default in read-only mode, so no violation report is sent) |

**Read-only mode** is the default for any target that isn't loopback (`"read_only": true` turns it on
anywhere). Every request other than GET, HEAD or OPTIONS is answered in the browser with 204 and never
sent, whatever its host, including the app's own POSTs, beacons and CSP reports; same-origin analytics
(`/_vercel/*`) is aborted. Intercepted writes are listed in `writes_blocked`. `probe_web.py` refuses a
non-loopback target without `--authorised-by`.

**Local files.** A generated page (a test report written to disk) is probed where people open it:
`--base-url path/to/report.html` (or a `file://` URL). A local file counts as loopback and is probed
read-only by default; `goto` steps resolve against the file (`{"goto": "#flow-3"}`). Behaviour can
differ from the same page served over HTTP (polling, `fetch` of sibling files, URL encoding of paths),
so probe the way it is used. Recorded requests keep their query string; a `request` glob without `?`
also matches the URL without its query.

**Next.js.** A throw in the root layout (or a provider or `next/dynamic` widget it renders) is caught
only by `global-error`, which renders its own document: the root is not empty, so reproduce it with
the stack trace, or by its effect with the global-error text in `observe.fallback_text` and an
`expect` on it, and use the effect `app_fallback` (B1, `caught_by` the global-error boundary). A
server component's trigger is server data: seed a test database, since `page.route` mocks only the
browser's requests. Pages behind `middleware.ts`/`proxy.ts` auth need a test session (a `goto` +
`fill` sign-in, or a seeded cookie in an init script).

**Apps behind MSAL, OIDC or another identity provider.** A single-page app that signs in through an
identity provider (Entra ID with MSAL, Auth0, Okta, Cognito) renders only its sign-in page without a
session, and the sign-in redirect leaves the app's origin, so a probe that follows it is
`inconclusive`. Options, least invasive first, all with the owner's agreement:

- **A captured session (`storage_state`).** A person signs in to a **test account** by hand in a
  Playwright browser and saves the context's storage state to a file outside the repository; probes
  load it with `probe_web.py --storage-state <file>` (and the repro spec takes the same flag). The file
  holds live tokens: never commit it, never copy its contents into the report or the evidence (which
  record only `signed_in_session: true`), and delete it after the run. A storage state carries cookies
  and localStorage, not sessionStorage, so it works for MSAL only when the test build caches tokens in
  `localStorage`.
- **The app's own test mode**: a mock-auth flag or test identity provider the app already supports for
  local development or end-to-end tests.
- **Seeding an MSAL cache entry** in an init script, only with the owner's explicit consent and test
  credentials.

Never use a real user's account, and never sign in to a production tenant. When none is available, probe
what renders without a session (the sign-in page, public routes), set `run_status` to `partial`, and
say in the limitations which routes went unprobed because they need a session.

**Third parties are blocked.** Every request to a host that isn't loopback, the base URL's host or
in `allowed_hosts` is aborted and listed in `blocked_requests`, GETs included, so a probe never
sends analytics hits or ad requests. Console errors caused by that blocking go to
`blocked_console_errors` and never count towards a reproduction or against a baseline.
`third_party: "allow"` lifts the block (only with the user's agreement), and
`service_workers: "block"` stops service workers for the run. Errors inside blocked scripts, and
their effect on the page, are therefore untested: say so in the limitations.

**Effects, not only errors.** Many real failures throw nothing visible: a fetch failure swallowed by
`.catch(() => {})` leaves search dead with no message, and a `404.html` with relative asset URLs
loads unstyled at a nested path. Describe the defect's observable effect in `expect` (and in
`expect` steps, checked at that point):

| Assertion | Holds when |
|---|---|
| `{"visible": sel}` / `{"hidden": sel}` | the element is visible / absent or hidden |
| `{"text_visible": t}` / `{"text_absent": t}` | the text is visible / not visible |
| `{"count": sel, "equals"\|"min"\|"max": n}` | the number of matching elements |
| `{"style": sel, "property": p, "equals"\|"contains"\|"not_equals"\|"not_contains": v}` | a computed style |
| `{"request": glob, "made": true\|false, "status": n}` | a request was (not) made, optionally with that status |
| `{"page_error": s}` / `{"console_error": s}` | an error containing the text was raised / logged |

When every assertion holds and no step failed, `effect_observed` is true and the probe reproduces
(`reproduced_by: ["effect"]`), with or without an error. Assert the defect precisely, so that nothing
else explains it: "no results **and** no second request for the index after the server recovered" pins the
memoised failure; "no results" alone could be an empty index.

Always run one **baseline** probe first (`"kind": "baseline"`, contract-valid data, assertions that
describe healthy behaviour, such as results appearing and the stylesheet applying). It never counts
as reproduced; `baseline_ok` is true when there were no errors of the app's own and every assertion
held. If the baseline already errors or fails, fix the scenario before blaming the code; if the
target itself is degraded (its own API returns 500s), mock the failing requests in the browser, or
record the probe as `baseline_not_clean` with a note and set `run_status` to `partial`. A probe that
couldn't run at all is `errored`, with a note. A probe whose page ended off the app (the browser's
own error page after following a link to a blocked host, a redirect to an identity provider, or the
`observe.root_selector` missing) is `inconclusive`: its evidence says `inconclusive` with the reason,
`baseline_ok` is false and nothing is reproduced, because none of its checks ran on the app. Record
the outcome `inconclusive` with a note.

Triggers worth forcing: null fields, `[]`, missing keys, 10,000-character strings, emoji and
right-to-left text, HTTP 401/404/500, slow responses, offline, another locale or time zone, stored
state from an older version or corrupted, fast navigation (a slow response arriving after the user
left), React StrictMode double rendering, and an optional monkey run (gremlins.js) if the user wants.

**Mobile:** use the repo's own e2e tool if it has one (Detox, Maestro, Espresso, XCUITest,
`integration_test`) with a mock server or the platform's network mocking. Capture:

- Android: `adb logcat -d -b crash` and `adb logcat -d *:E`, plus `adb shell screencap`.
- iOS simulator: `xcrun simctl spawn booted log show --last 5m --predicate 'process == "<App>"'`,
  the `.ips` crash report, and `xcrun simctl io booted screenshot`.
- Flutter: the `flutter run` / `flutter drive` output (`FlutterError` and the red/grey error widget).

Also try rotation, backgrounding and returning, "Don't keep activities" (Android process death), and
denying permissions. Write the flow (Maestro YAML, Detox test) to the output directory as the repro
spec.

A finding is `reproduced` only with a probe whose evidence shows the error **and** the effect (empty
root, fallback text, process death in the log), or, without a stack trace, whose expectations all
held (`effect_observed`); write what the probe saw in `repro.observed_effect`. An effect alone is
enough for a B3/B4 degradation; a B1/B2 crash without a stack trace also needs the probe to show the
app blanked (`root_empty`, a screen fallback for B2, or the app-wide fallback for `app_fallback`).
Paste the stack the probe captured into `repro.stack_trace` (its error line must be in the evidence);
for minified production frames, say in `repro.source_mapping` how each frame maps to the source
location. One probe reproduces one finding. If a probe doesn't reproduce it, keep the finding at
`traced` and record what was tried in `repro.not_reproduced_reason`, or dismiss it if the probe
showed the trace was wrong.

### 6. Telemetry (optional)

If the user provides a crash-reporting export, match its stack traces to findings. A match
confirms the trigger happens in production: set `telemetry` on the finding and use it as the
likelihood basis. Production crashes that match no finding are new leads for step 4.

### 7. Rank crashes

Score = blast radius × reach × likelihood × recoverability (anchors in `RUBRIC.md`):

| Factor | Values |
|---|---|
| Blast radius | B1 = 3, B2 = 2 |
| Reach | critical_path = 3, high_traffic = 2, unknown = 2, rare = 1 |
| Likelihood | normal_data = 4, edge_data = 3, rare_environment = 2, adversarial = 1 |
| Recoverability | crash_loop = 3, restart_or_navigate = 2, retry_fixes = 1 |

Priority: **P0** ≥ 48, **P1** ≥ 24, **P2** ≥ 8, **P3** below 8. A `pattern` finding is capped at P1.
Crashes are listed by score, then confidence (reproduced, traced, pattern); `rank` is the position.
Confidence is shown next to the priority, never folded into it. Degradations are not scored.

### 8. Write repro steps

For each crash:

```text
Environment:   browser/device + OS version, viewport, locale, time zone, storage state
Preconditions: user role, account data shape, feature flags, persisted state
Trigger data:  the exact mock response, stored value or input
Steps:         1. … 2. …
Expected vs actual, the tier, and which boundary caught it (or that nothing did)
Stack trace:   if reproduced
Repro spec:    the script or flow in the output directory
Fix:           guard, validation at the data boundary (zod, kotlinx.serialization, Codable),
               a migration for stored state, or error-boundary placement
Verify:        how to show the fix works (usually: the repro spec exits 0)
```

### 9. Report, validate and render

Write `crash_report.json`, with `run_status` (`complete`, `partial` or `not_run`, and
`run_status_reason` unless complete). A static-only report is `not_run`: it is valid, but nothing in
it may be `reproduced` and the headline must not imply a runtime check. Then:

```bash
python scripts/validate_crash_report.py <out_dir>/crash_report.json --write-summary \
    --repo <repo> --candidates <out_dir>/candidates.json
python scripts/render_report.py <out_dir>/crash_report.json [--brief] [--sarif <out_dir>/crashes.sarif]
```

Fix every validation error; don't weaken the report to pass. The headline states counts and the
worst finding plainly ("5 likely crashes, 4 reproduced; a P0 crash loop for users upgrading from
0.2.0"). Then tell the user:

- the P0 and P1 crashes, each with its trigger and fix in one or two lines;
- how many were reproduced, traced or pattern-only;
- what didn't run or wasn't covered, and what would raise confidence (running the app, a device,
  telemetry);
- where the evidence, repro specs and report are.

## Report fields that the validator checks

- Every finding: file and line (and, with `--repo`, the snippet is on that line and every trace hop
  exists), a concrete trigger with its basis, trigger data for data and stored-state triggers, a
  boundary and effect that match the tier (never an unused boundary), sources, a remediation and a
  verification step. `source: "agent"` findings have a basis whose excerpts are on the cited lines.
- `reproduced`: a probe that reproduced it and lists it, with its evidence file, a screenshot on
  disk, a trace, and a stack trace (with frames, its error line in the probe's evidence, not shared
  with another finding), or the probe's observed effect with `repro.observed_effect` (B1/B2 crashes
  also need the probe to show the app blanked or the app-wide fallback).
- `baseline_clean` probes: no page errors and no console errors of the app's own (errors from the
  probe's third-party blocking are ignored), and any healthy-behaviour assertions held.
  `baseline_not_clean` and `errored` probes have a note.
- Every probe ran against `app_run.target`'s host; a non-loopback target has `authorised_by`.
- `run_status` agrees with the probes, and a `not_run` report claims nothing that needs the app.
- No route handler or `/api/` path is an analysed UI route.
- On a `per_script` site, a B1/B2 crash whose probe didn't show an empty root draws a warning. `traced`: at least two trace hops,
  and evidence type `trace` (for a `source: "agent"` finding too; a judgment without a full trace is `pattern`).
- A probe whose evidence is inconclusive (the page left the app) has the outcome `inconclusive`, never
  `baseline_clean` or `reproduced`; `inconclusive`, `baseline_not_clean` and `errored` probes have a note.
- Crashes are B1/B2 with a ranking whose score and priority are recomputed; a crash loop needs a
  stored-state trigger; ranks follow the score. Degradations are B3/B4 and unranked.
- Every candidate is promoted or dismissed with a specific reason (not "false positive", not one
  sentence pasted onto more than three candidates), or dismissed in a group whose match rule it fits.
- Every route or screen is analysed or listed as not analysed with a reason (`item` is the route id or
  path, optionally followed by a description: `"RT-3 (the about page)"`); coverage counts match.
- A `native` site model with browser probes (or `web` in `metadata.platforms`) draws a warning: recon
  may have read a bundled mobile fixture; correct `site_model` and `blast_radius`.
- Every tool that didn't run says why; at least one tool ran.
- Nothing listed in `files_written` is inside the repository.
- Summary counts are recomputed, and overclaiming headlines are rejected. Numbers the headline states
  ("162 of 163 dismissed", "1 promoted", "2 traced B2 crashes", "4 of them reproduced", "3 degradations")
  are compared with the report.
