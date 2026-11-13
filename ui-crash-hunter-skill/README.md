# UI Crash Hunter Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It hunts web and mobile UI code (React, Next.js, Vue, Angular, React Native, Android, iOS,
Flutter) for code paths that are likely to crash the app or a whole screen, ranks them by
real-world severity, and writes reproduction steps for each. If the app can run, it forces each
trigger in the running app to confirm the crash, with a stack trace and a screenshot.

It doesn't bluff about the result:

- Every finding has a file and line, the exact trigger (data, stored value, environment or action)
  and why that trigger is realistic. "This could be null" isn't a finding.
- Each finding says how sure it is: `reproduced` (forced in the running app, by its error or, for
  silent failures, by its observed effect), `traced` (followed
  through the code) or `pattern` (a rule matched). Pattern-only findings can't be P0.
- The tier comes from the error-boundary map: the same throw is app-fatal (B1) with nothing above
  it and screen-fatal (B2) under a route boundary. Smaller failures (B3, B4) are listed separately
  and never ranked as crashes.
- Every lead from every tool is either promoted to a finding or dismissed with a reason.
- The report says which routes, screens and tools were covered. It never calls an app
  "crash-free".
- The source is never modified, nothing is installed without asking, and nothing is committed.
  Strict compiler configs, evidence and repro specs go outside the repository.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/ui-crash-hunter/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/ui-crash-hunter/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/ui-crash-hunter/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts are stdlib-only Python (3.9+),
except `probe_web.py`, which needs Playwright for Python (`requirements.txt`) and is only used to
confirm web crashes in a running app. The static analysers (TypeScript, ESLint, Semgrep, detekt,
SwiftLint, Android Lint, `dart analyze`) are used when the repository already has them, or when you
agree to install them.

## Usage

Ask the agent:

> Hunt for likely crashes in this app with the UI Crash Hunter skill.

or

> Use UI Crash Hunter on the checkout screens. The app runs with `npm run dev` on port 5173.

| Severity | Meaning |
|---|---|
| **B1** crash | The app terminates, the whole web app goes blank, or it crashes on every launch (crash loop) |
| **B2** crash | An error boundary replaces the whole screen, or the screen freezes |
| B3 degradation | One component is broken; the rest of the screen works |
| B4 degradation | A console error with no visible effect |

Crashes are ranked by blast radius × reach × likelihood × recoverability into P0–P3.

Scripts, in the order the skill runs them:

```bash
python scripts/recon.py <repo> --out recon.json                          # site model, routes or pages, boundaries (incl. router errorElement), contracts (incl. FastAPI), stored state, module-scope env config; Next.js layouts, proxy, route handlers (also a project nested beside its next.config.*); pages a Node server renders from template literals
python scripts/ui_graph.py <repo> --out graph.json                        # UI entry points and the files they import (tsconfig paths incl. referenced projects and nested app projects, Vite resolve.alias); warns on unresolved imports
python scripts/strict_overlay.py tsc <repo> --scratch <tmp> --out tsc.json  # strict tsc in a scratch config (checkJs for plain JS; a solution tsconfig compiles its app project; always names the tsconfig it compiled and warns when it misses the UI); --scope ui|all
python scripts/strict_overlay.py eslint <repo> --scratch <tmp> --out eslint.json
python scripts/contract_diff.py <repo> --out contract.json               # nullable contract fields vs unguarded access (OpenAPI, GraphQL, Prisma, types); --contract f --discover, --scope all, --defaults-present
python scripts/scan_pitfalls.py <repo> --sarif semgrep.sarif --extra tsc.json eslint.json contract.json --out candidates.json  # UI code only; --all-files
python scripts/scan_pitfalls.py --self-test                               # the multi-line promise-chain test, and recon/scan scope on a synthetic workspace and a Go tool with an HTML page in a string constant
python scripts/self_test.py                                               # all of the above, plus probe_web (file:// targets, query strings, a live file:// probe) and validator checks
python scripts/probe_web.py probes/C-1.json --check                        # validate a scenario's steps and assertions without a browser
python scripts/probe_web.py probes/C-1.json --out-dir evidence/C-1 --emit-spec repro/repro_C-1.py  # --authorised-by for a non-loopback target; --storage-state for a signed-in test session
python scripts/probe_web.py probes/D-1.json --out-dir evidence/D-1 --base-url out/report.html      # a generated local file, probed read-only as file://
python scripts/validate_crash_report.py crash_report.json --repo <repo> --candidates candidates.json --write-summary
python scripts/render_report.py crash_report.json --sarif crashes.sarif  # --brief, --color always|never
python scripts/sarif.py read detekt.sarif swiftlint.sarif                 # normalise any SARIF file
```

## Output

- `crash_report.json`, described by `schemas/crash_report.schema.json`:

| Section | Content |
|---|---|
| `recon` | The site model and blast-radius model (one render tree, or static pages where an error stops only its own script), routes, screens or pages with their reach, the error-boundary map, data layer, contracts, stored state, targets |
| `tools` | Every analyser and probe: ran, not run, errored or not applicable, and why |
| `run_status` | Whether the dynamic half ran: `complete`, `partial` or `not_run` (static only), with the reason |
| `triage` | Every candidate from every tool, promoted to a finding or dismissed with a reason (singly, or in groups with a checked match rule) |
| `probes` | Each dynamic confirmation, including a clean baseline run; read-only against any non-loopback target |
| `crashes` | B1/B2 findings, ranked: location, trigger, boundary, trace, score, repro steps, fix |
| `degradations` | B3/B4 findings, unranked |
| `coverage`, `limitations` | What was and wasn't analysed |

