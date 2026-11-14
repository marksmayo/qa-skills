# Design: `resilience-prober-skill`

Status: Built (v1.1.0). Build order: 21.

## Mission

The skill checks how a running web app behaves when the things it depends on go wrong: slow or
failing APIs, rate limits, timeouts, broken or oversized payloads, going offline, and one
dependency failing while the rest work. It drives the app in a real browser, injects each fault
at the network layer, and records what the user would see and lose. Each problem becomes a
finding with a repro and a fix.

How it differs from the existing skills:

- **ui-crash-hunter** asks "what makes the UI crash?" and starts from the code. This skill asks
  "when a dependency fails, does the user still get somewhere sensible?" and starts from the
  running app. A crash is one possible outcome here, but most findings are softer: spinners that
  never end, silent failures, lost form input, retry storms and missing error messages.
- **code-security-auditor** sends attacker payloads to the server. This skill never sends
  anything unusual to the server; every fault is created in the browser.
- **Native mobile apps** belong to `mobile-app-auditor`. This skill covers mobile web through
  Playwright's device profiles.

It reuses the UI crash hunter's crash tiers (B1 app-fatal, B2 screen-fatal) unchanged, so a crash
found here and a crash found there mean the same thing.

## Decisions

- **Faults are injected in the browser only.** Playwright's `page.route` fulfils, delays or
  aborts requests. Nothing reaches a real server in a broken form, so the skill is safe to run
  against a shared test environment.
- **Writes are never forwarded during fault scenarios.** A POST, PUT, PATCH or DELETE that a
  scenario targets is answered locally with the injected fault. Writes the scenario doesn't target
  follow the shared harness's normal rule (blocked unless `allow_form_submission` is set).
- **Browser-only in v1.** Server-side faults (Toxiproxy putting latency or resets between a local
  backend and its database or cache) are planned for a later version, local backends only.
- **The baseline must be clean.** Every flow runs once with no faults first. If the baseline
  already fails, the flow's fault results are reported as `untested`, because a fault can't be
  blamed for a failure that happens anyway.

## Non-negotiable rules

- **Every finding comes from a run.** It names the flow, the step, the fault (endpoint pattern,
  fault type and parameters), the observed effect, a screenshot and the request log. A code
  reading alone is a `suspected` note, never a finding.
- **"Nothing broke" is not "resilient".** The report lists exactly which flows, endpoints and
  fault types were tried. Combinations that weren't tried are listed as untested.
- **A degradation needs a user-visible effect.** A console error alone is a B4-style note and is
  never ranked with findings.
- **Expected behaviour is stated before the run.** For each scenario the agent writes what a good
  outcome looks like (an error message and a retry button within 10 seconds, the typed text kept).
  The finding compares actual with expected, so the bar can't move after the result is known.
- **Retries are measured, not guessed.** A retry storm needs request counts and timings from the
  run log.
- **No real personal data in payloads.** Injected bodies and typed input use synthetic values.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | Base URL of a local or test environment |
| `flows` | required | Named flows as steps (as in the WCAG auditor config), or journey files from journey-mapper |
| `authorised_by` | required for non-loopback | Same rule as the WCAG auditor |
| `faults` | standard set | Subset of the fault catalogue, or extra custom faults |
| `endpoints` | discovered | Glob patterns; by default every API request the baseline makes |
| `devices` | desktop Chromium, one mobile profile | Playwright device names, e.g. `Pixel 7` |
| `budgets` | see Rating | Spinner, error-message and recovery time limits |
| `auth` | optional | Storage state or login recipe through the shared harness |
| `har` | optional | A HAR file of real traffic (production allowed, read offline); weights reach by each endpoint's share of requests |

## Fault catalogue (`data/faults.json`)

| Group | Faults | Parameters |
|---|---|---|
| Latency | slow response, very slow response | 3 s and 15 s added delay |
| Server errors | 500, 502, 503 with `Retry-After`, 504 | Status and a plausible error body |
| Rate limit | 429 with and without `Retry-After` | |
| Auth expiry | 401 mid-flow, 403 | Tests session-expiry handling |
| Timeout | request hangs, then aborts | Abort after 30 s, or never answer |
| Connection | `connectionreset`, `connectionrefused`, `internetdisconnected` | Playwright abort codes |
| Offline | whole context offline mid-flow, then back online | `context.set_offline` |
| Payload | empty body, `{}`, `[]`, wrong `Content-Type`, truncated JSON, HTML error page with 200, 5 MB body | |
| Partial | one named dependency fails while the others work | e.g. recommendations down, checkout API up |
| Flapping | fails twice, then succeeds | Tests that retry actually recovers |
| Static assets | a script chunk, font or image fails to load | Catches lazy-loaded chunk failures |

