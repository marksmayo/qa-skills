---
name: code-security-auditor
description: Audit a codebase for security weaknesses with open-source scanners (Semgrep OSS, Bandit, gosec, Brakeman, gitleaks, osv-scanner, pip-audit, npm audit, zizmor, Trivy, Checkov and more) plus a deep manual review, trace each finding from attacker-controlled input to the dangerous operation, rate it with CVSS v4.0, and write safe reproduction steps, a fix and a regression test. Optionally confirms findings with canary-only proofs of concept against localhost or a test environment the user names. Every candidate is triaged, every tool's status is recorded, and the report never calls the code secure.
version: 1.9.0
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
   - `confirmed`: a proof of concept ran against a local or test instance (or, for a CLI, library
     or stdio server, a command PoC in a copy outside the repository) and its proof passed.
   - `traced`: a complete source -> sink path with no effective sanitiser, read in the code.
   - `possible`: the weakness is real but part of the path is not shown (say which part in `path.gap`).
   Informational issues go in `observations`, not findings.
3. **Every candidate is triaged.** Promote it to a finding, record it as an observation, or dismiss
   it with a reason someone else can check (the guard and its line, the invariant, or why the input
   is not attacker-controlled). "False positive" on its own is not a reason. A triage row links to
   what it became through `finding`: `"finding": "F-3"` for a promoted candidate and
   `"finding": "O-1"` (with `decision: observation`) for an observation; there is no separate
   `observation` field. A grouped candidate (`grouped_lines`, documented FFI `unsafe` blocks in one
   file) is triaged once, and its reason covers every line in the group.
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
commands, MCP tools, local listeners, config files the program loads, mobile deep links and
exported components, CI workflow triggers such as `pull_request_target`, `workflow_run` and
`issue_comment` (read only from the workflow's `on:` block), and client-side sources in JavaScript and
inline scripts: `location.search`/`hash`, `useSearchParams()`, `postMessage`, `document.referrer`,
`window.name`, web storage and cookies, so a static site has entry points too; an inline script
repeated across generated pages is listed once with `also_in`; server code, config files and test
setup are not client sources), a first guess at each route's auth requirement, data stores,
outbound calls, secret configuration, dependency manifests, CI workflows and review leads. Then
read the code to confirm it: add entry points the patterns missed (CI workflows, dynamically
registered routes), correct auth guesses (`unknown` is not `public`), and write the trust
boundaries and the auth model (authentication, sessions, authorisation). The map decides where the
deep review goes; record it in `recon`. If you drop an entry point recon found (a duplicate, a
string that only looks like a listener), list it in `recon.removed_entry_points` with the reason:
the validator's `--recon` checks every recon entry point is either in the report or removed with a
reason. Removals are matched by id, so one of two entry points recon found on the same line (two
client sources in one template line) can be removed while the other is kept.

**Data directories.** A web app often ships a data tree next to its code (a directory of thousands
of JSON records, CSV exports). recon, the pattern scan, the secret scan and `run_scanners.py`'s
inventory prune a directory whose own files number 200 or more, are at least 95% data files
(`.json`, `.csv`, `.xml`, `.txt`, ...) and include no source code, without opening any of them
(`scripts/data_dirs.py`), and record it under `skipped_data_dirs` with the file count and reason;
Semgrep is passed `--exclude` for it. Each script takes `--exclude-dir PATH` (repeatable) to leave
out more, and `--scan-data-dirs` to read them after all. A pruned directory was not checked for
secrets or code: `seed_report.py` adds it to `limitations`; keep it there, or rerun the secret scan
with `--scan-data-dirs` when the data could hold credentials.

