# Test Case Designer Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It turns a ticket's acceptance criteria into manual test cases, each mapped to its criterion, ready to run
or import into Xray, TestRail or Zephyr. For every criterion it picks the test design technique that fits
and says why (boundary values for limits, partitions for classes, decision tables for combined rules,
state transitions for statuses, pairwise for several parameters, scenarios for flows), generates exactly
the rows the technique calls for, and writes one case per row with steps, synthetic test data and
observable expected results. Vague wording becomes a question rather than an assumption.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/test-case-designer/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/test-case-designer/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/test-case-designer/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and nothing else.
`fetch_ticket.py` reads Jira with `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` (read-only), or
works from an issue saved by a Jira connector.

## Usage

> Design the test cases for SHOP-310 for Xray.

```bash
python scripts/fetch_ticket.py SHOP-310 --out run/ticket.json
python scripts/analyse_criteria.py run/ticket.json --out run/analysis.json
python scripts/design_rows.py model.json --out run/rows.json
python scripts/export_cases.py test_cases.json --xray xray.json --csv cases.csv
python scripts/validate_cases.py test_cases.json --ticket run/ticket.json --model model.json --rows run/rows.json --xray xray.json --csv cases.csv
python scripts/render_report.py test_cases.json --markdown cases.md
```

## Example

`examples/SHOP-310.issue.json` is a loyalty-points story with five criteria that need five techniques.
`examples/model.json` is the model, `examples/run/` the normalised ticket, analysis and rows, and
`examples/test_cases.example.json` the 42 cases, with `xray.json`, `cases.csv` and `cases.md` exported from
it. `examples/README.md` has the answer key.

```bash
python scripts/validate_cases.py examples/test_cases.example.json --ticket examples/run/ticket.json --model examples/model.json --rows examples/run/rows.json --xray examples/xray.json --csv examples/cases.csv
python scripts/render_report.py examples/test_cases.example.json --brief
```

## Files

- `SKILL.md`, `RUBRIC.md`
- `schemas/test_cases.schema.json`, `schemas/model.schema.json`
- `data/expected-terms.json`: vague expected results, timing words, reserved test data
- `data/ambiguity-lexicon.json`: shared with the ticket readiness reviewer (edit it in `shared/`)
- `scripts/analyse_criteria.py`, `scripts/design_rows.py`, `scripts/export_cases.py`
- `scripts/validate_cases.py`, `scripts/render_report.py`
- `scripts/fetch_ticket.py`: shared copy (edit it in `shared/`)

## Design principles

1. One technique per criterion, chosen for a reason the criterion shows.
2. Rows come from the model, so coverage can be checked, not asserted.
3. Every expected result is something a tester can see.
4. Questions beat assumptions; synthetic data only.
