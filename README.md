# qa-skills

A collection of agent skills for QA processes, designed to be vendor-neutral and portable across Claude Code, OpenAI Codex, Gemini CLI, and other coding agents. Skills are plain Markdown, JSON, and dependency-free Python — no installs required.

## Skills

| Skill | Description |
|---|---|
| [test-suite-evaluator-skill](test-suite-evaluator-skill/) | Evaluates an automated test suite for effectiveness, trustworthiness, engineering quality, CI readiness, and operational health. Produces evidence-backed 1–10 scores with confidence ratings, score caps for critical weaknesses, and a prioritised improvement plan — without bluffing about evidence it cannot access. |

## Installation

Each skill is a self-contained directory. Copy it into your agent's skills location and point the agent at its `SKILL.md`:

- **Claude Code:** `.claude/skills/<skill-name>/` (project) or your user-level skills directory
- **OpenAI Codex:** `.agents/skills/<skill-name>/`, or include `SKILL.md` in the task context
- **Gemini CLI:** `.gemini/skills/<skill-name>/`, referenced from the project's Gemini instructions

Exact discovery paths vary by client version — see each skill's README for details.

## Skill structure

Every skill follows the same layout:

- `SKILL.md` — complete agent instructions
- `RUBRIC.md` — scoring anchors and evidence rules (where applicable)
- `schemas/` — machine-readable output contracts
- `scripts/` — stdlib-only Python helpers (rendering, validation)
- `examples/` — sample outputs conforming to the schema
- `README.md` — per-skill installation and usage

## License

[MIT](LICENSE)
