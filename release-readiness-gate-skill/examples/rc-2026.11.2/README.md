# Example: release candidate 2026.11.2

A minor release, commit `a7c23de` (build `ci-4821`) of "harbour-suite", a sample monorepo whose history
`make_repo.py` builds with fixed ids: the previous release `v2026.11.1` (`a35dba3`), a fix to the stored
theme (`54f1544`), and a booking-page change (the candidate).

The evidence is other skills' example reports, listed in place by `reports.json`, so each skill's own
validator finds the evidence beside its report. They describe different sample apps; here they stand in
for one candidate's evidence. Each has a provenance file in `provenance/`, because those skills don't
write a `subject` block yet:

| Report | Provenance | Notes |
|---|---|---|
| code-security-auditor `security_audit.example.json` | candidate commit and build | fresh, valid |
| wcag-auditor `audit.example.json` | candidate | fresh, valid; `does_not_conform`, "Book an appointment" blocked |
| ui-crash-hunter `crash_report.example.json` | `a35dba3`, build `ci-4790` | **stale**: two commits behind |
| journey-to-tests `journey_tests_report.example.json` | candidate | fresh, valid |
| test-impact-selector `impact_report.example.json` | candidate | fresh, valid |
| `reports/ci.xml` | candidate | A real pytest JUnit run of the ticket-to-tdd bookshop sample, with the hostname replaced; its six discount tests are the TDD example's red tests and fail |

There is no flaky-test-hunter report, although a minor release requires one. `release_policy.json` is
the starter policy, with `validator_args` added for these reports. `waivers.json` has three waivers.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| Forgeable session key (security F-1, CVSS 9.1, P0) | blocks: `critical-security`, `open-p0` | I-001 |
| The two CI workflow injections (F-2 and F-3, CVSS 9.9, P0) | block | I-002, I-003 |
| Actions pinned to mutable tags (F-14, P2, but CVSS 9 or more) | blocks on `critical-security` only | I-028: the policy decides, not the priority |
| Mobile menu toggle (WCAG F-1, P0); its waiver W-2 expired on 2026-11-01 | blocks; W-2 listed as expired | I-004 |
| Day picker keyboard trap (WCAG F-2, P0, blocks "Book an appointment"); W-1 by the Accessibility lead until 2026-12-31 | waived | I-005 under `waived` (`open-p0` and `wcag-critical-path`) |
| W-3 waives security F-2, signed by the QA lead | rejected: not on the security approver list | listed under `expired_or_invalid_waivers`; F-2 still blocks |
| Crash loop C-1 (P0) in the stale crash report | still blocks (a known blocker blocks until a fresh report shows it fixed) | I-006, marked stale; the report is also missing evidence |
| Six failing gating cases in `tests.test_discount_codes` | one blocker for the suite | I-045 `red-gating-tests` |
| No flaky-test-hunter report | missing evidence, absent | `flakiness`, with how to produce it |
| Crash report two commits behind | missing evidence, stale | `crashes`: commit and build mismatch, 2 commits behind, `site/book.html` and `web/src/prefs.ts` changed since |
| Crash report evidence completeness 0.71 | warning | `low-completeness` |

The verdict is `no_go`, with 7 blockers, 1 waived, 2 missing and 1 warning. Evidence completeness is
0.71 (5 of 7 requirements met). Overall confidence is medium: the WCAG audit's high, capped because
evidence is incomplete. Non-blocking P1 and serious findings are under `risks_accepted`. What each source
didn't test is under `untested`, verbatim: the WCAG audit's 21 untested SCs, the security audit's three
areas not reviewed, and the tools the crash hunter didn't run.

The flaky, migration, performance and privacy rules are `not_evaluable`, because no report for them
was supplied. The policy doesn't require the migration, performance and privacy reports for a minor
release, and no migrations changed since `v2026.11.1`.

```bash
python release-readiness-gate-skill/scripts/validate_gate.py release-readiness-gate-skill/examples/rc-2026.11.2/gate_report.json --reports --recompute release-readiness-gate-skill/examples/rc-2026.11.2/collection.json
python release-readiness-gate-skill/scripts/render_report.py release-readiness-gate-skill/examples/rc-2026.11.2/gate_report.json
```

Run from the repository root. `commands.sh` reruns everything. It needs `HS`, an empty directory for the
sample repository. A rerun changes `collected_at` and `generated_at` only.
