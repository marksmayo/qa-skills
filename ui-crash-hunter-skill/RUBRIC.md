# UI Crash Hunter rubric

Anchors for tiers, boundaries, confidence, ranking and triage. `SKILL.md` says when to apply them;
`data/pitfalls.json` is the machine-readable pitfall catalogue.

## Tiers

| Tier | Class | Effect values | Web example | Mobile example |
|---|---|---|---|---|
| B1 | Crash | `app_terminated`, `white_screen`, `app_fallback`, `crash_loop` | Render error above every boundary: React unmounts the root and the page is blank; or an app-scoped boundary (Next.js `global-error`) replaces every page with its own fallback (`app_fallback`) | Uncaught exception or force unwrap: the process dies |
| B1 | Crash (loop) | `crash_loop` | Bad value in localStorage read during the first render: blank on every load | Corrupt SharedPreferences/UserDefaults read at launch: crashes on every start |
| B2 | Crash | `screen_replaced`, `screen_frozen` | Route boundary or Next.js `error.tsx` replaces the page; render loop freezes the tab | Flutter error widget fills the screen; ANR on a screen's main thread |
| B3 | Degradation | `component_replaced`, `component_broken` | A widget's own boundary shows a fallback; a list never leaves "Loading…"; on a static page, a script's feature (search, menu) is dead or the page loads unstyled | One card shows an error state |
| B4 | Degradation | `console_only` | Unhandled rejection or event-handler throw with no visible effect | Logged exception, UI unchanged |

The validator checks that the effect matches the tier. When unsure between two tiers, pick the one
your evidence shows; a probe settles it.

## Error-boundary map

What catches a throw decides the tier. Record each boundary's scope and what it covers, and look
hardest at what is outside all of them.

