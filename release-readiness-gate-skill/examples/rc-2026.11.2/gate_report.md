# Release readiness gate

Candidate `a7c23dea801a` 2026.11.2 (build ci-4821), release type minor; policy `release-readiness-gate-skill/examples/rc-2026.11.2/release_policy.json` (the starter policy, an example), as of 2026-11-12.

Verdict: no_go

No go: 7 blocking issue(s) in the evidence provided, 1 waived. Required evidence is also incomplete (ui-crash-hunter stale; flaky-test-hunter absent).

7 blocker(s), 1 waived, 2 missing evidence, 1 warning(s). Evidence completeness 0.71, overall confidence medium.

## Blockers

| Issue | Rules | Source | Priority | Severity | Confidence | Title |
|---|---|---|---|---|---|---|
| I-001 | critical-security, open-p0 | code-security-auditor F-1 | P0 | critical | confirmed | Hard-coded session signing key lets anyone forge a login as any user |
| I-002 | critical-security, open-p0 | code-security-auditor F-2 | P0 | critical | traced | pull_request_target workflow checks out and runs untrusted pull-request code with a write token |
| I-003 | critical-security, open-p0 | code-security-auditor F-3 | P0 | critical | traced | Pull-request title is expanded into a shell script in the privileged workflow |
| I-004 | open-p0 | wcag-auditor F-1 | P0 | critical | medium | Mobile menu button cannot be reached or operated by keyboard |
| I-006 | open-p0 | ui-crash-hunter C-1 (stale report) | P0 | tier B1 | reproduced | Theme saved by version 0.2.0 crashes the app on every launch |
| I-028 | critical-security | code-security-auditor F-14 | P2 | critical | traced | GitHub Actions pinned to mutable tags, including a third-party action that receives the token |
| I-045 | red-gating-tests | ci-junit tests.test_discount_codes | - | - | observed | 6 failing case(s) in tests.test_discount_codes |

## Missing evidence

- **crashes** (ui-crash-hunter): stale - commit_mismatch; build_mismatch; 2 commit(s) behind the candidate. Run the ui-crash-hunter skill against the candidate commit, validate it with its own validator, and save it with provenance (a subject block or a sidecar provenance.json naming the commit and build).
- **flakiness** (flaky-test-hunter): absent. Run the flaky-test-hunter skill against the candidate commit, validate it with its own validator, and save it with provenance (a subject block or a sidecar provenance.json naming the commit and build).

## Waivers

- I-005 waived by W-1 (Accessibility lead, accessibility), expires 2026-12-31: Keyboard focus is trapped in the preferred-day picker. Reason: The booking day picker trap is fixed in the new picker shipping on 2026-11-30; until then the booking page links to phone booking above the picker.
- Not applied W-2 (Accessibility lead): expired on 2026-11-01 (as of 2026-11-12)
- Not applied W-3 (QA lead): QA lead is not on the waiver_approvers list for security (code-security-auditor:F-2)

## Warnings

- low-completeness: ui-crash-hunter measured 0.71 against 0.8

## Risks accepted (non-blocking, at or above serious or P1)

- **wcag-auditor**: F-10 P1 critical Mobile number field has no accessible name; F-11 P1 critical Full name field is labelled only by its placeholder; F-12 P1 critical Home page carousel moves automatically with no way to pause it; F-9 P1 critical Form errors are shown only by a red border; F-3 P1 serious Logo link has no accessible name on every page; F-4 P1 serious Navigation links have insufficient text contrast; F-5 P1 serious Navigation links show no focus indicator; F-6 P1 serious Footer links are hidden under the sticky call bar when focused; F-7 P1 serious Button text is clipped with larger text spacing and at 200% zoom; F-8 P1 moderate Footer address text has insufficient contrast; F-13 P2 serious Contact page has an empty title; F-14 P2 serious Services page declares German but is written in English; F-15 P2 serious Promotion banner forces horizontal scrolling at 320 px
- **code-security-auditor**: F-10 P1 high Invoice import parses YAML with the unsafe loader (code execution); F-11 P1 high Password reset tokens are short, non-cryptographic and never expire; F-12 P1 high Werkzeug 3.0.1 multipart parser can be made to exhaust memory (CVE-202; F-4 P1 high Report title is compiled as a Jinja template (server-side template inj; F-5 P1 high SQL injection in invoice search returns other users' invoices; F-6 P1 high Blind SQL injection through the invoice list sort parameter; F-7 P1 high Any signed-in user can read any invoice by id (IDOR); F-8 P1 high Path traversal in attachment downloads reads any file the app can read; F-9 P1 medium Open redirect after sign-in through the next parameter; F-16 P2 high run.py starts the Werkzeug debugger on all interfaces
- **ui-crash-hunter**: C-2 P1 An imported recipe (no author) breaks the recipe list; C-3 P1 A recipe with no ratings yet cannot be opened

## Untested or not run (as each source reported it)

- code-security-auditor, areas not reviewed: git history (secrets), deployment and infrastructure, transitive dependencies outside requirements.txt resolution (3)
- code-security-auditor, tools not run: gitleaks, osv-scanner, trivy (3)
- wcag-auditor, SCs untested: 1.3.2, 1.3.3, 1.3.4, 1.4.5, 1.4.11, 2.1.4, 2.2.1, 2.3.1, 2.4.1, 2.4.3, 2.4.5, 2.5.1 ... (21)
- wcag-auditor, tools not run: vnu (1)
- ui-crash-hunter, tools not applicable: Android Lint / detekt, SwiftLint, dart analyze (3)
- ui-crash-hunter, tools not run: eslint (strict overlay), semgrep (2)
- journey-to-tests, tests not run or blocked: ACC-V2 (1)
- journey-to-tests, testability gaps: GAP-001 (1)
- test-impact-selector, sources not run: framework_tools (1)

## Sources

| Source | Skill | Validator | Freshness | Required |
|---|---|---|---|---|
| `code-security-auditor-skill/examples/security_audit.example.json` | code-security-auditor | valid | fresh | yes |
| `wcag-auditor-skill/examples/audit.example.json` | wcag-auditor | valid | fresh | yes |
| `ui-crash-hunter-skill/examples/crash_report.example.json` | ui-crash-hunter | valid | stale (commit_mismatch, build_mismatch; 2 commit(s) behind) | yes |
| `journey-to-tests-skill/examples/journey_tests_report.example.json` | journey-to-tests | valid | fresh | yes |
| `test-impact-selector-skill/examples/impact_report.example.json` | test-impact-selector | valid | fresh | yes |
| `release-readiness-gate-skill/examples/rc-2026.11.2/reports/ci.xml` | ci-junit | valid | fresh | yes |

## Limitations

- The gate only reads reports: it ran no scanner or test, and every rating is the source skill's.
- The policy is the starter policy shipped with the skill (an example), not one the team wrote.