Each fault is applied on a chosen step of a flow, to one endpoint pattern at a time. The default
plan is every fault group on every critical-path step's first API call, plus the partial and
static-asset faults on each flow once. `--plan-only` prints the scenario count before anything
runs.

## Workflow

1. **Recon.** Record the stack and data layer if source is available (fetch, axios, React Query,
   Apollo, service workers), retry and timeout settings, and the error boundary map from the UI
   crash hunter's `recon.py` if present. Source is optional.
2. **Baseline.** Run each flow with no faults through `browser_harness.py`. Record every request
   (method, URL pattern, status, timing), a screenshot per step and the visible text. This gives
   the endpoint inventory and a "healthy" reference.
3. **Plan.** Build scenarios: flow × step × endpoint × fault, with the expected outcome written for
   each. Prune duplicates (two steps hitting the same endpoint with the same fault).
4. **Run scenarios.** For each one: replay the flow to the step, type synthetic input where the
   flow has a form, inject the fault, wait for the budget, then record the checks below. Reset the
   context between scenarios so state can't leak.
5. **Recovery check.** Remove the fault and use the app's own retry control if it shows one
   (Playwright clicks only controls whose accessible name matches retry, try again or reload).
   Record whether the step completes and whether the typed input survived.
6. **Classify and rank** each failing scenario, merge scenarios with one root cause (the same
   missing error handler behind four faults) into one finding, and write repro specs.
7. **Validate and render.**

## Checks per scenario

| Check | How it's measured | Effect code |
|---|---|---|
| Crash | `pageerror`, empty app root, error-boundary fallback text, as in the UI crash hunter | `crash_b1`, `crash_b2` |
| Blank screen | Visible text under 20 characters and screenshot mostly one colour, with no crash signal | `blank_screen` |
| Endless loading | A progress indicator (`role=progressbar`, `aria-busy=true`, known spinner classes) still visible after the spinner budget, with no error text | `endless_loading` |
| No error message | The request failed but no new text, alert, toast or `role=alert` appeared within the message budget | `silent_failure` |
| Misleading state | A failed fetch shown as an empty result ("No orders yet"), or a failed write shown as success | `misleading_state` |
| Lost input | Text typed before the fault is gone after recovery | `lost_input` |
| No recovery | After the fault is removed, the step can't be completed without a full reload | `no_recovery` |
| Retry storm | More than 10 requests to the failing endpoint in 30 s, or retries without growing gaps, or `Retry-After` ignored | `retry_storm` |
| Chunk failure | A lazy chunk failed and the next navigation shows a blank or broken screen | `chunk_failure` |
| Console only | Errors logged, nothing visible | `console_only` (note, not a finding) |

The misleading-state check compares the scenario's text with the baseline: if the baseline
showed data and the fault shows an empty-state message with no error wording, it's misleading.
The agent confirms each one from the screenshots.

## Rating

**Impact** of the effect:

| Impact | Weight | Effects |
|---|---|---|
| Fatal | 4 | `crash_b1`, `crash_b2`, `blank_screen` |
| Stuck | 3 | `endless_loading`, `lost_input`, `no_recovery`, `chunk_failure` |
| Misleading | 3 | `misleading_state`, `silent_failure` on a write |
| Friction | 2 | `silent_failure` on a read, `retry_storm` |

**Fault likelihood** (how often this happens in real life): latency, 5xx, timeout, offline and
static-asset failure 3; 429, 401 mid-flow and partial failure 2; malformed payloads 1.

**Reach:** critical-path step 3; any other flow step 2; a step only reachable through an
uncommon route 1. With a `har` input, an endpoint carrying 20% or more of requests raises reach
by one (to at most 3), and under 1% lowers it by one (to at least 1); the report says which.

`score = impact x likelihood x reach`. P0 >= 27, P1 >= 18, P2 >= 9, otherwise P3.

Floors and caps:

- `lost_input` or `misleading_state` on a write (the user thinks a payment or booking went
  through) is at least P1.
- A retry storm against a 429 or 503 is at least P2, because it makes an outage worse.
- A finding seen on only one device profile and not reproduced on a second run is capped at P2.

**Confidence:** `reproduced` (seen on two runs of the same scenario), `observed` (once), and
`suspected` (code reading, never ranked). A P0 needs `reproduced`.

## Output (`resilience_report.schema.json`)

