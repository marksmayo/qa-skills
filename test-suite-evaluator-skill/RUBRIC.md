# Scoring Rubric

Use integer dimension scores. Half-points may be used only in the final weighted calculation.

## Universal anchors

| Score | Meaning |
|---:|---|
| 1 | Fundamentally broken, misleading, or absent |
| 2 | Severe weaknesses; little confidence |
| 3 | Major gaps; frequent false confidence likely |
| 4 | Weak but partially useful |
| 5 | Basic and serviceable; material gaps remain |
| 6 | Competent; generally useful but inconsistent |
| 7 | Strong; weaknesses are contained and visible |
| 8 | Very strong; dependable with minor gaps |
| 9 | Excellent and supported by operational evidence |
| 10 | Exceptional, sustained, measurable, and hard to game |

## 1. Test effectiveness

Consider meaningful behaviours, critical risks, negative and failure paths, state transitions, side effects, regression value, and likely defect detection.

- **1–2:** Tests mostly execute code or UI steps; little outcome verification.
- **3–4:** Happy-path bias, major critical behaviours missing, weak regression value.
- **5–6:** Core paths covered; uneven negative, boundary, and failure-path depth.
- **7–8:** Risk-oriented scenarios with meaningful outcomes and credible regressions.
- **9–10:** Demonstrated defect yield, mutation strength, and low escape rates.

## 2. Suite trustworthiness

Consider false-pass risk, false-failure risk, oracle quality, retry misuse, environmental realism, suppression debt, and diagnostic clarity.

- **1–2:** Green results are actively misleading.
- **3–4:** Material swallowed failures, weak oracles, or broad retries.
- **5–6:** Mostly believable, but known blind spots or unstable dependencies exist.
- **7–8:** Results are dependable and failure types are distinguishable.
- **9–10:** Historical data confirms strong predictive value and sustained health.

## 3. Reliability and determinism

Consider flake history, arbitrary sleeps, races, order dependence, clocks, randomness, shared state, parallel safety, and external dependencies.

These anchors decide the reliability cap (below 4 caps the overall at 5), so pick between 3 and 4 by them, not by feel.

- **1–2:** Results change from run to run for most of the suite, or the suite cannot complete without manual intervention.
- **3:** Any of: the suite was red in every assessed run for non-product reasons (timeouts, order dependence, shared state); known flaky tests with no quarantine, owner or tracking; fixed sleeps are the main synchronisation across many tests; tests depend on execution order or on state left by other tests.
- **4:** Nondeterminism is present but contained: at least one complete run was green or the flaky tests are few, identified and tracked; sleeps and shared state are local.
- **5–6:** Repeated runs agree apart from rare, explained failures; time, randomness and external dependencies are mostly controlled.
- **7–8:** Repeated runs agree; clocks, randomness and network are controlled; parallel and random-order runs pass.
- **9–10:** Historical CI shows a sustained low flake rate, with quarantine and retry data to prove it.

## 4. Test design and level selection

Assess whether behaviours are tested at the cheapest appropriate level, with sensible balance across unit, component, API, integration, contract, and end-to-end layers.

- **1–2:** One level only, and the wrong one for most behaviours (everything through the browser, or only trivial unit tests).
- **3–4:** Heavy imbalance; costly levels test rules a cheaper level could, or integration boundaries are never exercised.
- **5–6:** Sensible layers exist but overlap or leave gaps (no contract or integration tests at real boundaries).
- **7–8:** Each behaviour tested at the cheapest level that can catch its defects; a few end-to-end journeys guard critical flows.
- **9–10:** Deliberate, documented test strategy, and evidence (escape analysis, mutation) that the levels catch what they target.

## 5. Assertion and oracle quality

Assess specificity, semantic correctness, side-effect checks, error assertions, failure messages, snapshot discipline, and whether expected values are independently derived.

- **1–2:** Assertion-free tests, tautologies, or assertions on mocks only.
- **3–4:** Mostly existence, visibility, status-code or snapshot-only checks; outcomes and side effects unchecked.
- **5–6:** Specific assertions on main outcomes; side effects, error content and negative cases checked unevenly.
- **7–8:** Outcomes, side effects and errors asserted with independently derived expected values; snapshots reviewed and small.
- **9–10:** As 7–8, plus mutation or fault-injection evidence that the oracles detect wrong results.

