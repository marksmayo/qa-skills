---
name: test-suite-evaluator
description: Evaluate an automated test suite for effectiveness, trustworthiness, engineering quality, CI readiness, maintainability, coverage evidence, and operational health. Produces evidence-backed 1–10 scores, confidence ratings, critical caps, and prioritised remediation. Works with standalone test repositories and must not invent application-code coverage.
version: 1.5.0
---

# Test Suite Evaluator

## Mission

Assess how much confidence an automated test suite deserves, not merely how polished its repository looks.

Produce a rigorous, readable assessment that distinguishes:

1. **Testing effectiveness** — whether meaningful defects are likely to be detected.
2. **Suite trustworthiness** — whether green and red results can be believed.
3. **Engineering quality** — whether the suite is maintainable, reviewable, and appropriately designed.
4. **Operational health** — whether CI history demonstrates reliability, speed, and sustained ownership.
5. **Evidence completeness** — how much of the assessment is verified rather than inferred.

The evaluator may inspect files, run safe repository commands, execute tests, inspect reports, and analyse CI configuration when available. Never modify application or test code unless explicitly asked.

## Non-negotiable rules

- Never claim line, branch, condition, or function coverage without source-linked instrumentation or a credible coverage report.
- Never call test-derived behaviour inference “code coverage.” Use **observed behavioural coverage**, **scenario representation**, or **test-intent coverage**.
- Missing evidence is `not_assessed` or `unknown`, not automatically zero.
- Every scored dimension must include evidence, confidence, and reasoning.
- Distinguish product failures, test failures, and environment failures.
- Do not reward abstractions, retries, dashboards, test counts, or coverage percentages without assessing their actual value.
- Penalise false-pass risk more heavily than cosmetic weaknesses.
- Static inspection alone cannot justify a 10/10 overall score.
- Do not reveal secrets found during inspection. Report only file location and secret type. A publicly documented development credential (a local storage emulator's well-known key, a framework's sample key) is not a secret leak, but treat it the same way: name its location and what it is, never its value.

## Step 1: Establish assessment mode

Determine the strongest supported mode.

### Black-box mode

Available evidence may include tests, fixtures, configuration, CI definitions, execution reports, screenshots, logs, and traces.

Can assess behavioural breadth, scenario depth, inputs, assertions, reliability indicators, maintainability, CI design, and diagnostics.

Cannot verify implementation branch coverage.

### Grey-box mode

Adds one or more of: API specifications, route definitions, GraphQL schemas, protobufs, requirements, tickets, acceptance criteria, architecture documents, database schemas, or explicit risk models.

Can additionally assess specification coverage, interface coverage, requirement traceability, state coverage, role coverage, and integration-boundary coverage.

### White-box mode

Adds application source and preferably runtime coverage or mutation reports.

Can assess implementation-linked coverage, untested modules, branch gaps, mutation effectiveness, and test-to-code alignment.

Record the mode and explain why it applies. The inventory must support it: white-box needs application source, grey-box needs requirements or specifications.

White-box with only source (no runtime coverage or mutation report) is weaker than white-box with instrumentation. Say which in `metadata.notes`; the validator warns when white-box rests on source alone. If the suite has a coverage provider configured (`@vitest/coverage-v8`, `coverage.py`, `jest --coverage`, JaCoCo; for Rust `grcov`, `cargo-llvm-cov`, or `cargo-tarpaulin`; for Go `go test -cover`, and the equivalent for other ecosystems), try a coverage run before settling for source only.

## Step 2: Inventory available evidence

Inspect, where available:

- Test files and framework configuration
- Package manifests and lockfiles
- CI/CD workflows
- Test reports and historical artifacts
- Coverage and mutation reports
- Retry, quarantine, skip, and flaky-test data
- API schemas and requirement sources
- Application source
- Documentation and contribution guidance
- Secrets handling and environment configuration

Create an evidence inventory with status: `available`, `partial`, `missing`, or `not_applicable`. Give each entry a `kind` naming the RUBRIC evidence-completeness item it counts as (`application_source`, `test_source`, `ci_configuration`, `recent_execution`, `historical_ci`, `runtime_coverage`, `mutation_report`, `requirements`, `defect_history`, or `other`), and list all nine RUBRIC items, as `missing` when absent. Evidence completeness is computed from these entries. `RUBRIC.md` ("What counts as each evidence kind") says precisely what qualifies for each kind, including defect history and requirements; follow it rather than judging case by case.

`application_source` is `available` when all of the repository's own product code is readable, even if a library it imports lives in another repository you could not read (say so in `details`); it is `partial` when part of the system under test itself is unreadable (a service, a package, or a private submodule whose code the tests exercise).

Give an entry an `origin` (`repository`, `ci`, `supplied`, or `assessor`) and, for reports and logs, the date it describes (`as_of`). Your own runs during the assessment are `recent_execution` with origin `assessor`; they are never `historical_ci`, however many times you repeat them (the validator rejects that). Stale or hand-written artefacts committed to the repository (an old local run log, a hand-written per-route percentage summary) are `other`: mention them, but they earn no points and cannot support coverage claims.

