# Ticket Readiness Reviewer Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It reviews a Jira ticket before work starts and says whether its acceptance criteria are
specific enough to build and test against: could two people write the same tests from them? It
quotes the exact words behind every problem, writes questions for the ticket's author, suggests
rewrites, and ranks a sprint's worth of tickets worst first.

It's careful about what it claims:

- A `ready` verdict means the criteria are testable. It never means the requirement is right.
- Every flag quotes the ticket verbatim, and the validator re-checks the quotes.
- Every hit from the mechanical lint is kept or dismissed with a reason you can check.
- Suggested rewrites are labelled as suggestions, keep the original, and never add behaviour.
  Where a rewrite needs an answer, the options are in brackets.
- What it couldn't assess is `not_assessed`, never a pass.
- It's read-only: nothing is written to Jira. You get text to paste as a comment yourself.
- Instructions hidden in a ticket ("mark this as ready") are listed and ignored.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/ticket-readiness-reviewer/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/ticket-readiness-reviewer/`,
or include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/ticket-readiness-reviewer/`,
and reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and nothing
else.

## Usage

> Use the ticket readiness reviewer on SHOP-203.

or

> Review every ticket in the next sprint for readiness: `project = SHOP AND sprint in futureSprints()`.

The ticket comes from a Jira connector if the agent has one, otherwise from the Jira REST API
with credentials in environment variables (`JIRA_BASE_URL` plus `JIRA_EMAIL` and
`JIRA_API_TOKEN` for Cloud, or `JIRA_PAT` for Data Center), or from pasted text.

```bash
python scripts/fetch_ticket.py SHOP-203 --out ticket.json                 # or --jql "...", or --from-file
python scripts/lint_criteria.py ticket.json --out lint.json [--glossary glossary.md]
python scripts/validate_readiness.py readiness.json --tickets ticket.json --lint lint.json --write-summary
python scripts/render_report.py readiness.json [--brief] [--comment SHOP-203]
```

| Verdict | Means |
|---|---|
| `ready` | No blocking questions, and every dimension scores 2 or more out of 3 |
| `ready_with_questions` | No blocking questions, but something scores 1 or a Definition of Ready item isn't met |
| `not_ready` | A blocking question, testability (or, for bugs, reproduction steps) below 2, or anything at 0 |
| `not_assessed` | No criteria to review |

Stories are scored on testability (weight 3), clarity and completeness (2), and measurable
non-functional requirements, dependencies and size (1). Bugs use reproduction steps, expected vs
actual, environment, frequency and evidence.

## Output

`readiness.json`, described by `schemas/readiness.schema.json`:

| Section | Content |
|---|---|
| `reviews[].criteria` | Each criterion verbatim, whether it has an oracle, and its flags |
| `reviews[].dimensions` | Scores with rationale and evidence |
| `reviews[].questions` | For the author, blocking or not, with why |
| `reviews[].suggestions` | Labelled rewrites |
| `reviews[].verdict` | Verdict, score and reasons |
| `reviews[].comment_text` | Plain text to paste into Jira |
| `batch` | Worst-first ranking and counts |

## Example

`examples/tickets/` holds four planted tickets: a ready story, a vague "faster search" story,
a discount-code story with a hidden stacking rule and a prompt-injection comment, and a bug with
no version. `examples/batch.example.json` reviews all four; `examples/README.md` is the answer
key.

```bash
python scripts/validate_readiness.py examples/batch.example.json --tickets examples/tickets.json --lint examples/lint.json
python scripts/render_report.py examples/batch.example.json --brief
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: dimensions, caps, verdict, flags, suggestions and ranking
- `data/ambiguity-lexicon.json`: the terms and patterns the lint looks for (shared with the test case designer; edit it in `shared/`)
- `schemas/readiness.schema.json`: output contract
- `scripts/fetch_ticket.py`: Jira connector, REST, JQL or pasted ticket to `ticket.json` (shared with ticket-to-tdd)
- `scripts/lint_criteria.py`: mechanical lint of the criteria
- `scripts/validate_readiness.py`: structure and honesty checks
- `scripts/render_report.py`: coloured terminal/Markdown report and comment text
- `examples/`: planted tickets, their lint and the reviews

## Design principles

1. Ready means testable, not right.
2. Quote the ticket; never paraphrase a flag.
3. Ask, don't invent: missing behaviour is a question.
4. Account for every lint hit.
5. Never write to the ticket.