- Evidence (`evidence.json` and a screenshot per probe) and repro specs, which exit 1 while the
  crash reproduces and 0 once it is fixed.
- Optionally, SARIF for code-scanning dashboards.

See `examples/` for a complete sample run against `examples/pantry/`, a small React and TypeScript
recipe app with deliberate crash bugs. The tool outputs in `examples/run/`, the evidence and the
screenshots are real:

- a P0 crash loop: a theme saved by version 0.2.0 isn't valid JSON for 0.3.0, so the app is blank on
  every launch;
- two contract-valid responses (`author: null`, `rating: null`) that break the recipe list and the
  recipe page;
- a malformed recipe link that blanks the app;
- a traced-only crash on iOS 16 (`Object.groupBy`), which the available browsers couldn't reproduce;
- three degradations, and six dismissed candidates with their reasons.

```bash
python scripts/validate_crash_report.py examples/crash_report.example.json \
    --repo examples/pantry --candidates examples/run/candidates.json
python scripts/render_report.py examples/crash_report.example.json
```

To rerun the probes: `cd examples/pantry && npm install && npm run build && npm run serve`, then
`python scripts/probe_web.py examples/probes/C-1.json --out-dir <tmp>`.

## What the validator enforces

- Every finding has a file and line (and with `--repo`, the quoted snippet is on that line), a
  concrete trigger with its basis, exact trigger data for data and stored-state triggers, sources,
  a fix and a verification step.
- The effect matches the tier, and the boundary that caught it exists in the boundary map; screen
  and component fallbacks must name their boundary.
- `reproduced` needs a reproducing probe that lists the finding, its evidence file, a screenshot on
  disk, and a stack trace (with frames, its error in the probe's evidence, not shared with another
  finding) or the probe's observed effect (every `expect` assertion held, described in
  `repro.observed_effect`); an effect alone is enough for B3/B4, while a B1/B2 crash without a stack
  trace needs the probe to show the app blanked (or, for `app_fallback`, the app-wide fallback).
  `traced` needs a trace of at least two hops, and with `--repo` every hop must exist.
- Agent-found findings (`source: "agent"`) carry a basis whose excerpts are re-read from the repo.
- A clean baseline has no errors of the app's own; errors caused by the probe blocking third-party
  requests don't count. `baseline_not_clean` and `errored` probes need a note.
- Every probe ran against `app_run.target`'s host; a non-loopback target needs `authorised_by`.
- `run_status` agrees with the probes; a `not_run` report claims nothing that needs the running app.
- Dismissal reasons are specific: generic ones, and one reason pasted onto more than three
  candidates, are rejected; grouped dismissals must match their rule. API route handlers can't be
  analysed UI routes, and unused boundaries can't catch anything.
- Crashes are B1/B2 and degradations B3/B4; scores, priorities and ranks are recomputed; a crash
  loop needs a stored-state trigger; pattern findings are capped at P1.
- Every candidate is triaged; every route is analysed or listed with a reason; every tool that
  didn't run says why.
- Nothing the run wrote is inside the repository.
- Summary counts are recomputed, and headlines such as "crash-free" are rejected.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: tiers, boundary map per framework, confidence, ranking anchors, triage, pitfall catalogue
- `data/pitfalls.json`: per-platform pitfall packs (patterns, triggers, fixes, matching tool rules)
- `schemas/crash_report.schema.json`: output contract
- `scripts/recon.py`: platforms, site and blast-radius model, routes or pages, boundaries, data layer, contracts, stored state, targets
- `scripts/ui_graph.py`: UI entry points, the import graph (with `tsconfig` `paths` aliases), and file classes (test, route handler, UI)
- `scripts/scan_pitfalls.py`: pattern scan over UI code, and merging of SARIF and other candidates (excluded files counted)
- `scripts/strict_overlay.py`: strict `tsc` and ESLint runs from scratch configs (nothing written to the repo)
- `scripts/contract_diff.py`: nullable contract fields against unguarded access (OpenAPI, GraphQL, Prisma, TypeScript, Kotlin, Swift, Dart)
- `scripts/probe_web.py`: Playwright probes (errors with console Error arguments, white screens and
  observable-effect assertions; third parties blocked; read-only mode; blocked storage, init scripts,
  browser preferences) and standalone repro specs
- `scripts/validate_crash_report.py`: structure and honesty checks
- `scripts/render_report.py`: coloured terminal/Markdown report (`--brief`), with probes, and SARIF export
- `scripts/sarif.py`: SARIF reader and writer (shared with Code Security Auditor)
- `requirements.txt`: Playwright, for `probe_web.py` only
- `examples/`: sample app, tool outputs, probes, evidence, repro specs and report

## Design principles

1. A likely crash is not a crash: say how sure you are, and prove it when the app can run.
2. A finding without a concrete trigger is not a finding.
3. The boundary map, not the throw, decides how bad it is.
4. Account for every lead, every route and every tool.
5. Look, don't touch: no source changes, no installs without asking, no commits.
