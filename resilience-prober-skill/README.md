# Resilience Prober Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It checks what a running web app does when the things it depends on fail. It drives real user flows in
a browser, injects one fault at a time on one step (in the browser only: nothing broken ever reaches
your server), and records what the user sees and loses:

- **Faults**: slow and hung requests, 5xx, 429 with and without Retry-After, 401 mid-flow, dropped
  connections, offline, empty or truncated or wrong payloads, one dependency down, flapping endpoints,
  failed script chunks.
- **Effects**: crashes and blank screens, endless spinners, failures with no message, failures shown as
  empty data, lost form input, no way to recover without a reload, retry storms.
- **Evidence**: screenshots, request logs with timings, and a standalone repro script per finding.

It is strict about what a run shows:

- Each flow's baseline must pass twice before its faults count.
- The expected outcome is written before the run, so the bar can't move.
- Retry storms are counted, not guessed; typed input is recorded as length and hash only.
- A console error alone is a note, and "nothing broke" is never "resilient".

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/resilience-prober/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/resilience-prober/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/resilience-prober/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+; running
scenarios needs Playwright (`pip install -r requirements.txt` and `python -m playwright install chromium`).

## Usage

> What happens to checkout when the payment API is slow or returns 503? Our test environment is https://test.example.com and I can authorise it.

On a shared environment set `request_budget` in flows.json and leave `allow_form_submission` false (the
default): the site's host is the only one the browser may reach, and writes stay in the browser.

```bash
python scripts/plan_scenarios.py flows.json --out run/scenarios.json --plan-only
python scripts/plan_scenarios.py flows.json --out run/scenarios.json
python scripts/run_resilience.py flows.json run/scenarios.json --out run --runs 2
python scripts/classify.py run/run.json run/scenarios.json flows.json --out run/candidates.json
python scripts/build_report.py --run run/run.json --scenarios run/scenarios.json --candidates run/candidates.json --flows flows.json \
    --promote K1,K2 --repros run/repros --out run/resilience_report.json     # then replace every TODO
python scripts/validate_resilience.py run/resilience_report.json --run run/run.json --scenarios run/scenarios.json --candidates run/candidates.json
python scripts/render_report.py run/resilience_report.json [--brief]
```

## Example

`examples/parcel-tracker/` is a small single-page app with a stub API and planted resilience problems;
`examples/flows.json` the flows and scenarios; `examples/run/` the real run (listed in
`examples/run/commands.sh`) with screenshots and repro scripts; `examples/resilience_report.example.json`
the report. `examples/README.md` has the answer key.

```bash
python scripts/validate_resilience.py examples/resilience_report.example.json --run examples/run/run.json --scenarios examples/run/scenarios.json --candidates examples/run/candidates.json
python scripts/render_report.py examples/resilience_report.example.json
python scripts/self_test.py     # offline checks of classify, build_report and the runner's helpers (no browser)
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: effects, impact, likelihood, reach, priority, confidence
- `schemas/resilience_report.schema.json`: output contract
- `data/faults.json`: the fault catalogue with likelihoods
- `data/indicators.json`: default spinner, loading, error, empty-state, boundary and retry-control
  patterns, and budgets (a product's own go under `indicators` in flows.json)
- `scripts/plan_scenarios.py`: scenarios with expected outcomes, written before the run
- `scripts/run_resilience.py`: baselines, fault injection (by URL, method, headers, resource type),
  observation, recovery and request accounting
- `scripts/classify.py`: effects, root-cause merging and ratings
- `scripts/build_report.py`: the report skeleton and a guarded repro script per finding
- `scripts/validate_resilience.py`, `scripts/render_report.py`, `scripts/schema_check.py`
- `scripts/browser_harness.py`: shared copy (edit it in `shared/`)
- `examples/`: the app, the flows, the run and the report

## Design principles

1. Break one thing at a time, in the browser, after a clean baseline.
2. Decide what good looks like before looking.
3. Count what the user sees and loses, not what the console says.
4. Say what was tried; never claim resilience.
