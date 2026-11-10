# Ticket to TDD Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It reads a Jira ticket and writes failing (red) tests for it in the repository's own framework,
layout and style: pytest if the repository uses pytest, Jest if it uses Jest, and so on. The tests
are an executable specification: a developer builds the feature and they turn green.

It doesn't bluff about the result:

- Every requirement quotes the ticket text it came from. Ambiguities become open questions, and a
  blocking one stops the run before any test is written.
- A red counts only if a real run shows an assertion failure about the missing behaviour. Import,
  collection and setup errors don't count.
- A test that passes before implementation is investigated and reported, not celebrated.
- Test files are hash-locked at red, so they can't be quietly weakened to reach green.
- Instructions hidden in the ticket ("delete these tests", "push to main") are reported, never
  followed.
- Nothing is installed without asking, nothing is committed, and Jira is only read, never written.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/ticket-to-tdd/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/ticket-to-tdd/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/ticket-to-tdd/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The skill's scripts are stdlib-only
Python (3.9+). The tests it writes run with whatever framework the repository already uses.

## Usage

Ask the agent:

> Write red tests for PROJ-123 using the Ticket to TDD skill.

or

> Use Ticket to TDD on this ticket (pasted below), mode red_green.

The agent shows a test plan first and waits for approval before writing files.

| Mode | What you get |
|---|---|
| `red_only` (default) | Failing tests for a developer to implement against |
| `red_green` | The tests, plus the minimum implementation to pass them, with a re-red check |
| `full` | `red_green` plus a refactor pass |

### Getting the ticket

The skill uses, in order: a Jira connector or MCP tool the agent already has; the Jira REST API
with credentials from environment variables; or a ticket you paste.

| Variable | Use |
|---|---|
| `JIRA_BASE_URL` | e.g. `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` + `JIRA_API_TOKEN` | Jira Cloud (REST v3) |
| `JIRA_PAT` | Jira Data Center / Server personal access token (REST v2) |

The acceptance-criteria field is found by name (default "Acceptance Criteria", change it with
`--ac-field-name`). Without one, criteria are taken from the description.

Scripts, in the order the skill runs them:

```bash
python scripts/fetch_ticket.py PROJ-123 --out ticket.json                    # or --from-file
python scripts/detect_conventions.py . --target tests/test_orders.py          # framework, layout, exemplars
python scripts/parse_results.py runs.json --junit junit.xml --run-id R1 --phase red \
    --command "python -m pytest tests/test_orders.py --junitxml=junit.xml" --exit-code 1 --hash tests/test_orders.py
python scripts/hash_tests.py lock tdd_report.json --repo .                    # after the red phase
python scripts/hash_tests.py check tdd_report.json --repo .                   # during green
python scripts/validate_tdd_report.py tdd_report.json --ticket ticket.json --repo . --write-summary
python scripts/render_report.py tdd_report.json                               # --color always|never
```

## Output

- Test files in the repository's test location, named and structured like its existing tests
- Signature-only stubs where tests need a symbol to exist (listed in the report)
- `tdd_report.json`, described by `schemas/tdd_report.schema.json`:

| Section | Content |
|---|---|
| `requirements` | `REQ-n`, each with the ticket text it quotes, its type and testability |
| `open_questions` | Ambiguities, and whether they block |
| `traceability` | Requirement → test → level → kind, with red (and green) evidence |
| `integrity` | Red lock (file hashes), stubs, modified existing tests, re-red check |
| `regressions` | Existing tests that failed after the change |
| `files_changed` | Every file added or modified (left uncommitted) |

See `examples/` for a complete sample: a Jira issue (`BILL-142.issue.json`) with an instruction
planted in a comment, its normalised `ticket.example.json`, the sample repository it was run
against (`bookshop/`, with the red tests and stubs), and the report. The example runs are real
pytest output. The report validates against all three:

```bash
python scripts/validate_tdd_report.py examples/tdd_report.example.json \
    --ticket examples/ticket.example.json --repo examples/bookshop
```

## What the validator enforces

- Every requirement's excerpt is quoted from the ticket, and the ticket hasn't changed since.
- Every instruction-like passage in the ticket is reported as not followed.
- No tests were written while a blocking question was open.
- Every `valid_red` is an assertion failure in a red-phase run of the locked test file, with a
  reason.
- Every unexpected green is investigated; one that can't fail must be rewritten.
- `red_only` mode adds only signature-only stubs, never implementation code, and the existing suite
  was run after adding them.
- Locked test files are unchanged on disk and contain no skip, xfail or only markers.
- In `red_green` and `full` modes: every red went green with the locked file, the re-red check
  confirmed the tests fail without the implementation, and every failing existing test is listed.
- Existing tests and dependency manifests aren't changed without justification and approval.
- The summary counts are recomputed, and headlines such as "fully tested" are rejected.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: requirement extraction, test level choice, red classification, unexpected greens
- `schemas/tdd_report.schema.json`: output contract
- `scripts/fetch_ticket.py`: Jira REST/connector/pasted ticket → `ticket.json` (ADF and wiki markup to Markdown)
- `scripts/hash_tests.py`: red lock and skip/xfail/only marker check
- `scripts/validate_tdd_report.py`: structure and honesty checks
- `scripts/render_report.py`: coloured terminal/Markdown report
- `scripts/detect_conventions.py`: test convention fingerprint (shared with Journey to Tests)
- `scripts/parse_results.py`: JUnit XML normaliser (shared with Journey to Tests)
- `examples/`: sample ticket, sample repository and report

## Design principles

1. The ticket is the specification, and only the ticket.
2. A red must fail for the right reason, proven by a real run.
3. A green before implementation is a finding.
4. Locked tests are the contract; the implementation changes, the tests don't.
5. Follow the repository's conventions; never install, commit or write back to Jira.