| Section | Content |
|---|---|
| `scope` | Targets, authorisation, devices, flows with their steps, fault set, budgets |
| `metadata` | Browser and Playwright versions, harness version, HAR source if used, blocked requests |
| `baseline` | Per flow: passed or failed, endpoint inventory, screenshots |
| `scenarios` | Every scenario: flow, step, endpoint, fault, expected, observed effects, evidence paths, status `ran`, `untested` or `errored` |
| `findings` | Merged: effect, impact, likelihood, reach, score, priority, confidence, scenarios, user impact, repro, fix, verification step |
| `notes` | Console-only and suspected items |
| `coverage` | Fault group x critical step matrix with ran / untested counts |
| `headline` | Counts by priority, flows with a P0, scenarios run of planned, overall confidence |
| `limitations` | Server-side faults not tried, browsers not covered, flows whose baseline failed |

Repro format: environment, flow and step, fault (endpoint pattern, type, parameters), synthetic
input, expected vs actual, request log excerpt, screenshot, and a standalone Playwright script
(written to the output directory) that replays it.

## Validator fails when

- A finding has no scenario, or its scenario has no screenshot or request log.
- A scenario's expected outcome is missing or was written after the run (timestamps in
  `run.json`).
- A flow whose baseline failed has scenarios marked `ran`.
- A P0 isn't `reproduced`, or a `suspected` item is ranked.
- A `retry_storm` has no request counts, or a `lost_input` has no before and after values (stored
  as lengths and hashes, never the text).
- `console_only` appears in findings.
- Coverage counts don't match the scenario list.
- The headline calls the app "resilient" or says it "handles all failures".

## Edge cases

- **Service workers** can answer requests before `page.route` sees them. The harness disables
  service workers by default and runs one extra scenario set with them on, offline only.
- **WebSockets and server-sent events:** v1 can close a socket (`page.route_web_socket`) but not
  fake messages; anything deeper is listed as untested.
- **GraphQL:** one endpoint serves everything, so faults match on operation name from the request
  body rather than the URL.
- **Third-party scripts** (analytics, chat widgets) are a natural partial-failure test and are
  included by default; the report separates first-party and third-party endpoints.
- **Optimistic UI:** a write shown as done before the server answers is expected; it's only
  misleading if the UI doesn't roll back or warn after the injected failure.
- **Flaky baselines:** run the baseline twice; if the two disagree, mark the flow `unstable` and
  skip its fault scenarios.

## Tool stack

| Tool | Licence | Role |
|---|---|---|
| Playwright for Python | Apache-2.0 | Browser, routing, offline, device profiles (through `browser_harness.py`) |

Everything after the browser run is stdlib Python. The skill's `requirements.txt` declares
Playwright only, with the same range as the WCAG auditor.

## Files

```text
resilience-prober-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt
  data/faults.json                 # fault catalogue: group, parameters, likelihood weight
  data/indicators.json             # spinner and empty-state patterns, retry-control names
  schemas/resilience_report.schema.json
  scripts/browser_harness.py       # shared copy
          plan_scenarios.py        # baseline log + config -> scenarios.json (--plan-only)
          run_resilience.py        # Playwright: baseline, scenarios, recovery -> run/
          classify.py              # effects per scenario from evidence -> candidates.json
          validate_resilience.py  render_report.py
  examples/
```

## Example

`examples/parcel-tracker/` is a small single-page app with a stub API served by a stdlib Python
server on loopback. Planted problems, with the answer key in its README:

- the tracking page shows a spinner forever when the status API times out;
- a 500 on the order list shows "You have no parcels yet";
- the address form clears itself when saving fails;
- a failed recommendations widget takes down the whole page (B2);
- the client retries a 503 every 200 ms and ignores `Retry-After`;
- a lazy-loaded chunk failing leaves a blank screen after navigation.

Things that look like problems but aren't, so triage has something to dismiss: a deliberate
optimistic "Saved" that rolls back with a message, and a console warning from a font that falls
back cleanly. `examples/run/` is a real run, with screenshots and request logs.

## Resolved questions

Agreed with the user before the build.

1. **Toxiproxy server-side faults in v1?** Later; v1 is browser-only.
2. **Budgets?** 10 s for an error message to appear, 30 s before a spinner counts as endless.
3. **Production HAR to weight reach by real traffic?** Optional input.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Delays are queued, not slept.** Playwright for Python's synchronous route handlers can't wait, so a
  delayed or hung request is held and released by the runner's own polling loop.
- **Faults on a flow's first step are installed before the page loads**, because that step's requests
  (a notices poll, for example) start with the page. The first version installed them afterwards and
  missed the retry storm.
- **Expected outcomes are templates** from the step (read or write) and the fault group with the
  budgets, written by `plan_scenarios.py` with a `planned_at` time the validator compares with each
  run's start.
- **Recovery** is the app's retry control, or the step's actions again without re-typing input and
  without a reload.
- **Merging:** the same effect at one step and endpoint across faults; no_recovery across a step's
  endpoints; retry storms per endpoint. Not reproduced caps at P2.
