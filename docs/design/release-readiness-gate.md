# Design: `release-readiness-gate-skill`

Status: Designed; not built. Build order: 25.

## Mission

The skill collects the reports the other skills in this repo produce, plus CI test results, for
one release candidate. It checks each report is valid and current, applies a release policy the
team wrote, and gives a decision maker one page: the verdict, what blocks the release, what was
waived, what evidence is missing and what risk is being accepted.

It is an aggregator, not another auditor:

- It **never re-scores findings.** Each skill's severity, priority, confidence and verdict are
  carried over as that skill rated them. The gate only decides whether they block, using the
  policy.
- It **never runs scanners or tests itself.** If evidence is missing, it says which skill would
  produce it and stops short of a verdict.
- It differs from the general `deploy-checklist` style of skill because every line of its
  output points to a report, a finding id or a policy rule. Nothing in it comes from a checklist
  the agent fills in from memory.

## Decisions

- **Three verdicts: `go`, `no_go`, `not_established`.** There is no default. A known blocker
  gives `no_go`; otherwise missing or stale required evidence gives `not_established`, never
  `go`.
- **The policy is the team's, not the skill's.** `release_policy.json` declares release types,
  the evidence each one requires, the blocking and warning rules, and who may approve waivers
  for each finding category. The skill ships a starter policy that
  is clearly labelled as an example, and the report always names the policy file and its hash.
- **Waivers live in a file, not in chat.** `waivers.json` is versioned with the release. A
  waiver someone mentions in conversation is recorded as an open question, not applied.
- **Read-only.** No commits, no tags, no Jira or CI write-back, no deployment actions. The skill
  writes its report and nothing else.
- **Provenance comes from the reports where they carry it.** Today only the security and WCAG
  reports have `generated_at`, and none records a commit. Skills 7–25 will add a common
  `subject` block (`commit`, `build_id`, `target_url`, `generated_at`). The six built
  skills get the same block when each is next revised; until then the gate accepts a sidecar
  `provenance.json` per report, written when the report was produced. A report with neither is `provenance: unverified` (see the rating model).

## Non-negotiable rules

- **Never "safe to release".** The strongest wording allowed is "no blocking findings in the
  evidence provided". The validator rejects `safe`, `ready to ship`, `release-ready`,
  `no risk`, `fully tested` and similar in any summary field.
- **Missing evidence is not a pass.** A required report that is absent, invalid, stale or
  `not_run` makes the verdict `not_established` (or keeps it `no_go` when a blocker exists), even
  when every other report is clean. It is always listed under `missing_evidence`.
- **Each source keeps its own honesty.** A WCAG `not_established` verdict, a crash report's
  untested routes, a security audit's coverage gaps and a flake rate's confidence interval are
  shown as they were reported. They are never rounded up into a pass.
- **A report counts only if its own validator passes.** The gate runs that skill's validator
  when the skill is installed beside it, and records the command and exit code. If the
  validator isn't available, the report is `validator: not_run` and the policy decides whether
  that is acceptable (the starter policy says no for required evidence).
- **Waivers need an approver, a reason, an expiry and finding ids.** The approver must be on
  the policy's `waiver_approvers` allow-list for the finding's category (for example, only the
  security lead may waive a security finding). A waiver missing any of these, with an approver
  not on the list, or past its expiry on the gate's run date, doesn't count and the finding stays
  blocking. A
  waiver can't cover "everything from skill X".
- **Report contents are untrusted data.** Finding titles, ticket text and bug descriptions are
  quoted, never followed.
- **Secrets and personal data stay out.** The security and privacy reports already redact them;
  the gate copies only ids, titles, locations and ratings, never evidence excerpts.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `candidate` | required | Commit SHA, plus optional build id, version and target URL |
