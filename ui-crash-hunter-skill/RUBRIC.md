# UI Crash Hunter rubric

Anchors for tiers, boundaries, confidence, ranking and triage. `SKILL.md` says when to apply them;
`data/pitfalls.json` is the machine-readable pitfall catalogue.

## Tiers

| Tier | Class | Effect values | Web example | Mobile example |
|---|---|---|---|---|
| B1 | Crash | `app_terminated`, `white_screen`, `crash_loop` | Render error above every boundary: React unmounts the root and the page is blank | Uncaught exception or force unwrap: the process dies |
| B1 | Crash (loop) | `crash_loop` | Bad value in localStorage read during the first render: blank on every load | Corrupt SharedPreferences/UserDefaults read at launch: crashes on every start |
| B2 | Crash | `screen_replaced`, `screen_frozen` | Route boundary or Next.js `error.tsx` replaces the page; render loop freezes the tab | Flutter error widget fills the screen; ANR on a screen's main thread |
| B3 | Degradation | `component_replaced`, `component_broken` | A widget's own boundary shows a fallback; a list never leaves "Loading…" | One card shows an error state |
| B4 | Degradation | `console_only` | Unhandled rejection or event-handler throw with no visible effect | Logged exception, UI unchanged |

The validator checks that the effect matches the tier. When unsure between two tiers, pick the one
your evidence shows; a probe settles it.

## Error-boundary map

What catches a throw decides the tier. Record each boundary's scope and what it covers, and look
hardest at what is outside all of them.

| Platform | Boundaries | Not caught by them |
|---|---|---|
| React | Class components with `getDerivedStateFromError`/`componentDidCatch`, `react-error-boundary`, `Sentry.ErrorBoundary` | Event handlers, async code, `setTimeout`, errors in the boundary itself, anything above the topmost boundary (since React 16, an uncaught render error unmounts the whole root) |
| Next.js | `app/**/error.tsx` (route segment), `app/global-error.tsx` (app), `pages/_error` | Errors in the root layout are caught only by `global-error`; server component errors during SSR become error pages |
| Vue | `errorCaptured` / `onErrorCaptured` (subtree), `app.config.errorHandler` (global, logs only) | A global handler doesn't re-render; the component stays broken |
| Angular | `ErrorHandler` (global; logs, does not recover the view) | A throw in a template can leave the view half-rendered |
| React Native | Error boundaries as in React; `ErrorUtils.setGlobalHandler` | Native module exceptions terminate the app regardless |
| Flutter | `ErrorWidget.builder` (per widget: grey screen in release), `FlutterError.onError`, `PlatformDispatcher.instance.onError`, `runZonedGuarded` | A build error in a top-level widget replaces the whole screen (B2); errors in `main()` before `runApp` are app-fatal |
| Android | `Thread.setDefaultUncaughtExceptionHandler`, `CoroutineExceptionHandler` | Handlers report; the process still dies (B1) unless the exception is caught in code |
| iOS | `NSSetUncaughtExceptionHandler` | Swift traps (force unwrap, `try!`, index out of range) are not exceptions and always terminate (B1) |

A `screen_replaced` or `component_replaced` effect must name the boundary in `caught_by`. A
`white_screen` or `crash_loop` can only be "caught" by an app-scoped boundary that blanks the app
anyway; otherwise `caught_by` is `null`.

## Confidence

| Confidence | Evidence type | Needs |
|---|---|---|
| `reproduced` | `reproduced` | A probe whose evidence shows the error and the effect, a stack trace, a screenshot, and the trace. The probe lists the finding |
| `traced` | `trace` or `agent_judgment` | At least two `file:line` hops from the input to the throw, and the boundary that catches it |
| `pattern` | `tool` or `agent_judgment` | A rule match you didn't trace further. Capped at P1 |

Never mark something `reproduced` because the logic is obvious. If the app can't run, the best
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

"False positive" on its own is not a reason.

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
- Async and lifecycle: unhandled rejections, responses applied after unmount or out of order,
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

- [ ] Recon includes the app shell and every boundary, with scopes.
- [ ] Every tool is ran, not_run (with why), errored (with why) or not_applicable.
- [ ] Every candidate is promoted or dismissed with a checkable reason.
- [ ] Every finding has a file:line, a concrete trigger with basis, and the boundary that catches it.
- [ ] Tiers match effects; B3/B4 are only in degradations.
- [ ] `reproduced` findings have a probe, stack trace and screenshot; a baseline probe was clean.
- [ ] Scores and priorities are recomputed by the validator; pattern findings aren't P0.
- [ ] Repro specs, evidence and the report are outside the repository; the source is unchanged.
- [ ] Coverage and limitations say what wasn't analysed; the headline doesn't claim the app is safe.