- **Baseline console errors** (the decoy font) are reported as notes.
- **Not built:** recon from source, device profiles beyond one desktop, HAR weighting, GraphQL
  operation matching, WebSocket closing, and the service-worker scenario set.
- **Example results:** 11 scenarios, 8 fault types, all run twice after clean baselines; 9 findings
  (6 P0, 3 P1), all reproduced, matching the seven planted defects plus one real one (the status page
  ignores `r.ok`); both decoys produced notes only. The F1 repro script reproduces the lost input.
- **Testing:** 13 of 13 seeded report violations are caught.

### v1.1.0: after a run on a real Next.js staging deployment

A read-only run against a Next.js App Router site on Vercel showed these gaps; v1.1.0 closes them.

- **The runner could not test a remote site**: the site was never registered with the browser harness,
  so its own document was blocked as a third party. The site, `targets` and `allowed_hosts` are now
  harness targets; page loads go through the harness's goto (paced, 429/503 back-off) except where a
  fault must be in place first.
- **Writes were let through by default** (`allow_form_submission` defaulted to true in the runner). It is
  false now, and the report records it. The harness counts every request it lets out; `request_budget`
  caps a run (no new run starts past it, and a run the harness had to cut short is marked failed, not
  evidence). run.json records the harness's metadata and counts.
- **A fault that never fired counted as tested**, and as a silent failure: Next.js had prefetched the
  payload before the fault was installed. Runs record `fault_triggered`; a scenario with none is
  `not_triggered`. The request log keeps the query (sensitive values redacted), marks injected and held
  requests, and records response times.
- **Next.js**: faults match request headers (`RSC: 1`, `Next-Router-Prefetch`) and resource type,
  `"from": "load"` installs a fault before the page loads, `"prefetch": "block"` makes navigations fetch,
  and `unreachable` lists server-side data browser faults can't reach.
- **Indicators were fixed**: the app's error boundary wording, skeletons and empty states couldn't be
  named, so a boundary render came out as a silent failure. `indicators` in flows.json (global or per
  flow) is merged over the defaults and recorded in run.json; `boundary_selectors` and `loading_words` are
  new.
- **The watch ended 2-3 s after the step's selector showed**, so a widget that never stopped loading on a
  server-rendered page couldn't be endless loading. Failing faults are now watched for the whole window.
- **False positives**: a retry storm counted every URL a wildcard matched (eleven chunks once each); a
  static-asset fault whose screen kept its content was a silent failure; a read whose step completed with
  all its controls was a silent failure. Storms count per URL; static assets and completed reads are
  excluded (a read that lost controls the baseline showed still counts).
- **Lost input was measured after the click**, so a quick reset could look like an empty field before
  the fault. It is measured after the last fill.
- **Failed baselines had no evidence and the report's baseline section was free text**; a tampered
  "passed" validated. The failing step is screenshotted and observed, `classify.py` derives the baseline
  section, the validator compares it and requires the headline to name broken flows.
- **No report builder**: `build_report.py` writes the skeleton (with `TODO` placeholders the validator
  rejects) and a repro script per finding with a third-party and write guard. The validator also checks
  `scope.faults` against what ran, rejects absolute claims ("production ready", "recovers gracefully from
  every failure"), and allows `low` confidence.
- **Not done**: client-side versus full navigation is still not recorded, and visible controls are
  compared with the baseline only for the silent-failure rule.

### v1.2.0: after a run on a server-rendered Flask and Jinja site

- **The report builder crashed on emoji**: control names were cut with `String.slice` (UTF-16 units),
  splitting a flag emoji into a lone surrogate that no UTF-8 writer accepts. Names are cut by code point
  (`Array.from`), and `build_report.py` and the validator replace any lone surrogate with U+FFFD.
- **A fault on the page's own document was rated a P0 blank screen**: the 15 characters measured were the
  fault catalogue's injected 502 body, not the app. Requests record `resource_type` and `navigation`; a
  run whose fault answered the document is a `document_fault` note.
- **Type-ahead queries were a retry storm**: requests were grouped by path with the query dropped, so
  different search terms (and the page's own aborted, superseded query) counted as retries ignoring
  Retry-After. Storms now group by path and query with cache-busting parameters dropped, and skip requests
  the page aborted itself.
- **A type-ahead step never recovered**: recovery removed every `fill`, so a step that only types did
  nothing. Such a step re-sends the field's value (`refill_input`); a run from the older runner is a
  `recovery_not_tried` note, not no_recovery.
- **Testing:** `scripts/self_test.py` covers these with synthetic runs (no browser).
- **Not done** (from the same run): offline during a form-submit navigation still errors, `flapping` is
  not a failing group, a widget lost to a missing static asset raises nothing, redirect targets can't be
  faulted, a failed baseline can't be promoted, and recovery of a plain-navigation step is a reload.