## 6. Test scope and coverage

Score the strongest evidence available:

- Instrumented implementation coverage
- Specification or interface coverage
- Requirement or risk coverage
- Observed behavioural breadth
- Scenario depth
- Input-space coverage

Never translate inferred scenarios into line or branch percentages.

- **1–2:** Large parts of the product have no tests at all.
- **3–4:** Major features or critical interfaces (authentication, payments, permissions, data deletion) untested.
- **5–6:** Core features and most interfaces tested; known untested critical interfaces, or measured coverage with large gaps in risky modules.
- **7–8:** Critical interfaces and risks all tested; gaps are in low-risk areas and known.
- **9–10:** Coverage measured against source or specification and risk, with no untested critical path.

## 7. Isolation and state management

Assess idempotence, setup/teardown, unique data, database isolation, cleanup after failure, execution-order independence, and parallel safety.

- **1–2:** Tests share mutable state or a live shared environment and break each other.
- **3–4:** Shared users, records or databases with no cleanup; order dependence likely; tests reach real external services.
- **5–6:** Mostly isolated; some shared fixtures or cleanup gaps; external services stubbed but not all.
- **7–8:** Unique data per test, cleanup on failure, no outbound network beyond loopback (verified with a network guard where possible), parallel-safe.
- **9–10:** As 7–8, proven by random-order and parallel runs over time.

## 8. Maintainability and appropriate reuse

Assess readability, locality, stable interfaces, fixtures/builders, selector quality, configuration, duplication, abstraction abuse, and blast radius of change.

High reuse is not inherently good. Penalise indirection that obscures intent.

- **1–2:** Tests are copy-pasted scripts or hidden behind abstractions nobody can follow.
- **3–4:** Heavy duplication or deep indirection; brittle selectors; small product changes break many tests.
- **5–6:** Readable with some duplication or over-abstraction; mixed selector quality.
- **7–8:** Intent is clear at the test site; fixtures and builders reduce noise without hiding behaviour; stable selectors.
- **9–10:** As 7–8, with a low and measured cost of change.

## 9. CI/CD integration

Assess triggers, PR gating, sharding, caching, artifact retention, environment provisioning, secrets, scheduled suites, exit codes, quarantine handling, and deployment gates.

A YAML file alone does not demonstrate CI readiness.

- **1–2:** No CI runs the tests, or CI ignores their exit code.
- **3–4:** CI runs only part of the suite (for example not the end-to-end layer), on weak triggers, or not as a required check; configured thresholds never run.
- **5–6:** CI runs the suite on pull requests; gating, artefacts or provisioning incomplete.
- **7–8:** Required checks on pull requests, realistic provisioning, artefacts on failure, scheduled suites for slow layers.
- **9–10:** As 7–8, with run history showing the gates are enforced.

## 10. Debuggability and reporting

Assess assertion messages, logs, traces, screenshots, network evidence, step reporting, reproducibility, and classification of product/test/environment failures.

- **1–2:** Failures give little more than a non-zero exit code.
- **3–4:** Generic assertion messages; no logs, traces or screenshots kept from CI.
- **5–6:** Clear assertion diffs; some artefacts kept, but not reliably on failure.
- **7–8:** Failures name the behaviour, and artefacts (traces, screenshots, logs, JUnit) are kept on failure; product, test and environment failures can be told apart.
- **9–10:** As 7–8, plus failure classification and trends tracked over time.

## 11. Performance and feedback speed

Assess total runtime, time to first failure, slow-test concentration, setup overhead, parallel efficiency, and fitness for PR versus scheduled execution.

Score from measured runtimes of the suite, never from your own tool limits: a command that hit your timeout says nothing about the suite. If you could not measure it, the dimension is not assessed.

- **1–2:** PR feedback takes more than 30 minutes, or the suite is too slow to run before merging at all.
- **3–4:** PR feedback takes 15 to 30 minutes, or a hook runs a multi-minute suite on every commit; slow tests are concentrated and unaddressed.
- **5–6:** PR feedback in 10 to 15 minutes; some slow-test concentration or setup overhead.
- **7–8:** PR feedback under 10 minutes (unit layer under 5); slow suites scheduled separately; parallelism used.
- **9–10:** Fast feedback sustained over time, with timing data and slow-test budgets enforced.

