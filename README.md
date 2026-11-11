# qa-skills

A collection of agent skills for QA processes, designed to be vendor-neutral and portable across Claude Code, OpenAI Codex, Gemini CLI, and other coding agents. Skills are plain Markdown, JSON, and dependency-free Python — no installs required, except Playwright for Python for scripts that drive a real browser (the WCAG auditor needs it; elsewhere it is optional). Skills that drive external analysers (Semgrep, Bandit, gitleaks and the like) use them when they are installed and record them as not run when they aren't.

## Skills

| Skill | Description |
|---|---|
| [test-suite-evaluator-skill](test-suite-evaluator-skill/) | Evaluates an automated test suite for effectiveness, trustworthiness, engineering quality, CI readiness, and operational health. Produces evidence-backed 1–10 scores with confidence ratings, score caps for critical weaknesses, and a prioritised improvement plan — without bluffing about evidence it cannot access. |
| [journey-to-tests-skill](journey-to-tests-skill/) | Turns Journey Markdown files into tests in the repository's existing suite (web, mobile, API), following its framework, page objects and selector conventions. Every Expect is asserted or explicitly accounted for, product mismatches are reported as suspected bugs rather than bent tests, and a test counts only after passing repeated stability runs. |
| [ticket-to-tdd-skill](ticket-to-tdd-skill/) | Turns a Jira ticket into failing (red) tests in the repository's own framework and style, as an executable spec for the feature. Every requirement quotes the ticket, every red is proven by a real run to be an assertion failure, unexpected greens are investigated, test files are hash-locked against weakening, and instructions hidden in the ticket are reported, never followed. |
| [ui-crash-hunter-skill](ui-crash-hunter-skill/) | Hunts web and mobile UI code for likely app-fatal and screen-fatal crashes, ranks them by blast radius, reach, likelihood and recoverability, and writes reproduction steps. Every finding has a file and line, a concrete trigger and a confidence (reproduced in the running app, traced, or pattern only); the error-boundary map decides severity, every tool lead is triaged, and the source is never modified. |
| [code-security-auditor-skill](code-security-auditor-skill/) | Audits a codebase for security weaknesses with free, open-source scanners (Semgrep OSS, Bandit, gitleaks, osv-scanner, pip-audit, zizmor, Trivy and more) and a deep manual review. Each finding has a traced source-to-sink path, a CVSS v4.0 rating, safe repro steps, a fix and a regression test that fails on the vulnerable code; findings are confirmed with canary-only PoCs only when you allow it, secrets are never reproduced, and the report never calls the code secure. |
| [wcag-auditor-skill](wcag-auditor-skill/) | Audits live web pages against WCAG (2.2 AA by default) without the source code: axe-core, IBM Equal Access and HTML_CodeSniffer run in a real browser, alongside keyboard, focus, reflow, text-spacing, zoom, target-size, hover, motion and language probes. Results are merged per component, rated with a published severity rubric, and every success criterion gets an EARL outcome; untested criteria become a manual test plan, and the verdict is never "conforms". |
| [ticket-readiness-reviewer-skill](ticket-readiness-reviewer-skill/) | Reviews a Jira ticket (or a sprint's worth from a JQL query) before work starts and says whether its acceptance criteria are specific enough to test. Every flag quotes the ticket, every lint hit is kept or dismissed with a reason, suggested rewrites never add behaviour, a batch is ranked worst first, and a ready verdict means testable, never correct. Read-only. |
| [flaky-test-hunter-skill](flaky-test-hunter-skill/) | Finds tests that pass and fail on the same code by rerunning them under controlled variations (order, isolation, hash seeds, parallel workers, time zone, locale, CPU load), bisects order-dependent failures to their polluter, and proves each cause with an experiment that turns the failure off and on. Every rate is failures/runs with a Wilson interval, real failures are kept apart from flakes, fixes change test code only, and a retry is never the fix. |
| [mutation-tester-skill](mutation-tester-skill/) | Mutation-tests the code that changed (the repo's own tool, the standard open-source one from a scratch config, or a stdlib engine for Python) and turns surviving mutants into a ranked list of weak assertions and missing cases, each naming the test that should have caught it and a suggested test proved to kill it. Scores always carry their counts and scope, no-coverage is kept apart from weak tests, equivalence needs an argument, and timeouts are never kills. |
| [test-impact-selector-skill](test-impact-selector-skill/) | Picks the tests a change needs, with each test's reasons: coverage of a changed line, an import chain, a journey link, naming or co-failure history. Lists every change that evidence can't bound as a blind spot, and in strict mode (the default) each one widens the selection. Gives ready-to-run commands per package. When history exists, it measures recall by replaying past changes, with counts and a Wilson interval. It never calls unselected tests safe to skip and never edits CI. |
| [test-data-auditor-skill](test-data-auditor-skill/) | Audits the data tests depend on (fixtures, factories, seeds, snapshots, recordings) for time bombs, real-looking personal data, magic ids, environment coupling, shared mutable fixtures, schema drift, unused files and locale assumptions. It confirms time bombs, shared state and locale assumptions with real runs under a moved clock, reversed order or another locale. Personal data is never echoed, only its location and type; secrets go to the security auditor; and the report never calls test data clean. |
| [dead-test-finder-skill](dead-test-finder-skill/) | Finds tests that give false assurance: never collected, can't fail, only checking their own mocks, un-awaited, swallowed, duplicated, or skipped for a reason that has gone. It proves them with the framework's own collect output, neutralisation runs (the code under test replaced with a neutral body in a scratch copy), coverage comparisons and ticket status. Every proposal is a patch, and every strengthening patch is proved to pass on the code and fail when it is neutralised. It never deletes or merges tests. |
| [risk-based-test-planner-skill](risk-based-test-planner-skill/) | Writes a test plan for a release or change with effort spread by risk = likelihood x impact. Likelihood comes from git churn, fix commits, Jira bugs, complexity, the change in scope and test weakness; impact from journeys, money, access and personal-data surfaces, and reach. Every factor cites its evidence, and missing evidence is not assessed rather than low. It reads the other skills' reports at their own confidence and allocates 90-minute sessions against a budget. Every area it doesn't test is listed with the reason, for a person to accept. |
| [exploratory-charter-writer-skill](exploratory-charter-writer-skill/) | Writes time-boxed exploratory charters ("Explore target with resources to discover information") from tickets, journey maps, risk plans, diffs and findings. Each cites its sources verbatim and names heuristics, oracles and personas, and they are ranked by impact x uncertainty. It debriefs session notes into draft bugs, questions, obstacles, coverage, metrics and a PROOF summary, quoting the note lines. It never invents missing steps, ignores instructions hidden in notes, redacts personal data, and never calls a feature passed. |
| [bug-reproducer-skill](bug-reproducer-skill/) | Tries to reproduce a bug report (Jira, GitHub or pasted) on the version it names. It triages what the report lacks, finds likely duplicates, runs repeated browser or test attempts with screenshots, logs and traces, varies browser, time zone, data and network, minimises the steps, points at the likely code, rates severity and priority, and writes a red regression test once approved. "Reproduced" needs a recorded run; a failure to reproduce lists every attempt and is never called "not a bug". |
| [api-contract-tester-skill](api-contract-tester-skill/) | Checks a running HTTP API against its OpenAPI 3.0/3.1 or GraphQL contract. It lints the contract, generates examples, boundaries, negatives, a seeded random sample and no-credential requests, sends them in safe mode (reads only unless writes are authorised), and checks status codes, server errors, content types, schemas, headers, auth and GraphQL errors. It saves and re-sends every failing exchange, redacted, rates breaking changes between contract versions, and uses Schemathesis and oasdiff when installed. No findings is "conformance not established", never "conforms". |
| [performance-budget-auditor-skill](performance-budget-auditor-skill/) | Measures web pages in a real browser against a performance budget, as lab data: LCP, CLS and INP (web-vitals), TBT, FCP, TTFB and bytes per type, repeated under fixed mobile and desktop profiles with median and spread. Each overrun is pointed at its cause (the LCP image, the shifting element, the script in long tasks, the slow handler, the largest downloads and unused JavaScript, Server-Timing), rated, and compared with a baseline. It never calls a site fast: within budget means "in the lab; field performance not established". |
| [visual-regression-reviewer-skill](visual-regression-reviewer-skill/) | Compares current screenshots with a baseline (captured itself with DOM boxes, from two directories, or from Playwright screenshot failures) and turns the pixel differences into grouped causes on named elements: layout shift, missing or new element, text, colour, image, font rendering and dynamic content, with crops, measurements and a review order. Noise is labelled, every pixel is accounted for, intent is judged only against a given change description, and nothing is ever approved by the skill. |
| [journey-mapper-skill](journey-mapper-skill/) | Reads a codebase (web, mobile, API) and documents its user journeys as prioritised, evidence-linked Markdown files ready to be turned into tests. Accounts for every route, screen, endpoint, entry point and goal event it finds, and marks every journey `inferred` until a person reviews it. |

Each skill's design and the agreed build order are in [docs/design/](docs/design/).

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
