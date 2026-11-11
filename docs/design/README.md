# Skill designs

Design documents for the skills in this repository. Each skill, once built, follows the
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
| 6 | [WCAG auditor](wcag-auditor.md) | `wcag-auditor-skill/` | Built (v1.0.0) |
| 7 | [Ticket readiness reviewer](ticket-readiness-reviewer.md) | `ticket-readiness-reviewer-skill/` | Built (v1.0.0) |
| 8 | [Flaky test hunter](flaky-test-hunter.md) | `flaky-test-hunter-skill/` | Built (v1.0.0) |
| 9 | [Mutation tester](mutation-tester.md) | `mutation-tester-skill/` | Built (v1.0.0) |
| 10 | [Test impact selector](test-impact-selector.md) | `test-impact-selector-skill/` | Built (v1.0.0) |
| 11 | [Test data auditor](test-data-auditor.md) | `test-data-auditor-skill/` | Built (v1.0.0) |
| 12 | [Dead test finder](dead-test-finder.md) | `dead-test-finder-skill/` | Built (v1.0.0) |
| 13 | [Risk-based test planner](risk-based-test-planner.md) | `risk-based-test-planner-skill/` | Built (v1.0.0) |
| 14 | [Exploratory charter writer](exploratory-charter-writer.md) | `exploratory-charter-writer-skill/` | Built (v1.0.0) |
| 15 | [Bug reproducer](bug-reproducer.md) | `bug-reproducer-skill/` | Built (v1.0.0) |
| 16 | [API contract tester](api-contract-tester.md) | `api-contract-tester-skill/` | Built (v1.0.0) |
| 17 | [Performance budget auditor](performance-budget-auditor.md) | `performance-budget-auditor-skill/` | Built (v1.0.0) |
| 18 | [Visual regression reviewer](visual-regression-reviewer.md) | `visual-regression-reviewer-skill/` | Designed |
| 19 | [Cross-browser checker](cross-browser-checker.md) | `cross-browser-checker-skill/` | Designed |
| 20 | [Internationalisation checker](i18n-checker.md) | `i18n-checker-skill/` | Designed |
| 21 | [Resilience prober](resilience-prober.md) | `resilience-prober-skill/` | Designed |
| 22 | [Database migration reviewer](db-migration-reviewer.md) | `db-migration-reviewer-skill/` | Designed |
| 23 | [Privacy flow auditor](privacy-flow-auditor.md) | `privacy-flow-auditor-skill/` | Designed |
| 24 | [Mobile app auditor](mobile-app-auditor.md) | `mobile-app-auditor-skill/` | Designed |
| 25 | [Release readiness gate](release-readiness-gate.md) | `release-readiness-gate-skill/` | Designed |
| 26 | CI failure triager | `ci-failure-triager-skill/` | Agreed; design not written |
| 27 | Test case designer | `test-case-designer-skill/` | Agreed; design not written |
| 28 | Post-deploy validator | `post-deploy-validator-skill/` | Agreed; design not written |
| 29 | Hypercare anomaly watcher | `hypercare-anomaly-watcher-skill/` | Agreed; design not written |
| 30 | Requirements traceability auditor | `requirements-traceability-auditor-skill/` | Agreed; design not written |
| 31 | CI suite runner | `ci-suite-runner-skill/` | Agreed; design not written |
| 32 | QA orchestrator (gate-aware) | `qa-orchestrator-skill/` | Agreed; design not written |

The table order is the agreed build order. Skills 7–24 are ordered so that each shared script
is built by the first skill that needs it: `fetch_ticket.py` moves to `shared/` with 7,
`run_tests.py` arrives with 8, `diff_scope.py` with 9, and `browser_harness.py` with 15. The
mobile app auditor comes late because it needs emulators and simulators and reuses the WCAG and
crash-hunter rating models. The release readiness gate is last because it reads every other
skill's report.

Skills 26–32 were added on 2026-11-11, after comparing the repo with the QA agent workflow
designs (skills A1 to A6 and the orchestrator in the "QA Agent Workflow" and "QA Agent Mesh"
frames). They fill the gaps that comparison found:

| # | Skill | Board item |
|---|---|---|
| 26 | CI failure triager | `/triage-failures` (A2): classify each CI failure as real, flaky, environment or data; find the owning change; draft the defect |
| 27 | Test case designer | `/design-cases` (A1): manual or xRay-style cases from acceptance criteria, each mapped to its criterion |
| 28 | Post-deploy validator | `/pvt-validate` (A4): read-only production smoke and data checks, performance against a baseline, a go/no-go after deploy |
| 29 | Hypercare anomaly watcher | `/hypercare-watch` (A5): metrics and logs after a release, anomalies correlated to it, draft incidents |
| 30 | Requirements traceability auditor | `/coverage-govern` (A6): requirement to test to run across tickets, with the gaps |
| 31 | CI suite runner | `/run-suite`: trigger a CI pipeline by environment and tags, then collect its results |
| 32 | QA orchestrator | The gate-aware orchestrator: it chains the skills across the gates and keeps a human in the loop |

The orchestrator comes last because it calls the others. Each design is written, and its open
questions are put to the user, before that skill is built.

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

Skills 7–24 add to this:

```text
ticket-readiness-reviewer ──► ticket-to-tdd
bug-reproducer ──► regression test (ticket-to-tdd conventions)

journey-mapper ──► risk-based-test-planner ──► exploratory-charter-writer
      │                    ▲
      │                    └── churn and defects (diff_scope), findings from every auditor
      └──► journeys for performance, visual, cross-browser, i18n, resilience, privacy

flaky-test-hunter, mutation-tester, dead-test-finder, test-data-auditor
      └──► evidence for test-suite-evaluator
test-impact-selector ◄── diff_scope + coverage maps

wcag-auditor + ui-crash-hunter ──► mobile-app-auditor (same rating models, native apps)

every skill's report + CI results ──► release-readiness-gate ──► go / no_go / not_established
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
