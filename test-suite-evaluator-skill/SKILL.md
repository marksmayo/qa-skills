---
name: test-suite-evaluator
description: Evaluate an automated test suite for effectiveness, trustworthiness, engineering quality, CI readiness, maintainability, coverage evidence, and operational health. Produces evidence-backed 1–10 scores, confidence ratings, critical caps, and prioritised remediation. Works with standalone test repositories and must not invent application-code coverage.
version: 1.1.0
---

# Test Suite Evaluator

## Mission

Assess how much confidence an automated test suite deserves, not merely how polished its repository looks.

Produce a rigorous, readable assessment that distinguishes:

1. **Testing effectiveness** — whether meaningful defects are likely to be detected.
2. **Suite trustworthiness** — whether green and red results can be believed.
3. **Engineering quality** — whether the suite is maintainable, reviewable, and appropriately designed.
4. **Operational health** — whether CI history demonstrates reliability, speed, and sustained ownership.
5. **Evidence completeness** — how much of the assessment is verified rather than inferred.

The evaluator may inspect files, run safe repository commands, execute tests, inspect reports, and analyse CI configuration when available. Never modify application or test code unless explicitly asked.

## Non-negotiable rules

- Never claim line, branch, condition, or function coverage without source-linked instrumentation or a credible coverage report.
- Never call test-derived behaviour inference “code coverage.” Use **observed behavioural coverage**, **scenario representation**, or **test-intent coverage**.
- Missing evidence is `not_assessed` or `unknown`, not automatically zero.
- Every scored dimension must include evidence, confidence, and reasoning.
- Distinguish product failures, test failures, and environment failures.
- Do not reward abstractions, retries, dashboards, test counts, or coverage percentages without assessing their actual value.
- Penalise false-pass risk more heavily than cosmetic weaknesses.
- Static inspection alone cannot justify a 10/10 overall score.
- Do not reveal secrets found during inspection. Report only file location and secret type.

## Step 1: Establish assessment mode

Determine the strongest supported mode.

### Black-box mode

Available evidence may include tests, fixtures, configuration, CI definitions, execution reports, screenshots, logs, and traces.

Can assess behavioural breadth, scenario depth, inputs, assertions, reliability indicators, maintainability, CI design, and diagnostics.

Cannot verify implementation branch coverage.

### Grey-box mode

Adds one or more of: API specifications, route definitions, GraphQL schemas, protobufs, requirements, tickets, acceptance criteria, architecture documents, database schemas, or explicit risk models.

Can additionally assess specification coverage, interface coverage, requirement traceability, state coverage, role coverage, and integration-boundary coverage.

### White-box mode

Adds application source and preferably runtime coverage or mutation reports.

Can assess implementation-linked coverage, untested modules, branch gaps, mutation effectiveness, and test-to-code alignment.

Record the mode and explain why it applies. The inventory must support it: white-box needs application source, grey-box needs requirements or specifications.

## Step 2: Inventory available evidence

Inspect, where available:

- Test files and framework configuration
- Package manifests and lockfiles
- CI/CD workflows
- Test reports and historical artifacts
- Coverage and mutation reports
- Retry, quarantine, skip, and flaky-test data
- API schemas and requirement sources
- Application source
- Documentation and contribution guidance
- Secrets handling and environment configuration

Create an evidence inventory with status: `available`, `partial`, `missing`, or `not_applicable`. Give each entry a `kind` naming the RUBRIC evidence-completeness item it counts as (`application_source`, `test_source`, `ci_configuration`, `recent_execution`, `historical_ci`, `runtime_coverage`, `mutation_report`, `requirements`, `defect_history`, or `other`), and list all nine RUBRIC items, as `missing` when absent. Evidence completeness is computed from these entries.

Do not block the assessment merely because some evidence is absent. Continue with explicit limitations.

## Step 3: Run safe checks when practical

Prefer read-only or non-destructive commands.

Examples:

- Dependency-aware lint or test commands already defined by the repository
- Test discovery/listing
- Dry runs
- Existing report generation
- Static searches for sleeps, retries, skips, weak assertions, hard-coded state, secrets, or unstable selectors

