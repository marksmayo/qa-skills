---
name: resilience-prober
description: Check how a running web app behaves when what it depends on goes wrong - slow or failing APIs, rate limits, expired sessions, hung requests, dropped connections, going offline, malformed payloads, one dependency down, flapping endpoints and failed script chunks. Each fault is injected in the browser only (Playwright routing), on a chosen step of a real user flow, after a clean baseline, with the expected outcome written before the run. Records what the user sees and loses - crashes, blank screens, endless spinners, silent failures, misleading empty states, lost input, no way to recover, retry storms - with screenshots, request logs and a standalone repro script. Never calls an app resilient.
version: 1.2.0
---

# Resilience prober

## Mission

Find out whether a user still gets somewhere sensible when a dependency fails. Drive real flows,
break one thing at a time at the network layer, and record the user-visible effect against an
outcome written down beforehand. Merge scenarios with one root cause, rate them, and hand over a
repro and a fix for each.

The UI crash hunter starts from the code and asks what makes the UI crash; this skill starts from
the running app. It reuses the crash hunter's tiers (B1 app-fatal, B2 screen-fatal). The code security
auditor sends attacker payloads to the server; this skill never sends anything unusual to the server.
Native apps belong to the mobile app auditor.

## Non-negotiable rules

1. **Faults are injected in the browser only.** Playwright fulfils, delays or aborts the request, or
   takes the context offline. A write a scenario targets is answered in the browser, never forwarded.
2. **The baseline must be clean.** Each flow runs twice without faults first; a failed or unstable
   baseline makes that flow's scenarios `untested`.
3. **Expected behaviour is written before the run** (`plan_scenarios.py`), with the budgets: an error
   message within 10 s, no spinner past 30 s, input kept, recovery without a reload.
4. **Every finding comes from a run**: the flow, step, fault, effect, a screenshot and the request log.
   Reading code gives a `suspected` note, never a finding.
5. **A degradation needs a user-visible effect.** A console error alone is a note.
6. **Retries are measured**: request counts and gaps from the run log.
7. **"Nothing broke" is not "resilient".** The report lists the flows, endpoints and faults that ran,
   separately the planned faults that didn't, and what the browser can't reach (`unreachable`). Never
   "production ready" or "recovers gracefully from every failure".
8. **Synthetic data only** in typed input and injected bodies; input is recorded as length and SHA-256.
9. **Safe targets.** Loopback needs nothing; other hosts need `authorised_by`. The site's host is
   registered with the browser harness and every other host is blocked. Writes are blocked unless
   `allow_form_submission` is true (default false; the report records which). Set a `request_budget`
   for anything shared: one page load of a modern app is 50-80 requests, and the default plan is
   2 baselines per flow plus `--runs` per scenario.
10. **A fault that never fired tested nothing.** A scenario whose fault matched no request is
    `not_triggered`, has no effects and is not counted as run.
11. **A broken journey without faults is the first result.** A failed baseline step is screenshotted
    and observed, and the headline names the flow.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `flows` | required | `flows.json`: site, app root, flows with named steps (actions, a `done` selector, `input`, `write`), `critical_path` |
| `scenarios` | required | Explicit `{flow, step, endpoint, fault, method?, headers?, resource_type?, from?}` or `{"auto": {"endpoints": {...}, "faults": [...]}}` |
| `faults` | `data/faults.json` standard set | Latency, server errors, rate limits, auth expiry, timeouts, connection, offline, payload, partial, flapping, static assets |
| `budgets` | 10 s message, 30 s spinner | `data/indicators.json` |
| `indicators` | `data/indicators.json` | The product's own wording and selectors, in flows.json (global or per flow): `spinner_selectors` (e.g. `.animate-pulse` skeletons), `loading_words` ("loading availability"), `empty_state_words` ("no results"), `boundary_fallback_words` and `boundary_selectors` (the app's error boundary, e.g. "this route hit turbulence"), `error_selectors`, `error_words`. Lists are added to the defaults; run.json records the merged set |
| `authorised_by`, `auth`, `allow_form_submission` | writes blocked | `allow_form_submission` defaults to false here; `targets`, `allowed_hosts`, `host_map` and a `harness` block pass through to `browser_harness.py`; `auth: {"storage_state": file}` runs signed in, with a session a person captured by signing in by hand (`python scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>`; cookies, localStorage, IndexedDB and sessionStorage); the run stays read-only and records only that a session was used, never the file or its contents |
| `request_budget` / `--max-requests` | none | Requests the browser may send to the site in the whole run |
| `setup`, `init_script` | none | Actions after the first load (dismiss a cookie banner), JavaScript before every page (seed localStorage) |
| `prefetch` | allow | `block` aborts prefetch requests (`Next-Router-Prefetch`, `Sec-Purpose: prefetch`), so a navigation fetches and a fault can reach it |
| `unreachable` | none | `[{flow?, step?, what, reason}]`: data the browser cannot reach (fetched by a server component, cached at the edge); copied to the plan and the report |
| `devices` | desktop Chromium | Add a mobile profile for mobile web |
| `har` | none | Real traffic, to weight reach by each endpoint's share of requests |

## Workflow

`run_resilience.py` needs Playwright (`pip install -r requirements.txt`, `python -m playwright install
chromium`; ask first). Everything else is stdlib.

```bash
python scripts/plan_scenarios.py flows.json --out <run>/scenarios.json [--plan-only]
python scripts/run_resilience.py flows.json <run>/scenarios.json --out <run> --runs 2
python scripts/classify.py <run>/run.json <run>/scenarios.json flows.json --out <run>/candidates.json
python scripts/build_report.py --run <run>/run.json --scenarios <run>/scenarios.json --candidates <run>/candidates.json \
    --flows flows.json --promote K1,K2 --repros <run>/repros --out <run>/resilience_report.json
```

