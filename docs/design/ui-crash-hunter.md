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

Status: **built** (v1.0.0). The implementation differs from the original plan as follows:

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

```text
ui-crash-hunter-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt
  data/pitfalls.json                    # per-platform packs (regex, trigger, fix, matching tool rules)
  schemas/crash_report.schema.json
  scripts/recon.py  scan_pitfalls.py  strict_overlay.py  contract_diff.py  probe_web.py
          validate_crash_report.py  render_report.py
          sarif.py                      # shared copy
  examples/crash_report.example.json  pantry/  run/  probes/  evidence/  repro/
```
