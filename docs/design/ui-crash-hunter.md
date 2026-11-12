# Design: `ui-crash-hunter-skill`

Status: Built (v1.0.0); see [Files](#files) for how the build differs from this design. Build order: 4.

## Mission

The skill hunts for code paths in UI code (web and mobile) that are likely to crash. It ranks
them by real-world severity and writes reproduction steps for each one. If the app can be run,
the skill tries to confirm each crash by forcing its triggering condition.

## Decisions

- **Both B1 (app-fatal) and B2 (screen-fatal) count as crashes.** B3 and B4 are reported as
  degradations in a separate list, and are never ranked alongside crashes.
- **Web and mobile are both in v1.**

## Non-negotiable rules

- **"Likely crash" is not "crash".** Confidence levels:
  - `reproduced`: forced in a running app, with a stack trace and screenshot.
  - `traced`: a realistic input reaches unguarded code.
  - `pattern`: lint or heuristic only.
- **Every finding cites a file and line, plus a concrete trigger** (data shape, environment or
  user action). "This could be null" with no trigger isn't a finding.
- **The skill never modifies source or config.** Stricter compiler and lint configs live only in
  a scratch directory.
- **"No findings" is not "crash-free".** The report states which routes, screens and components
  were analysed.

## Tiers

| Tier | Class | Meaning |
|---|---|---|
| **B1** | Crash | Fatal to the whole app: native termination, a white screen across the SPA, or a **crash loop** (bad persisted state crashes every launch, so a reload doesn't help) |
| **B2** | Crash | Fatal to one screen or route: an error boundary or fallback replaces the whole screen, or the screen freezes (render loop, hang, ANR on Android) |
| B3 | Degradation | One component is broken and the rest of the screen works |
| B4 | Degradation | A console error or unhandled rejection with no visible effect |

Whether a given throw is B1 or B2 depends on which error boundary (if any) catches it. So the
skill maps every boundary: React `ErrorBoundary`, Next.js `error.tsx`, Vue `errorCaptured`,
Angular `ErrorHandler`, Flutter `ErrorWidget.builder` and `FlutterError.onError`, and native
uncaught-exception handlers.

## Workflow

1. **Recon.** Identify:
   - framework and platform
   - routes and screens
   - the error-boundary map
   - the data layer (fetch/axios/React Query/Apollo/Retrofit/URLSession)
   - API contracts (OpenAPI, GraphQL schema, TypeScript types, generated mobile models)
   - persisted state (localStorage, IndexedDB, AsyncStorage, SharedPreferences, UserDefaults,
     Core Data/Room)
   - supported targets (`browserslist`, `minSdkVersion`, iOS deployment target)
2. **Static analysis**, using strict overlays written to the scratch directory only:

   | Stack | Tools |
   |---|---|
   | TypeScript and JavaScript | A `tsc` run with `strictNullChecks` and `noUncheckedIndexedAccess`; the new errors it finds become candidates. ESLint with `react-hooks/exhaustive-deps`, `@typescript-eslint/no-floating-promises`, `no-non-null-assertion`, `no-unnecessary-condition` |
   | Android (Kotlin/Java) | Android Lint, detekt (`!!`, `lateinit`, `UnsafeCallOnNullableType`) |
   | iOS (Swift) | SwiftLint (`force_unwrapping`, `force_cast`, `force_try`) |
   | Flutter | `flutter analyze` / `dart analyze` (null-check operator `!`, `use_build_context_synchronously`) |
   | All | Semgrep custom rules from the pitfall catalogue |

3. **Contract diff.** Fields that the API schema marks as nullable or optional, but that the UI
   accesses without a guard. This is the biggest source of real crashes in production.
4. **Agent trace.** For each candidate, the agent asks:
   - Can a realistic input reach this code?
   - What guards are in the way?
   - Which boundary catches the error? That decides B1, B2, B3 or B4.
5. **Dynamic confirmation** (if the app can run).
   - **Web:** Playwright with `page.route` injecting edge-case responses.
   - **Mobile:** Detox or Maestro with a mock server, or the platform's network mocking.
   - **Triggers forced:**
     - null fields, `[]`, missing keys, 10k-character strings, emoji and RTL text
     - HTTP 500/401, slow responses, offline
     - locale and timezone changes, storage disabled or quota exceeded, corrupted persisted
       state
     - fast navigation (stale responses arriving after unmount)
     - React StrictMode double invocation
     - rotation and backgrounding on mobile
     - an optional gremlins.js or monkey run
   - **Captured:** `pageerror` events, console errors, logcat and device logs, the stack trace,
     and a screenshot.
6. **Optional telemetry.** A Sentry, Crashlytics or Bugsnag export, if provided, raises
   likelihood scores and confirms findings that already happen in production.
7. **Rank, write repro steps, validate and render.**

## Pitfall catalogue (RUBRIC.md, one pack per framework)

**Web**

- **Data and parsing:**
  - unguarded optional data (`data.items[0].name`)
  - `JSON.parse` without try/catch
  - `Invalid Date` → `RangeError` from `toISOString`
  - `toFixed` or `toLocaleString` on undefined
- **SSR and browser APIs:**
  - `window` or `localStorage` accessed during SSR
  - hydration mismatches
  - APIs not supported by the declared browser targets
- **Async and component lifecycle:**
  - unhandled promise rejections
  - setting state after unmount
  - `setState` during render or effect-dependency loops
  - race conditions between in-flight responses
- **Type escapes and resources:**
  - TypeScript `!` and `as` hiding nulls
  - deep recursion, huge unvirtualised lists
  - regexes with catastrophic backtracking
- **Error handling:** no error boundary around third-party widgets.

**Mobile**

- **Unsafe unwraps:** Swift `!` / `try!` / `as!`, Kotlin `!!` / uninitialised `lateinit`, Dart
  `!`.
- **Lifecycle:**
  - Android: fragment or activity lifecycle misuse (UI work after `onDestroy`, `commit` after
    `onSaveInstanceState`)
  - Flutter: `setState` after `dispose`, and `BuildContext` used across an async gap
- **Threading:**
  - work on the main thread (ANR)
  - UIKit called from a background thread
- **Permissions and platform features:**
  - missing permission-denied handling
  - deep links with missing or invalid parameters
  - process death and state restoration
  - platform API used below the minimum SDK or OS version
- **Memory:** OOM from large bitmaps or images.

## Ranking (crashes only)

**Score** = blast radius × reach × likelihood × recoverability.

| Factor | Scale |
|---|---|
| Blast radius | B1 > B2 |
| Reach | Critical path (auth, checkout, onboarding) > high-traffic route > rarely used route. Uses journey-mapper priorities or analytics if available |
| Likelihood | **Normal data** > **plausible edge data** > **rare environment** > adversarial input |
| Recoverability | Crash loop > needs restart or navigation away > a retry fixes it |

- **Priority tiers.** The score maps to P0–P3.
- **Confidence** is shown separately.

## Repro step format

```text
Environment:   browser/device + OS version, viewport, locale, timezone, storage state
Preconditions: user role, account data shape, feature flags, persisted state
Trigger data:  exact mock response / input (JSON) that causes it
Steps:         1. … 2. …
Expected vs Actual (+ tier, which boundary caught it or didn't)
Stack trace:   if reproduced
Repro spec:    a Playwright/Detox/Maestro script written to the output dir (not the repo)
Fix:           guard / schema validation at the boundary (zod, kotlinx.serialization, Codable) /
               error-boundary placement
```

## Validator fails when

- A finding has no file and line, or no trigger.
- A `pattern`-confidence finding is ranked P0.
- A B3 or B4 finding appears in the crash ranking.
- A finding is marked `reproduced` without a stack trace or screenshot.

## Files

Status: **built** (v1.2.0). The implementation differs from the original plan as follows:

- **More scripts.** Beyond `strict_overlay.py`, `contract_diff.py` and the renderer and validator:
  - `recon.py` finds platforms, targets, routes and screens, the error-boundary map, the data
    layer, contracts and stored state;
  - `scan_pitfalls.py` is a stdlib pattern scan over `data/pitfalls.json` that always runs, and it
    merges SARIF results and the other scripts' candidates into one `candidates.json`;
  - `probe_web.py` runs the web probes with Playwright for Python and emits standalone repro specs.
    It is the only script with a dependency (`requirements.txt`).
- **Every candidate is triaged.** The report's `triage` lists each candidate as promoted (to a
  finding id) or dismissed (with a reason). The validator checks this against `candidates.json`.
- **Probes are first-class.** Each probe has a scenario file and an evidence file. A finding is
  `reproduced` only through a probe whose evidence shows the error and the effect (empty root or a
  fallback text). A baseline probe with contract-valid data must be clean. Errors caught by a
  boundary are counted from the console, because boundaries log rather than throw.
- **Tiers are tied to effects.** `boundary.effect` is one of `app_terminated`, `white_screen` or
  `crash_loop` (B1), `screen_replaced` or `screen_frozen` (B2), `component_replaced` or
  `component_broken` (B3), and `console_only` (B4). Screen and component fallbacks must name the
  boundary that caught the error.
- **The ranking is numeric:**
  - blast radius B1 = 3, B2 = 2;
  - reach critical_path = 3, high_traffic or unknown = 2, rare = 1;
  - likelihood normal_data = 4, edge_data = 3, rare_environment = 2, adversarial = 1;
  - recoverability crash_loop = 3, restart_or_navigate = 2, retry_fixes = 1.

  Priority: P0 ≥ 48, P1 ≥ 24, P2 ≥ 8, otherwise P3. Pattern-only findings are capped at P1, and a
  crash loop needs a stored-state trigger.
- **No Semgrep rules ship with v1.** Semgrep wasn't available to test rules against, so the
  catalogue's regex patterns run through `scan_pitfalls.py`. The skill tells the agent to write
  Semgrep rules to the scratch directory when Semgrep is installed, and to merge the SARIF output.
- **Mobile probes have no helper script.** The agent writes the flow in the repo's own e2e tool
  (Detox, Maestro, Espresso, XCUITest, `integration_test`) to the output directory. SKILL.md lists
  the log and screenshot commands to capture.
- **The example is a real run.** `examples/pantry/` is a small React and TypeScript app. The tool
  outputs, six probes (one clean baseline), screenshots and repro specs come from real runs:
  - a P0 crash loop from a stored-format change;
  - two contract-valid null fields;
  - a malformed-link white screen;
  - one traced-only crash that needs Safari before 17.4;
  - three degradations and six dismissed candidates.
- **Found on a real static site, and fixed (v1.1.0).** A run on a vanilla-JavaScript multi-page site
  (about 2,000 HTML pages sharing a few scripts) showed the skill assumed a single-page app:
  - `recon.py` reported no platform, no routes and no boundaries, and called every uncaught error
    app-fatal. It now sets a site model (`spa`, `ssr`, `multi_page`, `single_page_static`, `native`)
    and a blast-radius model. Static sites are `per_script`: the HTML renders without script, so an
    uncaught error breaks that script's feature (B3) or only logs (B4), and is B1 only where the page
    content is rendered by script. Recon lists the pages (or one entry per set of scripts when there
    are many), the scripts each loads, scripts that inject others, inline scripts, and error pages
    whose relative asset URLs break at nested paths.
  - `reproduced` needed a stack trace and a blank root, so a swallowed fetch failure that left search
    dead, and a `404.html` that loaded unstyled at a nested path, could only be `traced`. Probes now
    take observable-effect assertions (`expect`: visible or absent elements and text, counts,
    requests made or not with a status, computed styles, error text) plus `type`, `unroute` and
    `expect` steps. A probe whose assertions all hold reproduces by its effect; the validator accepts
    that for B3/B4 with `repro.observed_effect`, and for B1/B2 only when the probe also shows the app
    blanked. Baselines are `"kind": "baseline"` and assert healthy behaviour.
  - The probe now blocks third-party requests by default; the validator's clean-baseline check
    ignores console errors caused by that blocking.
  - `strict_overlay.py tsc` was "not applicable" without a tsconfig. Plain JavaScript is now checked
    with `checkJs` through a config generated in the scratch directory, using the repo's or the
    PATH's `tsc`, with a loose baseline (newer compilers default to strict) so only what strictness
    adds is reported. It is `not_applicable` only when no `tsc` is available.
  - The promise rule judged `.then(` by the few lines around it, so it flagged a chain whose `.catch`
    came several lines later and missed a real unhandled call elsewhere. `scan_pitfalls.py` now
    parses each chain whole (strings, comments and regex literals masked, brackets balanced from the
    receiver to the end of the statement) and treats it as handled when any link is `.catch` or a
    two-argument `.then`, or when the chain is returned, awaited, an arrow body or passed to a call.
    `--self-test` covers both cases. The pantry example's candidates are unchanged.
  - Importing a sibling script wrote `__pycache__` into the skill folder; the scripts that import
    `sarif.py` now set `sys.dont_write_bytecode`, and `probe_web.py` says to run it rather than import
    it (or use `python -B`).
- **Found on a real Next.js App Router app, and fixed (v1.2.0).** A static run and a read-only live
  run against a Next.js 16 / React 19 / Prisma app showed:
  - `strict_overlay.py` stripped comments with a regex that read the `/*` in `"@/*"` (every
    create-next-app tsconfig) as a comment, so the config failed to parse and the whole repo was
    compiled; and its baseline compile wrote `tsconfig.tsbuildinfo` into the repo. Configs are now
    read by a string-aware JSONC reader; both compiles go through scratch configs (incremental off,
    build info in the scratch dir) and run side by side; `--scope ui` (default) compiles only what the
    UI entry points import. On that app the tsc step went from 14 min 34 s (two sequential full
    compiles) to 47 s with `--scope all` and 22 s with `--scope ui`. Type roots include `node_modules`
    when the config lists `types` such as `vitest/globals`.
  - The ESLint step read only `package.json`, so plugins loaded through `eslint-config-next` looked
    absent and the tool was wrongly `not_applicable`. It now asks ESLint (`--print-config`) which
    plugins the config loads, and skips the type-aware rules (with a note) when the config has no type
    information.
  - Recon turned an OpenAPI document built in code into 34 UI routes and cookie options
    (`path: '/'`) into three `/` routes, and didn't model layouts. Route objects now count only in
    router configuration; recon records Next.js layouts (the root layout becomes a `shell` item),
    special files, `middleware.ts`/`proxy.ts`, `next/dynamic` imports and route handlers with their
    methods (API code, never UI routes), lists Prisma schemas and code-built OpenAPI documents as
    contracts, and marks boundary definitions nothing renders as `used: false`.
  - 98.7% of 1,146 candidates were dismissed, mostly tests, route handlers and modules no page
    imports. `ui_graph.py` builds the import graph from the UI entry points, resolving `tsconfig`
    `paths` (`@/*`); `scan_pitfalls.py` excludes tests, route handlers and unreachable modules from
    its own scan and from `--extra`/`--sarif` candidates, counting each with examples (1,146 became
    356 on that app, with every exclusion listed). The pitfall rules no longer match
    `new Date().toISOString()`, Intl formatters built from constants, or `toFixed` guarded on the
    same line. `contract_diff.py` reads Prisma schemas, ignores route handlers' private types, and
    skips (and counts) name collisions and Prisma client delegates (about 500 sites on that app).
  - The live half needed a wrapper: the probe had no way to make storage throw, set Firefox
    preferences, keep the app's own POSTs and CSP reports off a remote target, or keep the stack of an
    Error a boundary logs (Firefox's console text is `JSHandle@object`). `probe_web.py` now has
    `storage_blocked`, `init_scripts`, `browser_prefs`, `launch_args`, `bypass_csp`, a read-only mode
    (default for non-loopback targets, which also need `--authorised-by`), console arguments
    evaluated to `{name, message, stack}`, and repro specs that carry all of it.
  - Next.js `global-error` renders its own document, so a B1 there could only be reproduced with a
    stack trace. The new effect `app_fallback` (B1, caught by an app or global boundary) can be
    reproduced by the app-wide fallback text.
  - The validator accepted a trace hop to a file that doesn't exist, 1,131 identical dismissal
    reasons, an API endpoint marked as an analysed UI route, a finding "reproduced" by another
    finding's probe with the stack trace `"x"`, and probes against an unapproved host. It now rejects
    each of these, adds `triage.dismissed_groups` (a class of candidates dismissed under a match rule
    it checks), a required top-level `run_status` (`complete`, `partial`, `not_run`) with the shared
    `not_run` rules, `source: "agent"` findings with a re-read `basis`, probe outcome
    `baseline_not_clean`, and notes on errored probes. The renderer has `--brief`, a probes section and
    the full (trimmed) stack. The example's repro specs were re-emitted from the same scenarios; its
    evidence is unchanged.
- **v1.3.0** (a run on a React 18 + Vite 7 single-page app with a React Router data router, MUI and an
  identity-provider sign-in):
  - The defaults analysed a fraction of the app without saying so. `ui_graph.py` read only
    `tsconfig.json`, so a solution-style config (`"files": []` plus `references` to
    `tsconfig.app.json`, the create-vite layout) lost every `@/` alias: 29 of 275 files were reachable
    and the unresolved count said 0. It now follows `references`, reads a literal Vite
    `resolve.alias`, counts `@/`/`~/` specifiers that resolve nowhere, and warns when imports did not
    resolve or under 30% of UI files are reachable (`scan_pitfalls.py` copies the warnings into
    `scope.warnings`). `strict_overlay.py tsc` extended the solution file, so `--scope all` compiled
    nothing and reported "0 new errors"; it now compiles the referenced app project, is `errored` when
    the compile covered no source file (`--listFiles`), and warns on a baseline of more than 50 errors.
    The ESLint plugin probe asked about `eslint.config.js` (outside a `**/*.{ts,tsx}` flat config) and
    reported no plugins; it now asks about a source file under `src/`. ESLint candidates carry their
    source line as the snippet.
  - The Next.js file heuristics fired on a plain React app (`src/modules/x/pages/...` became 21 routes
    and UI entry points); they now apply only when `next` is a dependency, to `app/` and `pages/` at the
    project root or under `src/`. Recon missed React Router `errorElement` boundaries and said every
    throw was B1; it now finds `errorElement`/`ErrorBoundary` route props, route-module
    `ErrorBoundary` exports, TanStack `errorComponent` and `withErrorBoundary`. It lists modules that
    read `import.meta.env` and throw at module scope (`module_scope_config`), recognises a FastAPI app
    as a code-built OpenAPI document (with the export command) and a FastAPI-only repository as
    `site.kind: "api"` instead of a static site.
  - False positives: `contract_diff.py` matched `products.filter(` against an optional field named
    `filter` (132 of 142 candidates); calls of built-in method names, and calls of non-function fields,
    are now skipped and counted. FastAPI defaults (`list = []`) were "optional"; in a FastAPI document a
    non-null default now counts as present (`--defaults-present` elsewhere), and `X-Input` schemas are
    skipped when `X-Output` exists. Access sites are looked for only in UI-reachable files and never in
    tool configs (`eslint.config.js` matched a contract field), which `scan_pitfalls.py` now excludes
    even with `--all-files`. `--discover` keeps the UI's own types alongside `--contract`.
    `WEB-NUMBER-FORMAT-ON-NULL` no longer fires on receivers that are always numbers (`Math.max(...)`,
    `parseFloat`, arithmetic) or after an `isNaN`/`Number.isFinite` check on the two lines before.
  - A baseline probe whose page ended on `chrome-error://` after a sign-in click was `baseline_ok:
    true`. `probe_web.py` now marks a probe that left the app's origin, landed on the browser's own
    page or lost the app root as `inconclusive` (never clean or reproduced), with the new probe outcome
    `inconclusive`; the repro spec exits 2 in that case. `--storage-state` loads a test-account session
    a person captured by signing in (recorded only as `signed_in_session: true`), and SKILL.md
    describes the options for apps behind MSAL or OIDC.
  - The validator compares the numbers a headline states ("161 of 163 dismissed") with the report, and
    requires evidence type `trace` for `traced` findings (RUBRIC no longer pairs `traced` with
    `agent_judgment`).
  - The example is unchanged: rerunning `scan_pitfalls.py` and `contract_diff.py` on `pantry/` gives the
    same candidates.

```text
ui-crash-hunter-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt
  data/pitfalls.json                    # per-platform packs (regex, trigger, fix, matching tool rules)
  schemas/crash_report.schema.json
  scripts/recon.py  ui_graph.py  scan_pitfalls.py  strict_overlay.py  contract_diff.py  probe_web.py
          validate_crash_report.py  render_report.py
          sarif.py                      # shared copy
  examples/crash_report.example.json  pantry/  run/  probes/  evidence/  repro/
```
