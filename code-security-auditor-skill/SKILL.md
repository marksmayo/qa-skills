---
name: code-security-auditor
description: Audit a codebase for security weaknesses with open-source scanners (Semgrep OSS, Bandit, gosec, Brakeman, gitleaks, osv-scanner, pip-audit, npm audit, zizmor, Trivy, Checkov and more) plus a deep manual review, trace each finding from attacker-controlled input to the dangerous operation, rate it with CVSS v4.0, and write safe reproduction steps, a fix and a regression test. Optionally confirms findings with canary-only proofs of concept against localhost or a test environment the user names. Every candidate is triaged, every tool's status is recorded, and the report never calls the code secure.
version: 1.0.0
---

# Code Security Auditor

## Mission

Find the security weaknesses in a codebase that an attacker could actually use, prove as many as
can be proved safely, and give the team what it needs to fix each one: where it is, how the input
gets there, how bad it is, how to reproduce it, how to fix it and how to stop it coming back.

What sets this apart from asking for a quick review:

- **Several tools, not just reading.** Semgrep, language-specific analysers, secret and dependency
  scanners and CI auditors all run where they apply, and each one's output is triaged.
- **A path for every finding.** A tool hit is a candidate. It becomes a finding when you trace a
  path from a source the attacker controls to the sink, through every sanitiser on the way.
- **Ratings that can be checked.** CVSS v4.0 vectors are recomputed by the validator; priority
  combines severity, confidence and exposure.
- **Repro steps and a regression test** for every finding, in the repository's own test framework.

## Non-negotiable rules

1. **"No findings" never means "secure".** The report states coverage: languages, files, entry
   points, tools and review areas, and what was not covered. Never write that the code is secure,
   safe or free of vulnerabilities.
2. **A tool hit is a candidate until a path is traced.** Confidence levels:
   - `confirmed`: a proof of concept ran against a local or test instance and its proof passed.
   - `traced`: a complete source -> sink path with no effective sanitiser, read in the code.
   - `possible`: the weakness is real but part of the path is not shown (say which part in `path.gap`).
   Informational issues go in `observations`, not findings.
3. **Every candidate is triaged.** Promote it to a finding, record it as an observation, or dismiss
   it with a reason someone else can check (the guard and its line, the invariant, or why the input
   is not attacker-controlled). "False positive" on its own is not a reason.
4. **Dynamic verification needs permission and a safe target.** Only after the user has explicitly
   agreed, and only against localhost or a test environment they named. Never production unless
   they ask for it in writing; record their words in `verification.production_written_permission`.
5. **Proofs of concept are canaries.** Read-only, boolean or error-based SQL injection; computed
   markers (`{{7*191}}` -> `1337`); reserved domains (`canary.example`); the app's own harmless
   files. Never: destructive statements, writes to real data, time-based or resource-exhaustion
   payloads, code-execution primitives, reverse shells, persistence, cloud metadata addresses, or
   sending anything to a host outside the target. `run_poc.py` enforces this before sending and the
   validator checks it again. If the only proof needs something forbidden, the finding stays
   `traced` and says why.
6. **Secrets are never reproduced.** Report the type, file, line, a fingerprint and whether it is in
   git history. Never the value: not in the report, the PoC files, the evidence or your messages.
   Snippets stop before the value.
7. **The report is sensitive.** It contains working exploit details. Write it, the evidence and the
   tool outputs outside the repository (or to a gitignored directory), never commit them, and keep
   the `notice` header that says so.
8. **Look, don't change.** Don't modify the source, install anything into the repository, or commit.
   Fixes and tests go in the report; a fixed copy for checking them goes outside the repository.

## Inputs

- The repository (required), and optionally a scope (directories, services) and exclusions.
- Whether dynamic verification is allowed, and the target: a local start command or a test URL.
  Ask; don't assume. Without it, the best confidence is `traced`.
- Test accounts for verification (two users of the same role, plus an admin if there is one, make
  access-control checks possible).
- Optional: a journey map, threat model or previous report.

## Workflow

### 1. Recon: map the attack surface

```bash
python scripts/recon.py <repo> --out <out>/recon.json
```

It finds the stack, entry points (HTTP routes, GraphQL operations, webhooks, queue consumers, CLI
commands, mobile deep links and exported components), a first guess at each route's auth
requirement, data stores, outbound calls, secret configuration, dependency manifests, CI workflows
and review leads. Then read the code to confirm it: add entry points the patterns missed (CI
workflows, dynamically registered routes), correct auth guesses (`unknown` is not `public`), and
write the trust boundaries and the auth model (authentication, sessions, authorisation). The map
decides where the deep review goes; record it in `recon`.

