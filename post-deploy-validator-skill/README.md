# Post-Deploy Validator Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

Right after a deploy, it checks the live environment **read-only** and recommends go or no-go for a person
to decide:

- the deployed **version** is the expected one;
- key **pages and endpoints** answer correctly, signed in with a **named set of test credentials** where
  needed;
- the **data** the release depends on is sane, through the API;
- **response times** haven't regressed against the skill's **previous run**.

It sends only GET and HEAD requests plus the named sign-in. Anything else in the plan is refused and
recorded. Credentials, tokens and cookies never reach a file.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/post-deploy-validator/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/post-deploy-validator/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/post-deploy-validator/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and nothing else.

## Usage

> 1.9.0 just went out to production. Run the post-deploy checks with the smoke-reader account and tell me
> whether we keep it.

```bash
export PVT_SMOKE_READER_USERNAME=... PVT_SMOKE_READER_PASSWORD=...     # from your secret store, not the plan
python scripts/run_pvt.py pvt_plan.json --expect-version 1.9.0 --out runs/2026-11-12T0630/ --baseline runs/2026-11-05T0600/run.json
python scripts/recommend.py runs/2026-11-12T0630/run.json --decision-owner "Release manager" --out pvt_report.json
python scripts/validate_pvt.py pvt_report.json --run runs/2026-11-12T0630/run.json --baseline runs/2026-11-05T0600/run.json
python scripts/render_report.py pvt_report.json
```

## Example

`examples/harbour-tickets/server.py` is a small ferry-ticket site and API standing in for production on
loopback, in two releases. `examples/runs/` holds a real run of 1.8.0 (the baseline) and a real run of 1.9.0
against it, both made by `examples/commands.sh`. `examples/pvt_report.example.json` is the report.
`examples/README.md` has the answer key.

```bash
python scripts/validate_pvt.py examples/pvt_report.example.json --run examples/runs/release-1.9.0/run.json --baseline examples/runs/baseline-1.8.0/run.json
python scripts/render_report.py examples/pvt_report.example.json
```

## Files

- `SKILL.md`, `RUBRIC.md`
- `schemas/pvt_plan.schema.json`, `schemas/pvt_report.schema.json`
- `scripts/run_pvt.py`: the read-only runner (request guard, sign-in, assertions, timings, baseline)
- `scripts/recommend.py`, `scripts/validate_pvt.py`, `scripts/render_report.py`

## Design principles

1. Never write to production; refuse what isn't a read.
2. Secrets stay in memory; values are recorded only where asked.
3. Compare with the last run on the same plan, not with a guess.
4. Recommend with reasons; leave the decision to a person.