| Platform | Boundaries | Not caught by them |
|---|---|---|
| React | Class components with `getDerivedStateFromError`/`componentDidCatch`, `react-error-boundary` (`<ErrorBoundary>`, `withErrorBoundary`), `Sentry.ErrorBoundary`; router boundaries: a route's `errorElement` or `ErrorBoundary` (React Router 6.4+ data routers and 7, Remix; also a route module's exported `ErrorBoundary`) and TanStack Router's `errorComponent`, which catch render, loader and action errors and failed `lazy()` imports in their route and its children (scope `route`: a throw there is B2) | Event handlers, async code, `setTimeout`, errors in the boundary itself, anything above the topmost boundary (since React 16, an uncaught render error unmounts the whole root), and anything above the router: providers, the root render and module-scope code |
| Next.js | `app/**/error.tsx` (its segment's page and nested segments, not the `layout` in its own folder), `app/global-error.tsx` (app: replaces the whole document, effect `app_fallback`), `pages/_error` | The root layout and everything it renders (providers, the shell, `next/dynamic` widgets) is caught only by `global-error`, so a throw there is B1 on every page. Server component errors during SSR become error pages; their trigger is server data (seed a test database), not a browser mock. `middleware.ts`/`proxy.ts` runs before every matched page. Route handlers (`app/**/route.ts`) are API code: a throw is an HTTP 500, not a UI crash, so they are out of scope |
| Vue | `errorCaptured` / `onErrorCaptured` (subtree), `app.config.errorHandler` (global, logs only) | A global handler doesn't re-render; the component stays broken |
| Angular | `ErrorHandler` (global; logs, does not recover the view) | A throw in a template can leave the view half-rendered |
| React Native | Error boundaries as in React; `ErrorUtils.setGlobalHandler` | Native module exceptions terminate the app regardless |
| Flutter | `ErrorWidget.builder` (per widget: grey screen in release), `FlutterError.onError`, `PlatformDispatcher.instance.onError`, `runZonedGuarded` | A build error in a top-level widget replaces the whole screen (B2); errors in `main()` before `runApp` are app-fatal |
| Static multi-page site (also pages a Node server renders from HTML template literals) | None needed: each page's HTML renders without script, and each `<script>` runs on its own (`blast_radius: per_script`); `window.onerror` / `unhandledrejection` handlers only log | An uncaught error stops the rest of that script and its handler, not the page (B3/B4). B1 only where the page content is rendered by script (an empty `<div id="app">`), or the script hides or removes it. A `404.html` with relative asset URLs is served at any missing path and loses its CSS and scripts at nested paths |
| Android | `Thread.setDefaultUncaughtExceptionHandler`, `CoroutineExceptionHandler` | Handlers report; the process still dies (B1) unless the exception is caught in code |
| iOS | `NSSetUncaughtExceptionHandler` | Swift traps (force unwrap, `try!`, index out of range) are not exceptions and always terminate (B1) |

A `screen_replaced`, `component_replaced` or `app_fallback` effect must name the boundary in
`caught_by` (for `app_fallback`, an `app`- or `global`-scoped one). A `white_screen` or `crash_loop`
can only be "caught" by an app-scoped boundary that blanks the app anyway; otherwise `caught_by` is
`null`. A boundary component that is defined but never rendered (recon `used: false`) catches
nothing and can't be `caught_by`.

## Confidence

| Confidence | Evidence type | Needs |
|---|---|---|
| `reproduced` | `reproduced` | A probe that lists the finding, with its evidence file, a screenshot and the trace, and either a stack trace with the error's effect, or the effect alone (`effect_observed`: every `expect` assertion held) with `repro.observed_effect`. An effect alone suffices for B3/B4; a B1/B2 crash without a stack trace also needs `root_empty` (a screen fallback for B2, or the app-wide fallback text for `app_fallback`). A stack trace is the one the probe captured: its error line appears in the evidence and it has frames. One probe's stack or effect reproduces one finding; minified frames are mapped to source in `repro.source_mapping` |
| `traced` | `trace` | At least two `file:line` hops from the input to the throw, and the boundary that catches it. The same for a `source: "agent"` finding: if you traced it, the evidence type is `trace` |
| `pattern` | `tool` or `agent_judgment` | A rule match (`tool`) or your reading of the code (`agent_judgment`) that you didn't trace further. Capped at P1 |

Effect assertions must pin the defect, not something that could have another cause: "search shows
nothing **and** the index is not requested again after the server recovers", not just "search shows
nothing". Never mark something `reproduced` because the logic is obvious. If the app can't run, the best
possible confidence is `traced`.

## Ranking anchors (crashes only)

**Reach**

| Value | Score | Anchor |
|---|---|---|
| `critical_path` | 3 | Entry screen, sign-in, checkout or payment, onboarding, the app shell, or a P1 journey |
| `high_traffic` | 2 | A main screen most users visit, or a P2 journey |
| `unknown` | 2 | No journey map, analytics or clear reading; the validator warns |
| `rare` | 1 | Settings, admin, edge flows, or a P3 journey |

**Likelihood**

| Value | Score | Anchor |
|---|---|---|
| `normal_data` | 4 | Contract-valid data most users have or will have: a new item with no ratings, stored state every upgraded user has, telemetry showing it happens |
| `edge_data` | 3 | Contract-valid but less common: an empty list, an imported item with no author, a long name, emoji |
| `rare_environment` | 2 | A supported but uncommon environment: an older OS or browser within the declared targets, storage blocked, low memory, a slow or flaky network |
| `adversarial` | 1 | Malformed links or input, tampered storage, contract violations by the server |

Telemetry showing the crash in production justifies `normal_data` whatever the data shape.

**Recoverability**

| Value | Score | Anchor |
|---|---|---|
| `crash_loop` | 3 | The bad input is stored, so reloading or relaunching crashes again. Needs a `persisted_state` trigger |
| `restart_or_navigate` | 2 | The user must reload, relaunch or navigate away, and may hit it again |
| `retry_fixes` | 1 | A retry (pull to refresh, the boundary's "try again") clears it |

**Priority:** P0 ≥ 48, P1 ≥ 24, P2 ≥ 8, P3 below 8. Examples: a B1 crash loop on the shell with
normal data scores 3 × 3 × 4 × 3 = 108 (P0); a B2 on the landing screen with edge data scores
2 × 3 × 3 × 2 = 36 (P1); a B1 from a malformed link scores 3 × 2 × 1 × 2 = 12 (P2).

## Triggers

A trigger is concrete when someone else could set it up from the report alone:

| Kind | Must say | Example |
|---|---|---|
| `data` | The response or input, exactly (`trigger.data`) | `GET /api/recipes` item with `"author": null` |
| `persisted_state` | The stored key and value (`trigger.data`) and how it got there | `localStorage['pantry.prefs'] = 'dark'`, written by 0.2.0 |
| `environment` | Browser/OS/device and version range, setting or resource limit | Safari < 17.4, which lacks `Object.groupBy`, within `iOS >= 16` |
| `user_action` | The action sequence | Double-tap "Pay" before the first request returns |
| `lifecycle` | The lifecycle event | Leave the screen while the save request is in flight |
| `navigation` | The URL, deep link or route params | `/#/recipes/%E0%A4%A` |

`trigger.basis` says why it is realistic: the contract line, a changelog entry, the declared targets,
telemetry, or the code path that produces it. "Could be null" is rejected.

## Triage

**Promote** when a realistic trigger reaches the line without a working guard. Merge candidates for
the same root cause into one finding (list each candidate id), keeping the throwing line first in
`locations`.

**Dismiss** with a reason someone can check:

- the guard and its line (`Stars.tsx:2 returns early when rating === null`);
- an invariant (`routes is a non-empty constant`, `#root is in the static HTML`);
- the trigger can't occur (`the field is required in the contract and validated by zod at api.ts:14`);
- a duplicate of another candidate that is promoted.

"False positive" on its own is not a reason, and neither is one sentence pasted onto many
candidates: the validator rejects a reason shared by more than three individual dismissals.

**Dismiss in a group** when one checkable rule covers a class of candidates, in
`triage.dismissed_groups`: a `rule_id`, a `match` (any of `pitfall_id`, `path` glob, `tool`, source
`rule`, `snippet` regex), the specific reason that holds for every match, and the candidate ids. With
`--candidates` the validator checks each listed candidate matches. Good groups: tsc
`noUncheckedIndexedAccess` hits on `x.split(...)[0]` (`rule: TS2532`, `snippet: "split\\("`; `split`
always returns at least one element), or `WEB-INVALID-DATE` on Prisma `DateTime` fields (`snippet`
naming them; the client returns `Date` objects). A group never covers a candidate you didn't read the
rule for.

**Scope.** UI crash hunting covers code the UI runs. `scan_pitfalls.py` excludes tests, route
handlers and modules no UI entry point imports by default and counts them in `scope.excluded`; read
the examples it lists, and rerun with `--all-files` if something is reached in a way the import graph
can't follow. Route handlers belong to the API contract tester; module-scope code and server
components the UI renders stay in scope.

## Agent-found findings

A finding no candidate pointed at sets `"source": "agent"` and a `basis`: file, line and an excerpt of
that line (or `{"evidence": "<path>"}` for runtime evidence). The validator re-reads each excerpt with
`--repo`; renderers label these "found by review".

## The dynamic half

`run_status` is `complete` (the planned probes ran), `partial` (some couldn't, or the target was
degraded) or `not_run` (static only), with `run_status_reason` unless complete. A `not_run` report has
no reproduced findings, no probe outcomes that need a running app, and no headline implying a runtime
check. Probe outcome `baseline_not_clean` records a baseline that showed the app's own errors (a
degraded target), `errored` a probe that couldn't run, and `inconclusive` a probe whose page ended off
the app (the browser's error page, a sign-in redirect to an identity provider, no app root), so none of
its checks ran on the app; all three need a note. An app behind sign-in is probed with a captured
test-account session (`--storage-state`) or its own test mode, never a real account; otherwise the
signed-in routes are not probed and `run_status` is `partial`.

**Targets.** Every probe's evidence must have run against `app_run.target`'s host. A target that is
not loopback needs `app_run.authorised_by`, and runs read-only (`probe_web.py` answers every
non-GET request in the browser and never sends it).

## Pitfall catalogue

Each pack lists what `scan_pitfalls.py` matches (pattern only) and what needs tools, the contract
diff or reading. Full entries, triggers and fixes are in `data/pitfalls.json`.

**Web**

- Data and parsing: unguarded optional data (`data.items[0].name`), `JSON.parse` without try/catch,
  `Invalid Date` reaching `toISOString`, `toFixed` on null, Intl formatters fed invalid codes,
  `decodeURIComponent`/`new RegExp(userText)`/`new URL(x)` without try/catch, `reduce` without an
  initial value.
- SSR and browser APIs: `window`/`localStorage` at module scope in SSR code, hydration mismatches,
  APIs missing in the declared targets.
- Configuration at module scope: a Next.js root layout reading configuration, or a Vite module that
  reads `import.meta.env` and throws when a value is missing or invalid (recon's
  `module_scope_config`). It runs on import, before any boundary renders, so it blanks the app (B1);
  the trigger is a build or deployment with the variable unset.
- Async and lifecycle: unhandled rejections (the scan reads each promise chain whole, across lines, and counts a `.catch` or two-argument `.then` anywhere in it, or a returned, awaited or passed-on chain, as handled), responses applied after unmount or out of order,
  `setState` during render, effect-dependency loops.
- Type escapes and resources: TypeScript `!` and `as any`, deep recursion, huge unvirtualised lists,
  catastrophic regex backtracking.
- Error handling: no boundary around the shell, routes or third-party widgets.

**Android:** `!!`, unsafe `as`, `lateinit` read before set, `toInt()`/`parseInt` on external input,
`runBlocking`/blocking calls on the main thread (ANR), fragment commits after
`onSaveInstanceState`, full-size bitmap decodes (OOM), missing permission-denied handling, process
death, APIs above `minSdkVersion`.

**iOS:** force unwrap, `try!`, `as!`, implicitly unwrapped optionals, `fatalError` in reachable
branches (e.g. an unknown enum value from the server), unchecked `[0]`, UIKit off the main thread,
APIs above the deployment target.

**Flutter:** the `!` null-check operator, `BuildContext`/`setState` after an `await` without a
`mounted` check, `int.parse`/`DateTime.parse`/`jsonDecode` without handling, `first`/`last`/`single`
on possibly empty lists, casts of decoded JSON to non-nullable types.

## Reviewer checklist

- [ ] Recon includes the app shell (for Next.js, the root layout) and every boundary, with scopes;
      unused boundaries aren't credited; API route handlers aren't counted as UI routes.
- [ ] Every tool is ran, not_run (with why), errored (with why) or not_applicable.
- [ ] Every candidate is promoted or dismissed with a checkable reason, individually or in a group
      with a match rule.
- [ ] `run_status` says whether the dynamic half ran; probes ran against the approved target.
- [ ] Every finding has a file:line, a concrete trigger with basis, and the boundary that catches it.
- [ ] Tiers match effects; B3/B4 are only in degradations.
- [ ] `reproduced` findings have a probe, a screenshot and a stack trace or an observed effect; a
      baseline probe was clean (errors from third-party blocking don't count).
- [ ] On a static multi-page site, tiers follow the per-script blast radius.
- [ ] Scores and priorities are recomputed by the validator; pattern findings aren't P0.
- [ ] Repro specs, evidence and the report are outside the repository; the source is unchanged.
- [ ] Coverage and limitations say what wasn't analysed; the headline doesn't claim the app is safe.
