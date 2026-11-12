# Test Suite Evaluator Skill

A portable repository-assessment skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It evaluates an automated test suite without bluffing about evidence it cannot access. It supports:

- **Black-box mode:** tests, configuration, and reports only
- **Grey-box mode:** adds specifications, schemas, requirements, or architecture
- **White-box mode:** adds application source, runtime coverage, and mutation data

The evaluator produces evidence-backed 1–10 scores, confidence ratings, score caps for critical weaknesses, and a prioritised improvement plan.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory and point Claude to `SKILL.md`. Common setups place project skills under `.claude/skills/test-suite-evaluator/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/test-suite-evaluator/`, or include `SKILL.md` in the task context. The skill avoids model-specific tool syntax.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/test-suite-evaluator/`, and reference `SKILL.md` from the project’s Gemini instructions. It uses plain Markdown, JSON, and Python only.

Exact skill discovery paths can vary by client version. The core skill is deliberately vendor-neutral.

## Usage

Ask the agent:

> Evaluate this automated test suite using the Test Suite Evaluator skill. Run available tests and inspect CI artifacts when practical. Produce both the terminal report and the machine-readable JSON result.

For terminal rendering:

```bash
python scripts/render_report.py evaluation.json
```

Force or disable colour:

```bash
python scripts/render_report.py evaluation.json --color always
python scripts/render_report.py evaluation.json --color never
```

Validate an evaluation:

```bash
python scripts/validate_evaluation.py evaluation.json
```

The validator checks the JSON against the schema, then recomputes what the rubric defines: all fifteen dimensions with their weights, the raw weighted and group scores, every cap that applies, the overall score and its band, and evidence completeness from the inventory. It also checks that the assessment mode and execution evidence match the inventory, and that every score has evidence. It rejects any code-coverage claim made without a runtime coverage report or from test-derived evidence. Both scripts read and write UTF-8 on every platform.

## Files

- `SKILL.md` — complete agent instructions
- `RUBRIC.md` — detailed scoring anchors and evidence rules
- `schemas/evaluation.schema.json` — machine-readable output contract
- `scripts/render_report.py` — coloured terminal/Markdown renderer
- `scripts/validate_evaluation.py` — schema and rubric validator (recomputes scores, caps and completeness)
- `examples/evaluation.example.json` — sample result

## Design principles

1. Unknown is not zero.
2. Test-derived inference is not code coverage.
3. A green suite is valuable only if it is trustworthy.
4. Scores require evidence and confidence.
5. Critical weaknesses cap the overall rating.
6. Diagnosis and prioritisation matter more than a vanity score.