**Sample projects, copied files and code inside strings.** Tool and library repositories often carry
deliberately broken sample projects (`examples/shop/` with its own `requirements.txt`, a sample site
with its own `package.json`, a `testdata/` app with an `expected.json`) and files kept identical in
several folders (a script copied into every package, declared in a `tools/check_shared.py`-style
mapping). A planted defect in a sample is not the repository's finding, and a copied file is one
piece of code. So, by default, recon, the pattern scan, `run_scanners.py` (its inventory, Semgrep
and Bandit) and `merge_findings.py` leave out every fixture project (`scripts/audit_scope.py` on the
shared `repo_layout.py`) and list it under `fixtures_excluded` with the reason; `--include-fixtures`
audits them too, `--fixture-dir NAME|PATH` names more. The secret scan still reads them (a real key
committed in a sample is still exposed) but sets aside their low-entropy literals, and those of any
file under `examples/`, `fixtures/`, `testdata/` or `samples/`, the way it sets aside test-file
literals; a real-looking key stays a candidate. Their lockfiles are listed under `fixture_lockfiles`
and never decide `dependency_advisories`. A file kept identical in several places is read once: its
entry points, outbound calls and pattern hits are listed on the canonical copy with `copies`
(`copy_groups` lists the groups), and a Semgrep or Bandit hit on a copy joins the canonical
candidate. `seed_report.py` names each left-out fixture project in `target.excluded` and the
limitations, and the copies in each entry point's note and triage reason; the validator's `--recon`
rejects a report that does not name a fixture project recon left out. In Python and
JavaScript/TypeScript, recon and the pattern scan match only code: a pattern that lies wholly inside
a string, docstring or comment (a scanner's own pattern table, a usage example in a docstring, a
fixture string holding sample server code) is not an entry point, a queue consumer, an MCP tool, an
agent permission handler or a pattern hit, which keeps recon in agreement with the validator (it
rejects entry points inside strings). Pitfalls that read string content by design (security header
values) say so with `match_in_strings`, and a Jinja `|safe` inside a Python template string is
matched by the pitfall's `string_regex`. The secret scan does not report a detection rule's own
regular expression in a rule catalogue, or a `fake` password in a documented connection string.

**Server-rendered templates and rate limits.** recon lists server-side templates (`templates`:
HTML with `{{ }}` or `{% %}`, `.jinja`, `.j2`, `.njk`), part of the web surface, which the pattern
scan's `templates` pack reads. It records `rate_limiting`: the library (Flask-Limiter, slowapi,
django-ratelimit, express-rate-limit, ...), the routes with a limit decorator, and where a limiter's
key reads the client address from `X-Forwarded-For`, `CF-Connecting-IP` or a similar header any
client can set; each such key, a web app with no limiter at all, and every public route that
returns a collection (search, list, export, autocomplete) are `availability` review leads. Python
servers started from code (`app.run`, `uvicorn.run`) are `listener` entry points with their bind
host.

A directory named `e2e`, `test(s)`, `spec`, `playwright` or `cypress` is test code only when it is
not a package of its own: a workspace package `packages/e2e/` whose `package.json` has `bin`,
`main` or `exports` is product code, and recon, the pattern scan and the secret scan read it
(`scripts/test_paths.py`). Entry points under `examples/`, `samples/`, `demos/` or a benchmark or
testbed app carry `example: true` (seeded with a note), and `frameworks` labels frameworks seen
only there (`Next.js (examples only: examples)`) or used only as a client (`GraphQL (client only)`).

**Node CLIs, MCP servers and agent bridges.** recon lists package.json `bin` commands and
commander/yargs/`parseArgs`/`process.argv` dispatch (`cli`, one entry per named subcommand), MCP
tools registered with `registerTool`/`server.tool` (`mcp_tool`, with the transport: `stdio` or
`http`), `http`/`net` `createServer` and `WebSocketServer` listeners with their bind host
(`listener`: `loopback`, `all` for `listen(port)` with no host, or `unknown`), and config files
loaded from the working directory (`config_input`). `seed_report.py` gives CLIs, config inputs,
stdio MCP tools and loopback listeners exposure `local`; a listener on every interface or with an
unknown bind host stays `internet` until you read it. Agent permission handlers (ACP
`requestPermission`, `canUseTool`) and composite actions that expand an input in `run:` are review
leads. In the deep review check: where config files are searched for (parent directories, the home
directory) and whether a `.js`/`.ts` config is executed; `npx -y <pkg>` and other install-on-run
commands in plugin or MCP manifests; MCP tool argument validation (paths outside the workspace,
commands built from arguments) remembering that a prompt-injected model supplies those arguments;
loopback servers (bind address, Host and Origin checks against DNS rebinding, a random path token
or secret, CORS); `spawn`/`execFile` with a program taken from config or a flag, and any
`shell:` option (Windows re-parses arguments through `cmd.exe`); and what an agent permission
handler approves without asking.