Judge against the suite's purpose: a nightly end-to-end suite can take longer than a PR gate.

## 12. Test data management

Assess factories, uniqueness, cleanup, boundary data, versioning, sensitive data handling, and reproducibility.

- **1–2:** Tests rely on whatever data the shared environment holds, or on copies of production data.
- **3–4:** Hard-coded literals tied to an environment's data; no factories; no cleanup.
- **5–6:** Some builders or fixtures; boundary data partial; cleanup uneven.
- **7–8:** Factories or builders create what each test needs; boundary and invalid data covered; synthetic data only.
- **9–10:** As 7–8, with versioned seed data and checks that keep it valid.

## 13. Environment portability and realism

Assess local/CI parity, dependency pinning, documented prerequisites, containerisation, configuration, production similarity, and hidden assumptions.

- **1–2:** Runs only on one person's machine or one shared environment.
- **3–4:** Undocumented prerequisites; hidden assumptions (time zone, locale, services) break runs elsewhere.
- **5–6:** Documented and runnable locally with effort; CI and local runs differ in important ways.
- **7–8:** Pinned dependencies, scripted or containerised setup, CI and local parity; realistic dependencies (a real database in a container) where it matters.
- **9–10:** As 7–8, proven by clean runs on fresh machines and in CI.

## 14. Security, privacy, accessibility, compatibility

Apply relevant subdomains only. Inspect both product testing and safety of the test infrastructure itself.

- **1–2:** Relevant subdomains are untested and the test infrastructure itself leaks secrets or uses real personal data.
- **3–4:** Relevant subdomains largely untested; test infrastructure handles secrets carelessly.
- **5–6:** Some authorisation, input-validation, accessibility or browser checks; gaps in relevant areas.
- **7–8:** Relevant subdomains have targeted tests (authorisation negatives, accessibility checks, supported browsers); test secrets are fake or managed.
- **9–10:** As 7–8, run continuously with tracked results.

## 15. Ownership and operational discipline

Assess clear owners, review standards, skip/quarantine expiry, triage, health metrics, dependency maintenance, and inclusion of tests in delivery practices.

- **1–2:** No owner; failing or skipped tests accumulate unattended.
- **3–4:** Skips and quarantines without owner or expiry; red builds tolerated.
- **5–6:** Owners and review exist; triage informal; some stale skips.
- **7–8:** Clear owners, skip expiry, prompt triage, dependency updates kept current.
- **9–10:** Health metrics tracked and acted on over time.

## Evidence levels

- **verified:** direct runtime, source-linked, or historical evidence
- **specification_derived:** matched against authoritative requirements or interfaces
- **test_derived:** inferred from tests and configuration
- **mixed:** a dimension whose evidence items have different levels; each item then states its own level
- **unknown:** unsupported by supplied evidence (the level of every unassessed dimension)

A dimension's level must hold for every piece of its evidence. Use `mixed` rather than the strongest level present.

## Confidence

- **High:** multiple direct, consistent evidence sources (at least two evidence items, not `test_derived` alone)
- **Medium:** credible but incomplete evidence
- **Low:** mostly static inference, small sample, or contradictory evidence

## Evidence completeness calculation

Suggested point values:

- Application source: 15
- Test source: 15
- CI configuration: 10
- Recent execution results: 15
- Historical flake/retry/runtime data: 15
- Runtime coverage report: 10
- Mutation report: 5
- Requirements/specifications: 10
- Defect/incident history: 5

Count only evidence that is usable and relevant. Partial evidence receives half credit.

Record each item in the evidence inventory with its `kind` (`application_source`, `test_source`, `ci_configuration`, `recent_execution`, `historical_ci`, `runtime_coverage`, `mutation_report`, `requirements`, `defect_history`). Evidence completeness is the sum of their points: `available` scores full points, `partial` half, and `missing` or `not_applicable` none. Entries of kind `other` score nothing. The validator recomputes it, allowing one point either way for rounding. Report it to one decimal (72.5%); the validator and the renderer both print one decimal.

