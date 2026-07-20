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

## 4. Test design and level selection

Assess whether behaviours are tested at the cheapest appropriate level, with sensible balance across unit, component, API, integration, contract, and end-to-end layers.

## 5. Assertion and oracle quality

Assess specificity, semantic correctness, side-effect checks, error assertions, failure messages, snapshot discipline, and whether expected values are independently derived.

## 6. Test scope and coverage

Score the strongest evidence available:

- Instrumented implementation coverage
- Specification or interface coverage
- Requirement or risk coverage
- Observed behavioural breadth
- Scenario depth
- Input-space coverage

Never translate inferred scenarios into line or branch percentages.

## 7. Isolation and state management

Assess idempotence, setup/teardown, unique data, database isolation, cleanup after failure, execution-order independence, and parallel safety.

## 8. Maintainability and appropriate reuse

Assess readability, locality, stable interfaces, fixtures/builders, selector quality, configuration, duplication, abstraction abuse, and blast radius of change.

High reuse is not inherently good. Penalise indirection that obscures intent.

## 9. CI/CD integration

Assess triggers, PR gating, sharding, caching, artifact retention, environment provisioning, secrets, scheduled suites, exit codes, quarantine handling, and deployment gates.

A YAML file alone does not demonstrate CI readiness.

## 10. Debuggability and reporting

Assess assertion messages, logs, traces, screenshots, network evidence, step reporting, reproducibility, and classification of product/test/environment failures.

## 11. Performance and feedback speed

Assess total runtime, time to first failure, slow-test concentration, setup overhead, parallel efficiency, and fitness for PR versus scheduled execution.

## 12. Test data management

Assess factories, uniqueness, cleanup, boundary data, versioning, sensitive data handling, and reproducibility.

## 13. Environment portability and realism

Assess local/CI parity, dependency pinning, documented prerequisites, containerisation, configuration, production similarity, and hidden assumptions.

## 14. Security, privacy, accessibility, compatibility

Apply relevant subdomains only. Inspect both product testing and safety of the test infrastructure itself.

## 15. Ownership and operational discipline

Assess clear owners, review standards, skip/quarantine expiry, triage, health metrics, dependency maintenance, and inclusion of tests in delivery practices.

## Evidence levels

- **verified:** direct runtime, source-linked, or historical evidence
- **specification_derived:** matched against authoritative requirements or interfaces
- **test_derived:** inferred from tests and configuration
- **unknown:** unsupported by supplied evidence

## Confidence

- **High:** multiple direct, consistent evidence sources
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