Do not install large dependencies, access production systems, rotate secrets, delete data, or run destructive suites without explicit permission.

Record commands attempted, exit status, and whether results are representative.

Record the strongest execution evidence as `metadata.execution`:

- `full_run` / `partial_run`: you ran all or part of the suite during the assessment
- `reports_only`: you did not run it, but credible recent reports or CI results exist
- `static_only`: nothing was run and no reports exist
- `not_executable`: discovery or execution was attempted and failed, and no credible reports exist

The first three need recent execution results in the inventory; the last two must not claim them.

## Step 4: Build a behaviour model

From each test or scenario, derive where possible:

- Feature or capability
- Starting state
- Actor or role
- Action
- Expected state transition
- Observable assertions
- Input classes
- Positive, negative, boundary, permission, concurrency, dependency-failure, and recovery dimensions

Aggregate this into behavioural coverage matrices. State clearly that these are test-derived unless matched against specifications or source.

## Step 5: Score the dimensions

Use the rubric in `RUBRIC.md`. Score only supported dimensions.

Core dimensions and default weights:

| Dimension | `id` | Weight |
|---|---|---:|
| Test effectiveness | `effectiveness` | 16 |
| Suite trustworthiness | `trust` | 14 |
| Reliability and determinism | `reliability` | 10 |
| Test design and level selection | `design` | 8 |
| Assertion and oracle quality | `assertions` | 8 |
| Test scope and coverage | `coverage` | 8 |
| Isolation and state management | `isolation` | 7 |
| Maintainability and appropriate reuse | `maintainability` | 7 |
| CI/CD integration | `ci` | 6 |
| Debuggability and reporting | `debuggability` | 4 |
| Performance and feedback speed | `performance` | 3 |
| Test data management | `test_data` | 3 |
| Environment portability and realism | `portability` | 2 |
| Security, privacy, accessibility, compatibility | `security_a11y` | 2 |
| Ownership and operational discipline | `ownership` | 2 |

Weights total 100. List all fifteen dimensions, each once. Reweight only when a dimension is genuinely not applicable: mark it `applicable: false` with a `not_applicable_reason`, give it weight 0 and score null, and rescale the other default weights proportionally so they total 100 again. Do not reweight merely because evidence is missing; keep the default weight, set the score to null, and lower evidence completeness and confidence instead.

For each dimension output:

- `score`: integer 1–10, or null when not assessed (never 0)
- `confidence`: high, medium, or low
- `evidence_level`: verified, specification_derived, test_derived, or unknown
- concise finding
- concrete supporting evidence with file paths or command results
- risks
- recommendations

A scored dimension must carry at least one piece of evidence and cannot have evidence level `unknown`; a dimension the evidence does not support is not assessed (score null), with the finding saying why.

## Step 6: Apply score caps and critical findings

Apply the lowest applicable cap after computing the weighted score.

Default caps (with the `rule` id recorded in `headline.applied_caps`):

- Test effectiveness below 4 → overall maximum 4 (`effectiveness_below_4`)
- Suite trustworthiness below 4 → overall maximum 4 (`trust_below_4`)
- Reliability below 4 → overall maximum 5 (`reliability_below_4`)
- Meaningful assertion/oracle quality below 3 → overall maximum 3 (`assertions_below_3`)
- Suite cannot be discovered or executed and no credible reports exist (`execution: not_executable`) → mark overall as provisional; maximum 5 (`not_executable`)
- Material secret or production-data exposure → critical finding (`category: secret_exposure` or `production_data_exposure`, severity `critical`); maximum 4 until remediated (`secret_or_production_data`)
- Broad error swallowing, globally excessive retries, or systematic false-pass behaviour → critical finding with `category: systematic_false_pass`; maximum 4 (`systematic_false_pass`)
- Static-only assessment (`execution: static_only` or `not_executable`) → maximum 8 (`static_only`)

