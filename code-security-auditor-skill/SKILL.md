---
name: code-security-auditor
description: Audit a codebase for security weaknesses with open-source scanners (Semgrep OSS, Bandit, gosec, Brakeman, gitleaks, osv-scanner, pip-audit, npm audit, zizmor, Trivy, Checkov and more) plus a deep manual review, trace each finding from attacker-controlled input to the dangerous operation, rate it with CVSS v4.0, and write safe reproduction steps, a fix and a regression test. Optionally confirms findings with canary-only proofs of concept against localhost or a test environment the user names. Every candidate is triaged, every tool's status is recorded, and the report never calls the code secure.
version: 1.2.0
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
6. **Secrets are never reproduced.** Report the type, file, line, a fingerprint, whether the file is
   tracked or gitignored, and whether the secret is in git history. Never the value: not in the report, the PoC files, the evidence or your messages.
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
commands, mobile deep links and exported components, CI workflow triggers such as
`pull_request_target`, `workflow_run` and `issue_comment`, and client-side sources in JavaScript and
inline scripts: `location.search`/`hash`, `useSearchParams()`, `postMessage`, `document.referrer`,
`window.name`, web storage and cookies, so a static site has entry points too; an inline script
repeated across generated pages is listed once with `also_in`; server code, config files and test
setup are not client sources), a first guess at each route's auth requirement, data stores,
outbound calls, secret configuration, dependency manifests, CI workflows and review leads. Then
read the code to confirm it: add entry points the patterns missed (CI workflows, dynamically
registered routes), correct auth guesses (`unknown` is not `public`), and write the trust
boundaries and the auth model (authentication, sessions, authorisation). The map decides where the
deep review goes; record it in `recon`.

**Next.js.** recon models the App Router and `pages/api`: every exported handler (`export async
function GET`, `export const POST = ...`, `export { handler as GET, handler as POST }`) is an entry
point at the line of its export, and its auth is read from the handler body (and the same-file
functions it calls): a session or guard call (`getServerSession`, `auth()`, `getToken`, or one of
the repository's own helpers that wraps them, listed in `auth_helpers` with where each is
defined), a machine credential (`cron_secret`, `api_key`, `signature`, `token`), coverage by
`middleware.ts`/`proxy.ts` (its `matcher` and any protected-prefix list are parsed and recorded in
`middleware`), or none, which is reported as `public` with "no session, guard or credential check
in this handler". That is the main access-control lead in a Next app: page middleware usually does
not cover `/api/*` unless the matcher says so, so each route handler must check auth itself.
Server actions (`'use server'`) are entry points too (`server_action`, POST by action id). In the
deep review also read: NextAuth callbacks (`jwt`, `session`, `redirect`, `signIn`), route segment
config (`dynamic`, `revalidate`, and `Cache-Control` on responses that read the session), server
actions' argument checks, `redirect()`/`NextResponse.redirect`/`router.push` with values from
`searchParams`, and Prisma writes that take the request body whole.

**FastAPI and Flask.** recon reads Python routes from the syntax tree, so decorators split over
several lines are found and decorators quoted in docstrings (usage examples) are not. It follows
routers across files: a route's path is the `include_router(prefix=...)` (or
`register_blueprint(url_prefix=...)`) chain plus the router's own `prefix`, and its auth is the
union of the dependencies on the route (`dependencies=[...]`, `Depends()`/`Security()` parameters,
`Annotated` aliases), on its router (`APIRouter(dependencies=[...])`, including a project's own
APIRouter subclass), on the `include_router()` call and on the app. A dependency counts as auth when
its name says so (`get_current_user`, `require_admin`, `check_..._permission`, an API-key header);
`auth_evidence` names each one and where it is. A route whose router is never included anywhere
recon can resolve stays `unknown`. In the deep review read those dependencies themselves: what they
validate (signature, issuer, audience, expiry), any bypass for development (a static token or extra
roles from an environment variable, which `PY-AUTH-ENV-BYPASS` flags), and whether a path parameter
feeding a data-access lookup is also filtered by the caller's tenant or ownership.

**Azure Functions and Azure DevOps.** Function apps (Python v2 model) are entry points too:
`@app.route` with its `auth_level` (`ANONYMOUS` is `public`, `FUNCTION`/`ADMIN` need a key,
`api_key`) under the `host.json` route prefix, and blob, queue, Service Bus, Event Hub, Cosmos DB
and timer triggers (`queue_consumer`, or `other` for timers): whoever can write to the queue or
container controls the input. Azure DevOps pipelines are listed in `ci_workflows` with their
triggers (a pipeline with no `trigger:` key builds every branch), runtime parameters, service
connections, environments and deployment jobs; templates are recognised as the files other
pipelines include. Review leads point at every-branch triggers with secrets or service
connections, runtime parameters, and service connections outside a deployment job.

