# Design: `code-security-auditor-skill`

Status: Built (v1.1.0), see "As built" at the end. Build order: 5.

## Mission

The skill finds security weaknesses through static analysis of the codebase. For each one it
traces a path from attacker-controlled input to the dangerous operation, rates the finding, and
writes reproducible proof-of-concept (PoC) steps. If the user allows it, the skill confirms
findings against a local or test instance.

Some agent environments already have a `security-review` or `owasp-security` skill. This one is
different in four ways:

- It combines several scanning tools, not only agent reading.
- It traces each finding from source to sink.
- It rates findings with CVSS.
- It produces repro steps and a regression-test sketch for each finding.

## Decisions

- **Every tool must be free to run on closed-source client code without a commercial licence.**
  CodeQL is excluded: its licence doesn't allow this without GitHub Advanced Security. Semgrep
  OSS with community rules is the main static-analysis (SAST) engine. Rules and features that
  need a paid account are not used.
- **Dynamic verification is opt-in,** and only against localhost or a test environment the user
  names.

## Non-negotiable rules

- **"No findings" doesn't mean "secure".** The report always states coverage: which languages,
  files, tools and areas were reviewed, and which weren't.
- **A tool hit is a candidate until a path is traced.** Confidence levels:
  - `confirmed`: a PoC ran against a local or test instance.
  - `traced`: a full source → sink path exists with no effective sanitiser.
  - `possible`: a pattern matched but the path is incomplete.
  - `informational`.
- **Dynamic verification needs permission and a safe target.** It runs only after the user has
  explicitly confirmed the target, and never against production unless the user asks for it in
  writing.
- **PoCs must be safe.**
  - **Payloads:** canary or marker payloads only. Boolean or error-based SQL injection,
    `echo` with a marker string, a marker `alert`.
  - **Never destructive:** no `DROP`, `rm`, or writes to real data.
  - **No DoS risk:** no time-based payloads that could cause one.
  - **No persistence, and no exfiltration beyond the canary.**
- **Secrets are redacted.** The report gives the file, line, secret type and whether the secret
  is in git history. It never includes the value.
- **The report is sensitive.** It's written to a gitignored output directory, never committed,
  and its header says it contains exploit details.

## Workflow

1. **Recon: attack-surface map.** Identify:
   - the stack and frameworks
   - entry points: routes, GraphQL resolvers, queue consumers, CLI commands, webhooks, file
     parsers, mobile deep links and exported Android components
   - trust boundaries
   - the authentication model, sessions and the authorisation layer
   - data stores, outbound calls, and where secrets are configured

   The map is part of the output and decides where the deep review effort goes.
2. **Automated scans.** All tools are open source. Each runs only if it's available; tools that
   didn't run are recorded as `not_run`.

   | Area | Tools |
   |---|---|
   | Multi-language SAST | Semgrep OSS + community rules (`p/default`, `p/owasp-top-ten`, language packs) |
   | Language-specific | Bandit (Python), gosec (Go), Brakeman (Rails), eslint-plugin-security (JS/TS), SpotBugs + FindSecBugs (Java/Kotlin), Psalm taint analysis (PHP), MobSF static scan (Android/iOS packages, if provided) |
   | Secrets | gitleaks, including git history |
   | Dependencies | osv-scanner, pip-audit, npm audit, with a **reachability check** by the agent: is the vulnerable function actually called? |
   | IaC, containers and CI | Trivy (config + filesystem), Checkov, hadolint, and the agent reviewing GitHub Actions (`pull_request_target`, `${{ }}` expression injection, unpinned actions) |

   All outputs are normalised through **SARIF** using the shared `sarif.py`.
3. **Triage.** For each candidate the agent reads the code and decides:
   - Is the source attacker-controlled?
   - Does a sanitiser or framework protection apply, such as an ORM's parameterised queries or
     template auto-escaping?
   - Is the code reachable from an entry point?

   Each candidate is marked a false positive (reason recorded) or promoted to a finding.
