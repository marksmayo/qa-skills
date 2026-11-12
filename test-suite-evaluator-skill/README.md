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

Write a Markdown report (all sections: score card, findings, dimensions with their evidence, behavioural and interface coverage, execution results, recommendations by group with acceptance criteria, limitations, and a commands appendix):

```bash
python scripts/render_report.py evaluation.json --markdown report.md
```

Validate an evaluation:

```bash
python scripts/validate_evaluation.py evaluation.json
```

The validator checks the JSON against the schema, then recomputes what the rubric defines: all fifteen dimensions with their weights, the raw weighted and group scores, every cap that applies, the overall score and its band, and evidence completeness from the inventory. It also checks that the assessment mode and execution evidence match the inventory, and that every score has evidence. It checks that each dimension's evidence level holds for all of its evidence (or is `mixed` with a level per item), that the execution value matches the layers and runs recorded, that every failure is classified, that the assessor's own runs are not offered as CI history, that recommendations follow the Step 9 groups, that a reliability score of 7 or more does not stand beside test failures the runs disagree on, that a thin suite (coverage below 4) is capped at 4 and its few tests' quality is not rated with high confidence, and that each interface coverage entry accounts for every untested interface (by name or as a counted group). It rejects any code-coverage claim made without a runtime coverage report or from evidence that is not verified; a stated percentage is a claim even in a sentence that also says "no", while file names, backticked identifiers and interface coverage named as such ("route coverage is 100% by method and path") are not claims. Both scripts read and write UTF-8 on every platform.

Evaluations written for an older contract fail with a migration hint; SKILL.md "Migrating older evaluations" lists the changes.

## Files

- `SKILL.md` — complete agent instructions
- `RUBRIC.md` — detailed scoring anchors and evidence rules
- `schemas/evaluation.schema.json` — machine-readable output contract
- `scripts/render_report.py` — terminal renderer (ANSI colour) and Markdown renderer (`--markdown`)
- `scripts/validate_evaluation.py` — schema and rubric validator (recomputes scores, caps and completeness)
- `scripts/list_interfaces.py` — optional: lists FastAPI, Flask, Next.js and Express interfaces from source (routers, prefixes and includes resolved, nothing imported) and marks the ones a test's URL literal reaches (`python scripts/list_interfaces.py <repo> --tests <test dir> --out interfaces.json`)
- `examples/evaluation.example.json` — sample result: a black-box, static-only evaluation
- `examples/evaluation.partial-run.example.json` — sample result: a white-box partial run of a synthetic Next.js app (unit layer run twice and with coverage, end-to-end layer not run), with execution results, interface coverage and mixed evidence levels

## Design principles

1. Unknown is not zero.
2. Test-derived inference is not code coverage.
3. A green suite is valuable only if it is trustworthy.
4. Scores require evidence and confidence.
5. Critical weaknesses cap the overall rating.
6. Diagnosis and prioritisation matter more than a vanity score.
