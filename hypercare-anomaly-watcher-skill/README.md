# Hypercare Anomaly Watcher Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

For the days after a release it watches **Datadog** metrics and **Sentry** issues, read-only, on a schedule
you install. Each check is a tick. It compares every metric with a statistical baseline (the same hour on
the days before the release) and confirms only what persists. It says whether a change began with the
release, and drafts incidents for a named person to raise. It never pages anyone and never creates
anything in Datadog, Sentry or an incident tool.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/hypercare-anomaly-watcher/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/hypercare-anomaly-watcher/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/hypercare-anomaly-watcher/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and nothing else.
Live watching needs `DD_API_KEY` and `DD_APP_KEY` (a read-only application key is enough) and a Sentry
token with read scopes in `SENTRY_AUTH_TOKEN`.

## Usage

> We released harbour-shop 2.4.0 at 22:00 UTC. Watch it for 72 hours and tell me if anything needs an incident.

```bash
python scripts/watch.py init --config hypercare.json --state state/     # prints a cron line; install it yourself
python scripts/watch.py tick --state state/                             # what the schedule runs
python scripts/render_report.py state/
python scripts/validate_watch.py state/
```

## Example

`examples/hypercare.json` watches four Datadog queries and a Sentry project for harbour-shop 2.4.0.
`examples/recorded/` is simulated monitoring data (`examples/simulate.py`, fixed seed) in the APIs'
response shapes, and `examples/state/` is the watch after seven hourly ticks, made by `examples/commands.sh`.
`examples/README.md` has the answer key.

```bash
python scripts/validate_watch.py examples/state --recorded examples/recorded
python scripts/render_report.py examples/state
```

## Files

- `SKILL.md`, `RUBRIC.md`
- `schemas/hypercare_config.schema.json`, `schemas/watch_state.schema.json`
- `scripts/sources.py`: read-only Datadog and Sentry access, or recorded responses
- `scripts/detect.py`: robust z, Poisson tails, the state machine
- `scripts/watch.py`: `init` and `tick`
- `scripts/validate_watch.py`, `scripts/render_report.py`

## Design principles

1. Compare with the same hour on other days, not with a guess.
2. One bad hour is a suspicion; two is a finding.
3. Say why something is tied to the release, or say it isn't known.
4. Draft for a person; never page, post or create.