4. **Deep review by the agent**, aimed at the high-risk areas from the recon map. These are the
   weaknesses tools miss:
   - **Authorisation:** IDOR/BOLA (accessing another user's records by changing an ID), missing
     function-level checks, mass assignment, tenant isolation.
   - **Business logic:** races and TOCTOU (double-spend, coupon reuse), state-machine bypasses,
     negative quantities.
   - **Authentication and sessions:** JWT pitfalls (`alg:none`, unverified signature), reset-token
     entropy, session fixation.
5. **Variant analysis.** For each confirmed bug pattern, search the whole codebase for siblings,
   and write a Semgrep rule if possible. The rule is shipped in the report so it can become a
   CI guard.
6. **Verification (opt-in).** Start the app locally, or use the given test URL, and run the safe
   PoC. Record the request and response as evidence.
7. **Rate, write repro steps, validate and render.** Optionally export SARIF for GitHub code
   scanning.

## Pitfall catalogue (RUBRIC.md and `data/pitfalls.json`, organised by CWE)

| Category | Weaknesses |
|---|---|
| Injection | SQL and NoSQL injection, OS command injection, server-side template injection, LDAP and XPath injection, header and log injection |
| Web | XSS (reflected, stored, DOM), CSRF, open redirect, CORS misconfiguration, clickjacking, missing security headers |
| Server-side | SSRF (including cloud metadata endpoints), path traversal, unsafe file upload, XXE, insecure deserialisation |
| Access control | IDOR/BOLA, missing authorisation checks, mass assignment, privilege escalation |
| Cryptography | Weak algorithms or modes, hard-coded keys, predictable randomness for tokens, disabled TLS verification |
| Data exposure | Secrets in code or history, sensitive data in logs, verbose errors, stack traces sent to clients |
| Language-specific | Prototype pollution, ReDoS, `eval`, `pickle`, `yaml.load`; for C/C++, integer overflow and memory safety |
| Mobile | Exported components, insecure WebView settings (`setJavaScriptEnabled` + `addJavascriptInterface`), secrets in the app bundle, missing cert pinning where policy requires it, insecure local storage |
| Supply chain and CI | Reachable vulnerable dependencies, dependency confusion, GitHub Actions injection, unpinned actions |

## Rating

- **Severity:** a CVSS v4.0 base vector and score, plus the CWE ID and the OWASP Top 10 category.
- **Confidence:** the levels above, recorded separately from severity.
- **Priority:** severity × confidence × exposure (internet-facing vs needs authentication vs
  admin-only).

Validator caps:

- A Critical finding needs at least `traced` confidence.
- A `possible` finding can't be ranked above High priority.

## Repro step format

```text
Preconditions:   role/account needed, config/feature flags, data setup
Path:            source (file:line) → transforms → sink (file:line)
Steps:           1. … 2. … (raw HTTP request / curl, or UI steps)
Safe payload:    canary payload + what proves it (marker reflected, boolean diff, error signature)
Expected (secure) vs Actual (vulnerable)
Impact:          what a real attacker gains
Fix:             specific change + safer API to use
Regression test: sketch in the repo's framework (ticket-to-tdd conventions)
Evidence:        verification run log if `confirmed`
```

## Files

```text
code-security-auditor-skill/
  SKILL.md  RUBRIC.md  README.md
  data/pitfalls.json                    # CWE → patterns, sinks, sanitisers, CVSS hints
  schemas/security_audit.schema.json
  scripts/run_scanners.py               # detect + run available tools, record versions / not_run
          sarif.py                      # shared copy
          merge_findings.py             # dedupe by (CWE, sink location)
          export_sarif.py
          validate_security_audit.py  render_report.py
  examples/security_audit.example.json
```

## As built (v1.0.0)

Differences from the draft above, and decisions made while building:

- **Files.** `export_sarif.py` became `render_report.py --sarif` (as in the UI crash hunter). Added:
  `recon.py` (attack-surface map), `scan_pitfalls.py` (the catalogue as a built-in scanner, so the
  skill works with no tools installed), `scan_secrets.py` (values never written; fingerprints and
  history via `git log -p`), `run_poc.py` (safety-linted PoC runner that records evidence) and
  `cvss4.py`. Variant rules ship as files next to the report.
- **CVSS v4.0** is a port of the FIRST reference calculator (BSD-2-Clause, header kept), with the
  Red Hat `cvss` library's epsilon rounding. It matches all 206,094 test vectors in that library
  (every base vector plus 100,000 random vectors with threat, environmental and supplemental
  metrics). Plain `Math.round` disagrees with them on about 0.4% of vectors because of
  floating-point error.
- **Priority** is numeric: severity weight (critical 4, high 3, medium 2, low 1) x confidence
  (confirmed 4, traced 3, possible 2) x exposure (public 3, authenticated 2, restricted 1); P0 >= 32,
  P1 >= 18, P2 >= 8. "Admin-only" became `restricted`, which also covers internal or adjacent
  networks, log access and upstream compromise. `informational` is not a confidence level;
  informational items are `observations`.
- **Merging** is by sink location (overlapping lines and a shared pitfall or CWE), not (CWE, sink):
  tools tag the same bug with different CWEs (Semgrep's `tainted-sql-string` carries CWE-704 and
  CWE-915), and Semgrep anchors multi-line calls on their first line while Bandit uses the line
  with the expression.
- **Tools.** zizmor was added for GitHub Actions. ESLint security plugins and Psalm run only when the
  repository already has them (adding them changes the repository). SpotBugs + FindSecBugs and
  MobSF need a build or a server, so they are recorded as `not_run` with instructions, and their
  SARIF can be merged. Semgrep uses `p/default` and `p/owasp-top-ten`, which need no login, with
  metrics off. Tools run with `PYTHONUTF8=1`, because Semgrep's SARIF writer fails on Windows code
  pages otherwise.
- **Dependencies** are assessed in their own `dependencies` section (`yes`, `conditional`, `no`,
  `unknown`, each with a basis); `yes` and `conditional` need a finding.
- **Verification evidence** goes further than the draft. Each confirmed PoC is re-run against a copy
  with the recommended fixes, and its proof must fail there (`fixed_build_result`). Each regression
  test is run against the vulnerable code and must fail (`fails_on_vulnerable_code`).
- **OWASP** categories are the 2025 Top 10.
- **The report** may be written inside the repository only if the path is gitignored; the validator
  checks with `git check-ignore`.
- **Found on a real static site, fixed.** A run on a static website (about 2,000 HTML pages, 670
  commits, a Python tool folder and a gitignored service-account key) showed seven gaps.
  `scan_secrets.py` reported the path in `GOOGLE_APPLICATION_CREDENTIALS` as a secret, so the
  validator rejected any report that named the key file; paths to credential files now go to
  `credential_files` with the file's git status, and are never treated as values. It also reported
  Google's public `token_uri`; public service-account and OAuth fields (and plain https URLs under
  `*_uri`/`*_url` keys, unless a path segment looks like a token) are skipped. Every secret's file
  now gets `git_status` (`git ls-files`, `git check-ignore`) and a per-path history check
  (`git log --all -- <path>`), so "gitignored, never committed" is distinguishable from
  "committed" without the full scan (`history_basis: path`). The full history scan
  (`git log --all -p`) didn't finish in 25 minutes; it now feeds commits to `git log --no-walk
  --stdin -i -G<keywords>` in batches with pathspec exclusions (binary, media, lock, vendored,
  minified, plus `--history-exclude` globs), caps the text read per file per commit, and stops at
  `--history-budget` seconds with `status: partial` and the commits scanned; the validator makes
  the report's limitations say so, and checks secret blocks against the scanner. `PY-ARCHIVE-EXTRACT`
  matched BeautifulSoup's `Tag.extract()`; it now needs a tarfile/zipfile/shutil import and an
  archive word near the call. `recon.py` found only the CLI scripts; it now lists client-side
  sources (URL, fragment, `postMessage`, referrer, `window.name`, storage, cookies) in JavaScript
  and inline scripts, deduplicated across generated pages, and every CI workflow trigger with who
  can start it (new entry-point kind `client_source`). On the example it now finds the
  `pull_request_target` entry point the agent had added by hand. `run_scanners.py` no longer
  leaves an empty `checkov/` folder when Checkov doesn't run.

## As built (v1.1.0): fixes from a run on a real Next.js application

A run on a Next.js 16 App Router + Prisma + NextAuth application (128 route handlers, about 440
TypeScript files, 179 commits, `node_modules` installed, only npm of the scanners available) showed
these gaps; all are fixed, and `examples/kite-notes/` (a synthetic Next.js fixture) with
`scripts/self_test.py` keeps them fixed.

- **The validator's secret-value check** collected every value on every secret candidate's line,
  dismissed placeholders included, and searched the serialised JSON, keys included. A report could
  not contain "password", "pass" (so not `passes_after_fix`, "bypass" or a reset-password route).
  Values now come only from findings with a secret block and from candidates that weren't dismissed
  (a dismissed one counts only if it looks like a real key: 16+ characters, 3.5+ bits per
  character); placeholders and values under 8 characters are skipped; only string values are
  searched, short values as whole tokens; the error names the candidate and the JSON path.
- **Next.js patterns.** The JavaScript pack only knew Express sources. Sources now include
  `nextUrl.searchParams`, `searchParams.get`, `await request.json()`/`formData()`, `params.x`,
  `body.x`, `headers().get` and `cookies().get`; the open-redirect pattern knows
  `NextResponse.redirect`, `redirect()` and `router.push`. Nine patterns were added: Prisma raw SQL
  from a variable, mass assignment (`data: body`), `startsWith('/')`-only redirect checks, CSV
  formula injection, `<style>` interpolation, shared-cache headers on session responses, OAuth
  `state` not bound to the session, unverified webhooks, and uploads stored with the client's
  content type. `dangerouslySetInnerHTML` with a string literal is no longer flagged.
  `scan_pitfalls.py` gained `suppress_file` and `require_path`.
- **recon and Next.js auth** (`scripts/nextjs_auth.py`). 193 of 221 entry points had come back
  `auth: unknown` with the handler at line 1, and `export { handler as GET }` was missed. Each
  exported handler is now cut out by bracket matching (strings, comments, regex literals and
  templates skipped) at its real line, with the same-file functions it calls, and classified: a
  session call, one of the repository's guard helpers (found by following helper calls up to three
  levels across the repository; recorded in `auth_helpers`), a machine credential (new auth values
  `cron_secret`, `api_key`, `signature`, `token`), middleware coverage (`middleware.ts`/`proxy.ts`
  matcher and protected-prefix lists parsed; new entry kind `middleware`), or none: `public`, with
  that reason. Server actions are entry points (`server_action`). On the application, unknown fell
  from 193 to 16. Client sources no longer include server files, config files or test setup, and
  `URLSearchParams` that builds an outbound query; data stores skip `*.config.*`; auth-flow leads
  ignore route parameters (`/track/:token/open`).
- **Secret scan.** 185 hits, none real: 160 were test literals, 6 `.env.example` placeholders, 8
  hyphenated placeholders in documentation, and two lists of field names. Test-file literals that
  don't look real go to `test_fixtures`; template env files, instruction values, hyphenated words
  in documentation, documented `user:password@` connection strings and list elements are not
  secrets; a hard-coded fallback always is. The application now gives 5 candidates. Per-file git
  lookups (two per file) are batched (two per hundred files), and `elapsed_s` is recorded.
  Secrets only in history become candidates (`history_only`, `git_status: removed`).
- **Dependencies.** npm audit's range was reported as the version, `fixed_in` was empty, and a
  package installed twice (a production 6.x and a dev 8.x) read as one. `merge_findings.py` now
  reads `package-lock.json`: installed version, the advisory's own range (a small npm range
  matcher), a `dev` flag, `via` (npm audit's effects plus the lockfile's dependents) and
  `fixed_in` from the range; one entry per installed major.
- **Scale.** 211 candidates and 220 entry points had to be copied into the report by hand.
  `seed_report.py` writes everything that is copied and marks the judgement `TODO(seed)`, which
  the validator rejects. `run_scanners.py` records `elapsed_s` and `results` per tool, prints
  progress every 30 seconds, and gives npm audit its own timeout (`--npm-audit-timeout`, 600 s).
- **Schema 1.1** (1.0 reports stay valid): the auth values and entry kinds above, `auth_evidence` on
  entry points, `recon.middleware`, area `availability`, secret `git_status: removed`, dependency
  `range`/`dev`/`via`, agent-found findings (`source: agent` with a `basis` the validator re-reads
  with `--repo`; the renderer labels them "found by review"), and `business_impact`, which raises
  a P2 or P3 finding one level (never to P0) for direct financial loss, fraud or a regulated-data
  breach. Ranking is now priority first. The headline ban allows "secure" in technical phrases
  ("the Secure cookie flag") and still rejects verdicts. Advisories in one package may share one
  "upgrade X" finding (the validator checks they are the same package).
- **Rubric.** AT:P is for conditions the attacker can't influence and that are unusual in
  production; traced issues that need only common conditions are AT:N. A Next.js framework
  section covers middleware coverage, server actions, NextAuth callbacks and Prisma.
- **SKILL.md.** Next.js guidance, testing a variant without Semgrep (a one-entry catalogue for
  `scan_pitfalls.py --catalogue`), and a budget for Node regression tests (prepare one copy, mock
  Prisma and NextAuth at the module boundary, `not_run` with a note when the brief rules it out).
- **Renderer.** `--brief` counts dismissals by reason instead of listing each one, and `--help`
  prints the whole description.
- **Timings on the application:** `scan_secrets.py --history` 28 s before, 18-20 s after (warm
  cache; the first run took 4 min 7 s cold); `recon.py` 10 s before, 9-15 s after (it now cuts out
  every handler and follows helpers); `scan_pitfalls.py` 3 s before, 7 s after (more patterns).
  npm audit's own time (about 6 minutes) is the registry's; it is now reported and bounded.
