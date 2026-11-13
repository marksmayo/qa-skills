# CI Failure Triager Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It takes a failed CI run on **GitHub Actions** or **Azure DevOps** and, for every failing job and test,
says whether it is **real**, **flaky**, **environment**, **data**, a **consequence** of another failure (an
aggregate gate job) or **unclassified**, with the evidence:
the log lines (quoted by line number, ANSI codes stripped, secrets redacted), the JUnit result or a
Vitest, Jest, node:test or `go test` (plain or `-json`) console summary, lint and format findings
(golangci-lint, gofmt), a re-run of the failed jobs on
the same commit, the run history and known failure signatures. For real failures it finds the **owning
change** (the commit since the last green run that touched what failed) and drafts a defect; for data
failures it drafts a test-data issue. Drafts stay in the result log; nothing is filed.

Re-running jobs is an outward action, so it re-runs only failed jobs, once, and only with permission.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/ci-failure-triager/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/ci-failure-triager/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/ci-failure-triager/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and `git`. A live
fetch needs a GitHub token (`GITHUB_TOKEN`, `GH_TOKEN` or `gh auth login`) or an Azure DevOps PAT
(`AZURE_DEVOPS_EXT_PAT`).

## Usage

> Main went red on run 123 in example-org/tidewater. Work out what broke; you may re-run the failed jobs.

```bash
python scripts/fetch_run.py --system github --repo example-org/tidewater --run-id 123 --out run/
python scripts/extract_failures.py --run run/ --out run/failures.json
python scripts/rerun.py --run run/ --authorised-by "repo owner, in the request"   # or --observed for someone else's re-run
python scripts/owning_change.py --run run/ --repo . --out run/changes.json   # --max-commits N when there is no last green run
python scripts/classify.py --run run/ --out run/candidates.json
python scripts/validate_triage.py triage.json --run run/
python scripts/render_report.py triage.json --defects-md drafts.md
```

## Example

`examples/make_repo.py` builds "tidewater", a sample package with a fixed history; `examples/recorded/`
holds a failed GitHub Actions run (two attempts) and an Azure DevOps build of the same commit. The logs
and JUnit inside them are real local pytest and pip runs (`examples/record_example_run.py`), inside
envelopes shaped like each API; no CI was called. `examples/run/` is the normalised run with its failures,
candidates and owning changes; `examples/triage.example.json` is the triage and `examples/drafts.md` the
drafts. `examples/README.md` has the answer key.

```bash
python scripts/validate_triage.py examples/triage.example.json --run examples/run
python scripts/render_report.py examples/triage.example.json
```

## Files

- `SKILL.md`, `RUBRIC.md`
- `schemas/triage.schema.json`
- `data/signatures.json`: environment, data and real signatures (including Go module downloads, compile
  errors, data races, golangci-lint and gofmt)
- `scripts/fetch_run.py`: GitHub and Azure DevOps fetch and normalisation (or a recorded directory). The
  API token is never sent to the storage host a log or artifact download redirects to; a download that
  fails is recorded (`fetch_problems`) and makes the script exit 2.
- `scripts/extract_failures.py`, `scripts/rerun.py`, `scripts/owning_change.py`, `scripts/classify.py`
- `scripts/validate_triage.py`, `scripts/render_report.py`
- `scripts/self_test.py`: loopback redirect and download-failure checks, log extraction (including Go
  test, build, race, lint and format output), Go signatures, a run with no job, agent-named tests, local
  repeat runs, the owning change with and without a last green run, and a synthetic run through the whole
  pipeline with seeded violations (`python scripts/self_test.py`)
- `<run>/local_runs.json` (optional, written by the agent): local repeat runs of a failing test on the
  failing commit, with command and environment; see SKILL.md
- `scripts/parse_results.py`: shared copy (edit it in `shared/`)

## Design principles

1. Flaky needs a pass on the same commit; real needs a repeat.
2. Every classification has its evidence, quoted from the log.
3. The owning change shares files with the failure, or it isn't named.
4. Re-runs are asked for, recorded and minimal; drafts are never filed.
