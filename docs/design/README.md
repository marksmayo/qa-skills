# Skill designs

Design documents for planned skills in this repository. Each skill, once built, follows the
anatomy described in the root `CLAUDE.md` (`SKILL.md`, `RUBRIC.md`, `schemas/`, `scripts/`,
`examples/`, `README.md`).

| # | Design | Skill directory | Status |
|---|---|---|---|
| 0 | [Shared components and Journey Markdown format](shared-components.md) | `shared/`, `tools/check_shared.py` | Journey format, `detect_conventions.py`, `parse_results.py` and `sarif.py` built; rendering helpers not shared (each skill keeps its own renderer) |
| 1 | [Journey mapper](journey-mapper.md) | `journey-mapper-skill/` | Built (v1.0.0) |
| 2 | [Journey to tests](journey-to-tests.md) | `journey-to-tests-skill/` | Built (v1.0.0) |
| 3 | [Ticket to TDD](ticket-to-tdd.md) | `ticket-to-tdd-skill/` | Built (v1.0.0) |
| 4 | [UI crash hunter](ui-crash-hunter.md) | `ui-crash-hunter-skill/` | Built (v1.0.0) |
| 5 | [Code security auditor](code-security-auditor.md) | `code-security-auditor-skill/` | Built (v1.0.0) |
| 6 | [WCAG auditor](wcag-auditor.md) | `wcag-auditor-skill/` | Draft |

The table order is the agreed build order.

## How the skills fit together

```text
journey-mapper ──► journey-to-tests
      │
      └──► reach/priority input for ui-crash-hunter and wcag-auditor

code-security-auditor ─┐
ui-crash-hunter ───────┼──► repro steps ──► regression tests (ticket-to-tdd conventions)
wcag-auditor ──────────┘

test-suite-evaluator ◄── grades the suite that all of the above produce
```

## Decisions that apply to every skill

- **No git commits or branches.** Skills that write files leave changes uncommitted in the
  working tree and list every changed file in their report.
- **No ticket write-back in v1.** Skills read from Jira but never write to it.
- **Open-source tooling only.** Skills use tools that are free to run on closed-source client
  code without a commercial licence. Tools with restrictive licences (e.g. CodeQL) are excluded.
- **Web and mobile.** Skills that analyse UI code support both web and mobile stacks in v1.
- **Honesty rules carry over from `test-suite-evaluator`:**
  - Missing evidence is `untested` or `not_assessed`, never a pass.
  - Every finding carries evidence and a confidence rating.
  - "No findings" is never reported as "safe", "compliant" or "crash-free".
  - Each skill's validator script enforces these rules mechanically.
- **Tool availability is recorded.** A tool that isn't installed or fails is recorded as
  `not_run` or `errored`, and the skill lowers its evidence-completeness figure. Skills never
  fail silently.
- **Secrets are never output.** Reports give only the file location and the secret type.