Run `plan_scenarios.py --plan-only` first and check the count against the request budget, and look at
the baselines before spending the scenarios: a flow whose baseline fails makes its scenarios untested.

1. **Recon** (optional, with source): the data layer, retry and timeout settings, error boundaries
   (the UI crash hunter's `recon.py`).
2. **Plan.** Scenarios with their expected outcome, written before anything runs. Choose faults on
   every critical step's first API call, and partial and static-asset faults once per flow.
3. **Run.** Baselines twice, then each scenario twice (reproduced or only observed). Faults on a flow's
   first step are in place before the page loads. Recovery uses the app's own retry control (an
   accessible name matching retry, try again, reload) or the step's actions again, never a reload; a
   step that only types (type-ahead) is recovered by re-sending the field's value (`refill_input`).
4. **Classify.** Effects per run, merged by root cause (one effect at one step and endpoint across
   faults; no recovery across a step's endpoints; retry storms per endpoint), rated by `RUBRIC.md`.
5. **Triage.** `build_report.py` fills everything the run decides (scope, metadata, baseline, scenarios,
   coverage, unreachable, ratings, evidence, headline counts and confidence, provable limitations) and
   writes a guarded repro script per promoted candidate. Replace every `TODO`: promote, keep for
   review, or dismiss with a checkable reason (an optional background widget left empty, an optimistic
   update that rolls back with a message). For each finding write the title, the actual behaviour
   against the expected, the user impact, a fix (a timeout, an error state, keeping input, honouring
   Retry-After, a chunk retry), a verification step, and check the repro script.
6. **Validate and render.**

```bash
python scripts/validate_resilience.py <run>/resilience_report.json --run <run>/run.json --scenarios <run>/scenarios.json --candidates <run>/candidates.json
python scripts/render_report.py <run>/resilience_report.json [--brief]
```

Edge cases: service workers are blocked in scenarios (they would answer before routing); GraphQL needs
faults matched on the operation name; WebSockets can be closed but not faked; third-party scripts are
a natural partial failure; optimistic UI is only misleading if it doesn't roll back or warn. A fault on
the page's own HTML document (a navigation, a server-rendered page) shows the catalogue's injected body,
not the app: it becomes a `document_fault` note, never a blank screen or crash; aim faults at the
requests the page makes instead. Type-ahead queries for different terms are not retries (a storm counts
one URL and query, cache-busters dropped).

Next.js and other server-rendering frameworks:

- **Prefetch.** Links in view are prefetched at page load (`?_rsc=`, `Next-Router-Prefetch: 1`); a fault
  installed for a later step misses them and the scenario is `not_triggered`. Use `"from": "load"` (with
  `"headers": {"Next-Router-Prefetch": "1"}` to hit only the prefetch), or `"prefetch": "block"` so the
  navigation itself fetches, with `"headers": {"RSC": "1"}`.
- **Matching.** The document `/features` and its payload `/features?_rsc=x` share a path; tell them
  apart with `headers` or `resource_type` (`document`, `fetch`).
- **Server components** fetch on the server; browser faults reach only the payload carrying the result.
  List that data in `unreachable` so the report says it wasn't testable.
- **Error boundaries** (`error.tsx`) catch the error, so there is no page error: put the app's boundary
  wording or selector in `indicators` (`boundary_fallback_words`, `boundary_selectors`), or a boundary
  render is classed as a silent failure instead of a crash (Fatal).
- **Skeletons** (`.animate-pulse`) and text loaders go in `spinner_selectors` / `loading_words`, empty
  states ("No results") in `empty_state_words`, or endless loading and misleading empty states can't fire.
- **Watch window.** Server-rendered pages show their main content at once; the runner keeps watching for
  the scenario's whole window (10 s, 30 s while loading shows) for failing faults, so a widget that
  never stops loading is still caught.

## Output fields

`scope` (faults that ran, `faults_untested`), `metadata` (browser, requests sent, request budget, whether
writes were allowed, blocked requests, runs per scenario), `baseline` (per flow: status, and for a
broken flow the failed step, what it showed and screenshots), `scenarios[]` (with expected outcome,
status `ran | untested | errored | not_triggered`, reason, effects, screenshots), `findings[]` (effect,
impact, likelihood, reach, score, priority, confidence, floors, endpoints, faults, scenarios, expected,
actual, user impact, evidence, repro, fix, verification), `notes[]` (`console_only`, `document_fault`, `recovery_not_tried`), `coverage` (per
fault group: ran, untested, errored, not_triggered), `unreachable[]`, `headline`, `needs_review[]`,
`triage[]`, `limitations[]`. `run.json` keeps each matching request with its query (sensitive values
redacted), whether the injector answered or held it, and its response time.

## What the validator enforces

No `TODO` is left. Scenarios match the plan and the run, their expected outcome predates the run, and
nothing ran for a failed baseline; a scenario whose fault never fired is not_triggered. The baseline
section equals run.json, its screenshots exist, and the headline names every broken flow. `scope.faults`
holds only faults that ran, `faults_untested` the rest; metadata and `unreachable` equal the run and
the plan. Findings match their candidates, cite runs that triggered the fault, come from scenarios
that ran with screenshots and request logs, and have repro scripts. A P0 is reproduced. Retry storms
carry counts and timings; lost input carries lengths and hashes only. Console-only items are never
findings. Coverage, the headline and confidence (low when under half the scenarios ran, a critical
baseline failed, or scenarios ran once) are recomputed, and no summary, limitation or finding claims
resilience, production readiness or graceful recovery from every failure.