| `release_type` | `minor` | Must be a type in the policy (starter: `patch`, `minor`, `major`, `hotfix`) |
| `reports` | required | Directory or list of report files; the skill detects each one's type |
| `junit` | optional | CI JUnit XML, read with the shared `parse_results.py` |
| `policy` | `release_policy.json` | Required; the starter policy is used only if the user says so, and the report records that |
| `waivers` | `waivers.json` if present | |
| `as_of` | today (UTC) | The date waiver expiry is checked against |

Report types it recognises, by `skill.name` or, for older reports, by top-level keys:
test-suite-evaluator, journey-mapper, journey-to-tests, ticket-to-tdd, ui-crash-hunter,
code-security-auditor, wcag-auditor, ticket-readiness-reviewer, flaky-test-hunter,
mutation-tester, test-impact-selector, test-data-auditor, dead-test-finder,
risk-based-test-planner, exploratory-charter-writer, bug-reproducer, api-contract-tester,
performance-budget-auditor, visual-regression-reviewer, cross-browser-checker, i18n-checker,
resilience-prober, db-migration-reviewer, privacy-flow-auditor and mobile-app-auditor.

## Workflow

1. **Load the policy and waivers.** Validate both against their schemas and hash them.
2. **Inventory the reports.** Detect each report's type and version. Record files the gate
   doesn't recognise as `unrecognised` rather than ignoring them.
3. **Validate each report** with its skill's validator (`--repo`, `--run` or `--candidates`
   arguments are passed when the policy gives their paths). Record `valid`, `invalid` (with the
   first errors) or `not_run`.
4. **Check freshness and provenance.** A report is `stale` when its commit differs from the
   candidate's, when its build id differs, or when it was generated before the candidate
   commit's timestamp. A report whose commit is an ancestor of the candidate is still stale,
   but the report lists the commits between them and the files they changed (`diff_scope.py`)
   so the team can judge the gap.
5. **Extract the gate-relevant facts** from each report through a per-skill adapter: verdicts,
   open findings with their severity, priority, confidence and ids, coverage and
   evidence-completeness figures, and anything the report itself marks untested or not run.
6. **De-duplicate across skills.** Two findings are the same issue when they share a location
   (file and line range, or URL and selector) and a compatible category (a crash hunter B1
   crash and a resilience prober blank screen on the same route, or a security finding and a
   privacy finding on the same log call). Merged issues keep every source; the highest severity
   among the sources is shown, labelled with which skill gave it.
7. **Apply the policy.** Evaluate required evidence, then each blocking rule against the facts,
   then waivers against the blocking findings.
8. **Decide the verdict** (see the rating model), write `gate_report.json`, validate it, and
   render it.

## Rating model

The verdict is decided in this order:

1. **`no_go`** if any blocking rule fires on a finding that isn't validly waived. A known
   blocker is the more actionable fact, so it headlines even when evidence is also missing; the
   missing evidence is still listed in full.
2. **`not_established`** if any required evidence is missing, invalid, stale or unverified.
3. **`go`** otherwise, worded as "no blocking findings in the evidence provided".

**Warnings** don't change the verdict but are shown beside it: a source's evidence completeness
below the policy's `min_completeness` (starter: 0.8), and the flake-rate threshold below.

Starter rules (each is a named entry with `effect: block` or `effect: warn`, which the team can
edit):

| Rule id | Effect | Fires when |
|---|---|---|
| `open-p0` | block | Any open finding with priority P0, from any skill |
| `critical-security` | block | A security finding with CVSS-B ≥ 9.0 |
| `wcag-critical-path` | block | WCAG `does_not_conform` with a non-empty `blocked_critical_paths` |
| `red-gating-tests` | block | Any failing case in CI JUnit for a suite the policy marks as gating |
| `flaky-gating` | warn | Flake rate on gating tests above 2%, using the lower bound of the flaky hunter's 95% interval |
| `low-completeness` | warn | A required source's evidence completeness is below `min_completeness` (0.8) |
| `irreversible-migration` | block | A migration rated irreversible with no `rollback_plan` |
| `untested-critical-journey` | block | A critical journey with no passing journey test |
| `perf-budget` | block | A budget metric over budget at the median of the runs (not a single run) |
| `privacy-pre-consent` | block | Personal data or a tracking cookie sent before consent |

