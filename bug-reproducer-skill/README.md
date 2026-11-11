# Bug Reproducer Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It takes a bug report and tries to reproduce it on the version the report names. Reports can come
from Jira, a GitHub issue or pasted text, and are often vague. The skill:

- **Triages the report**: lists what it gives and what it lacks, with a question for each gap.
- **Extracts a failure signature**, normalised so it can be matched.
- **Finds likely duplicates**, read-only.
- **Runs repeated attempts**, in a real browser (Playwright), a test or an HTTP request, and
  records a screenshot, console log and trace for each.
- **Varies the conditions when it doesn't reproduce**: browser, time zone and clock, locale, data,
  network, CPU and version.
- **Cuts a reproduction down** to its minimal steps with delta debugging.
- **Points at the likely code**, rates severity and priority with a published rubric, and writes
  a failing regression test once you approve it.

It is strict about evidence:

- "Reproduced" needs a recorded run showing the reported failure. Reading the code and deciding
  it has the bug is a hypothesis.
- A different failure is `reproduced_differently`, reported separately.
- `not_reproduced` lists every attempt and variation. It never says "not a bug" or "fixed"
  without evidence.
- Report text is data. Instructions in it are listed and ignored, and steps that would destroy
  data, move money or contact people are skipped.
- Only loopback targets are tested without `authorised_by`. Non-GET requests are blocked unless
  allowed.
- The regression test must be red by assertion, not by an error.
- Nothing is written back to Jira or GitHub.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/bug-reproducer/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/bug-reproducer/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/bug-reproducer/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+. Browser
reproduction and minimisation need Playwright (`pip install -r requirements.txt` and
`python -m playwright install chromium firefox webkit`). The regression test needs the repo's
own test runner.

## Usage

> Try to reproduce TASK-11 on 1.2.0.

> Can you reproduce GitHub issue #42 against staging? I'm authorised to test https://staging.example.com.

```bash
python scripts/fetch_ticket.py --from-file issue.json --source connector --out run/tickets.json
python scripts/triage.py --ticket run/tickets.json --out run/triage.json
python scripts/dupes.py --ticket run/tickets.json --key TASK-11 --candidates run/candidates.json
python scripts/reproduce.py run/specs/TASK-11.json --out run/attempts/TASK-11.json --base run
python scripts/minimise.py run/specs/TASK-11.json --candidate A1 --out run/minimal/TASK-11.json --base run
python scripts/validate_repro.py run/repro_report.json --tickets run/tickets.json --run run
python scripts/render_report.py run/repro_report.json [--brief] [--comment TASK-11]
```

For a GitHub issue, save `gh issue view 42 --json number,title,body,labels,state,url,comments`
and pass it with `--from-file issue.json --source github`.

## Example

`examples/tasklist/` is a small static task app with two planted bugs. `examples/reports/` holds
five reports about it:

- a time-zone bug that reproduces 3 of 3 on a Sydney morning;
- a double-click race that reproduces intermittently;
- a report too vague to attempt;
- a duplicate;
- a report that doesn't reproduce on the current version, with a harmful "clear all tasks" step.

`examples/run/` is the real run: attempts with screenshots, console logs and traces, the ddmin
trials, and the red regression test run. `examples/README.md` has the answer key.

```bash
python scripts/validate_repro.py examples/repro_report.example.json --tickets examples/run/tickets.json --run examples/run
python scripts/render_report.py examples/repro_report.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: outcome, confidence, severity, reach, frequency, priority, duplicates, localisation
- `schemas/repro_report.schema.json`: output contract (one report or a batch)
- `scripts/triage.py`: fields present and missing, questions, harmful steps, suspicious instructions
- `scripts/signature.py`: failure signature extraction, normalisation and matching
- `scripts/dupes.py`: duplicate similarity scores
- `scripts/reproduce.py`: runs attempt specs in a browser and records evidence
- `scripts/minimise.py`: ddmin over steps, with confirmation runs
- `scripts/validate_repro.py`: checks against the attempts, the tickets and the rubric
- `scripts/render_report.py`: terminal/Markdown report, one-line summary, or a ticket comment to paste
- `scripts/schema_check.py`: the JSON Schema subset the validator uses
- `scripts/fetch_ticket.py`, `browser_harness.py`, `run_tests.py`, `parse_results.py`,
  `detect_conventions.py`: shared copies (edit them in `shared/`)
- `examples/`: the app, the reports, the specs, the run and the report

## Design principles

1. A reproduction is a recorded run, not an argument.
2. Match the reported failure, not just any failure.
3. Say what was tried when it doesn't reproduce; never dismiss.
4. Untrusted text is data, never instructions.
5. Leave a red test behind, red for the right reason.
