# Test Suite Evaluator Skill

A portable repository-assessment skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It evaluates an automated test suite without bluffing about evidence it cannot access. It supports:

- **Black-box mode:** tests, configuration, and reports only
- **Grey-box mode:** adds specifications, schemas, requirements, or architecture
- **White-box mode:** adds application source, runtime coverage, and mutation data

The evaluator produces evidence-backed 1–10 scores, confidence ratings, score caps for critical weaknesses, and a prioritised improvement plan.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory and point Claude to `SKILL.md`. Common setups place project skills under `.claude/skills/test-suite-evaluator/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/test-suite-evaluator/`, or include `SKILL.md` in the task context. The skill avoids model-specific tool syntax.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/test-suite-evaluator/`, and reference `SKILL.md` from the project’s Gemini instructions. It uses plain Markdown, JSON, and Python only.

Exact skill discovery paths can vary by client version. The core skill is deliberately vendor-neutral.

## Usage

Ask the agent:

> Evaluate this automated test suite using the Test Suite Evaluator skill. Run available tests and inspect CI artifacts when practical. Produce both the terminal report and the machine-readable JSON result.

For terminal rendering:

```bash
python scripts/render_report.py evaluation.json
```

Force or disable colour:

```bash
python scripts/render_report.py evaluation.json --color always
python scripts/render_report.py evaluation.json --color never
```

Write a Markdown report (all sections: score card, findings, dimensions with their evidence, behavioural and interface coverage, execution results, recommendations by group with acceptance criteria, limitations, and a commands appendix):

```bash
python scripts/render_report.py evaluation.json --markdown report.md
python scripts/render_report.py evaluation.json --brief     # one screen: scores, caps, findings, next steps
```

Validate an evaluation:

```bash
python scripts/validate_evaluation.py evaluation.json
python scripts/validate_evaluation.py evaluation.json --interfaces interfaces.json   # also compare with list_interfaces.py
python scripts/validate_evaluation.py evaluation.json --conventions conventions.json # also check what CI runs (detect_conventions.py --exclude-fixtures)
```

The validator checks the JSON against the schema, then recomputes what the rubric defines: all fifteen dimensions with their weights, the raw weighted and group scores, every cap that applies, the overall score and its band, and evidence completeness from the inventory. It also checks that the assessment mode and execution evidence match the inventory, and that every score has evidence. It checks that each dimension's evidence level holds for all of its evidence (or is `mixed` with a level per item), that the execution value matches the layers and runs recorded, that every failure is classified, that the assessor's own runs are not offered as CI history, that recommendations follow the Step 9 groups, that a reliability score of 7 or more does not stand beside test failures the runs disagree on, that a thin suite (coverage below 4) is capped at 4 and its few tests' quality is not rated with high confidence, that a repository whose discovery found no tests (`execution: no_tests`) scores effectiveness, trust, assertions and coverage 1, records test source as missing and leaves the dimensions that grade existing tests unassessed, that each interface coverage entry accounts for every untested interface (by name or as a counted group), and that a `ci` score of 1 or 2 for a suite that exists comes with a `ci_runs_no_tests` finding, which caps the overall at 5 (no CI job runs the behaviour tests). It rejects any code-coverage claim made without a runtime coverage report or from evidence that is not verified; a stated percentage is a claim even in a sentence that also says "no", while file names, backticked identifiers and interface coverage named as such ("route coverage is 100% by method and path") are not claims. Both scripts read and write UTF-8 on every platform.

Evaluations written for an older contract fail with a migration hint; SKILL.md "Migrating older evaluations" lists the changes.

## Files

- `SKILL.md` — complete agent instructions
- `RUBRIC.md` — detailed scoring anchors and evidence rules
- `schemas/evaluation.schema.json` — machine-readable output contract
- `scripts/render_report.py` — terminal renderer (ANSI colour) and Markdown renderer (`--markdown`)
- `scripts/validate_evaluation.py` — schema and rubric validator (recomputes scores, caps and completeness)
- `scripts/net_guard.py`, `scripts/net_guard.cjs` — network guards for the test run (a pytest plugin and a Node `--require` module, shared files): they refuse non-loopback connections and log a `loaded` line per process and each refusal (`--self-test` checks them)
- `scripts/list_interfaces.py` — optional: lists FastAPI, Flask, Azure Functions, Next.js and Express routes from source (Python through `scripts/py_routes.py`, the shared route reader; routers, prefixes and includes resolved, nothing imported), plus CLI commands (package.json `bin`, commander/yargs/cac, argparse/click/typer, pyproject scripts, Cargo binaries and their clap sub-commands and options), package exports and MCP tools, each with its `kind`, and marks the ones a test reaches (`python scripts/list_interfaces.py <repo> --tests <test dir> --out interfaces.json`, or `--no-tests` for a repository with no tests; `--self-test` checks it on synthetic repositories). In a Next.js project it also lists App Router pages (`kind: pages`, with catch-alls, `generateStaticParams`/`dynamicParams`, parallel-route slots and intercepting routes), metadata routes (robots, sitemap, manifest, favicon and generated or static icons and social images) and `pages/` router pages, gives route handlers the line of each export, and lists what it does not read (server actions, middleware, `public/` files, `next.config` rewrites and redirects) in `not_read` and on stderr. Files under `--tests` that declare a listed interface are not read as tests, and a `--tests` folder with nothing that looks like a test warns (`tests_warnings`). Go is read too (urfave/cli and cobra command tables, `flag` programs, `net/http` and common router routes, listeners, importable packages, with `_test.go` files as the tests). StaticFiles mounts and Flask static folders are routes, middleware is listed under `middleware`, routes and middleware added under an `if` carry `condition`, flags-only argparse programs and `sys.argv[1]` dispatch are commands, and PHP front controllers are read by pattern (`preg_match` on `REQUEST_URI`) and named in `notes`. A `dist/`, `build/` or `out/` folder is skipped only when something produces it (listed in `skipped_dirs`, `--include DIR` reads it); a committed one nothing produces is read as source. Rust is read through the shared `scripts/cargo_project.py` (nothing built): Cargo binaries and clap derive or builder sub-commands with their options and lines, reached only by a command line (a Cargo integration test running the binary with `CARGO_BIN_EXE_<name>`, assert_cmd or `Command::new`, a clap `parse_from` in a test module, or a Python or JavaScript process call with the binary in program position), never by its name in a data string. Sample and fixture projects (nested projects under `examples/`, `fixtures/`, `testdata/`...) are left out by default and listed under `fixtures_excluded` with their interface counts (`--include-fixtures`, `--fixture-dir DIR`); a script kept identical in several folders is listed once on its canonical copy with the others under `copies` (`--keep-copies`); interfaces of a script with its own self-test carry `self_test`, and `--self-tests` reads the repository's self-test scripts as tests (only their command lines reach anything). Vendored and bundled third-party trees are skipped and named in `notes`; the dominant language is decided by product lines (tests and tooling folders counted apart); when it is not read it warns on stderr and in `notes`, records `language_coverage` and exits 3, and a repository mostly (or 30% or more) Rust gets a warning naming what is read only in part
- `scripts/py_guard.py` — runs any Python test command (`python -m unittest`, a plain script) with the network guard loaded into every Python process it starts, child processes included, through a `sitecustomize.py` in `scripts/py_guard_site/` put first on `PYTHONPATH` (`python scripts/py_guard.py run --log <log> -- python -m unittest discover -s tests`; `--home <dir>` gives the run a scratch home while keeping `CARGO_HOME`, `RUSTUP_HOME`, Go's caches and the package-manager caches; `--self-test` checks it)
- `scripts/detect_conventions.py`, `scripts/run_tests.py` — shared: the convention detector (test areas, pytest configurations with what they collect, `self_tests`, CI gates; `--exclude-fixtures`) and the test runner, whose `--preset selftest` runs scripts' self-tests and documented validation commands and turns their `ok`/`FAIL` lines into test cases
- `scripts/cargo_project.py`, `scripts/net_guard_proxy.py` — shared: the static Cargo workspace model (targets, features, tests with `ignored` and `cfg`, clap CLIs) and the refusing-proxy network guard for Go and Rust test binaries (it keeps `CARGO_HOME`/`RUSTUP_HOME` and sets `CARGO_NET_OFFLINE`)
- `scripts/validate_evaluation.py --self-test` and `scripts/render_report.py --self-test` check the failure-count reconciliation (each run's classified failures must add up to its recorded failures) and the brief score card ("No caps applied", the decimal warning, and how many medium and low findings it leaves out)
- `examples/evaluation.example.json` — sample result: a black-box, static-only evaluation
- `examples/evaluation.partial-run.example.json` — sample result: a white-box partial run of a synthetic Next.js app (unit layer run twice and with coverage, end-to-end layer not run), with execution results, interface coverage and mixed evidence levels
- `examples/evaluation.no-tests.example.json` — sample result: a synthetic FastAPI service with no tests and no CI (`execution: no_tests`: discovery collected nothing, so the absence is verified and the evaluation is not provisional; the dimensions that grade existing tests are not assessed)

## Design principles

1. Unknown is not zero.
2. Test-derived inference is not code coverage.
3. A green suite is valuable only if it is trustworthy.
4. Scores require evidence and confidence.
5. Critical weaknesses cap the overall rating.
6. Diagnosis and prioritisation matter more than a vanity score.
