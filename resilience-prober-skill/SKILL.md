---
name: resilience-prober
description: Check how a running web app behaves when what it depends on goes wrong - slow or failing APIs, rate limits, expired sessions, hung requests, dropped connections, going offline, malformed payloads, one dependency down, flapping endpoints and failed script chunks. Each fault is injected in the browser only (Playwright routing), on a chosen step of a real user flow, after a clean baseline, with the expected outcome written before the run. Records what the user sees and loses - crashes, blank screens, endless spinners, silent failures, misleading empty states, lost input, no way to recover, retry storms - with screenshots, request logs and a standalone repro script. Never calls an app resilient.
version: 1.0.0
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
7. **"Nothing broke" is not "resilient".** The report lists the flows, endpoints and faults tried, and
   what wasn't.
8. **Synthetic data only** in typed input and injected bodies; input is recorded as length and SHA-256.
9. **Safe targets.** Loopback needs nothing; other hosts need `authorised_by`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `flows` | required | `flows.json`: site, app root, flows with named steps (actions, a `done` selector, `input`, `write`), `critical_path` |
| `scenarios` | required | Explicit `{flow, step, endpoint, fault, method?}` or `{"auto": {"endpoints": {...}, "faults": [...]}}` |
| `faults` | `data/faults.json` standard set | Latency, server errors, rate limits, auth expiry, timeouts, connection, offline, payload, partial, flapping, static assets |
| `budgets` | 10 s message, 30 s spinner | `data/indicators.json` |
| `authorised_by`, `auth`, `allow_form_submission` | as in `browser_harness.py` | |
| `devices` | desktop Chromium | Add a mobile profile for mobile web |
| `har` | none | Real traffic, to weight reach by each endpoint's share of requests |

## Workflow

`run_resilience.py` needs Playwright (`pip install -r requirements.txt`, `python -m playwright install
chromium`; ask first). Everything else is stdlib.

```bash
python scripts/plan_scenarios.py flows.json --out <run>/scenarios.json [--plan-only]
python scripts/run_resilience.py flows.json <run>/scenarios.json --out <run> --runs 2
python scripts/classify.py <run>/run.json <run>/scenarios.json flows.json --out <run>/candidates.json
```

1. **Recon** (optional, with source): the data layer, retry and timeout settings, error boundaries
   (the UI crash hunter's `recon.py`).
2. **Plan.** Scenarios with their expected outcome, written before anything runs. Choose faults on
   every critical step's first API call, and partial and static-asset faults once per flow.
3. **Run.** Baselines twice, then each scenario twice (reproduced or only observed). Faults on a flow's
   first step are in place before the page loads. Recovery uses the app's own retry control (an
   accessible name matching retry, try again, reload) or the step's actions again, never a reload.
4. **Classify.** Effects per run, merged by root cause (one effect at one step and endpoint across
   faults; no recovery across a step's endpoints; retry storms per endpoint), rated by `RUBRIC.md`.
5. **Triage.** Promote, `needs_review`, or dismiss with a checkable reason (an optional background
   widget left empty, an optimistic update that rolls back with a message). For each finding write the
   title, the actual behaviour against the expected, the user impact, a fix (a timeout, an error state,
   keeping input, honouring Retry-After, a chunk retry), a verification step, and a standalone repro
   script in `<run>/repros/`.
6. **Validate and render.**

```bash
python scripts/validate_resilience.py <run>/resilience_report.json --run <run>/run.json --scenarios <run>/scenarios.json --candidates <run>/candidates.json
python scripts/render_report.py <run>/resilience_report.json [--brief]
```

Edge cases: service workers are blocked in scenarios (they would answer before routing); GraphQL needs
faults matched on the operation name; WebSockets can be closed but not faked; third-party scripts are
a natural partial failure; optimistic UI is only misleading if it doesn't roll back or warn.

## Output fields

`scope`, `metadata`, `baseline` (status per flow), `scenarios[]` (with expected outcome, status, effects,
screenshots), `findings[]` (effect, impact, likelihood, reach, score, priority, confidence, floors,
endpoints, faults, scenarios, expected, actual, user impact, evidence, repro, fix, verification),
`notes[]` (console-only), `coverage` (per fault group: ran, untested, errored), `headline`,
`needs_review[]`, `triage[]`, `limitations[]`.

## What the validator enforces

Scenarios match the plan, their expected outcome predates the run, and nothing ran for a failed
baseline. Findings match their candidates, come from scenarios that ran with screenshots and request
logs, and have repro scripts. A P0 is reproduced. Retry storms carry counts and timings; lost input
carries lengths and hashes only. Console-only items are never findings. Coverage and the headline are
recomputed, and the summary never calls the app resilient.
