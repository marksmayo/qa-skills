# Journey to Tests Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It takes user journeys written in Journey Markdown (from the Journey Mapper skill, or by hand) and
adds tests for them to the repository's existing suite, using its own framework, page objects,
fixtures and selector conventions. It covers web, mobile and API journeys.

It doesn't bluff about the result:

- Every Expect in a journey becomes an assertion, or is listed as not automatable, blocked by a
  testability gap, or deferred, with a reason.
- When the app doesn't match the journey, the test is kept as written and the mismatch is reported
  as a suspected bug.
- A test counts as done only after it passes every stability run with the final test file.
  Flaky tests are reported, not counted.
- Nothing is installed without asking, application code is not changed, and nothing is committed.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/journey-to-tests/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/journey-to-tests/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/journey-to-tests/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The skill's scripts are stdlib-only
Python (3.9+). The tests it writes run with whatever framework the repository already uses.

## Usage

Ask the agent:

> Write tests for the P1 journeys in `journeys/` using the Journey to Tests skill.

or

> Turn `journeys/checkout/JRN-CHK-001-guest-completes-checkout-with-card.md` into Playwright tests
> and update the journey's automation status.

The agent shows a test plan first and waits for approval before writing files.

Scripts, in the order the skill runs them:

```bash
python scripts/journey_md.py lint journeys/                                  # journeys parse
python scripts/plan_coverage.py journeys/ --repo . --out report.json         # one entry per Expect
python scripts/detect_conventions.py . --target e2e/checkout                 # framework, layout, exemplars
python scripts/lint_tests.py e2e/checkout/JRN-CHK-001-guest-checkout.spec.ts # hard waits, assertions, tags
python scripts/parse_results.py runs.json --junit junit.xml --run-id R1 \
    --command "npx playwright test e2e/checkout" --exit-code 1 --hash e2e/checkout/JRN-CHK-001-guest-checkout.spec.ts
python scripts/update_journeys.py report.json --repo .                       # optional
python scripts/validate_report.py report.json --repo . --write-summary       # honesty checks
python scripts/render_report.py report.json                                  # --color always|never
```

## Output

- Test files in the repository's e2e/UI/API test location, with the journey ID in every test
  title and tag
- `journey_tests_report.json`, described by `schemas/journey_tests_report.schema.json`:

| Section | Content |
|---|---|
| `step_coverage` | Every Expect, with the file and line asserting it, or why nothing does |
| `results` | Every test: `pass`, `fail_product`, `fail_test`, `blocked_env`, `flaky` or `not_run`, with every run attempt |
| `suspected_bugs` | Expected vs actual for each product mismatch |
| `testability_gaps` | Elements with no stable selector, and the code change that would fix it |
| `files_changed` | Every file added or modified (left uncommitted) |

See `examples/` for a complete sample. It includes the sample repository (`acme-shop/`): the
journeys, the generated Playwright tests, and a page object. The example report validates
against it:

```bash
python scripts/validate_report.py examples/journey_tests_report.example.json --repo examples/acme-shop
```

## What the validator enforces

- No Expect is left `pending`. `not_automatable` is only allowed where the journey lists it.
- Every assertion reference points at an assertion line in a test file.
- Every test it wrote lints clean: no hard waits, an assertion in every test, a journey tag on
  every test, no `.only`.
- A `fail_product` result can't later become `pass`.
- A changed test file needs a recorded reason, and "changed the expected value" isn't an
  accepted reason.
- `pass` needs `repeat` passing runs of the exact file now on disk, proven by the SHA-256 hashes
  recorded at run time.
- If a journey's Expects were edited during the run, the report is rejected.
- Dependency manifests can't change without the user's approval.
- The summary counts are recomputed, and headlines such as "bug-free" or "complete coverage" are
  rejected.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: selector strategy, assertion rules, failure classification, anti-flake checklist
- `JOURNEY_FORMAT.md`: the Journey Markdown specification (shared with Journey Mapper)
- `schemas/journey_tests_report.schema.json`: output contract
- `schemas/journey.schema.json`: journey front matter contract (shared)
- `scripts/plan_coverage.py`: report skeleton with one entry per Expect
- `scripts/lint_tests.py`: test linter (JS/TS, Python, Java/Kotlin, Swift, Dart, C#, Gherkin, Maestro)
- `scripts/update_journeys.py`: writes `automation`/`tests` back into journeys
- `scripts/validate_report.py`: structure and honesty checks
- `scripts/render_report.py`: coloured terminal/Markdown report
- `scripts/journey_md.py`: Journey Markdown parser and linter (shared)
- `scripts/detect_conventions.py`: test convention fingerprint (shared with Ticket to TDD)
- `scripts/parse_results.py`: JUnit XML normaliser (shared with Ticket to TDD)
- `examples/`: sample report and the sample repository it describes

## Design principles

1. The journey is the specification; a mismatch is a finding, not a test to fix.
2. Every Expect is accounted for.
3. Stable selectors, or a recorded testability gap.
4. Done means passing every stability run.
5. Follow the repository's conventions; never install or commit on the user's behalf.