## What counts as each evidence kind

- **application_source:** the code under test, readable. `partial` when only some services or packages are available.
- **test_source:** the test code and its configuration.
- **ci_configuration:** the pipeline definitions that run the tests (workflow files, pipeline YAML). `partial` when a referenced template or required-check setting is unavailable.
- **recent_execution:** results from the current code: your own runs (origin `assessor`), or CI or local reports for the assessed commit or one close to it. `partial` when only some layers or tests ran. A committed log from months ago is `other`.
- **historical_ci:** outcomes of many CI runs over time (pass/fail per run, retries, durations), from the CI system or exported reports. Your own repeated runs are never historical CI, however many: they are recent execution and may support a flakiness observation for reliability. `partial` when fewer than about ten runs or a short window is available.
- **runtime_coverage:** a coverage report produced by instrumentation for the assessed code (coverage.py, V8/Istanbul, JaCoCo), from your run or a recent CI artefact. Hand-written summaries of coverage percentages are `other` and never support coverage claims.
- **mutation_report:** output of a mutation tool (Stryker, mutmut, PIT) for the assessed code.
- **requirements:** authoritative statements of intended behaviour: specifications (OpenAPI, GraphQL schema, protobuf), tickets with acceptance criteria, product or API documentation the team maintains. `available` when they cover the main features; `partial` when they cover only some, or when the only specification is generated from the code at runtime (it describes what the code does, not what it should do).
- **defect_history:** records of defects that escaped or were found: an issue tracker's bugs, incident reports, post-mortems, or a security audit report listing findings. Commit messages alone (`fix:` commits) are `partial` at most, because they show that something was fixed but not what failed or how it was found.
- **other:** anything else worth listing: stale logs, hand-written summaries, dashboards without data. It scores no points.

## Weighted and group scores

Dimension ids and default weights are in SKILL.md Step 5. All fifteen dimensions appear in every evaluation.

- **Raw weighted score:** `sum(score × weight) / sum(weight)` over the scored dimensions, to one decimal. Unassessed dimensions (score null) keep their default weight but are left out of the mean; they never count as zero.
- **Re-weighting:** only for a dimension marked `applicable: false` with a reason. Its weight becomes 0 and the other defaults are rescaled proportionally (`default × 100 / sum of applicable defaults`).
- **Testing effectiveness:** the `effectiveness` score.
- **Suite trustworthiness:** the `trust` score.
- **Engineering quality:** the weighted mean of `design`, `maintainability`, `debuggability`, `test_data`, and `portability`.
- **Operational health:** the weighted mean of `ci`, `performance`, and `ownership`, only when historical CI outcomes (`historical_ci`) are available or partial. Otherwise it is null (not assessed), because a CI file alone shows no operational history.
- **Overall score:** the raw weighted score lowered to the smallest limit among the caps that apply (SKILL.md Step 6). Every cap whose condition holds is listed with its rule, limit, and explanation, whether or not it binds.
- **Rating band:** taken from the overall score: below 3.0 Dysfunctional, below 5.0 Weak, below 6.5 Serviceable, below 8.0 Strong, below 9.0 Excellent, otherwise Exceptional.

## Coverage language

The words "code", "line", "branch", "statement", "condition", "function", "instrumented", "implementation", or "source" coverage, coverage percentages, and shares of the code ("90% of the code", "83% of lines", "statements 82%") may appear only when a runtime coverage report is in the inventory and, inside a dimension, only from verified evidence: an evidence item whose level is `verified`, a finding, risk or recommendation of a `verified` dimension, or, in a `mixed` dimension, a sentence that cites a verified evidence item as `[id]`.

- A stated percentage is a claim whatever else the sentence says. "No tests for 2FA, so branch coverage is 83%" is a claim; the "No" belongs to another clause.
- A named measure without a number is allowed when its own clause says it was not available ("Instrumented code coverage was not measurable from the supplied evidence").
- A configured threshold or target is not a claim that it was reached ("vitest.config.ts sets an 80% coverage threshold").
- File names, identifiers and backticked text are not claims (`service-form-branch-coverage.test.tsx`).
- Interface coverage ("121 of 127 route handlers are imported by a test") is not code coverage; record it in `interface_coverage`.
