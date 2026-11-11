# Rubric: release readiness gate

## Verdict

Decided in this order:

1. **`no_go`**: a blocking rule fires on an issue that no valid waiver covers.
2. **`not_established`**: any required evidence is `absent`, `invalid`, `validator_not_run` (unless the
   policy accepts it), `stale` or `unverified`.
3. **`go`**: otherwise, worded "no blocking findings in the evidence provided".

Warnings sit beside the verdict and never change it.

## Which findings count

| Source | Findings used | Required evidence met |
|---|---|---|
| Valid and fresh | yes | yes |
| Valid, stale or unverified | yes (a known blocker blocks until a fresh report shows it fixed) | no |
| Validator not run | yes | only if `validator_not_run_acceptable` |
| Invalid | no | no |
| Superseded (an older report of the same type) | no | - |

## Starter rules (`data/starter_policy.json`)

| Rule | Effect | Fires when |
|---|---|---|
| `open-p0` | block | Any open finding with priority P0, from any skill |
| `critical-security` | block | A code-security-auditor finding with CVSS-B >= 9.0 |
| `wcag-critical-path` | block | A WCAG or mobile audit that `does_not_conform`, with `blocked_critical_paths`, on a finding with `blocks_critical_path` |
| `red-gating-tests` | block | A failing or erroring JUnit case matching the `gating` patterns (one issue per suite) |
| `flaky-gating` | warn | A gating test's normal-run flake rate above 2%, by the lower bound of the flaky hunter's 95% interval |
| `low-completeness` | warn | A required source's evidence completeness below `min_completeness` (0.8), or reported as `partial` |
| `irreversible-migration` | block | A migration finding in category `irreversible` with no `rollback_plan` |
| `untested-critical-journey` | block | A journey with a critical priority (starter: P0) whose main path has no passing test |
| `perf-budget` | block | A performance finding whose worst median is over its budget |
| `privacy-pre-consent` | block | `tracking_before_consent`, or personal data sent in the `before` consent state |

A rule whose skill has no counted report is `not_evaluable`.

## Required evidence (starter)

| Release type | Required |
|---|---|
| `hotfix` | CI JUnit, bug-reproducer, test-impact-selector |
| `patch` | CI JUnit, code-security-auditor, test-impact-selector |
| `minor` | `patch` plus ui-crash-hunter, wcag-auditor, journey-to-tests, flaky-test-hunter |
| `major` | `minor` plus performance-budget-auditor, cross-browser-checker, privacy-flow-auditor, db-migration-reviewer (if migrations changed), mobile-app-auditor (if a mobile app ships) |

Conditions are evaluated from the files changed since `previous_tag` (needs a checkout); with no tag or no
checkout they are true.

## Freshness

`fresh` when provenance names the candidate commit (a 7+ character prefix is accepted), and build id and
target URL match where both sides give one. Otherwise `stale` with reasons (`commit_mismatch`,
`build_mismatch`, `target_mismatch`, `generated_before_commit`), or `unverified` with no provenance.

## Waivers

Valid only with finding ids (`skill:finding_id`, no wildcards, no whole skills), an approver on
`waiver_approvers[<category>]` for every finding it names (categories per skill in
`data/report_types.json`), a reason, and `expires >= as_of`. An issue is waived only when a valid waiver
names every finding merged into it. Invalid or expired waivers are listed with their problems.

## De-duplication

Two findings from different skills are one issue when their locations match (same file within 3 lines, or
the same URL and selector) and their kinds are compatible: crash with crash, API or functional; security
with privacy or test data; accessibility with accessibility; performance with performance. The issue shows
the highest severity among its sources and names the skill that gave it, and the best priority.

## Completeness and confidence

- **Evidence completeness** = requirements met / requirements in force (2 decimals).
- **Overall confidence** = the lowest `overall_confidence` among required sources that report one
  (medium if none does); at most medium when completeness is under 1 or any required validator was not run;
  low under 0.5.

## Risks accepted

Non-blocking, unwaived issues at or above `show_threshold` (starter: priority P1 or severity serious/high),
grouped by skill.
