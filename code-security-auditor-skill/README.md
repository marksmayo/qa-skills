# Code Security Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It audits a codebase for security weaknesses with free, open-source scanners and a deep manual
review, traces each finding from attacker-controlled input to the dangerous operation, rates it
with CVSS v4.0, and writes safe reproduction steps, a fix and a regression test. With your
permission it confirms findings against localhost or a test environment, using canary payloads only.

It doesn't bluff about the result:

- A tool hit is a candidate. It becomes a finding only with a traced path, and each finding says
  how sure it is: `confirmed` (a PoC ran and its proof passed), `traced` (the path is in the code)
  or `possible` (part of the path is missing, and the report says which part).
- Every candidate from every tool is promoted or dismissed with a reason you can check, and every
  vulnerable dependency is assessed for reachability.
- Every tool is listed as ran, not run, errored or not applicable, with the reason. CodeQL and
  other tools that need a licence for closed-source code are not used.
- CVSS v4.0 scores are recomputed from their vectors by a port of the FIRST calculator, tested
  against 206,094 reference vectors.
- Proofs of concept never destroy, slow down, persist or exfiltrate, and never touch production
  unless you ask in writing. A finding that can only be proved unsafely stays `traced`.
- Secret values never appear: only the type, location, a fingerprint and whether they're in history.
- The report states what was covered and what wasn't, and never calls the code secure. It contains
  exploit details, so it is written outside the repository and never committed.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/code-security-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/code-security-auditor/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/code-security-auditor/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts are stdlib-only Python (3.9+).
The scanners are optional and used when installed: Semgrep, Bandit, pip-audit and zizmor install
with `pip`; gitleaks, osv-scanner, gosec, Trivy and hadolint are single binaries; Brakeman is a gem.
The agent asks before installing anything.

## Usage

Ask the agent:

> Run a security audit of this repository with the Code Security Auditor skill.

or

> Use Code Security Auditor on services/billing. You can verify findings against the app on
> localhost:8080 with the test accounts in .env.test.

| Priority | Meaning |
|---|---|
| **P0** | Score >= 32, e.g. a confirmed high or traced critical issue reachable without an account |
| **P1** | Score >= 18, e.g. a confirmed high issue for any signed-in user. `possible` findings stop here |
| P2 | Score >= 8 |
| P3 | Score < 8 |

Priority = severity (CVSS v4.0 band) x confidence x exposure (public, authenticated, restricted).

Scripts, in the order the skill runs them:

```bash
python scripts/recon.py <repo> --out recon.json                   # attack surface: routes (per-handler auth for Next.js; FastAPI routers and dependencies), Azure Functions triggers, server actions, middleware, client-side sources, GitHub Actions and Azure DevOps triggers
python scripts/run_scanners.py <repo> --out-dir <out> [--offline] [--osv-local-db <dir>]
#   every applicable open-source scanner: status, time and results; lockfiles listed offline and advisories recorded as assessed, partial or not assessed
python scripts/scan_pitfalls.py <repo> --out patterns.json         # 118 CWE-mapped patterns in 15 packs (Express and Next.js sources, Azure Bicep/ARM and Azure Pipelines)
python scripts/scan_secrets.py <repo> --history --out secrets.json # secrets, never their values; git status and history per file
#   large repositories: --history-budget 300 (seconds; coverage recorded as partial) --history-exclude '*.html'
python scripts/merge_findings.py --root <repo> --patterns patterns.json --secrets secrets.json \
    --sarif semgrep.sarif zizmor.sarif --bandit bandit.json --deps pip-audit.json npm-audit.json --out candidates.json
python scripts/seed_report.py --recon recon.json --candidates candidates.json --scanners <out>/scanners.json \
    --out security_audit.json                                      # copied parts of the report; judgement left as TODO(seed)
python scripts/run_poc.py poc.json --check                         # safety lint only
python scripts/run_poc.py poc.json --target http://127.0.0.1:5057 --permission "..." --out evidence/F-1.json
python scripts/cvss4.py "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
python scripts/validate_security_audit.py security_audit.json --repo <repo> --candidates candidates.json --write-summary
python scripts/render_report.py security_audit.json [--brief] [--sarif findings.sarif]   # --sarif writes only the SARIF file
python scripts/sarif.py read results.sarif                          # normalise any SARIF file
python scripts/self_test.py                                         # built-in scanners against examples/kite-notes and examples/ferry-ops
```

## Output

- `security_audit.json`, described by `schemas/security_audit.schema.json`:

| Section | Content |
|---|---|
| `recon` | Stack, entry points with auth and exposure, trust boundaries, auth model, stores, outbound calls, secret config, high-risk areas |
| `tools` | Every scanner: ran, not run, errored or not applicable, version and why |
| `verification` | Whether dynamic checks were allowed, by whom, and against what |
| `triage` | Every candidate promoted, observed or dismissed with a reason |
| `findings` | Ranked: CWE, OWASP 2025, CVSS v4.0, confidence, exposure, priority, path, repro, PoC result, impact, fix, regression test |
| `dependencies` | Every advisory with reachability and its basis |
| `dependency_advisories` | Whether advisories were assessed, partially assessed or not assessed at all (schema 1.2), with the tools and lockfiles |
| `variant_rules` | Semgrep rules (or grep patterns when Semgrep is unavailable) for the bug patterns found, tested, for CI |
| `observations`, `coverage`, `limitations` | Informational issues, what was and wasn't reviewed |

- PoC specs and evidence (every request and response, secrets redacted), the regression tests, and
  optionally SARIF for code-scanning dashboards (titles, locations, CWE and severity; no exploit steps).