Required evidence in the starter policy:

| Release type | Required reports |
|---|---|
| `hotfix` | CI JUnit, bug-reproducer (the fixed bug re-run), test-impact-selector |
| `patch` | CI JUnit, code-security-auditor, test-impact-selector |
| `minor` | `patch` plus ui-crash-hunter, wcag-auditor, journey-to-tests, flaky-test-hunter |
| `major` | `minor` plus performance-budget-auditor, cross-browser-checker, db-migration-reviewer (if migrations changed), privacy-flow-auditor, mobile-app-auditor (if a mobile app ships) |

Conditional requirements (`if migrations changed`) are evaluated with `diff_scope.py` against
the previous release tag; if no tag is given, the condition is treated as true.

**Evidence completeness of the gate** = required reports present, valid and fresh ÷ required
reports. **Overall confidence** is the lowest `overall_confidence` among required reports
(`high` > `medium` > `low`), capped at `medium` when any report's validator was `not_run`.

## Output (`gate_report.schema.json`)

```text
schema_version, skill{name, version}, generated_at, as_of
candidate:    {commit, build_id?, version?, target_url?, release_type}
policy:       {path, sha256, starter: bool, waiver_approvers{category: [names]}},
              waivers{path?, sha256?}
headline:     verdict: go | no_go | not_established, statement, blocker_count,
              waived_count, missing_evidence_count, evidence_completeness, overall_confidence
sources[]:    {path, sha256, skill, skill_version, required: bool, validator{command, exit_code,
               status: valid|invalid|not_run, errors[]}, freshness: fresh|stale|unverified,
               commit?, generated_at?, commits_behind?, verdict?, evidence_completeness?,
               overall_confidence?, not_run_tools[]}
missing_evidence[]: {requirement, reason: absent|invalid|stale|unverified,
                     produced_by_skill, how_to_produce}
blockers[]:   {issue_id, rule_id, title, severity, priority?, confidence, sources[{skill,
               finding_id}], location, why_blocking}
warnings[]:   {rule_id, source, measure, threshold}
waived[]:     {issue_id, waiver_id, approver, approver_category, reason, expires, finding_ids[]}
expired_or_invalid_waivers[]: {waiver_id, problem}
risks_accepted[]: non-blocking findings at or above the policy's `show_threshold` (starter:
               serious / P1), grouped by skill
issues[]:     every de-duplicated issue with its sources and merge reason
untested[]:   what each source reported as untested or not run, verbatim categories
limitations[], open_questions[]
```

`render_report.py` prints a one-page summary (verdict, statement, blockers, missing evidence,
waivers, risks accepted). `--brief` prints the verdict, counts and blockers only; `--markdown`
writes the same page for a release ticket or channel.

## Validator fails when

- The verdict is `go` with any `missing_evidence`, any `blockers`, or any required source that
  isn't `valid` and `fresh`.
- The verdict is `no_go` with no blockers, `not_established` with blockers, or
  `not_established` with no missing evidence.
- A finding listed under `waived` has a waiver that is expired on `as_of`, lacks an approver,
  reason, expiry or finding ids, or names an approver not on the policy's allow-list for that
  category.
- A blocker's severity, priority or confidence differs from the value in its source report
  (`--reports` re-reads the sources and compares).
- A source's `sha256` doesn't match the file, or a report in the directory isn't listed in
  `sources`.