**Go CLIs and services.** recon reads Go entry points from the code's imports
(`scripts/go_entries.py`). A `.Get("x")` is an HTTP route only in a file that imports a router
(net/http, chi, gin, echo, gorilla/mux, fiber), only for that router's registration methods, and
only when the first argument is a path (`"/x"`, or Go 1.22's `"GET /x"`) followed by a handler:
an interpreter's `rt.Get("output")`, an options object's `o.Get("user-data-dir")` and an HTTP
client's `client.Get("http://...")` are not routes. urfave/cli (v2 `cli.App` and v3 `cli.Command`
tables, including `Commands: []*cli.Command{ {Name: ...} }`) and cobra commands are `cli` entries
(`ROOT` or `COMMAND`); flags are listed in `cli_flags` with the environment variables bound to them
(`EnvVars`, `cli.EnvVars(...)`), and env-bound flags are a review lead. `net.Listen`,
`tls.Listen` and `http.ListenAndServe` are `listener` entries with their bind read from the
address: `unix` (a socket file), `loopback`, `all` (`":8080"`) or `unknown`; routes served only on
a unix or loopback listener in the same file or package carry that `bind`, and `seed_report.py`
seeds them `local`. A file that reads a file from disk and decodes YAML, JSON, TOML or a viper
config is a `config_input`. For tools that run flow, test or config files, treat those files as
code: whoever writes them (a cloned repository, a pull request) controls every command, path, URL
and script they name, so trace their fields to `exec.Command`, `filepath.Join`, downloads and
logs. In the deep review also check: unix sockets and PID files at fixed `/tmp` paths (another
local user can pre-create them), credentials in URLs (`user:pass@`) or flags reaching a logger or
a file written with 0644, `http.Get` downloads that are installed or executed without a checksum,
archive extraction (zip slip: `filepath.Join(dir, f.Name)` without `filepath.IsLocal` or a prefix
check), `InsecureSkipVerify`, and how release binaries are built (a GoReleaser `main` that does not
exist in the repository means the published binaries come from elsewhere: say provenance is not
verifiable).

Vendored and embedded third-party trees are not the repository's code: `vendor/`, `Pods/`,
`third_party/`, `PrivateHeaders/` and `Headers/` trees, bundled `.app`/`.framework` directories and
embedded packages with their own manifest and licence under `drivers/`, `sdk/` or `external/`
(`scripts/lang_census.py`) are left out of the language counts, the walk and the pattern scan, and
listed in recon's `vendored` with the reason. Check that list once: nothing in it should be product
code.

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

recon also reads every Next.js project with the shared static reader (`scripts/next_app.py`, via
`scripts/next_surface.py`), so a content site with no API has entry points too: each App Router or
`pages/` page (`page`, GET, auth read from the page and its layouts or a covering middleware), each
generated metadata route (`metadata_route`: `robots.ts`, `sitemap.ts`, `opengraph-image.tsx`, icon
routes), the built-in image optimiser (`http_route` GET `/_next/image`, at the `images` config line,
with the remote sources it may fetch; absent for a static export, a custom loader or
`unoptimized: true`), service workers (`service_worker`, at the worker file in `public/`, with the
registration, scope and fetch strategies) and middleware in nested projects. Pages and route
handlers carry `static` (prerendered at build: `dynamic = 'force-static'` or `'error'`, or
`output: 'export'`) and `params_bounded` (`generateStaticParams` with `dynamicParams = false`: only
build-time values are served, any other id is a 404), and `seed_report.py` copies the reason into
the entry point's note. These bound the attack surface: a bounded `[id]` is not an IDOR lead, and a
static route that returns a list (a feed) is not an uncapped-collection lead; neither is a route
whose handler caps the result with a literal (`slice(0, 20)`, `take: 50`). Outbound calls are
matched on code only (prose in a string such as "whatever you have got." is not a call), and
scripts under `public/` are browser code, not server-side requests.

**Security headers, CSP and service workers.** recon's `security_headers` lists the header rules in
`next.config` `headers()`, `vercel.json`, `netlify.toml` and `public/_headers` (source, catch-all or
not, header names), each Content-Security-Policy with its directives and weaknesses, the headers
missing everywhere (`Content-Security-Policy`; `frame-ancestors`, when there is no `X-Frame-Options`
either; `Strict-Transport-Security`) unless code sets them (`set_in_code`), shared-cache
`Cache-Control` on catch-all sources, the `images` config and the third-party scripts the pages
load. Each is a review lead; the `security_headers` pack of the pattern scan flags the same
weaknesses at their lines (`HDR-CSP-UNSAFE-INLINE`, `HDR-CSP-UNSAFE-EVAL`,
`HDR-CSP-WILDCARD-SOURCE`, `HDR-CSP-TAG-MANAGER`, `HDR-CACHE-CATCH-ALL`). Headers the CDN, the host
or a proxy adds are not in the repository: before reporting a header as missing, check a deployed
or locally started response (a `run_poc.py` guard check with `header_matches`), or say in the
observation that it was not checked. In the review:

- **CSP:** `'unsafe-inline'` in `script-src` (without nonces or hashes, which make browsers ignore
  it) means the policy does not stop an injected script; `'unsafe-eval'`; `*`, `https:`, `data:` or
  `blob:` as a script source; a tag-manager host (`googletagmanager.com` and the like) allowed as a
  script source lets whoever can publish the container run any script, so treat container access
  as deploy access; `Content-Security-Policy-Report-Only` enforces nothing. Missing
  `frame-ancestors` (and `X-Frame-Options`) allows clickjacking; missing HSTS allows a downgrade on
  a first visit.
