# Journey Mapper Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It reads an application's codebase (web, mobile and API) and documents the user journeys in it.
Each journey is a Markdown file listing the persona, entry point, goal, steps with observable
expected outcomes, variants, test data needs and code evidence. The files are written to be
turned into automated tests, by hand or with the Journey to Tests skill.

It doesn't bluff about completeness:

- Every route, screen, endpoint, entry point and goal event it finds is either covered by a
  journey, or listed as an orphan or unreachable item with a reason.
- Coverage is reported as a lower bound.
- Every journey is marked `inferred` until a person reviews it.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/journey-mapper/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/journey-mapper/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/journey-mapper/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The skill uses plain Markdown, JSON and
stdlib-only Python (3.9+), so there's nothing to install.

## Usage

Ask the agent:

> Map the user journeys in this repository using the Journey Mapper skill.

or, for a large application:

> Map the checkout and account journeys using the Journey Mapper skill.

Scripts, in the order the skill runs them:

```bash
python scripts/extract_surface.py <repo> --out surface.json           # candidate surface
python scripts/validate_journey_map.py journeys/journey_map.json      # honesty checks
python scripts/write_journeys.py journeys/journey_map.json --out journeys
python scripts/journey_md.py lint journeys --strict
python scripts/render_report.py journeys/journey_map.json             # --color always|never
```

## Output

```text
journeys/
  index.md              journeys by domain, priority, automation status + flow diagrams
  coverage.md           every surface item and the journey that covers it, or why none does
  journey-graph.json    screen/state interaction graph
  journey_map.json      machine-readable result of the last run
  <domain>/JRN-<DOMAIN>-<NNN>-<slug>.md
```

See `examples/journeys/` for a complete sample.

Re-running is safe:

- Journeys are updated by ID.
- Files a person has marked `reviewed` or `approved` are never overwritten; a `.proposed.md`
  file is written beside them instead.
- `## Notes` sections are preserved.
- Nothing is deleted.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: priority model, granularity rules, evidence rules, automation anchors
- `JOURNEY_FORMAT.md`: the Journey Markdown specification (shared with Journey to Tests)
- `schemas/journey_map.schema.json`: output contract for a mapping run
- `schemas/journey.schema.json`: journey front matter contract (shared)
- `scripts/extract_surface.py`: heuristic extractor for routes, screens, endpoints, deep links,
  goals, flags and e2e tests
- `scripts/validate_journey_map.py`: structure and honesty checks
- `scripts/write_journeys.py`: idempotent writer for journey files, index, coverage and graph
- `scripts/journey_md.py`: Journey Markdown parser, linter and renderer (shared)
- `scripts/render_report.py`: coloured terminal/Markdown report
- `examples/journeys/`: sample output

## Supported stacks

The extractor detects:

| Area | Stacks |
|---|---|
| Web | Next.js (app and pages routers), Nuxt, SvelteKit, Remix, React Router, Vue Router, Angular, Django URLs |
| Mobile | Expo Router, React Navigation, Flutter (GoRouter, route maps), Android (navigation graphs, Compose navigation, manifest activities and deep links), iOS (SwiftUI views, UIKit view controllers, storyboards, URL schemes) |
| API | Express, Fastify, Koa, NestJS, Flask, FastAPI, Rails, Spring, Go `net/http`/Gin/Echo/chi, ASP.NET, OpenAPI documents |
| Goal events | `track`, `logEvent`, `capture`, `gtag('event', …)` and similar analytics calls |

Anything else is added by the agent during review.

## Design principles

1. Inferred is not intended.
2. Every step has code evidence.
3. Every surface item is accounted for.
4. Coverage is a lower bound, never "complete".
5. Human review decisions are never overwritten.
