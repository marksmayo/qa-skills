# qa-skills

A collection of agent skills for QA processes, designed to be vendor-neutral and portable across Claude Code, OpenAI Codex, Gemini CLI, and other coding agents. Skills are plain Markdown, JSON, and dependency-free Python — no installs required, except Playwright for the optional browser probes.

## Skills

| Skill | Description |
|---|---|
| [test-suite-evaluator-skill](test-suite-evaluator-skill/) | Evaluates an automated test suite for effectiveness, trustworthiness, engineering quality, CI readiness, and operational health. Produces evidence-backed 1–10 scores with confidence ratings, score caps for critical weaknesses, and a prioritised improvement plan — without bluffing about evidence it cannot access. |
| [journey-to-tests-skill](journey-to-tests-skill/) | Turns Journey Markdown files into tests in the repository's existing suite (web, mobile, API), following its framework, page objects and selector conventions. Every Expect is asserted or explicitly accounted for, product mismatches are reported as suspected bugs rather than bent tests, and a test counts only after passing repeated stability runs. |
| [ticket-to-tdd-skill](ticket-to-tdd-skill/) | Turns a Jira ticket into failing (red) tests in the repository's own framework and style, as an executable spec for the feature. Every requirement quotes the ticket, every red is proven by a real run to be an assertion failure, unexpected greens are investigated, test files are hash-locked against weakening, and instructions hidden in the ticket are reported, never followed. |
| [ui-crash-hunter-skill](ui-crash-hunter-skill/) | Hunts web and mobile UI code for likely app-fatal and screen-fatal crashes, ranks them by blast radius, reach, likelihood and recoverability, and writes reproduction steps. Every finding has a file and line, a concrete trigger and a confidence (reproduced in the running app, traced, or pattern only); the error-boundary map decides severity, every tool lead is triaged, and the source is never modified. |
| [journey-mapper-skill](journey-mapper-skill/) | Reads a codebase (web, mobile, API) and documents its user journeys as prioritised, evidence-linked Markdown files ready to be turned into tests. Accounts for every route, screen, endpoint, entry point and goal event it finds, and marks every journey `inferred` until a person reviews it. |

Planned skills are designed in [docs/design/](docs/design/).

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

Some files are shared between skills (for example the Journey Markdown format and its parser, the
test-convention detector and the JUnit results parser). The
canonical copy lives in `shared/` and each skill carries an identical copy so it stays
self-contained. `python tools/check_shared.py` checks the copies; `--fix` syncs them.

## License

[MIT](LICENSE)