### 2. Run the scanners

```bash
python scripts/run_scanners.py <repo> --out-dir <out> [--offline] [--osv-local-db <dir>]
```

Whatever runs, the pinned packages in lockfiles and requirements files are listed offline in
`<out>/dependencies.json`, and `scanners.json` records `dependency_advisories`: `assessed`,
`partial` (a lockfile no tool covered, say npm with only pip-audit) or `not_assessed` (no advisory
tool ran, for example with `--offline`). Copy it into the report's `dependency_advisories`; "not
assessed" is never "no vulnerable dependencies", and the limitations must say it. With
`--osv-local-db` (an osv-scanner offline database downloaded beforehand), osv-scanner checks
advisories without the network. Without any advisory tool, packages you can see are risky by
reading (an abandoned release line, a known-vulnerable pin) are agent findings with confidence
`possible`.

It runs every free, open-source tool that applies and is installed, writes their outputs to
`<out>` (refused if inside the repository) and records each tool as `ran`, `not_run` (with why),
`errored` or `not_applicable` in `<out>/scanners.json`. Copy those statuses into the report's
`tools`, with the elapsed time and number of results each one recorded. npm audit on a large lockfile
(over a thousand packages) can take several minutes; it has its own limit (`--npm-audit-timeout`,
default 600 seconds; every other tool has `--timeout`, default 900), and long runs print progress
every 30 seconds. Use only tools whose licences allow scanning closed-source code without a paid account:
CodeQL is excluded, and so are Semgrep rules or features that need a login. Ask before installing
anything; a missing tool is recorded, not silently skipped.

| Area | Tools |
|---|---|
| Multi-language SAST | Semgrep OSS with `p/default` and `p/owasp-top-ten` |
| Language-specific | Bandit (Python), gosec (Go), Brakeman (Rails), ESLint security plugins (JS/TS, only if the repo has them), Psalm taint analysis (PHP, only if installed in the repo), SpotBugs + FindSecBugs (Java/Kotlin: needs a build, run it there), MobSF (needs a package and a server) |
| Secrets | gitleaks (including history), plus `scan_secrets.py` |
| Dependencies | osv-scanner, pip-audit, npm audit |
| CI | zizmor (GitHub Actions), Checkov (GitHub Actions and Azure Pipelines), plus the pattern packs |
| IaC and containers | Trivy, Checkov (Terraform, Bicep, ARM, CloudFormation, Kubernetes, Helm, Dockerfiles, Compose), hadolint |

The built-in pattern packs cover Python, JavaScript/TypeScript, Java/Kotlin, Go, PHP, Ruby, C#, C,
iOS and Android manifests, GitHub Actions, Dockerfiles and Terraform, and Azure: Bicep and ARM
templates (public network access on data services, firewall rules from `0.0.0.0`, administrator
password login, secret parameters without `@secure()`, Key Vault access policies that list every
secret, FTP allowed, no `httpsOnly`, TLS below 1.2, account keys in app settings) and Azure
Pipelines YAML (every-branch triggers, literal secret variables, branch names and runtime
parameters pasted into scripts, service connections outside deployment jobs).

Then the built-in scans, which work with no tools installed:

```bash
python scripts/scan_pitfalls.py <repo> --out <out>/patterns.json     # data/pitfalls.json, organised by CWE
python scripts/scan_secrets.py <repo> --history --out <out>/secrets.json
python scripts/merge_findings.py --root <repo> --patterns <out>/patterns.json --secrets <out>/secrets.json \
    --sarif <out>/semgrep.sarif <out>/zizmor.sarif ... --bandit <out>/bandit.json \
    --deps <out>/pip-audit.json <out>/npm-audit.json <out>/osv.json [--npm-lock <repo>/package-lock.json] \
    --out <out>/candidates.json
python scripts/seed_report.py --recon <out>/recon.json --candidates <out>/candidates.json \
    --scanners <out>/scanners.json --repo <repo> --out <out>/security_audit.json
```

`seed_report.py` writes the copied parts of the report (entry points, tools rows, one triage row per
candidate, one dependencies row per advisory, coverage totals and counts) and marks everything that
needs judgement `TODO(seed)`; the validator rejects any placeholder left behind.