### 2. Run the scanners

```bash
python scripts/run_scanners.py <repo> --out-dir <out> [--offline]
```

It runs every free, open-source tool that applies and is installed, writes their outputs to
`<out>` (refused if inside the repository) and records each tool as `ran`, `not_run` (with why),
`errored` or `not_applicable` in `<out>/scanners.json`. Copy those statuses into the report's
`tools`. Use only tools whose licences allow scanning closed-source code without a paid account:
CodeQL is excluded, and so are Semgrep rules or features that need a login. Ask before installing
anything; a missing tool is recorded, not silently skipped.

| Area | Tools |
|---|---|
| Multi-language SAST | Semgrep OSS with `p/default` and `p/owasp-top-ten` |
| Language-specific | Bandit (Python), gosec (Go), Brakeman (Rails), ESLint security plugins (JS/TS, only if the repo has them), Psalm taint analysis (PHP, only if installed in the repo), SpotBugs + FindSecBugs (Java/Kotlin: needs a build, run it there), MobSF (needs a package and a server) |
| Secrets | gitleaks (including history), plus `scan_secrets.py` |
| Dependencies | osv-scanner, pip-audit, npm audit |
| CI | zizmor (GitHub Actions), plus the pattern pack |
| IaC and containers | Trivy, Checkov, hadolint |

Then the built-in scans, which work with no tools installed:

```bash
python scripts/scan_pitfalls.py <repo> --out <out>/patterns.json     # data/pitfalls.json, organised by CWE
python scripts/scan_secrets.py <repo> --history --out <out>/secrets.json
python scripts/merge_findings.py --root <repo> --patterns <out>/patterns.json --secrets <out>/secrets.json \
    --sarif <out>/semgrep.sarif <out>/zizmor.sarif ... --bandit <out>/bandit.json \
    --deps <out>/pip-audit.json <out>/npm-audit.json <out>/osv.json --out <out>/candidates.json
```

`merge_findings.py` merges hits on the same sink (overlapping lines and a shared pitfall or CWE)
into one candidate (`CAND-nnn`) with all its sources; secrets become `SEC-nnn` and dependency
advisories `DEP-nnn`, deduplicated across aliases. Secret values are never copied.

### 3. Triage every candidate

For each `CAND` and `SEC`, read the code and answer: is the source attacker-controlled? Does a
sanitiser or framework protection apply (parameterised queries, auto-escaping, a converter such as
`<int:id>`, an allow-list)? Is it reachable from an entry point? Then promote, observe or dismiss
(rule 3). Merge candidates with one root cause into one finding and list them all in
`finding.candidates`.