List every cap whose condition holds, with its limit and an explanation, even when it does not bind because the score is already lower. The overall score is the raw weighted score lowered to the smallest limit. Explain every cap. Never silently modify the score.

## Step 7: Calculate headline results

Report:

- **Raw weighted score**
- **Capped overall score**
- **Testing effectiveness score**
- **Suite trustworthiness score**
- **Engineering quality score**
- **Operational health score**, or `not_assessed`
- **Evidence completeness**, 0–100%
- **Overall confidence**
- **Assessment mode**

Use one decimal place for weighted calculations, while warning that small decimal differences are not materially significant.

How each is computed (see `RUBRIC.md`):

- Raw weighted score: the weighted mean of the scored dimensions. Unassessed dimensions are left out of the mean, never counted as zero.
- Testing effectiveness: the `effectiveness` score. Suite trustworthiness: the `trust` score.
- Engineering quality: the weighted mean of `design`, `maintainability`, `debuggability`, `test_data`, and `portability`.
- Operational health: the weighted mean of `ci`, `performance`, and `ownership`, and only when historical CI outcomes are in the inventory; otherwise null (`not_assessed`).
- A group score with no scored dimension is null.
- Evidence completeness: the RUBRIC points for the inventory, with partial evidence at half credit.
- Overall confidence cannot be `high` without execution evidence.

Rating bands:

- 1.0–2.9: Dysfunctional
- 3.0–4.9: Weak
- 5.0–6.4: Serviceable
- 6.5–7.9: Strong
- 8.0–8.9: Excellent
- 9.0–10.0: Exceptional and evidence-backed

A static-only assessment cannot be labelled “Exceptional.”

## Step 8: Detect gaming and misleading signals

Explicitly check for:

- Test-count inflation
- Meaningless or tautological assertions
- Retries masking first-run failures
- Skips, quarantines, and expected failures without owner or expiry
- Coverage exclusions that hide difficult code
- CI files that do not run on meaningful triggers
- Page objects or helpers created only for appearance
- Generated expected results derived from the system under test
- Snapshots or golden files accepted without review discipline
- Reports that are attractive but diagnostically weak
- Test names implying behaviour not actually asserted

## Step 9: Prioritise recommendations

Do not produce an unranked dump.

Return:

1. **Critical confidence risks**
2. **Three highest-leverage improvements**
3. **Quick wins**
4. **Structural improvements**
5. **Evidence needed for a stronger reassessment**

Each recommendation must include:

- impact: critical, high, medium, low
- effort: small, medium, large
- expected score dimensions affected
- concrete implementation suggestion
- acceptance criteria

## Step 10: Produce beautiful output

Generate both:

1. `evaluation.json`, conforming to `schemas/evaluation.schema.json`
2. A concise human report in Markdown or terminal format

Run `python scripts/validate_evaluation.py evaluation.json` and fix every error before reporting. Besides the schema it recomputes the weights, the raw and group scores, the caps, the overall score and band, and evidence completeness, and rejects scores without evidence and any code-coverage claim made without a runtime coverage report or from test-derived evidence. Write and read the JSON as UTF-8.

When terminal colour is supported, use ANSI colour through `scripts/render_report.py`:

- Green: 8–10
- Cyan: 6.5–7.9
- Yellow: 5–6.4
- Magenta: 3–4.9
- Red: 1–2.9 and critical findings
- Dim text: unknown or not assessed

Never rely on colour alone. Include text labels and symbols.

Recommended report order:

1. Title and assessment mode
2. Overall score card
3. Critical findings
4. Dimension scorecard
5. Behavioural coverage matrix
6. Highest-leverage improvements
7. Evidence limitations
8. Appendix with commands and inspected artifacts

## Required honesty language

Use direct language such as:

- “Instrumented code coverage was not measurable from the supplied evidence.”
- “This gap is inferred from test content, not verified against application source.”
- “Reliability confidence is low because historical CI outcomes were unavailable.”
- “The suite appears green partly because retries or exclusions suppress failures.”

Avoid vague praise. Do not describe a suite as robust, comprehensive, mature, or production-ready unless the evidence supports that statement.
