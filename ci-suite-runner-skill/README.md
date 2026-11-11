# CI Suite Runner Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It runs a named test suite in **GitHub Actions** or **Azure DevOps** on the environment and with the test
tags you choose. It triggers the pipeline, polls it to completion, and collects the results: the JUnit
totals and failed tests, the artifacts, and failed jobs' logs. Each trigger is confirmed with you unless a
pre-authorisation covers that suite and environment. Production and other protected environments always
need your confirmation.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/ci-suite-runner/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/ci-suite-runner/`, or include `SKILL.md`
in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/ci-suite-runner/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+. GitHub needs a token
that can dispatch workflows (`actions: write`) in `GITHUB_TOKEN` or `GH_TOKEN`. Azure DevOps needs a PAT
with Build (read and execute) in `AZURE_DEVOPS_EXT_PAT`.

## Usage

> Run the checkout regression on staging, checkout tests but not the slow ones.

```bash
python scripts/run_suite.py plan --catalogue suites.json --suite checkout-regression --environment staging --tags "checkout and not slow" --out runs/checkout-staging/
python scripts/run_suite.py trigger --run runs/checkout-staging/ --preauth preauth.json
python scripts/run_suite.py poll --run runs/checkout-staging/
python scripts/run_suite.py collect --run runs/checkout-staging/
python scripts/render_report.py runs/checkout-staging/suite_run.json
```

## Example

`examples/suites.json` catalogues two suites (a GitHub workflow and an Azure DevOps pipeline), and
`examples/preauth.json` grants staging and UAT runs until 2026-11-30. `examples/ci-stub/server.py` stands in
for both CI APIs on loopback and runs the requested tests for real with pytest. `examples/runs/` holds two
real runs against it, made by `examples/commands.sh`, and the log of what the stub received.
`examples/README.md` has the answer key.

```bash
python scripts/validate_suite_run.py examples/runs/checkout-staging/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
python scripts/render_report.py examples/runs/checkout-staging/suite_run.json examples/runs/smoke-production/suite_run.json
```

## Files

- `SKILL.md`, `RUBRIC.md`
- `schemas/suites.schema.json`, `schemas/preauth.schema.json`, `schemas/suite_run.schema.json`
- `scripts/run_suite.py`: `plan`, `trigger`, `poll`, `collect`
- `scripts/validate_suite_run.py`, `scripts/render_report.py`
- `scripts/parse_results.py`: shared copy (edit it in `shared/`)

## Design principles

1. Ask before starting anything, unless you said yes in advance, and never for production.
2. One request, one trigger.
3. Results from JUnit, with the run link.
4. Failures go to the triager.
