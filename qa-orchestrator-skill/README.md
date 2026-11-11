# QA Orchestrator Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It takes a change through the QA gates: Ready, Designed, Built and tested, Verified in CI, Release ready,
Deployed, Hypercare. At each gate it runs the right skill, records each report with its own validator's
result, and checks the gate's exit criteria. Then it asks the named person to approve, approve with an
override, or reject. A small script keeps the run's state in a temporary folder and refuses to move on
without a person's decision.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/qa-orchestrator/`,
with the other QA skills beside it (the default skills root is this directory's parent).

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/qa-orchestrator/`, or include `SKILL.md`
in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/qa-orchestrator/`, and reference `SKILL.md`
from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The script needs Python 3.9+; each step needs its
own skill installed.

## Usage

> Take SHOP-310 through the QA gates for release 2026.12. Ask me at each gate.

```bash
python scripts/orchestrate.py init --subject "SHOP-310 for release 2026.12"
python scripts/orchestrate.py next --state <state dir printed by init>
python scripts/orchestrate.py record --state <dir> --gate G1 --step readiness --report review.json
python scripts/orchestrate.py evaluate --state <dir> --gate G1
python scripts/orchestrate.py approve --state <dir> --gate G1 --by "QA lead" --how "in chat"
python scripts/render_report.py <dir>
```

The default gates are in `data/orchestrator.json`. Copy it, add each validator's arguments, and change the
steps, criteria and approvers to match how your team works.

## Example

`examples/orchestrator.example.json` is a six-gate configuration. `examples/state/` is a real run made by
`examples/commands.sh`, in which other skills' example reports stand in for one change's evidence:
- **G1 and G2:** approved.
- **G3:** approved with an override, because one test fails in CI.
- **G4:** rejected, because the release gate says no-go. The run stops there.

`examples/summary.md` is the exported summary, and `examples/README.md` has the answer key.

```bash
python scripts/validate_run.py examples/state --rerun
python scripts/render_report.py examples/state
```

Run these from the repository root: the recorded reports and validator commands use paths from there.

## Files

- `SKILL.md`, `RUBRIC.md`
- `data/orchestrator.json`: the default seven gates
- `schemas/orchestrator.schema.json`, `schemas/run_state.schema.json`
- `scripts/orchestrate.py`: `init`, `next`, `record`, `evaluate`, `approve`, `reject`, `reopen`, `status`
- `scripts/validate_run.py`, `scripts/render_report.py`

## Design principles

1. A person decides at every gate, and the record says who and how.
2. Criteria are checked before anyone is asked.
3. Evidence is the reports and their own validators, not the orchestrator's opinion.
4. Each skill keeps its own rules inside the run.
