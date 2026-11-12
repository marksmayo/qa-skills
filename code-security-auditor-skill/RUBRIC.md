# Code Security Auditor rubric

Anchors for confidence, CVSS v4.0 metrics, exposure, priority, triage, safe PoCs and secrets.
`SKILL.md` says when to apply them; `data/pitfalls.json` is the machine-readable weakness catalogue.

## Confidence

| Confidence | Needs | Typical evidence |
|---|---|---|
| `confirmed` | A PoC ran against localhost or a named test environment, with permission, and its proof passed. Evidence file with every request and response | Boolean diff, computed marker, Location header |
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
| AT | P when a deployment condition must hold (a missing env var, a .netrc on the server, a browser without SameSite=Lax) | Hard-coded fallback key: P |
| PR | N: no account; L: any account; H: admin | Self-registration counts as L |
| UI | N; P: victim does something normal; A: victim follows a crafted link or page | Open redirect: A |
| VC/VI/VA | Impact on the app | SQLi read-only (sqlite3 single statement): VC:H, VI:N |
| SC/SI/SA | Impact beyond the app | SSRF into internal services: SC:H; CI token writes to the repository: SI:H |

`scripts/cvss4.py` computes the score; the validator recomputes it. Severity bands: critical 9.0-10,
high 7.0-8.9, medium 4.0-6.9, low 0.1-3.9; a 0.0 score is an observation.

## Exposure

| Exposure | Weight | Anchor |
|---|---|---|
| `public` | 3 | Reachable without an account: public routes, any GitHub user opening a pull request, a malicious web page (CSRF) |
| `authenticated` | 2 | Any account of the normal kind (sign-up or customer accounts count) |
| `restricted` | 1 | Admin-only, internal network, adjacent network, log or server access, or a compromised third party |

Exposure describes the finding alone. When another finding changes it (a forged session makes an
authenticated route public), say so in `impact` and `chains_with`; don't re-rate.

## Priority

`priority_score = severity weight (critical 4, high 3, medium 2, low 1) x confidence weight
(confirmed 4, traced 3, possible 2) x exposure weight (3, 2, 1)`.

P0 >= 32, P1 >= 18, P2 >= 8, P3 < 8. Caps: Critical severity needs `traced` or `confirmed`;
`possible` findings are at most P1. Rank: priority score, then CVSS score, then confidence.

Examples: confirmed critical public = 48 (P0); confirmed high authenticated = 24 (P1); traced high
authenticated = 18 (P1); confirmed medium authenticated = 16 (P2); traced critical restricted = 12 (P2).

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
condition), `no` (the advisory needs an API or setting the code doesn't use), `unknown` (say what
would settle it). `yes` and `conditional` need a finding.

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

## Weakness catalogue

`data/pitfalls.json` has 90 patterns in 13 packs (Python, JavaScript/TypeScript, Java/Kotlin,
Android manifests, iOS, Go, PHP, Ruby, C#, C/C++, GitHub Actions, Dockerfiles, Terraform), each with
a CWE, an OWASP 2025 category, a typical CVSS vector, a fix and the matching rules of other tools.

| Category | Weaknesses |
|---|---|
| Injection | SQL and NoSQL, OS command, template (SSTI), eval, LDAP/XPath, header and log injection, format strings |
| Web | XSS sinks, CSRF, open redirect, CORS, cookie flags, postMessage origin checks |
| Server-side | SSRF, path traversal and zip slip, XXE, unsafe uploads |
| Access control | IDOR/BOLA, missing function-level checks, mass assignment (mostly agent review) |
| Authentication | Hard-coded session keys, JWT verification, reset tokens, rate limits |
| Cryptography | Weak hashes and ciphers, ECB, predictable randomness, disabled TLS verification |
| Data exposure | Secrets in code or history, credentials in logs, verbose errors |
| Language-specific | Deserialisation (pickle, YAML, Java, .NET, PHP, Ruby), prototype pollution, ReDoS, memory safety in C/C++ |
| Mobile | Exported components and deep links, WebView bridges, cleartext traffic, ATS exceptions, insecure storage |
| Supply chain and CI | `pull_request_target` checkouts, expression injection, unpinned actions, reachable vulnerable dependencies, `curl | sh` |
| Configuration | Debug mode, public storage, open admin ports, unencrypted storage |

OWASP Top 10 (2025): A01 Broken Access Control, A02 Security Misconfiguration, A03 Software Supply
Chain Failures, A04 Cryptographic Failures, A05 Injection, A06 Insecure Design, A07 Authentication
Failures, A08 Software or Data Integrity Failures, A09 Security Logging and Alerting Failures, A10
Mishandling of Exceptional Conditions.

## Reviewer checklist

- [ ] Recon covers every entry point (including CI workflows), with auth confirmed by reading.
- [ ] Every tool is ran, not_run (why), errored (why) or not_applicable; no licensed tools.
- [ ] Every candidate and advisory triaged with checkable reasons.
- [ ] Every finding has a traced path (or a stated gap), a CVSS vector with rationale, repro steps,
      impact, fix and a regression test that fails on the vulnerable code.
- [ ] Confirmed findings had permission, a safe target and canary-only PoCs; proofs fail on the fix.
- [ ] No secret values anywhere; the report and evidence are outside the repository.
- [ ] Coverage and limitations say what wasn't reviewed; the headline doesn't call the code secure.
