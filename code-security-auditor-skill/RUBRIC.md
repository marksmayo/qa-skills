# Code Security Auditor rubric

Anchors for confidence, CVSS v4.0 metrics, exposure, priority, triage, safe PoCs and secrets.
`SKILL.md` says when to apply them; `data/pitfalls.json` is the machine-readable weakness catalogue.

## Confidence

| Confidence | Needs | Typical evidence |
|---|---|---|
| `confirmed` | A PoC ran against localhost or a named test environment, with permission, and its proof passed. Evidence file with every request and response. For a CLI, library or stdio server: a command PoC (`run_poc.py --workdir`) in a copy outside the repository, with every command, exit code and output excerpt | Boolean diff, computed marker, Location header; a computed marker in a CLI's output, a canary file the payload created, a regression test failing with its assertion message |
| `traced` | The full path read in code: source (file:line) -> transforms -> sink, and the guards that were checked and why they fail | At least a source and a sink location, or the config line for configuration findings |
| `possible` | The weakness is real but part of the path is not shown; `path.gap` says which part | Weak randomness with no demonstrated prediction; a sink whose input source is unclear |

`informational` items (hardening, defence in depth) go to `observations`, not findings. A PoC
that ran and failed (`not_confirmed`) means the finding is at most `traced`, with the reason.

## CVSS v4.0 anchors

Rate the vulnerable system (the app) and subsequent systems (what it reaches: other services, the
user's browser, the CI token's repository). Base metrics only unless the user supplies threat or
environmental context.

