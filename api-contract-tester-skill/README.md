# API Contract Tester Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It checks that a running HTTP API does what its contract says. Give it an OpenAPI 3.0/3.1 document
or a GraphQL schema and a test instance. The skill:

- **Lints the contract**: missing operationIds, responses without schemas, examples that don't
  match their own schemas, keywords it can't check, and (GraphQL) a served schema that differs
  from the SDL.
- **Generates requests**: documented examples, boundary values, one-violation negatives, a seeded
  random sample, and requests without credentials.
- **Sends them in safe mode** (GET, HEAD and OPTIONS unless writes are authorised), rate-limited,
  and checks every response: undocumented status, server error, content type, schema conformance,
  required headers, invalid input accepted, valid input rejected, auth not enforced, GraphQL errors.
- **Saves and re-sends** each failing exchange, redacted, to separate repeatable from flaky.
- **Diffs two contract versions** and rates each change breaking, potentially breaking or compatible.
- **Compares the contract with the route handlers** in the code (Next.js App Router and Pages
  Router, Express, Flask, FastAPI): undocumented handlers, documented operations with no handler,
  and documented methods the handler doesn't export. No running API needed.
- **Copes with real deployments**: session-cookie auth, per-path request budgets, a stop-on-429
  policy per path, resumable runs, a configurable health probe, and an environment outage reported
  as one cause instead of a defect per operation.
- **Uses Schemathesis and oasdiff** when they're installed, and works without them.

It is strict about what a run can show:

- No findings is "conformance not established", never "conforms". Untested operations and
  checks it couldn't evaluate are listed as such.
- Every finding has a saved request and response.
- A failure that didn't repeat is kept as flaky, not dismissed.
- Credentials are redacted before anything is written; non-loopback targets need `authorised_by`;
  production hosts never receive writes.
- Whether to fix the API or the contract is marked as judgment.
- When the API can't be run, the report says `not_run` and claims nothing about behaviour.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/api-contract-tester/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/api-contract-tester/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/api-contract-tester/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and nothing
else. Optional: PyYAML (otherwise the bundled YAML subset parser is used), Schemathesis
(`pip install schemathesis`) for property-based generation, and oasdiff for a second opinion on
breaking changes.

## Usage

> Test our staging API against openapi.yaml. I'm authorised to test https://staging.example.com; read-only.

> Compare openapi.yaml with last release's contract and tell me what breaks clients.

```bash
python scripts/load_contract.py openapi.yaml --out run/contract.json
python scripts/gen_cases.py run/contract.json --out run/cases.json --seed 1611 --auth-configured
python scripts/run_schemathesis.py openapi.yaml --base-url http://127.0.0.1:8140 --out run/schemathesis.json --seed 1611 --bearer-env API_TOKEN
python scripts/send_cases.py run/contract.json run/cases.json --base-url http://127.0.0.1:8140 --out run/run.json --bearer-env API_TOKEN
python scripts/load_contract.py openapi.previous.yaml --out run/contract.prev.json
python scripts/spec_diff.py run/contract.prev.json run/contract.json --out run/diff.json
python scripts/merge_findings.py run/contract.json run/cases.json run/run.json --diff run/diff.json --schemathesis run/schemathesis.json --out run/candidates.json
python scripts/handler_map.py . --contract run/contract.json --out run/handlers.json
python scripts/validate_report.py run/report.json --run run/run.json --candidates run/candidates.json --contract run/contract.json --cases run/cases.json --diff run/diff.json --handlers run/handlers.json --repo .
python scripts/render_report.py run/report.json [--brief] [--sarif findings.sarif --contract run/contract.json]
```

Against a deployed target with app-level rate limits and session auth:

```bash
python scripts/send_cases.py run/contract.json run/cases.json --base-url https://staging.example.com --out run/run.json \
  --authorised-by "who" --cookie-env SESSION_COOKIE --rate 1 --probe api/health --probe-expect 200 \
  --path-budget api/appointments=15 --max-requests 150 --max-retries-429 0 --on-429 stop-path
```

On Git Bash for Windows, a command-line path starting with `/` is rewritten into a Windows path:
write paths without the leading slash (as above) or set `MSYS_NO_PATHCONV=1`.

Without a test instance, write a static-only report (`run_status: not_run`) from the contract, the
diff and the handler check:

```bash
python scripts/merge_findings.py run/contract.json --out run/candidates.json
python scripts/validate_report.py run/report.json --contract run/contract.json --diff run/diff.json --handlers run/handlers.json --repo .
```

## Example

`examples/clinic-api/` is a small stdlib appointment-booking API with planted contract breaks, an
OpenAPI 3.1 contract (`openapi.yaml`, 1.5.0), the previous 3.0 contract (`openapi.v1.yaml`, 1.4.0)
and a GraphQL schema. `examples/run/` and `examples/run-graphql/` are real runs against it (with
Schemathesis 4.33.2 in safe mode for the REST one), listed in `examples/run/commands.sh`;
`examples/README.md` has the answer key.

```bash
python scripts/validate_report.py examples/report.example.json --run examples/run/run.json --candidates examples/run/candidates.json --contract examples/run/contract.json --cases examples/run/cases.json --diff examples/run/diff.json --repo examples/clinic-api
python scripts/validate_report.py examples/report.static.example.json --contract examples/run/contract.json --cases examples/run/cases.json --diff examples/run/diff.json --repo examples/clinic-api
python scripts/render_report.py examples/report.example.json
python scripts/handler_map.py --self-test
```

`examples/report.static.example.json` is the same contract reported without a run. It also
carries a contract issue found by reading the server (`source: agent`, with a `basis` file, line
and excerpt that `--repo` re-reads).

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: severity, confidence, reach, priority, breaking-change ratings, verdict
- `schemas/api_contract_report.schema.json`: output contract
- `scripts/load_contract.py`: OpenAPI/GraphQL loading, `$ref` resolution, contract lint, introspection comparison
- `scripts/yaml_lite.py`: stdlib YAML subset parser (used without PyYAML)
- `scripts/jsonschema_lite.py`: stdlib JSON Schema subset; unsupported keywords make a check `cantTell`
- `scripts/gen_cases.py`: plan and case generator (examples, boundaries, negatives, seeded random, no-auth)
- `scripts/send_cases.py`: rate-limited sender in safe mode, with checks, redaction, exchanges and re-sends,
  request and path budgets, a 429 policy and resumable runs
- `scripts/handler_map.py`: static contract-vs-handler comparison (`--self-test` checks it on fixtures)
- `scripts/py_routes.py`: Flask, FastAPI and Azure Functions routes read with `ast`, with router and
  blueprint prefixes resolved (a shared file, the same in the journey mapper, security auditor and test suite
  evaluator; the canonical copy is `shared/scripts/py_routes.py` in the repository)
- `scripts/run_schemathesis.py`: optional Schemathesis wrapper with the same safe mode
- `scripts/spec_diff.py`: breaking-change diff; merges oasdiff output when present
- `scripts/merge_findings.py`: operation coverage and rated candidates
- `scripts/validate_report.py`, `scripts/render_report.py`, `scripts/schema_check.py`
- `scripts/sarif.py`: shared copy (edit it in `shared/`)
- `examples/`: the sample API, contracts, runs and reports

## Design principles

1. The contract is the oracle; a disagreement says which way to fix it, as judgment.
2. Every claim has an exchange; every exchange is redacted.
3. Coverage is part of the answer: untested and cantTell are never passes.
4. Safe by default: reads only, rate-limited, authorised targets.
