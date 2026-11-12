---
name: ui-crash-hunter
description: Hunt for code paths in web and mobile UI code that are likely to crash the app (B1, app-fatal) or a whole screen (B2, screen-fatal), rank them by real-world severity, and write reproduction steps for each. Uses static analysis, an API-contract diff and the error-boundary map, then confirms crashes in the running app where possible. Every finding has a file and line, a concrete trigger and a confidence level (reproduced, traced or pattern); degradations are listed separately and never ranked as crashes.
version: 1.1.0
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
  Never point probes at production or at real user accounts.
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
- **Platforms and frameworks**, and the supported targets (`browserslist`, `minSdkVersion`, iOS
  deployment target). Targets decide whether an API is "missing in a supported browser or OS".
- **Routes and screens.** Add the app shell (header, navigation, layout) as an item: it renders on
  every screen and is often outside every boundary.
- **The error-boundary map.** For each boundary, record its `scope` (`app`, `route`/`screen`,
  `component`, `global`) and what it covers. Check what is *outside* every boundary: the root
  render, the shell, providers, and code that runs before the first render.
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
| TypeScript / JavaScript | `strict_overlay.py tsc` (strictNullChecks, noUncheckedIndexedAccess; plain JavaScript without a tsconfig is checked with `checkJs` through a config generated in the scratch directory, using the repo's or the PATH's `tsc`, and is `not_applicable` only when there is no `tsc`) and `eslint` (react-hooks/exhaustive-deps, no-floating-promises, no-non-null-assertion, no-unnecessary-condition) using the repo's own installs |
| Android | Android Lint (`./gradlew lint`, SARIF via `lint { sarifReport = true }`), detekt (`--report sarif:...`) |
| iOS | SwiftLint (`swiftlint lint --reporter sarif`) with force_unwrapping, force_cast, force_try enabled in a scratch config |
| Flutter | `dart analyze` / `flutter analyze` (null-check and `use_build_context_synchronously` diagnostics) |
| All | Semgrep with rules written to the scratch directory, `--sarif` output |
| All | `scan_pitfalls.py`: the stdlib pattern scan over `data/pitfalls.json`, which always runs |

The **contract diff** (`contract_diff.py`) is the biggest single source of real crashes: fields the
contract marks nullable or optional, dereferenced in the UI without a guard. The strict `tsc`
overlay finds the same class of bug through the types, including accesses after destructuring that
name matching misses.

### 3. Triage every candidate

`candidates.json` holds leads, not findings. For each candidate, decide:

- **Promote** it to a finding (or merge it into one) when a realistic input reaches it unguarded.
- **Dismiss** it with a specific reason: the guard and its line, the invariant that makes it
  unreachable, or why the trigger can't happen.

Every candidate ends up in `triage.promoted` or `triage.dismissed`; the validator checks the count
against `candidates.json`. Then read the code for crash classes no tool finds (unsupported APIs in
the declared targets, render loops, stale responses, lifecycle misuse, deep links, permission
denial, process death): see the catalogue in `RUBRIC.md`. Those findings have `sources: [{"tool":
"agent"}]` and no candidate ids.

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
server), `expect`, `wait_for`, `wait_ms`). The evidence file records uncaught page errors with
stacks, console errors (a boundary that catches an error usually logs it), every request with its
status, failed requests, whether the root is empty (white screen) and which fallback texts are
visible, plus a screenshot. `--emit-spec` writes a standalone script that exits 1 while the problem
reproduces and 0 once it is fixed. Run `probe_web.py` rather than importing it; if a wrapper must
import it, run that with `python -B` so nothing is written into the skill folder.

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
held. If the baseline already errors or fails, fix the scenario before blaming the code.

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
app blanked (`root_empty`, or a screen fallback for B2). If a probe doesn't reproduce it, keep the finding at
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

Write `crash_report.json`, then:

```bash
python scripts/validate_crash_report.py <out_dir>/crash_report.json --write-summary \
    --repo <repo> --candidates <out_dir>/candidates.json
python scripts/render_report.py <out_dir>/crash_report.json [--sarif <out_dir>/crashes.sarif]
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

- Every finding: file and line (and, with `--repo`, the snippet is on that line), a concrete
  trigger with its basis, trigger data for data and stored-state triggers, a boundary and effect
  that match the tier, sources, a remediation and a verification step.
- `reproduced`: a probe that reproduced it and lists it, a screenshot on disk, a trace, and a stack
  trace, or the probe's observed effect with `repro.observed_effect` (B1/B2 crashes also need the
  probe to show the app blanked).
- `baseline_clean` probes: no page errors and no console errors of the app's own (errors from the
  probe's third-party blocking are ignored), and any healthy-behaviour assertions held.
- On a `per_script` site, a B1/B2 crash whose probe didn't show an empty root draws a warning. `traced`: at least two trace hops.
- Crashes are B1/B2 with a ranking whose score and priority are recomputed; a crash loop needs a
  stored-state trigger; ranks follow the score. Degradations are B3/B4 and unranked.
- Every candidate is promoted or dismissed with a reason.
- Every route or screen is analysed or listed as not analysed with a reason; coverage counts match.
- Every tool that didn't run says why; at least one tool ran.
- Nothing listed in `files_written` is inside the repository.
- Summary counts are recomputed, and overclaiming headlines are rejected.