- **Caching:** `Cache-Control: public, s-maxage=...` on `/:path*` or `/(.*)` applies to every
  route, including ones added later that read cookies; a CDN would serve one visitor's page to the
  next. Scope shared caching to static paths.
- **Image optimiser:** `images.remotePatterns` with hostname `**` (or the old `domains`) makes
  `/_next/image` an open image proxy and a cost amplifier; `dangerouslyAllowSVG` serves SVG that can
  carry script.
- **Third-party scripts** (`next/script`, `<script src>`, `@next/third-parties`): each runs with the
  page's privileges, and a versionless vendor URL cannot carry Subresource Integrity; name them and
  what they can read.
- **Service workers:** a worker answers every request in its scope. Check its fetch strategies
  (cache-first or stale-while-revalidate for navigations or for every request serves stored pages,
  including signed-in or error pages, until it is replaced), that it never stores cross-origin,
  opaque or per-user responses, that cache names are versioned and old caches deleted on
  `activate`, the scope it is registered with, `skipWaiting`/`clients.claim`, and that there is a
  way to roll back a bad worker (a kill-switch worker at the same URL).
- **Inline JSON-LD:** `dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }}` is its own
  pattern (`JS-JSONLD-STRINGIFY`), not an HTML sink: `JSON.stringify` does not escape `</script>`,
  so a value that comes from outside the repository (a CMS, the request, user content) can close the
  script element. When every value is a constant in the repository, record it as an observation (or
  dismiss it with that reason); otherwise trace the value like any XSS.

**Static and content sites.** A site with no API and no sign-in still has an attack surface, and a
complete review covers it: the pages and metadata routes recon lists, and for each dynamic segment
whether its parameters are bounded; the response headers and CSP above; every third-party script,
stylesheet and font (which vendors, loaded how, with or without SRI, and what the CSP allows); the
service worker; the image optimiser; route handlers (feeds, search, revalidation or preview hooks)
and server actions such as a contact form (spam, mail-header injection, outbound calls); where the
content comes from (repository constants and Markdown, a CMS, a build-time fetch): content from
outside the repository is attacker-controlled input to every sink it reaches, including JSON-LD;
redirects and rewrites in the config (open redirects through `destination` parameters); and
**committed agent and editor configuration**. recon lists `agent_config`: files such as
`.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`, `.cursor/mcp.json`,
`.vscode/settings.json`, `.vscode/tasks.json` and `.vscode/launch.json` that grant permissions
(shell commands, `git push`, web fetches to any domain), run commands (hooks, tasks, launch
configurations) or start MCP servers for everyone who opens the repository in that tool. Read
each: an allow-list or hook a contributor inherits by cloning is a supply-chain path, a token or a
personal path in it is data exposure, and a personal `*.local.json` usually belongs in
`.gitignore`. When the review finds nothing exploitable, the report says what it covered
(`coverage`, `limitations`) and records weaknesses that need another condition as observations;
render_report.py says "No findings" instead of printing an empty table.

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

**GitHub Actions.** Triggers come only from the `on:` block, so `issues: write` under
`permissions:` and a job named `release` are not triggers. Composite actions (`action.yml` under
`.github/` or at the root of an action repository) are listed in `ci_workflows` with their inputs
and `inputs_in_run` (inputs a `run:` script expands directly); the github_actions pack and zizmor
read them too. An input passed through `env:` and used as `"$NAME"` is the safe form.

### 2. Run the scanners

```bash
python scripts/run_scanners.py <repo> --out-dir <out> [--offline] [--osv-local-db <dir>] [--cargo-audit-db <dir>]
```

