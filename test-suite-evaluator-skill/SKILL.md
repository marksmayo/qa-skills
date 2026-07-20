---
name: test-suite-evaluator
description: Evaluate an automated test suite for effectiveness, trustworthiness, engineering quality, CI readiness, maintainability, coverage evidence, and operational health. Produces evidence-backed 1–10 scores, confidence ratings, critical caps, and prioritised remediation. Works with standalone test repositories and must not invent application-code coverage.
version: 1.0.0
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

Record the mode and explain why it applies.

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

Create an evidence inventory with status: `available`, `partial`, `missing`, or `not_applicable`.

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

| Dimension | Weight |
|---|---:|
| Test effectiveness | 16 |
| Suite trustworthiness | 14 |
| Reliability and determinism | 10 |
| Test design and level selection | 8 |
| Assertion and oracle quality | 8 |
| Test scope and coverage | 8 |
| Isolation and state management | 7 |
| Maintainability and appropriate reuse | 7 |
| CI/CD integration | 6 |
| Debuggability and reporting | 4 |
| Performance and feedback speed | 3 |
| Test data management | 3 |
| Environment portability and realism | 2 |
| Security, privacy, accessibility, compatibility | 2 |
| Ownership and operational discipline | 2 |

Weights total 100. Reweight only when a dimension is genuinely not applicable. Do not reweight merely because evidence is missing; instead lower evidence completeness and confidence.

For each dimension output:

- `score`: integer 1–10, or null when not assessed
- `confidence`: high, medium, or low
- `evidence_level`: verified, specification_derived, test_derived, or unknown
- concise finding
- concrete supporting evidence with file paths or command results
- risks
- recommendations

## Step 6: Apply score caps and critical findings

Apply the lowest applicable cap after computing the weighted score.

Default caps:

- Test effectiveness below 4 → overall maximum 4
- Suite trustworthiness below 4 → overall maximum 4
- Reliability below 4 → overall maximum 5
- Meaningful assertion/oracle quality below 3 → overall maximum 3
- Suite cannot be discovered or executed and no credible reports exist → mark overall as provisional; maximum 5
- Material secret or production-data exposure → critical finding; maximum 4 until remediated
- Broad error swallowing, globally excessive retries, or systematic false-pass behaviour → maximum 4
- Static-only assessment → maximum 8

Explain every cap. Never silently modify the score.

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