`scan_secrets.py` records each secret's file as `tracked`, `untracked` or `ignored` in git and
checks that path's history cheaply, so "present locally, gitignored, never committed"
(`git_status: ignored`, `in_git_history: no`, `history_basis: path`) is told apart from
"committed". `--history` reads every commit, but only file changes that touch a secret keyword,
without binary, lock, vendored or minified files, within `--history-budget` seconds (default
300). On a large repository add `--history-exclude '*.html'` (or other bulk generated content);
if the budget runs out, history is `partial` with the commits scanned, and the limitations must
say so. Paths to credential files (`GOOGLE_APPLICATION_CREDENTIALS=keys/sa.json`) and public
service-account fields (`token_uri`, certificate URLs) are not secrets; the credential files and
references are listed under `credential_files`: say in the report that the file exists and
whether it is committed, and name its path freely.

Low-entropy literals in test files (no provider format, under 4 bits of entropy per character or
under 20 characters) are fixtures: they are counted and listed under `test_fixtures`, not triaged
one by one; a real-looking key in a test is still a candidate. Placeholders are not secrets:
values in `.env.example`/`.env.sample`/`.env.template` unless they look like real keys, instruction
values (`generate-...`, `your-...`, `<...>`, `change-me`), words joined by hyphens in documentation,
`postgresql://user:password@` in documentation, lists of field names (`['clientSecret',
'paymentIntentId']`), references to another variable (`$NAME`, `$(NAME)` in Azure Pipelines,
`$env:NAME`, `%NAME%`), and ARM template expressions (`[parameters('x')]`). A hard-coded fallback (`getenv('KEY', 'dev-key')`) is always a candidate,
whatever it looks like, because it is used whenever the variable is unset. Secrets that exist only
in history become candidates too (`history_only`, `git_status: removed`, with the commits), so
each is triaged and rotated. The git lookups are batched (two `git log` calls per hundred files,
not two per file), and the output records `elapsed_s`.

`merge_findings.py` merges hits on the same sink (overlapping lines and a shared pitfall or CWE)
into one candidate (`CAND-nnn`) with all its sources; secrets become `SEC-nnn` and dependency
advisories `DEP-nnn`, deduplicated across aliases. Secret values are never copied. For npm audit,
each advisory is checked against the copies installed according to `package-lock.json`: `version`
is the installed version, `range` the advisory's own vulnerable range, `dev` whether every copy is
a devDependency, `via` the packages that pull it in, and `fixed_in` the first fixed version; a
package installed in two majors (a production copy and a dev copy) gives one entry per major, so an
advisory that only affects the dev copy doesn't read as production.

### 3. Triage every candidate

For each `CAND` and `SEC`, read the code and answer: is the source attacker-controlled? Does a
sanitiser or framework protection apply (parameterised queries, auto-escaping, a converter such as
`<int:id>`, an allow-list)? Is it reachable from an entry point? Then promote, observe or dismiss
(rule 3). Merge candidates with one root cause into one finding and list them all in
`finding.candidates`.