See `examples/` for a complete run against `examples/invoicer/`, a small, deliberately vulnerable
Flask app. The scanner outputs (Semgrep 1.184, Bandit 1.9.4, pip-audit 2.10.1, zizmor 1.32), PoC
evidence and test runs are real:

- 18 findings, 3 of them P0: a hard-coded session key that lets anyone sign in as anyone
  (confirmed by forging a cookie), and two ways for any pull request to run code in a privileged
  GitHub Actions job;
- 8 findings confirmed with canary PoCs (SSTI, two SQL injections, IDOR, path traversal, SSRF, open
  redirect, session forgery), and every proof re-run against a fixed copy, where it fails;
- 11 regression tests in the app's pytest style that fail on the vulnerable code and pass on the
  fixed copy;
- 16 dependency advisories, 3 reachable;
- 2 Semgrep variant rules that match exactly the vulnerable lines;
- 4 dismissed candidates with reasons, 3 observations, and the tools that weren't installed.

`examples/kite-notes/` is a second, smaller fixture: a Next.js App Router + Prisma + NextAuth
source tree (not runnable) with planted weaknesses, and `expected.json` saying what the built-in
scanners must find there. `scripts/self_test.py` checks them: 13 pattern hits and none in the safe
counterparts, per-handler auth for each route handler and server action, the middleware's
coverage, client sources only in browser code, and only the hard-coded fallback key reported as a
secret.

`examples/ferry-ops/` is a third fixture for a FastAPI, Azure Functions, Bicep/ARM and Azure
DevOps stack: routers included across files with router-level and `include_router()` dependencies,
route decorators quoted in a docstring, HTTP routes and triggers in a function app, misconfigured
Bicep and ARM templates, and pipelines, each with safe counterparts. `self_test.py` checks every
pattern hit exactly (24, none in the safe files), every entry point's path and auth, the pipelines,
the secret placeholders and the offline dependency inventory.

```bash
python scripts/validate_security_audit.py examples/security_audit.example.json \
    --repo examples/invoicer --candidates examples/run/candidates.json
python scripts/render_report.py examples/security_audit.example.json --brief
```

## What the validator enforces

- The schema, the sensitive notice, and CVSS scores recomputed from vectors.
- Priority and rank recomputed; Critical needs at least traced confidence; possible findings are at most P1.
- Confirmed findings need permitted verification against loopback or a named test host, a PoC that
  passes the safety lint, and evidence showing the proof passed.
- Traced findings list the guards checked; possible findings state the gap.
- Every candidate and advisory is triaged once; dismissals must be checkable; reachable advisories
  have findings.
- Locations match the source; agent-found findings' basis excerpts and observations' excerpts are
  on their lines; recon handler lines exist and are not inside a Python docstring; regression
  tests exist and fail on the vulnerable code.
- Dependency advisories (schema 1.2): `assessed` needs a dependency tool that ran; `not_assessed`
  must be in the limitations and rules out a "no vulnerable dependencies" headline.
- No secret values in the report or evidence (values of real or undismissed secrets, searched in
  string values only, with errors that name the candidate and never the value); nothing written
  inside the repository; no `TODO(seed)` placeholder left.
- Business impact raises a P2 or P3 one level, never to P0.
- Summary counts recomputed; headlines that call the code secure or safe rejected.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: confidence, CVSS v4.0 anchors, exposure, priority, triage, safe PoCs, secrets, catalogue
- `data/pitfalls.json`: 118 weakness patterns by CWE, with OWASP 2025 categories, CVSS hints, fixes and tool rule mappings
- `schemas/security_audit.schema.json`: output contract
- `scripts/recon.py`: attack-surface map
- `scripts/nextjs_auth.py`: Next.js route handlers, server actions and middleware, with per-handler auth (used by recon)
- `scripts/py_routes.py`: FastAPI, Flask and Azure Functions entry points from the syntax tree, routers and their dependencies followed across files (used by recon)
- `scripts/run_scanners.py`: detects and runs the open-source scanners, recording each one's status
- `scripts/scan_pitfalls.py`: pattern scan
- `scripts/scan_secrets.py`: secrets in the tree and git history, without values; whether each file is tracked, untracked or gitignored and was ever committed; credential files and paths to them listed separately
- `scripts/merge_findings.py`: merges SARIF, Bandit, ESLint, dependency and built-in results into candidates; npm advisories get the installed version, range, dev flag and via chain from `package-lock.json`
- `scripts/seed_report.py`: writes the report skeleton (entry points, tools, triage, dependencies) with TODO(seed) placeholders for the judgement
- `scripts/self_test.py`: checks the built-in scanners against `examples/kite-notes/` and `examples/ferry-ops/`
- `scripts/run_poc.py`: safety-linted PoC runner that records evidence
- `scripts/cvss4.py`: CVSS v4.0 calculator (port of the FIRST reference implementation, BSD-2-Clause)
- `scripts/validate_security_audit.py`: structure and honesty checks
- `scripts/render_report.py`: coloured terminal/Markdown report and SARIF export
- `scripts/sarif.py`: SARIF reader and writer (shared with UI Crash Hunter)
- `examples/`: sample app, scanner outputs, PoCs, evidence, regression tests, variant rules and report; `kite-notes/`, the Next.js fixture; `ferry-ops/`, the FastAPI, Azure Functions, Bicep/ARM and Azure Pipelines fixture

## Design principles

1. "No findings" is a statement about coverage, not about security.
2. A tool hit is a lead; a traced path is a finding; a passing canary is proof.
3. Prove only what can be proved safely, and say what wasn't.
4. Account for every candidate, every advisory and every tool.
5. Never reproduce a secret, never touch production without written permission, never commit the report.