| Metric | Use | Examples |
|---|---|---|
| AV | N: over the network; A: same network segment (a developer's machine on shared Wi-Fi); L: local or log access; P: physical | Debug server bound to 0.0.0.0 on a laptop: A |
| AC | H when the attacker must beat something they don't control (a PIN, a race, state prediction) | Reset-token prediction: H |
| AT | P only when a condition the attacker can't influence must hold and is unusual in production (a missing env var, a .netrc on the server, a browser without SameSite=Lax). N when the condition holds in the deployment as shipped or the attacker can arrange it (two requests at once, an IPv6 literal, a token that is issued anyway); put any difficulty in AC | Hard-coded fallback key: P. SSRF filter that misses IPv6: N. Double booking by racing two requests: N (AC:H if the window is narrow) |
| PR | N: no account; L: any account; H: admin, or a credential only operators hold | Self-registration counts as L. A database open to the internet that still needs its administrator password: PR:H, exposure `restricted` |
| UI | N; P: victim does something normal; A: victim follows a crafted link or page | Open redirect: A |
| VC/VI/VA | Impact on the app | SQLi read-only (sqlite3 single statement): VC:H, VI:N |
| SC/SI/SA | Impact beyond the app | SSRF into internal services: SC:H; CI token writes to the repository: SI:H |

A deployed setting you can't see (is the development bypass variable set in production?) is an
attack requirement: `AT:P` with the setting named in the rationale, and the finding stays `traced`
when the code path itself is read to the end. `possible` is for a path you could not read.

`scripts/cvss4.py` computes the score; the validator recomputes it. Severity bands: critical 9.0-10,
high 7.0-8.9, medium 4.0-6.9, low 0.1-3.9; a 0.0 score is an observation.

## Exposure

| Exposure | Weight | Anchor |
|---|---|---|
| `public` | 3 | Reachable without an account: public routes, any GitHub user opening a pull request, a malicious web page (CSRF) |
| `authenticated` | 2 | Any account of the normal kind (sign-up or customer accounts count) |
| `restricted` | 1 | Admin-only, internal network, adjacent network, log or server access, or a compromised third party |
| `local` | 1 | A local attacker supplies the input the program processes (a file, argument, stdin or environment variable): a CLI tool or library with no network listener. The attack vector is local (`AV:L`), which the CVSS score already reflects |

Exposure describes the finding alone. When another finding changes it (a forged session makes an
authenticated route public), say so in `impact` and `chains_with`; don't re-rate.

## Priority

`priority_score = severity weight (critical 4, high 3, medium 2, low 1) x confidence weight
(confirmed 4, traced 3, possible 2) x exposure weight (public 3, authenticated 2, restricted 1, local 1)`.

P0 >= 32, P1 >= 18, P2 >= 8, P3 < 8. Caps: Critical severity needs `traced` or `confirmed`;
`possible` findings are at most P1.

**Business impact** (`business_impact`) raises a P2 or P3 finding one level when exploiting it
causes `direct_financial_loss` (revenue lost on every use: a reusable discount, a price taken from
the client, a refund without a check), `fraud`, or a `regulated_data_breach` (health, payment or
government-id data). The basis says where the loss happens. It never creates a P0 and the
`possible` cap still applies; `none` records that it was considered.

Rank: priority, then priority score, then CVSS score, then confidence.

Examples: confirmed critical public = 48 (P0); confirmed high authenticated = 24 (P1); traced high
authenticated = 18 (P1); confirmed medium authenticated = 16 (P2); traced critical restricted = 12 (P2);
traced medium authenticated with direct financial loss = 12, raised to P1.

## Triage

**Promote** when the source is attacker-controlled, the path reaches the sink and no guard stops
it. One root cause is one finding listing every candidate (the XSS and SSTI hits on one
concatenation are one finding).

**Dismiss** with a reason someone can check:

- the guard and its line (`int converter on the route; bound parameter at invoices.py:38`);
- an invariant (`only the module constant COLUMNS is interpolated`);
- not attacker-controlled (`fixed argument list, no request data, shell=False`);
- a duplicate of a promoted candidate.

**Observation** for real but informational issues. "False positive" alone is rejected.

**Dependencies:** `yes` (the vulnerable path is used), `conditional` (only under a stated
condition), `no` (the advisory needs an API or setting the code doesn't use, or every installed
copy is a devDependency that isn't bundled into production), `unknown` (say what would settle it).
`yes` and `conditional` need a finding; several advisories in one package that one upgrade fixes
can share one "upgrade X" finding.

**Agent-found findings** (`source: agent`): no tool candidate led to them, so the `basis` (file,
line and an excerpt on that line, or runtime evidence) is what makes them checkable.

## Safe PoCs

Allowed:

- Boolean or error-based SQL injection that only reads (`' OR 1=1 --` vs `' OR 1=2 --`).
- Computed markers for template and expression injection (`{{7*191}}` -> `1337`).
- Reading a harmless file the app ships (its own `requirements.txt`) for traversal.
- Loopback URLs on the target itself (`/healthz`) for SSRF; reserved domains (`canary.example`) for
  redirects.
- Forged tokens or cookies for test accounts, passed via `{env:NAME}` so the evidence redacts them.

Never: `DROP`/`DELETE`/`UPDATE`/`INSERT`, `rm`, writes to real data; `SLEEP`, `pg_sleep`,
`WAITFOR DELAY`, `BENCHMARK`, payloads that exhaust memory or CPU; `__import__`, `os.system`,
`!!python/` tags, `Runtime.exec` and other code-execution primitives; reverse shells and
persistence; cloud metadata addresses (169.254.169.254 and equivalents); URLs to any host except
the target and reserved test domains. `run_poc.py --check` lints these, and a marker that already
appears in the request is rejected.

## Secrets

- Report type, file, line, key name, length, a `sha256:` fingerprint (first 16 hex characters) and
  `in_git_history` (`yes`, `no`, or `unknown` with why), with `history_basis` (`full`: every scanned
  commit; `path`: only commits touching that file) and the file's `git_status` (`tracked`,
  `untracked`, `ignored`).
- Gitignored, never committed (`ignored`, `no`, `path`): exposed only on the machines that hold it;
  rotation is a judgement call, say why. Committed: exposed to everyone with the repository.
- A secret in history is exposed even if removed from HEAD: `rotate: true`.
- A `partial` history scan (time budget or `--max-commits` reached) can't support `no` with basis
  `full`; name it in the limitations with the commits scanned.
- Paths to credential files and public fields (`token_uri`, `auth_uri`, certificate URLs) are not
  secrets. Note a credential file's existence and git status; its path can be named.
- Snippets stop before the value (`os.environ.get("SECRET_KEY",`).
- Test fixtures and `.env.example` placeholders are not secrets; real-looking keys in tests still are.
  `scan_secrets.py` sets aside test-file literals without a provider format and under 4 bits per
  character or 20 characters (`test_fixtures`), and skips instruction values (`generate-...`,
  `your-...`, `<...>`), hyphenated words in documentation, lists of field names, references to
  another variable (`$NAME`, `$(NAME)`, `$env:NAME`, `%NAME%`) and ARM expressions. A hard-coded
  fallback is never a placeholder.
- A secret only in history is a candidate (`history_only`, `git_status: removed`) and is rotated.
  Dismissing one is allowed only with a reason that says it was rotated or revoked, or why it needs
  no rotation (a public key by design such as an analytics write key, a placeholder, a fake test
  value); the validator rejects a dismissal that says neither. gitleaks history hits get the same
  facts (`history_commits`, and `history_only` when the file is gone from HEAD).
- Generic gitleaks hits (`generic-api-key`) where scan_secrets' rules see no secret, and low-entropy
  test-file literals, are set aside by `merge_findings.py` (`secrets_set_aside`, with reasons);
  provider-format hits are never set aside.

## Weakness catalogue

`data/pitfalls.json` has 129 patterns in 16 packs (Python, JavaScript/TypeScript, Java/Kotlin,
Android manifests, iOS, Go, PHP, Ruby, C#, C/C++, Rust, GitHub Actions workflows and composite
actions, Dockerfiles, Terraform, Azure Bicep/ARM, Azure Pipelines), each with a CWE, an OWASP 2025 category, a typical CVSS vector, a fix
and the matching rules of other tools.

| Category | Weaknesses |
|---|---|
| Injection | SQL and NoSQL (including Prisma `$queryRawUnsafe`/`$executeRawUnsafe`, `Prisma.raw` and `$queryRaw` called with a string), OS command, template (SSTI), eval, LDAP/XPath, header and log injection, format strings, CSV formula injection |
| Web | XSS sinks (including `dangerouslySetInnerHTML` with non-literal content and values in `<style>` blocks), CSRF, open redirect (`res.redirect`, `NextResponse.redirect`, `redirect()`, `router.push`, and `startsWith('/')`-only checks), CORS, cookie flags, postMessage origin checks, shared-cache headers on session responses |
| Server-side | SSRF, path traversal and zip slip, XXE, unsafe uploads (including the client's `file.type` stored as the content type, and uploads read whole into memory or handed to a parser with no size limit), unverified webhooks |
| Access control | IDOR/BOLA, missing function-level checks (recon: handlers with no auth call), mass assignment (`data: body`, `data: { ...body }`), mostly agent review |
| Authentication | Hard-coded session keys, JWT verification, reset tokens, rate limits, OAuth `state` not bound to the session, development bypass tokens or extra roles read from an environment variable with no environment guard |
| Cryptography | Weak hashes and ciphers, ECB, predictable randomness, disabled TLS verification |
| Data exposure | Secrets in code or history, credentials in logs, verbose errors |
| Language-specific | Deserialisation (pickle, YAML, Java, .NET, PHP, Ruby), prototype pollution, ReDoS, memory safety in C/C++ |
| Mobile | Exported components and deep links, WebView bridges, cleartext traffic, ATS exceptions, insecure storage |
| Supply chain and CI | `pull_request_target` checkouts, expression injection, unpinned actions, reachable vulnerable dependencies, `curl | sh`; Azure Pipelines every-branch triggers, literal secret variables, branch names and runtime parameters in scripts, service connections outside deployment jobs |
| Configuration | Debug mode, public storage, open admin ports, unencrypted storage; Azure public network access on data services, `0.0.0.0` firewall rules ("allow Azure services" admits every tenant), administrator password login, secret parameters without `@secure()`, Key Vault access policies listing every secret, FTP, no `httpsOnly`, TLS below 1.2, account keys in app settings |

OWASP Top 10 (2025): A01 Broken Access Control, A02 Security Misconfiguration, A03 Software Supply
Chain Failures, A04 Cryptographic Failures, A05 Injection, A06 Insecure Design, A07 Authentication
Failures, A08 Software or Data Integrity Failures, A09 Security Logging and Alerting Failures, A10
Mishandling of Exceptional Conditions.

JavaScript source patterns cover Express (`req.query`, `req.body`, `req.params`) and Next.js
(`request.nextUrl.searchParams`, `searchParams.get(...)`, `await request.json()`/`formData()`,
`params.x`, `body.x`, `headers().get`, `cookies().get`).

## Framework notes: Next.js

- Route handlers (`app/**/route.ts`) and `pages/api` are public endpoints unless the handler checks
  the session: `middleware.ts` (or `proxy.ts`) only covers what its `matcher` and any prefix list
  say, and page-protecting middleware usually leaves `/api/*` out. recon records per handler
  whether it calls a session or guard helper, a machine credential check, or nothing.
- Server actions (`'use server'`) are POST endpoints any client can call by action id; they need
  their own session and ownership checks, and their arguments are untrusted.
- NextAuth: `callbacks.redirect` (open redirect), `callbacks.jwt`/`session` (what goes in the
  token, stale roles after a change), `authorize` (rate limits, enumeration), and whether a deleted
  or disabled user keeps a valid JWT.
- Responses that read the session must not carry `Cache-Control: public` or `s-maxage`.
- Prisma: `$queryRaw` as a tagged template is parameterised; the `Unsafe` variants and `Prisma.raw`
  are not. `data: body` lets the caller set any column, including owner and role fields.

## Framework notes: FastAPI and Azure

- A FastAPI route's auth is the union of its own dependencies (`dependencies=[...]`,
  `Depends`/`Security` parameters, `Annotated` aliases), its router's, the `include_router()` call's
  and the app's. recon resolves these across files and names each dependency; read them.
- Development bypasses (a static token compared to the bearer, roles merged from an environment
  variable) are findings unless something fails closed outside local development.
- `UploadFile.file.read()` reads the whole body; parsers such as openpyxl then expand compressed
  content. Check for a limit at the proxy, the server and in the handler.
- Azure Functions: `auth_level=ANONYMOUS` is public; FUNCTION/ADMIN keys are shared secrets, not
  user identity. Triggers (blob, queue, Service Bus) take input from whoever can write to the source.
- Azure Pipelines: template expressions (`${{ parameters.x }}`) are pasted into scripts before the
  shell runs; predefined variables such as `$(Build.SourceBranchName)` carry attacker-chosen branch
  names. Service connections belong in deployment jobs behind an environment with approvals.

## Reviewer checklist

- [ ] Recon covers every entry point (including CI workflows, composite actions, server actions, middleware, CLI commands, MCP tools, local listeners and config inputs), with auth confirmed by reading; any recon entry point dropped is in `removed_entry_points` with the reason.
- [ ] Every tool is ran, not_run (why), errored (why) or not_applicable; no licensed tools.
- [ ] Every candidate and advisory triaged with checkable reasons.
- [ ] Every finding has a traced path (or a stated gap), a CVSS vector with rationale, repro steps,
      impact, fix and a regression test that fails on the vulnerable code.
- [ ] Confirmed findings had permission, a safe target and canary-only PoCs; proofs fail on the fix.
- [ ] No secret values anywhere; the report and evidence are outside the repository.
- [ ] Coverage and limitations say what wasn't reviewed; the headline doesn't call the code secure.