For each `DEP`, decide reachability: is the vulnerable function or code path used? `yes` and
`conditional` (reachable only under a stated condition) need a finding; `no` needs a basis
(which API the advisory needs and that the code doesn't call it). Note when a recommended fix
would make a not-reachable advisory reachable (for example, switching to the API it affects).

### 4. Deep review: what tools miss

Go through the high-risk areas from recon, entry point by entry point:

- **Access control:** IDOR/BOLA (change the id: is ownership checked?), missing function-level
  checks, mass assignment, tenant isolation, admin routes.
- **Authentication and sessions:** session signing keys and their fallbacks, JWT verification
  (`alg:none`, unverified signatures, missing audience), reset and magic-link tokens (entropy,
  expiry, single use, rate limits), session fixation, account enumeration.
- **Business logic:** races and TOCTOU (double spend, coupon reuse), state-machine bypasses,
  negative or huge quantities, client-trusted prices.
- **Server-side:** SSRF (and what the response reveals), file paths, uploads and parsers,
  deserialisation.
- **CI and supply chain:** `pull_request_target` and `workflow_run` with untrusted checkouts,
  `${{ github.event.* }}` in `run:`, unpinned third-party actions with tokens, reachable vulnerable
  dependencies.

Findings from this step have `sources: ["agent"]`.

### 5. Trace each finding

Record the path: `locations` with roles (`source`, `transform`, `sink`, `config`,
`guard_missing`), each with a file, line and a snippet that is on that line; a one-line
`path.summary`; and `path.sanitisers_checked`, the guards you looked for and why they don't stop
it. For `possible` findings, `path.gap` says what is missing.

### 6. Variant analysis

For each confirmed or traced bug pattern, search the whole codebase for siblings and write a
Semgrep rule when you can (taint mode for source -> sink bugs). Run it: it should match the known
instances, nothing safe, and nothing on a fixed copy. Ship it in `variant_rules` (the rule text and
a file outside the repository) so the team can add it to CI.

### 7. Verify (opt-in)

Only with permission (rule 4). Start the app locally or use the named test URL, with test
accounts. Write one PoC spec per finding and lint it first:

```bash
python scripts/run_poc.py poc.json --check
python scripts/run_poc.py poc.json --target http://127.0.0.1:5057 --permission "<what the user said>" --out <out>/evidence/F-1.json
```

A PoC is a list of HTTP steps (with a cookie jar) and a proof: `marker_in_response`,
`boolean_diff`, `header_matches` or `status`. Markers must not appear in the request (use computed
markers) unless reflection is the bug. Redirects are not followed, at most 30 requests are sent,
and `{env:NAME}` values (such as a forged cookie) are redacted in the evidence. When you can, run
the same PoCs against a copy with your recommended fixes applied: each proof should fail
(`poc.fixed_build_result: not_confirmed`).

### 8. Rate

**Severity:** a CVSS v4.0 base vector (`cvss.vector`), its score and severity, which the validator
recomputes; a `rationale` for the less obvious metrics (AT, PR, UI, subsequent-system impact).
Add the CWE ids and the OWASP Top 10 (2025) category.

**Priority** = severity weight x confidence weight x exposure weight:

| Severity | Weight | Confidence | Weight | Exposure | Weight |
|---|---|---|---|---|---|
| critical (9.0-10) | 4 | confirmed | 4 | public (no account) | 3 |
| high (7.0-8.9) | 3 | traced | 3 | authenticated (any account) | 2 |
| medium (4.0-6.9) | 2 | possible | 2 | restricted (admin, internal network, log access, upstream compromise) | 1 |
| low (0.1-3.9) | 1 | | | | |

P0 >= 32, P1 >= 18, P2 >= 8, otherwise P3. Caps: a Critical finding needs at least `traced`
confidence; a `possible` finding is at most P1. Rank by priority score, then CVSS score, then
confidence. Rate each finding on its own and use `chains_with` for combinations (a forged session
that makes an "authenticated" bug public), rather than inflating one rating.

### 9. Repro steps, fix and regression test

For every finding:

- `repro`: preconditions (role, config, data), numbered steps (raw HTTP or UI), the safe payload and
  what proves it, expected (secure) vs actual (vulnerable).
- `impact`: what a real attacker gains.
- `fix`: the specific change and the safer API to use.
- `regression_test`: a test in the repository's framework and style (detect them as Ticket to TDD
  does: same runner, fixtures and naming). Run it against the vulnerable code in a copy outside the
  repository: it must fail (`fails_on_vulnerable_code`), and should pass with the fix applied
  (`passes_after_fix`). For CI, configuration and dependency findings, the guard is a CI check
  (zizmor, Semgrep, pip-audit) with `result: not_run` and a note.

### 10. Report, validate and render

Write `security_audit.json` (schema: `schemas/security_audit.schema.json`) outside the repository,
with coverage, limitations (every tool that didn't run, every finding that couldn't be confirmed
and why) and every file you wrote. Then:

```bash
python scripts/validate_security_audit.py security_audit.json --repo <repo> --candidates candidates.json --write-summary
python scripts/render_report.py security_audit.json [--brief] [--sarif findings.sarif]
python scripts/cvss4.py "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N"
```

Fix every validator error. The SARIF export carries titles, locations, CWE tags and a
security-severity, not exploit steps, so it can go to GitHub code scanning. Give the user the
rendered report, the P0 and P1 findings first, and what wasn't covered.

## Report fields that the validator checks

- Schema conformance, including `sensitive: true` and a notice that warns about exploit details.
- CVSS scores and severities recomputed from the vectors (`scripts/cvss4.py`, a port of the FIRST
  calculator tested against 206,094 reference vectors); priority scores, priorities, the caps and
  the rank order.
- Confirmed findings: verification permitted, a loopback or named test target, a PoC that passes
  the safety lint, evidence for that finding showing the proof passed and the permission recorded.
- Traced findings list the guards checked; possible findings state the gap.
- Every candidate triaged once, every dismissal checkable, every dependency advisory assessed, and
  reachable ones linked to findings.
- Locations (file, line and snippet) against the source; regression tests exist and fail on the
  vulnerable code; variant rule files exist.
- No secret value in the report or the evidence, and nothing written inside the repository.
- Summary counts recomputed; headlines claiming the code is secure are rejected.