Do not block the assessment merely because some evidence is absent. Continue with explicit limitations.

## Step 3: Run safe checks when practical

Prefer read-only or non-destructive commands.

Examples:

- Dependency-aware lint or test commands already defined by the repository
- Test discovery/listing
- Dry runs
- Existing report generation
- Static searches for sleeps, retries, skips, weak assertions, hard-coded state, secrets, or unstable selectors

Do not access production systems, rotate secrets, delete data, or run destructive suites without explicit permission. Installing dependencies is fine when it goes into a scratch environment and nowhere else: a scratch virtual environment per Python component (`python -m venv <tmp>/venv` or `uv venv`, then the component's requirements, as CI does), or a scratch `node_modules` in the scratch copy. Never install into the checkout or a shared interpreter. Adding a measurement tool to that scratch environment only (pytest-cov or coverage.py, pytest-randomly) is also fine when the repository has none configured; say so in the run's notes.

### Workspaces, several runners and dogfood suites

- **One run per package.** A pnpm, npm or yarn workspace often has a runner config per package (Vitest
  `projects` such as `unit` and `integration` with different worker counts) and more than one runner (Vitest in
  the packages, `node --test` for scripts, the product's own runner for an end-to-end layer). Run each package's
  own test script from its directory (`pnpm --filter <pkg> test`, or `vitest run --project unit` inside it), keep a
  machine-readable report per package and runner, and add them up yourself. Count layers across packages: the
  unit projects of every package are one unit layer. Use `--project unit` for the shuffled and coverage runs, which
  rarely need the slow layers.
- **Dogfood suites.** A testing tool usually tests itself with itself: a suite run by the product's own CLI
  (`node <pkg>/dist/cli/bin.js run` in an app that depends on the workspace package). That suite is the
  repository's end-to-end layer: build the product first, run it as its package script does, and record it as its
  own layer with the product's runner as the tool.
- **Fixed ports.** Before a run, look for hard-coded ports in configs and helpers (`port: 4271`, `localhost:3000`)
  and check they are free; another process on the port gives `EADDRINUSE` or an "already running" failure that is
  yours, not the suite's. In a scratch copy, move the port and say so in the run's notes.
- **Coverage in a workspace.** Add the coverage provider to the scratch copy only, at exactly the runner's version
  (`@vitest/coverage-v8@<vitest version>`; a mismatch refuses to start), with `--filter` for the packages measured;
  it rewrites the scratch lockfile, never the checkout's. With projects, pass
  `--coverage.include='src/**'` so test helpers and configs are not counted.
- **A host OS that CI does not run.** When CI runs only Linux but you are on Windows or macOS, tests that fail
  identically in every run on your machine (paths, `os.devNull`, shell quoting, file locking, line endings) are
  `environment` failures for the assessment, not reliability evidence. Run them again under the CI OS (WSL, a
  container) when you can, but only if that environment has the same isolation as your first run: a WSL distro with
  device tools (`adb`) on its `PATH`, network access you did not guard, or the real home directory is not a safe
  place to run tests that control devices or write the profile. When you cannot, keep them `environment`, mark the
  layer `partial`, and score what they show under `portability` (and the contributing guide's claims, if it
  documents that OS for development).

A scoped test command that compiles cleanly and exits zero while running **zero tests** is not a pass: check the reported test count, not just the exit code (for example `cargo test -p <crate>` when that crate's tests live elsewhere, or a path/pattern filter that matches nothing).

### Python components and unavailable dependencies

- **One environment per component.** A monorepo with several Python services usually has one requirements file and one virtual environment per service. Build each in the scratch area and run each service's tests with its own interpreter; record a layer per service if they differ.
- **A private dependency you cannot fetch** (a private git submodule, an internal package index): never fetch it with credentials you were not given. Tests that import it fail at collection, and pytest stops the whole session on a collection error unless you pass `--continue-on-collection-errors`; pass it, so the rest of the suite still runs. Classify those failures as `environment`, and mark the layer `partial` with the reason (the tests that could not be collected did not run). You may stub the missing package to unlock the tests that only import it, but tests that read its data or behaviour still fail; say which results depend on the stub, keep those failures `environment`, and keep the layer `partial`.
- **Failures caused by your own limits** (the unavailable dependency, a service you chose not to provide) say nothing about the suite's reliability; see the reliability anchors.

### Check that running the tests is safe

Before running anything, find out what the tests would touch:

1. Read the test setup files (`vitest.setup.ts`, `jest.setup.js`, `conftest.py`, `globalSetup`) and the test environment files (`.env.test`). Look for defaults such as `process.env.DATABASE_URL ??= ...`, real API keys, and base URLs that point at shared or remote services.
2. Run in a scratch copy, not the checkout: export the tracked files (`git archive HEAD | tar -x -C <scratch>`, or a plain copy without `.git`) and link the existing dependency directory into it rather than installing. Leave `.git` out on purpose: tests of git-aware code call git on the enclosing checkout, and in a clone they find its index (one run left a stale `.git/index.lock` that blocked every later git command there). If the tests need a repository, they create their own. Point every output at the scratch copy or a temporary directory (`--outputFile`, `--coverage.reportsDirectory`, `--junitxml`). Test runners write caches (`node_modules/.vite`, `.pytest_cache`), so the original tree must not be the working directory.
3. Supply fake configuration for anything the tests read: a loopback database URL, obviously fake keys (`sk_test_fake`), and `NODE_ENV=test`.
4. Guard the network. Block every non-loopback connection for the run and count the attempts, with the guards this skill ships: for Node, `NODE_OPTIONS="--require <absolute path to this skill>/scripts/net_guard.cjs"` (forward slashes on Windows; it wraps `net.Socket.prototype.connect` and `dns.lookup`, refuses anything other than `127.0.0.1`, `::1` or `localhost`, and also applies inside Vitest and Jest worker threads); for Python, `scripts/net_guard.py` as a pytest plugin (`-p net_guard` with `PYTHONPATH` reaching `scripts/`), which patches `socket.socket.connect` and `socket.create_connection`. Set `QA_NET_GUARD_LOG=<scratch>/net_guard.log`; each guard writes `net_guard: loaded pid=<pid>` for every process it loads in and `net_guard: refused <host>:<port>` for each refusal (`--self-test` on either file checks it works here). Zero refused attempts is verified evidence for the isolation dimension; refused attempts are findings, with the hosts named. Check the log has a `loaded` line before claiming zero refusals: a guard that silently failed to load (a wrong path, a `PYTHONPATH` the shell didn't convert) also refuses nothing, so with no `loaded` line the isolation evidence is `not_assessed`. Neither guard loads into a Go, Rust or JVM test binary. For those, wrap the run with `python scripts/net_guard_proxy.py run --log <scratch>/net_guard.log --home <scratch>/home -- go test ./...`: it starts a refusing loopback proxy, points `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` at it (`NO_PROXY` loopback), gives the command an empty home (`HOME`, `USERPROFILE`, `XDG_*`, `APPDATA`, `LOCALAPPDATA`) and lists what the tests wrote there; its log has the same `loaded` and `refused` lines. It guards only clients that honour the proxy variables (Go's default transport does; a raw `net.Dial`, a client with its own transport, DNS and UDP do not, and nothing is logged for them), so grep the tests for those and record network isolation as `partial` evidence with that limit, never as verified.
5. Look for tests that control the host, before running them. Grep the tests and the helpers they call for commands run on the machine (`exec.Command`, `subprocess`, `child_process`, `Command::new`) that start, kill or talk to devices and processes (`adb`, `emulator`, `xcrun simctl`, `idevice*`, `pgrep`/`pkill`/`kill`, `taskkill`, `docker`, `systemctl`), and for writes under the user's home or profile (`os.UserHomeDir`, `~`, `$HOME`, `Path.home()`, `os.homedir()`, caches and downloads such as a browser fetched on first use). A test that kills "every matching process" or restarts a device server affects whatever else is running, including a real phone or emulator attached to the machine. Run with those tools off `PATH` (and their environment, such as `ANDROID_HOME`, unset) unless the user agreed, and leave the tests that need them as a `not_run` layer with the reason.
6. Run with a scratch home: set `HOME` **and** `USERPROFILE` (Windows reads the profile from `USERPROFILE`, so a test that sets or reads only `HOME` still writes into the real profile), plus `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `APPDATA`/`LOCALAPPDATA` where the code reads them, all pointing into the scratch area. Afterwards check the real home for files the run created; a write there is a finding (`isolation`), with the path.

**Windows paths.** From Git Bash (MSYS), a path argument is converted for Windows programs only when it is a whole argument: `--cov-report=json:/c/Users/...` (a path after a `json:` prefix) and a multi-entry `PYTHONPATH=/c/a;/c/b` are not converted, so Windows Python writes to `C:\c\Users\...` or silently ignores the entries (and with them the network guard). Pass Windows paths (`C:/Users/...`) to Windows programs, and check that outputs landed where you expected.

If the tests need services you cannot provide safely (a real database, staging, credentials), do not run that layer. Record it as not run with the reason.

### Time budget

A full run of a large suite can take ten minutes or more, and a coverage run longer. Your own tool time limit is not evidence about the suite: if a command times out on your side, run it in the background and wait, or run part of it and record `partial_run`. Never score performance from your own timeout. Use a machine-readable reporter (`vitest run --reporter=json --outputFile=<tmp>/run1.json`, `jest --json --outputFile`, `pytest --junitxml`, Playwright `--reporter=json`): it gives per-file timings and makes comparing runs exact.

Run the suite twice when practical, and make one more run in random order (`pytest -p randomly` with pytest-randomly in the scratch environment, `vitest run --sequence.shuffle`, `jest --randomize`), recording its seed. Tests that fail in one run and pass in the other are a verified flakiness observation for the reliability dimension; tests that fail only in the shuffled run depend on execution order, which is an isolation and reliability finding (name them, with the seed that reproduces it). Repeated runs during the assessment are still recent execution, not CI history.

When you measure coverage, tell the runner to write the report even when tests fail (Vitest `--coverage.reportOnFailure=true`; Jest writes it by default; `pytest --cov` writes it unless the session errors). Otherwise a single flaky failure leaves you with no coverage at all. Instrumentation slows the run and can add timeout failures; compare its failures with the uninstrumented run before blaming the product.

### Record what ran and what it showed

Record commands attempted, exit status, duration, the environment that matters (runtime version, worker count, fake variables set, network guard on), and whether results are representative.

Record the strongest execution evidence as `metadata.execution`:

- `full_run`: you ran every test layer the repository has during the assessment
- `partial_run`: you ran some layers or some tests, and at least one layer (for example an end-to-end layer that needs the app and a database) was not run or only partly run
- `reports_only`: you did not run it, but credible recent reports or CI results exist
- `static_only`: nothing was run and no reports exist
- `not_executable`: discovery or execution was attempted and failed, and no credible reports exist

The first three need recent execution results in the inventory; the last two must not claim them, and mark the evaluation `provisional` (`metadata.provisional: true`). `provisional` means exactly that: no execution evidence supports the scores. It is false for every other execution value.

For `full_run` and `partial_run`, record `metadata.execution_result`:

- `layers`: every test layer the repository has (unit, component, integration, end-to-end...), each `run`, `partial`, or `not_run`, with the reason when not fully run. A layer that exists and was not run makes the execution `partial_run`, never `full_run`. A layer is `run` only when all its tests were collected and executed; tests that could not even be collected (a missing dependency, an import error) make it `partial`.
- `runs`: each execution with its `origin` (`assessor`, `ci_report`, `local_report`), the layers it covered, and its passed, failed, skipped, and errored counts, duration, and whether it was instrumented for coverage.
- `failures`: every failing or erroring test, or a group of them with a `count`, classified as `product` (the system under test is wrong), `test` (the test is wrong, brittle, or timing-dependent), `environment` (the machine, a missing service, configuration), or `unknown` (not yet established; say what would settle it). Name the runs it failed in (`runs_failed`); a test that failed in some runs and not others is flaky.

A static review of a layer that did not run (for example Playwright specs read but not executed) may support design, assertion, maintainability and effectiveness judgements as `test_derived` evidence, but never execution, reliability or performance claims.

## Step 4: Build a behaviour model

From each test or scenario, derive where possible:

- Feature or capability
- Starting state
- Actor or role
- Action
- Expected state transition
- Observable assertions
- Input classes
- Positive, negative, boundary, permission, concurrency, dependency-failure, and recovery dimensions

Aggregate this into behavioural coverage matrices (`behavioural_coverage`: happy path, negative, boundary, permissions, concurrency, dependency failure, recovery). State clearly that these are test-derived unless matched against specifications or source.

### Interface coverage

When the application's interfaces can be listed, map each one to the tests that reach it and record the result in `interface_coverage` (total, tested, the untested ones, and how you matched them). This is cheap and often gives the strongest finding: an untested authentication, payment or data-deletion handler. It is interface coverage, not code coverage.

- **Next.js App Router:** list `app/**/route.ts` (and `.js`) handlers; a handler is tested when a test file imports it (resolve `@/` and other `tsconfig.json` `paths` aliases) or calls its URL. Count each exported method (`GET`, `POST`, `PATCH`, `DELETE`) when the tests show which method they exercise. Server actions (`'use server'`) and `page.tsx` files are separate interfaces.
- **FastAPI:** count operations (method and path), not functions. List them from `app.openapi()["paths"]` when the app imports in your scratch environment (note that `include_in_schema=False` routes are left out), or by walking each router's routes with its `APIRouter(prefix=...)` and the `include_router(..., prefix=...)` that mounts it. Do not walk `app.routes` for `APIRoute` objects alone: newer FastAPI releases keep included routers as wrapper objects there, so the walk finds only the routes declared on the app itself (one of sixteen in one assessment). Match tests by method and path template (`/items/{item_id}`).
- **Express, Fastify, Flask, Django, Rails, Spring:** the router or URL configuration lists the endpoints; match tests by URL and method.
- **OpenAPI or GraphQL:** operations from the specification (that makes it `specification_derived`).
- **CLI and other multi-call binaries:** when a tool dispatches to many sub-commands or utilities (a `git`-style subcommand table, a coreutils-style multi-call binary, a plugin or handler registry), that dispatch table *is* the interface list. Map each sub-command or utility to the tests that invoke it (by name on the command line, or by calling its entry function), and record it with `kind: "commands"` (or `kind: "cli"`). An untested destructive sub-command (delete, overwrite, force) is often the strongest finding here.

- **Libraries, CLIs and tool servers:** a package's public entry points (`package.json` `exports` subpaths, or `main`) are `kind: "package_exports"`; its `bin` commands and their sub-commands (commander, yargs, cac, argparse, click, typer; `pyproject` scripts; Cargo binaries) are `kind: "cli"`; the tools an MCP or agent tool server registers are `kind: "tools"`. For a CLI, SDK or MCP server these are the natural interfaces, not HTTP routes.

`python scripts/list_interfaces.py <repo> --tests <test dir>` lists FastAPI, Flask, Azure Functions, Next.js and Express routes (and a plain Node `http.createServer` as one entry), CLI commands, package exports and MCP tools from source (it never imports the app). Each entry carries its `kind`, the value for `interface_coverage[].kind`. A route is reached when a test's URL literal matches it; a command, tool or export when a test string names it or a test imports the export's source file. Naming a command is weaker than running it: read the hits before counting them. Computed names (tools registered in a loop over specs, a dynamic route prefix) come out as `<dynamic>` with a note; map those by hand. When it finds nothing it says so in `notes` ("no supported interface found"): that is not "no interfaces", so map them by hand or record interface coverage as not assessed.

It reads Go too: urfave/cli command tables (`cli.App`/`cli.Command` literals, `Commands`/`Subcommands` lists, commands held in variables or returned by functions), cobra (`Use:` and `AddCommand`), programs using the standard `flag` package (and `flag.NewFlagSet` sub-commands), `net/http` ServeMux routes (Go 1.22 `"GET /path/{id}"` patterns, gorilla `.Methods`), gin/echo/chi/fiber/httprouter routes with a literal path, listeners that register no route (`go-listener`: read them by hand, never counted as reached), and every importable package with an exported identifier (`package_exports`, its import path; `internal/` and `main` are left out). Only `_test.go` files are read as Go tests: a command is reached when a test in its package names the variable that holds it, a route when a test requests its URL (`httptest.NewRequest(http.MethodGet, "/x", ...)`), a package by its own tests or an import of its path. Languages are counted by the shared `lang_census.py`: `vendor/`, `third_party/`, header trees, built bundles, submodules and embedded third-party packages (a directory with its own manifest and licence under `drivers/`, `sdk/` and the like, or one whose manifest names another repository) are left out of the count and of the interfaces, and named in `notes`. When the dominant language is one it does not read, it says so loudly on stderr and in `notes`, records `language_coverage` (the census counts, `status` `unsupported_dominant`) and exits 3; a mostly-Rust repository (read only in part) gets `partial_dominant` and a warning: the list then covers incidental files only, so map the interfaces by hand or record interface coverage as not assessed.

**Reached is not authorised.** When fixtures replace the authentication or authorisation dependency (FastAPI `app.dependency_overrides[get_current_user] = ...` or a no-op permission check, a mocked `auth()` in Next.js, a test settings flag that disables auth), every route test runs without the real check, and "reached by a test" says nothing about access control. Record a second `interface_coverage` entry for the interfaces tested with the real dependency in place (for example "operations with a 401/403 test against the real permission check"); that is the security-relevant number, and an untested permission path is a finding.

List every untested interface in `untested`, or group them as `{"group": "14 operations under /v1/items/{item_id}/...", "count": 14}`; the entries must account for exactly `total - tested` (the validator checks).

Import mapping is `test_derived`; a coverage report listing each handler's executed lines makes it `verified`.

## Step 5: Score the dimensions

Use the rubric in `RUBRIC.md`. Score only supported dimensions.

Core dimensions and default weights:

| Dimension | `id` | Weight |
|---|---|---:|
| Test effectiveness | `effectiveness` | 16 |
| Suite trustworthiness | `trust` | 14 |
| Reliability and determinism | `reliability` | 10 |
| Test design and level selection | `design` | 8 |
| Assertion and oracle quality | `assertions` | 8 |
| Test scope and coverage | `coverage` | 8 |
| Isolation and state management | `isolation` | 7 |
| Maintainability and appropriate reuse | `maintainability` | 7 |
| CI/CD integration | `ci` | 6 |
| Debuggability and reporting | `debuggability` | 4 |
| Performance and feedback speed | `performance` | 3 |
| Test data management | `test_data` | 3 |
| Environment portability and realism | `portability` | 2 |
| Security, privacy, accessibility, compatibility | `security_a11y` | 2 |
| Ownership and operational discipline | `ownership` | 2 |

Weights total 100. List all fifteen dimensions, each once. Reweight only when a dimension is genuinely not applicable: mark it `applicable: false` with a `not_applicable_reason`, give it weight 0 and score null, and rescale the other default weights proportionally so they total 100 again. Do not reweight merely because evidence is missing; keep the default weight, set the score to null, and lower evidence completeness and confidence instead.

For each dimension output:

- `score`: integer 1–10, or null when not assessed (never 0). Use the anchors for the dimension in `RUBRIC.md`.
- `confidence`: high, medium, or low
- `evidence_level`: verified, specification_derived, test_derived, mixed, or unknown
- concise finding
- concrete supporting evidence with file paths or command results
- risks
- recommendations

A scored dimension must carry at least one piece of evidence and cannot have evidence level `unknown`; a dimension the evidence does not support is not assessed (score null, evidence level `unknown`), with the finding starting "Not assessed" and saying why. Placeholder findings are rejected: an empty or "n/a" finding, a marker such as TODO or TBD on its own or leading the text, or a short text holding one. A full sentence that uses such a word as the suite's own vocabulary ("the content rules check placeholder text") is not a placeholder.

**Evidence levels belong to evidence, not to a whole dimension.** The dimension's level must hold for every piece of its evidence. When a dimension rests on evidence of different levels (a coverage run and inferences read from tests), set `evidence_level: mixed` and write each evidence item as an object with its own level and an id: `{"id": "e1", "level": "verified", "text": "vitest --coverage: lines 81.2%"}`. A sentence of the finding, risks or recommendations that states a coverage figure must cite a verified item by its id (`[e1]`). Never pick `verified` for a dimension because some of its evidence is verified: that would license coverage wording for the inferred parts.

`confidence: high` needs at least two consistent evidence items and direct evidence (not `test_derived` alone).

Map file names and identifiers into evidence freely, including names that contain "coverage" (`service-form-branch-coverage.test.tsx`); write them in backticks when quoting. The coverage-language check ignores file names, identifiers and backticked text.

## Step 6: Apply score caps and critical findings

Apply the lowest applicable cap after computing the weighted score.

Default caps (with the `rule` id recorded in `headline.applied_caps`):

- Test effectiveness below 4 → overall maximum 4 (`effectiveness_below_4`)
- Suite trustworthiness below 4 → overall maximum 4 (`trust_below_4`)
- Reliability below 4 → overall maximum 5 (`reliability_below_4`)
- Meaningful assertion/oracle quality below 3 → overall maximum 3 (`assertions_below_3`)
- Test scope and coverage below 4 → overall maximum 4 (`coverage_below_4`): a suite that leaves major features or critical interfaces untested is Weak overall, however good its few tests are
- Suite cannot be discovered or executed and no credible reports exist (`execution: not_executable`) → mark overall as provisional; maximum 5 (`not_executable`)
- Material secret or production-data exposure → critical finding (`category: secret_exposure` or `production_data_exposure`, severity `critical`); maximum 4 until remediated (`secret_or_production_data`)
- Broad error swallowing, globally excessive retries, or systematic false-pass behaviour → critical finding with `category: systematic_false_pass`; maximum 4 (`systematic_false_pass`)
- Static-only assessment (`execution: static_only` or `not_executable`) → maximum 8 (`static_only`)

List every cap whose condition holds, with its limit and an explanation, even when it does not bind because the score is already lower. The overall score is the raw weighted score lowered to the smallest limit. Explain every cap. Never silently modify the score. When no cap applies, say so in the report ("No caps applied").

The reliability anchors in `RUBRIC.md` decide whether the reliability cap applies: a suite that was red in every assessed run for reasons in the suite itself scores 3 or lower, not 4. Failures caused by the assessment's own limits (a private dependency you could not fetch, a service you chose not to provide) do not count towards "red in every run"; judge reliability on the tests that could run. A few identified order-dependent tests, found by a shuffled run, with the rest of the suite agreeing across runs, are a 4 (or higher when they are tracked and the rest is controlled), not a 3.

**Thin suites.** Reliability, assertions, isolation and performance grade the tests that exist. When the suite reaches little of the product (coverage below 4), those scores describe a small sample: their confidence is `medium` at most (the validator checks), their findings say how few tests they rest on, and the `coverage_below_4` cap keeps the overall score from being carried by them.

`critical_findings` holds findings at `critical`, `high`, or `medium` severity; the report separates the findings of severity `critical` from the rest. Only the categories above cap the score. An untested security- or money-critical path (sign-in, two-factor, payments, data deletion, permissions) is at least a `high` finding with `category: other`, and lowers `effectiveness` and `coverage` through their anchors; it does not cap the score on its own unless the evidence shows systematic false passes. Give each finding the `dimensions` it affects.

## Step 7: Calculate headline results

Report:

- **Raw weighted score**
- **Capped overall score**
- **Testing effectiveness score**
- **Suite trustworthiness score**
- **Engineering quality score**
- **Operational health score**, or `not_assessed`
- **Evidence completeness**, 0–100%
- **Overall confidence**
- **Assessment mode**

Use one decimal place for weighted calculations, while warning that small decimal differences are not materially significant.

How each is computed (see `RUBRIC.md`):

- Raw weighted score: the weighted mean of the scored dimensions. Unassessed dimensions are left out of the mean, never counted as zero.
- Testing effectiveness: the `effectiveness` score. Suite trustworthiness: the `trust` score.
- Engineering quality: the weighted mean of `design`, `maintainability`, `debuggability`, `test_data`, and `portability`.
- Operational health: the weighted mean of `ci`, `performance`, and `ownership`, and only when historical CI outcomes are in the inventory; otherwise null (`not_assessed`).
- A group score with no scored dimension is null.
- Evidence completeness: the RUBRIC points for the inventory, with partial evidence at half credit.
- Overall confidence cannot be `high` without execution evidence.

Rating bands:

- 1.0–2.9: Dysfunctional
- 3.0–4.9: Weak
- 5.0–6.4: Serviceable
- 6.5–7.9: Strong
- 8.0–8.9: Excellent
- 9.0–10.0: Exceptional and evidence-backed

A static-only assessment cannot be labelled “Exceptional.”

## Step 8: Detect gaming and misleading signals

Explicitly check for:

- Test-count inflation
- Meaningless or tautological assertions
- Retries masking first-run failures
- Skips, quarantines, and expected failures without owner or expiry
- Coverage exclusions that hide difficult code
- CI files that do not run on meaningful triggers
- Page objects or helpers created only for appearance
- Generated expected results derived from the system under test
- Snapshots or golden files accepted without review discipline
- Reports that are attractive but diagnostically weak
- Test names implying behaviour not actually asserted
- Coverage-chasing tests: files named for coverage (`*-coverage.test.*`), titles starting "covers …", comments citing source line numbers, tests that call code without asserting its outcome
- Assertion-free tests (no `expect`/`assert` in the body; the `expect-expect` lint rule finds them) and `try { … } catch {}` inside a test body
- Configured thresholds that never run: coverage thresholds in `vitest.config.*` or `jest.config.*` when CI runs tests without `--coverage`, or quality gates on a job that is not required

JavaScript and TypeScript stacks (Vitest, Jest, Next.js, Playwright) add signs that are easy to miss:

- A global `environment: 'jsdom'` applied to API route or server tests, so server code runs with browser globals it never has in production
- A global `vi.mock`/`jest.mock` in a setup file that replaces a module (the database client, auth, a payment SDK) in every test, hiding integration behaviour everywhere
- The database client (Prisma, Drizzle, TypeORM) mocked in every test while CI provisions a real database only for migrations, so no test exercises a real query
- `npm test` script environment variables (`TZ=UTC`, `NODE_ENV`) silently overridden in a setup file, or tests that depend on `npm_package_version` and other variables only `npm run` sets (they pass under `npm test` and fail under `npx vitest`)
- An end-to-end layer (Playwright, Cypress) that CI never runs, or runs with retries that hide first-attempt failures

Python stacks (pytest, FastAPI, SQLAlchemy) have their own:

- `app.dependency_overrides[<auth dependency>] = <no-op>` in a shared fixture or conftest, so every route test runs without authentication or permission checks (the Python twin of a global `vi.mock` of auth); see "Reached is not authorised" in Step 4
- `pytest.MonkeyPatch()` created at module level in a conftest and never undone, or `os.environ[...] = ...` at import time in a conftest: the change leaks into every test in the session
- `importlib.reload(module)` in a test: configuration the module read at import stays changed for later tests, and other tests silently depend on that order
- Fixtures that drop and recreate a schema (`DROP SCHEMA public CASCADE`, `metadata.drop_all`) on whatever database URL the environment names, with no check that it is a test database (name or host)
- CI selecting tests by marker (`-m "not integration"`, `-m integration`) while some integration tests carry no marker, so they run in the wrong job or in none
- The same test directory run by two targets (two Makefile targets or CI jobs that both include a shared tests folder), which double-counts its results
- Unit tests that read data files from a git submodule or another repository, so they fail wherever that is unavailable

Azure DevOps pipelines:

- No `pr:` trigger in the YAML: pull-request validation then depends on branch policies that live in the project settings, not the repository. Record `ci_configuration` as `partial` and the gating as unknown rather than assuming either way
- `template:` references to files in another repository or a `templates/` folder you cannot read: the jobs they define are unknown, so say which stages you could not see

Other ecosystems have their own false-pass traps:

- **Rust:** a scoped command that compiles and reports success while running **0 tests** (`cargo test -p <crate>` when the integration tests live in a different crate, or a `--test`/name filter that matches nothing) is not a pass — read the "running N tests" line. `#[ignore]` on a test with no owner or expiry is a permanently silenced test, not a passing one. `#[cfg(unix)]`, `#[cfg(target_os = ...)]`, or feature-gated tests silently compile out on other platforms or without the feature, so CI on one target reports green while those tests never run anywhere; check which targets and feature sets CI actually exercises.
- **Go:** `go test -json` is the machine-readable reporter (each package and test as events; `gotestsum --junitfile` turns it into JUnit), `-count=1` defeats the result cache (a cached `ok` ran nothing now), and `-shuffle=on` (it prints the seed) is the random-order run. Tests gated by `runtime.GOOS` skips, `//go:build` tags, `testing.Short()` or an environment variable compile out or skip on other targets, so a green Linux CI may never run the macOS or Windows paths; check which `GOOS`, tags and `-short` settings CI uses. A test file that does not compile on one `GOOS` (a `syscall` call that exists only on Unix) makes `go test ./...` report the whole package as `[build failed]` there: that package's tests did not run, which is a portability finding and an `errored` run layer, not N failures. A test whose body is only `t.Skip(...)` is permanently silenced. `codecov.yml` `ignore:` lists (and `coverage.ignore` in other tools) can drop the weakest packages from the headline number: read them before quoting a coverage figure, and say which paths were excluded. A failing `t.Run` subtest also fails its parent, so failure counts from per-test events count it twice.
- **Generally:** tests gated behind a platform, OS, feature flag, environment variable, or build tag that the pipeline never sets are dead weight that still shows green; confirm the gated paths are run somewhere, and treat a run that executed no tests as `not_run`, never a pass.

## Step 9: Prioritise recommendations

Do not produce an unranked dump.

Return these groups, each recommendation carrying its group as `category`, and rank `priority` through the groups in this order:

1. **Critical confidence risks** (`critical_risk`): required when a finding of severity `critical`, a finding in a capping category (`secret_exposure`, `production_data_exposure`, `systematic_false_pass`), or a cap other than the static-only caps applies. A `high` or `medium` finding of category `other` does not require one on its own
2. **Three highest-leverage improvements** (`top_improvement`): one to three
3. **Quick wins** (`quick_win`): small effort only
4. **Structural improvements** (`structural`)
5. **Evidence needed for a stronger reassessment** (`evidence_needed`): required when any inventory item is missing or partial; name the inventory kinds it would supply in `evidence_kinds`

Each recommendation must include:

- impact: critical, high, medium, low
- effort: small, medium, large
- expected score dimensions affected (`dimensions_affected`; optional for evidence needed)
- concrete implementation suggestion
- acceptance criteria

## Step 10: Produce beautiful output

Generate both:

1. `evaluation.json`, conforming to `schemas/evaluation.schema.json`
2. A concise human report in Markdown or terminal format

Run `python scripts/validate_evaluation.py evaluation.json` and fix every error before reporting. Besides the schema it recomputes the weights, the raw and group scores, the caps, the overall score and band, and evidence completeness, and rejects scores without evidence, a reliability score of 7 or more beside test-class failures the runs disagree on, high confidence in the quality of a thin suite's few tests, untested interface lists that do not account for every untested interface, evidence levels that do not hold for every item, high confidence without direct and multiple evidence, an execution value its layers and runs contradict, unclassified failures, assessor runs offered as CI history, recommendations outside the Step 9 groups, and any code-coverage claim made without a runtime coverage report or from evidence that is not verified. A stated coverage percentage is a claim whatever else its sentence says ("No tests for 2FA, so branch coverage is 83%" is a claim). Write and read the JSON as UTF-8.

Render the report with `python scripts/render_report.py evaluation.json` for the terminal, or `--markdown report.md` for a Markdown file. Both show every section below.

When terminal colour is supported, use ANSI colour through `scripts/render_report.py`:

- Green: 8–10
- Cyan: 6.5–7.9
- Yellow: 5–6.4
- Magenta: 3–4.9
- Red: 1–2.9 and critical findings
- Dim text: unknown or not assessed

Never rely on colour alone. Include text labels and symbols.

Recommended report order:

1. Title and assessment mode
2. Overall score card (raw and capped score, each cap or "No caps applied", the decimal-significance warning)
3. Critical findings
4. Dimension scorecard, with each dimension's evidence, risks and recommendations
5. Behavioural coverage matrix and interface coverage
6. Recommendations by group, with acceptance criteria
7. Evidence limitations
8. Appendix with execution results, commands and inspected artifacts

## Migrating older evaluations

`metadata.skill_version` is required and names the contract an evaluation was written for. The validator warns when it differs from this skill's version. To bring an older evaluation forward:

- **Before 1.1:** rename dimension ids `trustworthiness` → `trust`, `oracles` → `assertions`, `scope` → `coverage`, `data` → `test_data`, `environment` → `portability`, `crosscutting` → `security_a11y`; add `metadata.execution` and a `kind` for every inventory entry.
- **1.3 → 1.4:** nothing to change; `interface_coverage[].kind` gained `package_exports` and `tools` (entries recorded as `other` for these may move to them).
- **1.2 → 1.3:** list the `coverage_below_4` cap when coverage is below 4 (and recompute the overall score); lower to `medium` the confidence of reliability, assertions, isolation and performance when coverage is below 4; make each `interface_coverage` entry's `untested` account for exactly `total - tested` (names, or `{"group", "count"}` entries); reliability 7 or more cannot stand beside a test-class failure that some runs show and others do not.
- **1.1 → 1.2:** add `metadata.skill_version`; add `metadata.execution_result` for `full_run` and `partial_run`; set `provisional` true only for `static_only` and `not_executable`; give every recommendation a `category` (and `evidence_kinds` for evidence needed); set unassessed dimensions' evidence level to `unknown`; split mixed-evidence dimensions into per-item levels (`mixed`).

## Required honesty language

Use direct language such as:

- “Instrumented code coverage was not measurable from the supplied evidence.”
- “This gap is inferred from test content, not verified against application source.”
- “Reliability confidence is low because historical CI outcomes were unavailable.”
- “The suite appears green partly because retries or exclusions suppress failures.”

Avoid vague praise. Do not describe a suite as robust, comprehensive, mature, or production-ready unless the evidence supports that statement.