For each `DEP`, decide reachability: is the vulnerable function or code path used? `yes` and
`conditional` (reachable only under a stated condition) need a finding; `no` needs a basis
(which API the advisory needs and that the code doesn't call it; "devDependency only, not in the
production bundle" is a basis once confirmed). Note when a recommended fix would make a
not-reachable advisory reachable (for example, switching to the API it affects). Several
advisories in one package that one upgrade fixes (`conditional` ones especially, such as
self-hosting-only issues in a framework) can be one "upgrade X" finding listing every `DEP` id in
`dependency.ids`; the validator only requires that they are the same package.

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

Findings from this step have `sources: ["agent"]`, and should set `"source": "agent"` with a
`basis`: a list of `{file, line, excerpt}` (or `{evidence}` for runtime evidence). With `--repo`
the validator re-reads each line and checks the excerpt is on it; the renderer labels these
findings "found by review". A finding with `source: agent` lists no candidates.

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

Without Semgrep, test a variant as a regex: write a catalogue with one entry in the
`data/pitfalls.json` shape (pack, regex, optional `require_near`/`suppress_near`/`suppress_file`)
to a scratch file, run `scan_pitfalls.py <repo> --catalogue <that file>` on the repository and on
a fixed copy, and record it in `variant_rules` with `engine: "grep"` (the regex in `rule`, the
catalogue file in `file`, what it matched in `matches`). When no finding is pattern-shaped, say so
in the limitations instead.

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
| low (0.1-3.9) | 1 | | | local (attacker-supplied input: file/arg/stdin/env) | 1 |

P0 >= 32, P1 >= 18, P2 >= 8, otherwise P3. Caps: a Critical finding needs at least `traced`
confidence; a `possible` finding is at most P1. Rank by priority score, then CVSS score, then
confidence. Rate each finding on its own and use `chains_with` for combinations (a forged session
that makes an "authenticated" bug public), rather than inflating one rating.

**Business impact.** CVSS rates the technical impact; it doesn't see that a discount code anyone
can reuse loses money on every order. When exploiting a finding causes direct financial loss,
fraud, or a breach of regulated data, set `business_impact` (`direct_financial_loss`, `fraud` or
`regulated_data_breach`, with a `basis` saying where and how). It raises a P2 or P3 finding one
level; it never makes a P0 (that still needs the score), and the `possible` cap still applies.
Ranking is by priority, then priority score, then CVSS score, then confidence.

**AT (attack requirements).** Use `AT:P` only for a condition the attacker cannot influence and
that is unusual in a production deployment (a missing environment variable, a non-default
setting, a particular browser). A condition that holds in the deployment as shipped, or that any
attacker can arrange (sending two requests at once, choosing an IPv6 literal, waiting for a token
to be issued), is `AT:N`; put the difficulty in AC if there is any.

**An unknown deployment setting belongs in AT, not in confidence.** When the code path is fully
read (a development bypass that a deployed environment variable switches on) but you cannot see
whether production sets it, the finding is `traced` with `AT:P` and the rationale names the
setting; `possible` is for a path you could not read to the end. An exposed service that still
needs a credential (a database open to the internet with password login) takes the privilege the
attacker must hold (`PR:H` for an administrator password) and exposure `restricted`; RUBRIC.md has
the anchors.

**Low-level, CLI and Rust targets.** For a command-line tool or a library with no network listener,
the attacker is usually local: they supply a file, an argument, stdin or an environment variable.
Rate those findings with `exposure: "local"` (weight 1, the same as `restricted`); the local attack
vector is already carried by the CVSS vector as `AV:L`, which lowers the score, so don't double-count
it. `scan_pitfalls.py` has a `rust` pack (`.rs`) for the usual low-level leads: `unsafe` blocks and
functions (CWE-119), `from_raw_parts`/`set_len`/`transmute` (CWE-125/787/704), narrowing integer
casts near size arithmetic (CWE-190/197), `Command::new` (CWE-78), symlink-following filesystem
calls (CWE-59/22 TOCTOU), and `unwrap()`/`expect()` on parsed input (a panic is a denial of
service). If the repository's dominant language has no pack, `scan_pitfalls.py` prints a loud
warning rather than a clean-looking candidate list drawn only from incidental files (CI workflows, a
Dockerfile); add a pack before trusting the run.

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

  For Node and Next.js apps, running a test against a copy means `npm ci` (often five minutes or
  more) and mocks for the database client and the session library. Budget for it: run tests for
  the P0 and P1 findings first, reuse one prepared copy for all of them, and mock at the module
  boundary (`vi.mock('@/lib/prisma')`, `vi.mock('next-auth')`) so each test calls the route handler
  function directly with a `Request`. If the brief rules out installing or running the toolchain,
  write the test as a sketch with `result: not_run` and a note that says so; never claim a result
  you didn't run.

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
security-severity, not exploit steps, so it can go to GitHub code scanning (with `--sarif` only the
file is written). Give the user the rendered report, the P0 and P1 findings first, and what wasn't
covered.

One report covers one repository (`--repo` re-reads every location against one root, and the
history scan and the commit are per repository). When several repositories make one system, write
one report each and link findings across them in `chains_with` as `<repository>#F-n` (the
repository name as in that report's `target.repository`); the validator checks local references
and leaves sibling ones to that report.

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
- Locations (file, line and snippet) against the source: the line must be the one that holds the
  snippet, so for a statement over several lines cite the line with the dangerous expression.
  Agent-found findings' basis excerpts are checked the same way, and so are observations'
  optional `excerpt`. Recon entry points' handler lines must exist, and in Python must not sit
  inside a string or docstring. Regression tests exist and fail on the vulnerable code; variant
  rule files exist.
- `dependency_advisories` (required from schema 1.2): `assessed` or `partial` need a dependency
  tool that ran, `not_assessed` lists no advisories, and anything but `assessed` has a reason, is
  named in the limitations, and rules out a headline claiming no vulnerable dependencies.
- No secret value in the report or the evidence, and nothing written inside the repository. The
  values come from findings with a secret block and from secret candidates that weren't dismissed
  (a dismissed one still counts if it looks like a real key); placeholder words and values under
  8 characters are skipped, only JSON string values are searched (not keys), short values must
  match as whole tokens, and an error names the candidate and the JSON path, never the value.
- No `TODO(seed)` placeholder from `seed_report.py` left anywhere.
- Secret blocks agree with the scanner: a secret it found in history is not reported as absent,
  `no` is not claimed when it could not tell, and `git_status` matches. A `partial` or `errored`
  history scan is named in the limitations.
- Summary counts recomputed; headlines claiming the code is secure, safe or free of issues are
  rejected ("secure" in a technical phrase such as "the Secure cookie flag" is fine).