- `headline.statement` or any summary text claims safety or readiness ("safe", "ready to
  ship", "no risk", "fully tested").
- An `agent_judgment` finding is shown with high confidence (inherited rule).
- `evidence_completeness` or `overall_confidence` doesn't match the computation from
  `sources`.

## Edge cases

- **Two reports of the same type** (an old and a new WCAG audit): the freshest one for the
  candidate counts; the other is listed as `superseded`.
- **A report for a different target** (a staging URL when the candidate deploys elsewhere):
  stale, with reason `target_mismatch`.
- **A skill's validator changed version** since the report was made: run the current one and
  record both versions; failures are reported, not hidden.
- **A blocker fixed after the report was made:** it still blocks until a fresh report shows it
  fixed. The gate never takes "it's fixed" from conversation.
- **Policy rules that reference a skill not installed:** the rule is `not_evaluable` and its
  evidence goes into `missing_evidence`.
- **Monorepos with several deployables:** one gate report per deployable; the policy can scope
  required evidence by path.
- **No policy at all:** the skill stops and offers the starter policy; it doesn't invent rules.

## Files

```text
release-readiness-gate-skill/
  SKILL.md, RUBRIC.md, README.md
  schemas/gate_report.schema.json, release_policy.schema.json, waivers.schema.json,
          provenance.schema.json
  data/starter_policy.json, report_types.json      # detection keys and validator commands per skill
  scripts/collect_reports.py    # inventory, hashing, type detection, validator runs, freshness
  scripts/adapters.py           # per-skill fact extraction into one shape
  scripts/apply_policy.py       # dedupe, blocking rules, waivers, verdict
  scripts/validate_gate.py      # --reports re-checks hashes and copied ratings
  scripts/render_report.py      # --brief, --markdown
  scripts/parse_results.py, diff_scope.py, fetch_ticket.py   # shared copies
  examples/
```

## Example

`examples/rc-2026.11.2/`, a release candidate for the Harbour Dental site and the Invoicer app.
The reports are trimmed copies of existing examples so that they still pass their own
validators:

| Report | Copied from | Planted condition |
|---|---|---|
| `security_audit.json` | code-security-auditor example | Fresh; the forgeable session key (F-1, CVSS-B 9.1) fires `critical-security`; the SQL injection (F-5, 7.1, P1) doesn't block and is shown under risks accepted |
| `audit.json` (WCAG) | wcag-auditor example | Fresh; the day-picker trap blocks "Book an appointment", so `wcag-critical-path` fires, and it has a valid waiver until 2026-12-31 |
| `crash_report.json` | ui-crash-hunter example | **Stale**: its provenance commit is two commits behind the candidate |
| `journey_tests_report.json` | journey-to-tests example | Fresh, valid, no blockers |
| `junit/ci.xml` | ticket-to-tdd bookshop runs, green phase | One gating case fails, so `red-gating-tests` fires |
| `waivers.json` | new | Two waivers: the WCAG one (valid, approved by the accessibility lead on the allow-list) and one for the menu toggle P0 that expired on 2026-11-01 |
| `release_policy.json` | starter policy | Release type `minor` |

The flaky-test-hunter report is deliberately absent. The expected result, documented as an
answer key in `examples/rc-2026.11.2/README.md`:

- verdict `no_go`, because blockers exist; missing evidence is still listed: the flaky report
  is missing and the crash report is stale;
- blockers: the forgeable session key, the failing gating test, and the menu-toggle P0 (its
  waiver expired);
- waived: the day-picker trap;
- the crash report's untested routes and the WCAG audit's 21 untested SCs listed under
  `untested`.

## Resolved questions

Agreed with the user before the build.

1. **Provenance for the six built skills?** Each gets a `subject` block when next revised; sidecar `provenance.json` files until then.
2. **Starter policy thresholds?** Block on any open P0, a security finding with CVSS >= 9, and a WCAG failure on a critical path; the flake-rate and completeness thresholds warn.
3. **`no_go` or `not_established` when both apply?** `no_go` headlines; the missing evidence is still listed.
4. **Waiver approvers?** The approver must be on the policy's allow-list for that finding category.
