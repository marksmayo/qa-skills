# Requirements Traceability Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It traces requirements from **Jira** (or a **requirements document**) to the code and commits that
implement them, the tests that check each acceptance criterion, and the latest test results. Then it
reports the gaps: criteria with no test, linked tests that failed or never ran, requirements implemented
but untested, requirements nothing names, and orphan tests. Every link carries evidence you can re-check
(a file and line, a commit, a ticket link, a test-management entry) and is marked `explicit` or
`inferred`. Only explicit tests that passed verify a requirement.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/requirements-traceability-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/requirements-traceability-auditor/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/requirements-traceability-auditor/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and `git`. Jira is
read with `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`, or from issues saved by a Jira connector.

## Usage

> Which of the 2026.12 loyalty stories are actually tested? Use last night's JUnit.

```bash
python scripts/fetch_ticket.py --jql "project = SHOP AND fixVersion = 2026.12 AND component = loyalty" --out run/tickets.json
python scripts/load_requirements.py --tickets run/tickets.json --doc docs/requirements.md --out run/requirements.json
python scripts/collect_evidence.py --requirements run/requirements.json --repo . --junit results/junit.xml --test-management xray.json --out run/evidence.json
python scripts/build_matrix.py --requirements run/requirements.json --evidence run/evidence.json --triage triage.json --out run/matrix.json
python scripts/validate_trace.py trace.json --requirements run/requirements.json --evidence run/evidence.json --junit results/junit.xml --repo .
python scripts/render_report.py trace.json --csv matrix.csv
```

## Example

`examples/make_repo.py` builds "harbour-loyalty", a sample package whose commits name their tickets.
`examples/tickets/` holds four Jira issues and `examples/requirements.md` two non-functional requirements.
`examples/test-management.json` is an Xray mapping, and `examples/run/` holds the normalised inputs, a real
pytest JUnit run and the evidence. `examples/trace.example.json` is the report and `examples/matrix.csv` its
matrix. `examples/README.md` has the answer key.

```bash
python scripts/validate_trace.py examples/trace.example.json --requirements examples/run/requirements.json --evidence examples/run/evidence.json --junit examples/run/junit.xml
python scripts/render_report.py examples/trace.example.json
```

## Files

- `SKILL.md`, `RUBRIC.md`, `schemas/trace.schema.json`
- `scripts/load_requirements.py`, `scripts/collect_evidence.py`, `scripts/build_matrix.py`
- `scripts/validate_trace.py`, `scripts/render_report.py`
- `scripts/fetch_ticket.py`, `scripts/parse_results.py`: shared copies (edit them in `shared/`)

## Design principles

1. Every link shows its evidence.
2. Exact keys; explicit beats inferred, and only explicit verifies.
3. A test that didn't run proves nothing.
4. Report the gaps, never "fully covered".