Whatever runs, the packages in lockfiles and requirements files are listed offline in
`<out>/dependencies.json` (a requirements file with version ranges lists every requirement, with its
`constraint` and `version` null when it is not pinned to one version; its lockfile row counts them as
`unpinned`), and `scanners.json` records `dependency_advisories`: `assessed`,
`partial` (a lockfile no tool covered, say npm with only pip-audit) or `not_assessed` (no advisory
tool ran, for example with `--offline`). Coverage is per lockfile: each tool row lists the lockfiles
it `covers`, so npm audit on one nested `package-lock.json` (a composite action's) does not count
as checking a pnpm workspace. `pnpm audit --json` runs next to each `pnpm-lock.yaml` when pnpm is
installed; when it isn't (corepack not enabled), the row is `not_run` with how to run it, and only
osv-scanner covers that lockfile. Copy the status into the report's `dependency_advisories`; "not
assessed" is never "no vulnerable dependencies", and the limitations must say it. With
`--osv-local-db` (an osv-scanner offline database downloaded beforehand), osv-scanner checks
advisories without the network. For Go modules, govulncheck runs next to each `go.mod`
(`govulncheck -format json ./...`, which needs the Go toolchain and the module cache) and covers
it; `merge_findings.py --deps <out>/govulncheck.json` reads its JSON stream and keeps the
call-graph level as `reachability`: `symbol` (a vulnerable function is reached; `call_path` names
the frames), `package` (imported, no vulnerable symbol reached) or `module` (required, package not
imported). `seed_report.py` seeds `symbol` as `reachable: yes` (write the finding, or say why the
path cannot run) and `package`/`module` as `no` with govulncheck's basis; a `-scan package` or
`-scan module` run proves nothing about symbols and seeds nothing. For Rust, cargo-audit runs on each
`Cargo.lock` (`cargo-audit audit --json`); with `--offline` it runs `--no-fetch --stale` against a
local RustSec copy (`--cargo-audit-db`, default `CARGO_HOME/advisory-db`) and needs no network, and
its tool row records the database's advisory count and last update (`advisory_db`). An offline copy
is only as current as that date: say so in the limitations (`seed_report.py` adds the line). The
yanked check reads the crates.io index and may fail offline; the error is kept in the row's reason.
cargo-deny (`check advisories`, JSON on stderr) runs too when installed. `merge_findings.py --deps`
reads both: vulnerabilities, unmaintained, unsound and yanked crates become `DEP-nnn` rows with a
`kind` (a yanked release's advisory is `yanked:<crate>@<version>`); an unmaintained crate needs a
basis like any other row (is there a maintained replacement, does the code use the affected API).
If you ran an advisory tool by hand, add its row with `output` pointing at the file it wrote and
`results` as counted there, add every advisory it reported to `dependencies`, and set
`dependency_advisories` from what it covered: the validator reads that output and rejects a row
whose file is missing or whose advisories are not in the report. Without any advisory tool, packages you can see are risky by
reading (an abandoned release line, a known-vulnerable pin) are agent findings with confidence
`possible`.

It runs every free, open-source tool that applies and is installed, writes their outputs to
`<out>` (refused if inside the repository) and records each tool as `ran`, `not_run` (with why),
`errored` or `not_applicable` in `<out>/scanners.json`. Copy those statuses into the report's
`tools`, with the elapsed time and number of results each one recorded. npm audit on a large lockfile
(over a thousand packages) can take several minutes; it has its own limit (`--npm-audit-timeout`,
default 600 seconds; every other tool has `--timeout`, default 900), and long runs print progress
every 30 seconds. zizmor audits workflows and composite actions; its online audits
(impostor commits, known-vulnerable actions, ref confusion) run when `GH_TOKEN` or `GITHUB_TOKEN` is
set and `--offline` is not given (the token is read from the environment, never put on the command
line), and the row's note says when they were skipped. Use only tools whose licences allow scanning closed-source code without a paid account:
CodeQL is excluded, and so are Semgrep rules or features that need a login. Ask before installing
anything; a missing tool is recorded, not silently skipped.

| Area | Tools |
|---|---|
| Multi-language SAST | Semgrep OSS with `p/default` and `p/owasp-top-ten` |
| Language-specific | Bandit (Python), gosec (Go), Brakeman (Rails), ESLint security plugins (JS/TS, only if the repo has them), Psalm taint analysis (PHP, only if installed in the repo), SpotBugs + FindSecBugs (Java/Kotlin: needs a build, run it there), MobSF (needs a package and a server) |
| Secrets | gitleaks (including history), plus `scan_secrets.py` |
| Dependencies | osv-scanner, pip-audit, npm audit, pnpm audit, govulncheck (Go, call-graph aware), cargo-audit and cargo-deny (Rust, RustSec) |
| CI | zizmor (GitHub Actions workflows and composite actions), Checkov (GitHub Actions and Azure Pipelines), plus the pattern packs |
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
                                                                     # [--exclude-dir DIR] [--scan-data-dirs]
                                                                     # [--include-fixtures] [--fixture-dir DIR]
python scripts/scan_secrets.py <repo> --history --out <out>/secrets.json
python scripts/merge_findings.py --root <repo> --patterns <out>/patterns.json --secrets <out>/secrets.json \
    --sarif <out>/semgrep.sarif <out>/zizmor.sarif <out>/gitleaks.sarif ... --bandit <out>/bandit.json \
    --deps <out>/pip-audit.json <out>/npm-audit.json <out>/pnpm-audit.json <out>/osv.json \
    [--npm-lock <repo>/package-lock.json] [--pnpm-lock <repo>/pnpm-lock.yaml] --scanners <out>/scanners.json \
    --out <out>/candidates.json
python scripts/seed_report.py --recon <out>/recon.json --candidates <out>/candidates.json \
    --scanners <out>/scanners.json --repo <repo> --out <out>/security_audit.json
```

`seed_report.py` writes the copied parts of the report (entry points, tools rows, one triage row per
candidate, one dependencies row per advisory, coverage totals and counts) and marks everything that
needs judgement `TODO(seed)`; the validator rejects any placeholder left behind. It refuses to
overwrite an existing file; to seed again (after a recon or merge rerun), seed to a new file, or
pass `--force` knowing it replaces everything you wrote. Never re-seed into a report you have
edited and then merge by hand: entry points and triage rows would be duplicated.

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
advisories `DEP-nnn`, deduplicated across aliases. Secret values are never copied. A gitleaks hit
from git history keeps its commit (`history_commits`, `in_git_history: yes`), and one in a file
that is gone from HEAD is `history_only` with `git_status: removed`, like scan_secrets'. Generic
gitleaks hits (`generic-api-key`) on a line where scan_secrets' own rules see no secret (a cache-key
hash, a field name, a placeholder), and low-entropy test-file literals, go to `secrets_set_aside`
with the reason instead of becoming candidates; provider-specific rules always stay candidates.
With `--scanners`, each tool's version comes from `scanners.json` (the gitleaks SARIF driver
reports a fixed placeholder version). For npm audit,
each advisory is checked against the copies installed according to `package-lock.json`: `version`
is the installed version, `range` the advisory's own vulnerable range, `dev` whether every copy is
a devDependency, `via` the packages that pull it in, and `fixed_in` the first fixed version; a
package installed in two majors (a production copy and a dev copy) gives one entry per major, so an
advisory that only affects the dev copy doesn't read as production. For a pnpm workspace,
`--pnpm-lock` (default `<repo>/pnpm-lock.yaml`) gives osv-scanner and pnpm audit advisories the
same facts: `dev` is true when every copy is reachable only from some workspace package's
devDependencies, and `via` names `importer>direct dependency` (`apps/lab>@vendor/devtools`), so
"only a dev tool of a private app pulls it in" no longer needs `pnpm why` by hand.

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
- **Availability:** unbounded result sets and response amplification (a search or export that
  returns every match: is there a page size with a maximum, a minimum query length, a cap on the
  response size, and what does one request cost in memory and time?), rate limits that are
  missing on expensive routes or keyed on a header the client sets (`X-Forwarded-For`,
  `CF-Connecting-IP` with no trusted-proxy count), and work done per request on large data. A
  finding here has `area: availability` and a CWE such as CWE-770 (allocation without limits) or
  CWE-348 (less trusted source); a proof of concept may show the size of one response or that a
  new header value resets the limit, never a load test.
- **Templates:** `|safe`, `{% autoescape false %}`, `Markup()`, values pasted into inline
  `<script>` blocks without `|tojson` (inside an unquoted expression even escaped values inject),
  unquoted attributes, and custom filters marked safe (check that the filter escapes first).
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

For a CLI, a library or a stdio MCP server there is no HTTP target. Use a command PoC
(`"kind": "command"`): files to write into the copy (the attacker's config file, input file or
fixture), then steps that are argument lists (never a shell string or `sh -c`), and a proof.
`--check` refuses a launcher in front of the program (`env`, `wsl`, `busybox`, `sudo`, `xargs`, ...) and an
interpreter given inline code (`python -c`, `node -e`/`--eval`/`-p`, `ruby -e`, `perl -e`, `php -r`,
`deno eval`, `npx -c`, awk without `-f`), even harmless code: the lint is a denylist and cannot judge
code, so put the code in `files` and run it by path, where it is linted (destructive file operations
such as `shutil.rmtree`, `os.remove`, `fs.rmSync`/`unlinkSync`, `Remove-Item` are refused) and kept.
The proof is one of `marker_in_output`, `exit_code` or `file_created`. A regression test that fails on the vulnerable
code is a command PoC too (run the test runner, prove the failure with `exit_code` and a marker from
the assertion message).

```bash
python scripts/run_poc.py poc.json --check
python scripts/run_poc.py poc.json --workdir <copy outside the repo> --repo <repo> --permission "<...>" --out <out>/evidence/F-2.json
```

**Guard checks.** To show that a guard holds rather than that a weakness is exploitable (an id
outside `generateStaticParams` gets a 404, the response carries the CSP the config sets, a
cross-origin request is refused), give the PoC `"purpose": "guard_check"`, name the observation it
supports in `finding` (`"O-3"`), and write the proof for the secure behaviour. run_poc.py records
`result: guard_held` when the proof passes and `guard_not_held` when it does not, never
`confirmed`, which only ever means a confirmed vulnerability. Cite a held guard as the
observation's `evidence`; a guard that did not hold is a candidate finding, and the validator
rejects it as an observation's evidence. Set the `run_poc` tool row (`area: dynamic`, seeded as
`not_run`) to `ran` with the number of PoCs and guard checks whenever any ran.

It runs in the copy with HOME, temp and XDG folders inside it and HTTP(S)_PROXY pointed at a closed
loopback port, refuses a working copy inside the repository, and records argv, exit codes and
output excerpts. Record the copy in `verification.local_copy`; the validator then accepts the
evidence for `confirmed`. When only command PoCs ran (no app was started), set
`verification.target_kind: "local_copy"` with `target: null`; an HTTP PoC can't be confirmed
against it.

Go test PoCs: the step environment keeps the real Go build and module caches (`GOCACHE`,
`GOMODCACHE`, `GOPATH`) with `GOPROXY=off` and `GOTOOLCHAIN=local`, so `go test` builds offline from
the module cache (warm it first with `go mod download` in the copy if the cache is empty). A cold
compile of a large package can pass 60 seconds: give that step `"timeout_s"` (up to 600). Run only
the regression tests (`go test ./pkg/x -run 'TestSecurityRegression'`), never the whole suite of a
tool that drives devices, browsers or the network, and set the tool's own opt-outs (update checks,
telemetry) in the step `env`.

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
service). Desktop tools and tools that load plugins or helper programs have one more class,
uncontrolled search paths (CWE-427): `RUST-SEARCH-PATH-CWD` (a path built from
`env::current_dir()` next to a launch, a library load or an `.exe`/`.dll`/`.so` name),
`RUST-SEARCH-PATH-RELATIVE` (a relative program or library path such as `"lib/x.so"` or
`"tools/x.exe"`, which resolves against the working directory) and `RUST-DLL-SEARCH-ORDER` (a DLL
loaded by bare name or `LoadLibrary` without `LOAD_LIBRARY_SEARCH_*` flags: the Windows search order
includes the application folder, the PATH and, for a relative path, the working directory). The
attacker is whoever controls the folder the tool starts in (a cloned repository, a download folder)
or the environment: exposure `local`, `AV:L` with `UI:P`. The fix resolves from `current_exe()` or an
absolute configured path. Noise is held down: matches inside `#[cfg(test)]` items and `#[test]`
functions are skipped (`--include-tests` keeps them), a narrowing cast of a value clamped, `min`'d
or asserted just before, or of a float (`.floor() as usize`, `x >= 0.0`), is not a candidate, and
`unsafe` blocks that carry a `// SAFETY:` comment and call foreign code are grouped into one
low-confidence candidate per file (`grouped_lines`); an undocumented or non-FFI `unsafe` block stays
on its own. Recon reads Rust crates with the shared Cargo model: clap CLIs and subcommands are entry
points at their struct and variant lines (never a `use` line), clap arguments are `cli_flags` with
their bound environment variables, and an environment variable that chooses an executable or a
library is a `config_input` entry point (method `ENV`) with a review lead. The `go` pack covers SQL built with Sprintf, shells and variable programs in
`exec.Command` (the context argument of `CommandContext` is not the program), `InsecureSkipVerify`,
request input in file paths, paths joined from CLI arguments, decoded fields or archive entry names
without a containment check, `math/rand` for security values (files that import `crypto/rand` are
not flagged), secret-named values and userinfo URLs passed to a logger, `http://` downloads from
remote hosts and fixed `/tmp` paths. If the repository's dominant language (vendored trees
excluded) has no pack, `scan_pitfalls.py` prints a loud warning and records `language_coverage`
(`unsupported_dominant`) rather than a clean-looking candidate list drawn from the minority it did
scan; add a pack before trusting the run.

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

  For Node and Next.js apps, running a test against a copy means installing the dependencies
  (often five minutes or more) and mocks for the database client and the session library. Use the
  repository's own package manager: `npm ci` for a package-lock.json; for a pnpm or yarn workspace,
  `pnpm install --frozen-lockfile` / `yarn install --immutable` in the copy's root. When `corepack`
  is not on PATH, `npx -y pnpm@<the version in package.json's packageManager> install
  --frozen-lockfile` works; then run the test inside the package (`npx vitest run <file>`). Budget for it: run tests for
  the P0 and P1 findings first, reuse one prepared copy for all of them, and mock at the module
  boundary (`vi.mock('@/lib/prisma')`, `vi.mock('next-auth')`) so each test calls the route handler
  function directly with a `Request`. If the brief rules out installing or running the toolchain,
  write the test as a sketch with `result: not_run` and a note that says so; never claim a result
  you didn't run.

  For Go, write `func TestSecurityRegression_X(t *testing.T)` in a `_test.go` file of the package
  (subtests with `t.Run("name", ...)` are fine). `regression_test.name` is the function name, or the
  `go test -run` form `TestParent/sub_name`; the validator finds either (and a Rust `fn name()`).
  Run it as a command PoC in the copy (see step 7 for the Go caches).

### 10. Report, validate and render

Write `security_audit.json` (schema: `schemas/security_audit.schema.json`) outside the repository,
with coverage, limitations (every tool that didn't run, every finding that couldn't be confirmed
and why) and every file you wrote. Then:

```bash
python scripts/validate_security_audit.py security_audit.json --repo <repo> --candidates candidates.json \
    --recon recon.json --write-summary
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
- Confirmed findings: verification permitted, a loopback or named test target (or, for a command
  PoC, `verification.local_copy` outside the repository and evidence that ran there), a PoC that
  passes the safety lint, evidence for that finding showing the proof passed and the permission
  recorded.
- With `--recon`: every entry point in recon.json is in `recon.entry_points` or in
  `recon.removed_entry_points` with a reason.
- Traced findings list the guards checked; possible findings state the gap.
- Every candidate triaged once, every dismissal checkable, every dependency advisory assessed, and
  reachable ones linked to findings. A dismissed secret that only git history holds must say it was
  rotated or revoked, or why it needs none (public by design, a placeholder, a fake test value).
- Locations (file, line and snippet) against the source: the line must be the one that holds the
  snippet, so for a statement over several lines cite the line with the dangerous expression.
  Agent-found findings' basis excerpts are checked the same way, and so are observations'
  optional `excerpt`. An observation's optional `evidence` must exist; a run_poc.py record cited
  there must be for that observation, record the permission and show `guard_held` (a guard that did
  not hold, or a confirmed vulnerability, is a finding). The `run_poc` tool row must be `ran` when
  any PoC or guard check ran, and may only be `ran` when verification was permitted. Recon entry
  points' handler lines must exist, and in Python must not sit
  inside a string or docstring. Regression tests exist and fail on the vulnerable code; variant
  rule files exist.
- `dependency_advisories` (required from schema 1.2): `assessed` or `partial` need a dependency
  tool that ran, `not_assessed` lists no advisories, and anything but `assessed` has a reason, is
  named in the limitations, and rules out a headline claiming no vulnerable dependencies. Each
  dependency tool counted there names an `output` that exists (relative to the report or absolute),
  its `results` match that file, and every advisory in it is in `dependencies`. No limitation may
  contradict the status: "Dependency advisories were not assessed" or "no dependency tool ran" is
  rejected when one ran (a `partial` report may say a named lockfile or ecosystem was not covered).
- No secret value in the report or the evidence, and nothing written inside the repository. The
  values come from findings with a secret block and from secret candidates that weren't dismissed
  (a dismissed one still counts if it looks like a real key); placeholder words and values under
  8 characters are skipped, only JSON string values are searched (not keys), short values must
  match as whole tokens, and an error names the candidate and the JSON path, never the value.
- No `TODO(seed)` placeholder from `seed_report.py` left anywhere, and no home-directory path
  (`C:\Users\<name>\...`, `/home/<name>/...`, `/Users/<name>/...`) outside
  `verification.local_copy` and `files_written`: `seed_report.py` writes `~` for the home directory in
  tool commands, output paths and database paths, and the validator expands `~` when it reads a
  dependency tool's output.
- With `--recon`, every fixture project recon left out (`fixtures_excluded`) is named in
  `target.excluded` or the limitations.
- Secret blocks agree with the scanner: a secret it found in history is not reported as absent,
  `no` is not claimed when it could not tell, and `git_status` matches. A `partial` or `errored`
  history scan is named in the limitations.
- Summary counts recomputed (`--write-summary` also fills each finding's `priority_score` and
  `priority`); headlines claiming the code is secure, safe or free of issues are
  rejected ("secure" in a technical phrase such as "the Secure cookie flag" is fine). Priority claims
  in the headline must match the counts: "all P3" or "only P3" with any P0 to P2 finding, "no P1"
  with a P1, or "2 P1" when there are 9 is rejected. `coverage.entry_points_total` is the number of
  entry points the report keeps in `recon.entry_points` (removed ones do not count).
