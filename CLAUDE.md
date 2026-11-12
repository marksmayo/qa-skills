# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A collection of vendor-neutral agent skills for QA processes, designed to work with Claude Code, OpenAI Codex, Gemini CLI, and other coding agents. Each skill lives in its own top-level directory (e.g. `test-suite-evaluator-skill/`). There is no build system, package manifest, or test suite — the repo is Markdown instructions, JSON schemas, and standalone Python scripts (stdlib only, no dependencies to install). The one exception is browser automation: scripts that drive a real browser use Playwright for Python, declared in that skill's `requirements.txt` and needed only for that script.

## Commands

Scripts run with plain `python` and take an evaluation JSON file as input:

```bash
# Render an evaluation as a coloured terminal/Markdown report
python test-suite-evaluator-skill/scripts/render_report.py evaluation.json
python test-suite-evaluator-skill/scripts/render_report.py evaluation.json --color always|never
python test-suite-evaluator-skill/scripts/render_report.py evaluation.json --markdown report.md

# Validate an evaluation JSON against the schema and the rubric (recomputes weights, scores, caps, band and evidence completeness)
python test-suite-evaluator-skill/scripts/validate_evaluation.py evaluation.json

# List FastAPI, Flask, Azure Functions, Next.js and Express interfaces and the test files whose URL literals reach them
python test-suite-evaluator-skill/scripts/list_interfaces.py <repo> --tests <test dir> [--out interfaces.json]
```

`examples/evaluation.example.json` is a valid sample input for both scripts; `examples/evaluation.partial-run.example.json` (a synthetic Next.js app) shows execution results, interface coverage and mixed evidence levels.

Journey mapper (run from the repo root; `examples/journeys/` is a complete sample):

```bash
python journey-mapper-skill/scripts/extract_surface.py <repo> --out surface.json [--goal-wrapper trackEvent]
python journey-mapper-skill/scripts/extract_surface.py --self-test
python journey-mapper-skill/scripts/validate_journey_map.py journey-mapper-skill/examples/journeys/journey_map.json [--repo <repo>] [--strict]
python journey-mapper-skill/scripts/write_journeys.py journey-mapper-skill/examples/journeys/journey_map.json --out journey-mapper-skill/examples/journeys
python journey-mapper-skill/scripts/journey_md.py lint journey-mapper-skill/examples/journeys --strict
python journey-mapper-skill/scripts/render_report.py journey-mapper-skill/examples/journeys/journey_map.json [--brief]
```

`--repo` re-reads every cited file and line and checks each journey's test paths; the example's repository is fictional, so it is validated without it.

Journey to tests (`examples/acme-shop/` is the sample repository the example report describes; `--repo` re-checks test files, assertion lines and hashes against it):

```bash
python journey-to-tests-skill/scripts/validate_report.py journey-to-tests-skill/examples/journey_tests_report.example.json --repo journey-to-tests-skill/examples/acme-shop
python journey-to-tests-skill/scripts/render_report.py journey-to-tests-skill/examples/journey_tests_report.example.json
python journey-to-tests-skill/scripts/lint_tests.py journey-to-tests-skill/examples/acme-shop/web/e2e/checkout
python journey-to-tests-skill/scripts/detect_conventions.py <repo> --target <path>
```

The example report's `runs[].file_hashes` are SHA-256 hashes of the example test files (line endings normalised). Editing `examples/acme-shop/web/e2e/` therefore invalidates the example until its runs are regenerated.

Ticket to TDD (`examples/bookshop/` is the sample repository; its runs are real pytest output, `--ticket` re-checks requirement excerpts against the normalised ticket):

```bash
python ticket-to-tdd-skill/scripts/fetch_ticket.py --from-file ticket-to-tdd-skill/examples/BILL-142.issue.json --source connector --out ticket.json
python ticket-to-tdd-skill/scripts/validate_tdd_report.py ticket-to-tdd-skill/examples/tdd_report.example.json --ticket ticket-to-tdd-skill/examples/ticket.example.json --repo ticket-to-tdd-skill/examples/bookshop
python ticket-to-tdd-skill/scripts/render_report.py ticket-to-tdd-skill/examples/tdd_report.example.json
python ticket-to-tdd-skill/scripts/hash_tests.py check ticket-to-tdd-skill/examples/tdd_report.example.json --repo ticket-to-tdd-skill/examples/bookshop
```

The example report locks `examples/bookshop/tests/test_discount_codes.py` by hash, and its `content_sha256` fingerprints `examples/ticket.example.json`; editing either invalidates the example. Running pytest inside `examples/bookshop/` is fine, but the red tests are meant to fail there.

UI crash hunter (`examples/pantry/` is the sample React app; the tool outputs in `examples/run/`, the probe evidence and screenshots are real; `--candidates` checks every candidate was triaged):

```bash
python ui-crash-hunter-skill/scripts/validate_crash_report.py ui-crash-hunter-skill/examples/crash_report.example.json --repo ui-crash-hunter-skill/examples/pantry --candidates ui-crash-hunter-skill/examples/run/candidates.json
python ui-crash-hunter-skill/scripts/render_report.py ui-crash-hunter-skill/examples/crash_report.example.json [--sarif out.sarif]
python ui-crash-hunter-skill/scripts/scan_pitfalls.py ui-crash-hunter-skill/examples/pantry --extra ui-crash-hunter-skill/examples/run/tsc.json ui-crash-hunter-skill/examples/run/contract.json
python ui-crash-hunter-skill/scripts/contract_diff.py ui-crash-hunter-skill/examples/pantry
python ui-crash-hunter-skill/scripts/recon.py ui-crash-hunter-skill/examples/pantry
python ui-crash-hunter-skill/scripts/scan_pitfalls.py --self-test
python ui-crash-hunter-skill/scripts/ui_graph.py ui-crash-hunter-skill/examples/pantry
python ui-crash-hunter-skill/scripts/render_report.py ui-crash-hunter-skill/examples/crash_report.example.json --brief
```

The example report quotes source lines of `examples/pantry/src/` by file and line, and its triage covers exactly the candidates in `examples/run/candidates.json`; editing either invalidates the example. `recon.py` models static multi-page sites as well as single-page apps, and probes can reproduce a degradation by an observable effect (`expect`) rather than a stack trace. `probe_web.py` and `strict_overlay.py` need the app built (`npm install && npm run build` in a copy of `examples/pantry/` outside the repo) and are not needed to validate the example. `probe_web.py` refuses a non-loopback target without `--authorised-by` and probes it read-only (writes answered in the browser, third parties blocked); `--storage-state` probes signed in. `contract_diff.py` takes `--discover` (also read the repo's own contracts), `--scope ui|all` and `--defaults-present`. `scan_pitfalls.py` follows imports from the UI entry points (resolving tsconfig `paths`) and excludes tests, route handlers and unreachable modules unless `--all-files` is given.

Code security auditor (`examples/invoicer/` is the sample, deliberately vulnerable Flask app; the scanner outputs in `examples/run/`, the PoC evidence in `examples/evidence/` and the regression runs in `examples/regression/` are real; `--candidates` checks every candidate and dependency advisory was triaged):

```bash
python code-security-auditor-skill/scripts/validate_security_audit.py code-security-auditor-skill/examples/security_audit.example.json --repo code-security-auditor-skill/examples/invoicer --candidates code-security-auditor-skill/examples/run/candidates.json
python code-security-auditor-skill/scripts/render_report.py code-security-auditor-skill/examples/security_audit.example.json [--brief] [--sarif out.sarif]
python code-security-auditor-skill/scripts/scan_pitfalls.py code-security-auditor-skill/examples/invoicer
python code-security-auditor-skill/scripts/recon.py code-security-auditor-skill/examples/invoicer
python code-security-auditor-skill/scripts/run_poc.py code-security-auditor-skill/examples/pocs/sqli-search.json --check
python code-security-auditor-skill/scripts/cvss4.py "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N"
python code-security-auditor-skill/scripts/self_test.py
python code-security-auditor-skill/scripts/seed_report.py --recon recon.json --candidates candidates.json --scanners scanners.json --out security_audit.json
python code-security-auditor-skill/scripts/run_scanners.py <repo> --out-dir <dir> [--offline] [--osv-local-db <dir>]
```

`examples/kite-notes/` is a synthetic Next.js, Prisma and NextAuth fixture that `self_test.py` checks against its `expected.json`; editing it or the JavaScript pack in `data/pitfalls.json` means updating `expected.json`. `self_test.py` also checks `examples/ferry-ops/` (a synthetic FastAPI service, Azure Functions, Bicep/ARM and Azure Pipelines) against its own `expected.json`; editing it, the `azure_iac`, `azure_pipelines`, `python` or `javascript` packs, or `py_routes.py` means updating that one. `run_scanners.py` writes `dependencies.json` and records `dependency_advisories` as assessed, partial or not assessed; `render_report.py --sarif` writes only the SARIF file. Findings may be `source: "agent"` with a `basis` that `--repo` re-reads.

The example report quotes source lines of `examples/invoicer/` by file and line, re-reads the planted secret to check its value never appears in the report or evidence, and its triage covers exactly the candidates in `examples/run/candidates.json`; editing either invalidates the example. Re-running the PoCs needs the app running locally (`pip install -r requirements.txt && flask --app invoicer run --port 5057` in a copy outside the repo); the scanners themselves are optional and only `run_scanners.py` uses them. `scripts/cvss4.py` is a port of the FIRST CVSS v4.0 calculator (BSD-2-Clause); keep its licence header.

WCAG auditor (`examples/harbour-dental/site/` is the sample site with planted barriers; `examples/run/` is a real run against it, including screenshots and accessibility-tree snapshots; `--run` checks tool statuses and the check matrix against `run.json`, `--candidates` checks every candidate was triaged):

```bash
python wcag-auditor-skill/scripts/validate_audit.py wcag-auditor-skill/examples/audit.example.json --run wcag-auditor-skill/examples/run --candidates wcag-auditor-skill/examples/run/candidates.json
python wcag-auditor-skill/scripts/render_report.py wcag-auditor-skill/examples/audit.example.json [--brief]
python wcag-auditor-skill/scripts/normalize.py wcag-auditor-skill/examples/run --out items.json
python wcag-auditor-skill/scripts/merge_findings.py items.json --run wcag-auditor-skill/examples/run --out candidates.json
python wcag-auditor-skill/scripts/fetch_engines.py --check
python wcag-auditor-skill/scripts/merge_findings.py --self-test
```

The example report's triage covers exactly the candidates in `examples/run/candidates.json`, and its findings cite screenshots in `examples/run/screenshots/`; regenerating the run (or editing `data/rule-map.json` or the merge logic) changes the candidates and invalidates the example. Re-running the audit needs Playwright (`pip install -r wcag-auditor-skill/requirements.txt && python -m playwright install chromium`), the engines from `fetch_engines.py`, and the site served on loopback (`python -m http.server 8137 --bind 127.0.0.1 --directory wcag-auditor-skill/examples/harbour-dental/site`). `data/rule-map.json` is generated from the pinned engines' own rule metadata by `scripts/build_rule_map.py` (needs Playwright and the engines; `--check` compares without writing); regenerate it whenever `data/engines.lock.json` changes.

Ticket readiness reviewer (`examples/tickets/` holds four planted Jira issues; `--tickets` re-checks criteria, flag excerpts, fingerprints and ignored instructions against the normalised tickets, `--lint` checks every lint flag was kept or dismissed):

```bash
python ticket-readiness-reviewer-skill/scripts/validate_readiness.py ticket-readiness-reviewer-skill/examples/batch.example.json --tickets ticket-readiness-reviewer-skill/examples/tickets.json --lint ticket-readiness-reviewer-skill/examples/lint.json
python ticket-readiness-reviewer-skill/scripts/render_report.py ticket-readiness-reviewer-skill/examples/batch.example.json [--brief] [--comment SHOP-203]
python ticket-readiness-reviewer-skill/scripts/fetch_ticket.py --from-file ticket-readiness-reviewer-skill/examples/tickets --source connector --out tickets.json
python ticket-readiness-reviewer-skill/scripts/lint_criteria.py ticket-readiness-reviewer-skill/examples/tickets.json --out lint.json
```

The example reviews fingerprint `examples/tickets.json` and cite lint ids from `examples/lint.json`; editing the tickets, `data/ambiguity-lexicon.json` (shared with the test case designer, canonical in `shared/data/`) or the lint changes them and invalidates the examples.

Flaky test hunter (`examples/ferry-booking/` is the sample package with planted flakes; `examples/run/` holds the real pytest runs, listed in `examples/run/commands.sh`; `--runs` recomputes every count, interval and status from them, `--repo` checks every proposed fix applies):

```bash
python flaky-test-hunter-skill/scripts/validate_flaky_report.py flaky-test-hunter-skill/examples/flaky_report.example.json --runs flaky-test-hunter-skill/examples/run/runs.json --smells flaky-test-hunter-skill/examples/run/smells.json --bisect flaky-test-hunter-skill/examples/run/bisect.json --repo flaky-test-hunter-skill/examples/ferry-booking
python flaky-test-hunter-skill/scripts/render_report.py flaky-test-hunter-skill/examples/flaky_report.example.json [--brief]
python flaky-test-hunter-skill/scripts/scan_pitfalls.py flaky-test-hunter-skill/examples/ferry-booking
python flaky-test-hunter-skill/scripts/run_tests.py --preset pytest --cwd flaky-test-hunter-skill/examples/ferry-booking --out runs.json --repeat 3
python flaky-test-hunter-skill/scripts/stats.py 2 20
```

`validate_flaky_report.py --bisect` takes several bisect files, and `bisect_polluter.py --relation enabler` finds the earlier test a victim needs when it fails alone. For Node repositories, `run_tests.py --npm-script test` reuses the repository's own test script, and `plugins/clock_shift.mjs` shifts the clock in Vitest or Node runs as `plugins/clock_shift.py` does for pytest.

Every run records a `code_hash` of `examples/ferry-booking/`, and the report counts only runs with its hash, so editing the package invalidates the example until the runs are regenerated with `commands.sh` (which needs pytest and pytest-xdist, and gives different counts on another machine or at another time of day).

Mutation tester (`examples/fare-calc/` is the sample package and `change.diff` the change in scope; `examples/run/` is the real run, listed in `examples/run/commands.sh`, with both engines' raw results; `--mutants` checks the report holds exactly the normalised mutants, `--proofs` that every cited suggested test passed on the original and failed on its mutants):

```bash
python mutation-tester-skill/scripts/validate_mutation_report.py mutation-tester-skill/examples/mutation_report.example.json --mutants mutation-tester-skill/examples/run/mutants.json --runs mutation-tester-skill/examples/run/runs.json --proofs mutation-tester-skill/examples/run/proofs.json --repo mutation-tester-skill/examples/fare-calc
python mutation-tester-skill/scripts/render_report.py mutation-tester-skill/examples/mutation_report.example.json [--brief]
python mutation-tester-skill/scripts/diff_scope.py --repo mutation-tester-skill/examples/fare-calc --diff mutation-tester-skill/examples/fare-calc/change.diff
python mutation-tester-skill/scripts/detect_tools.py mutation-tester-skill/examples/fare-calc
python mutation-tester-skill/scripts/run_engine.py ranges --scope <scope.json>
python mutation-tester-skill/scripts/path_scope.py --repo <repo> --path <dir|file[:a-b]> --out scope.json
```

StrykerJS runs with `run_engine.py stryker --repo <repo> --scope <scope.json> --run-dir <dir>`, then `normalize_results.py --stryker <dir>/stryker-report.json --stryker-run <dir>/stryker.run.json`; `prove_kill.py --runner vitest|jest` proves suggested JavaScript tests, and its `--env` takes several `K=V` pairs per flag. `builtin_mutator.py --tests` limits the engine to named tests and `--pytest-args` passes pytest options. No example uses Stryker.

The report records a `code_hash` of `examples/fare-calc/` and quotes each mutant's original line, so editing the package invalidates the example until the run is repeated. `builtin_mutator.py` edits the scoped source files one mutant at a time and restores them (it checks the tree hash before and after); run it on a copy if in doubt. The mutmut step needs mutmut 3, which needs fork() (Linux, macOS or WSL). The example's `builtin.json` predates position-based mutant ids, so a rerun renumbers the builtin mutants.

Test impact selector (`examples/harbourline/` is the sample monorepo, `examples/changes/` the five changes and `examples/history/` twelve earlier changes with real full-run JUnit; `examples/run/` is the real run, listed in `examples/run/commands.sh`; `--selection` checks the report's computed sections against `select_tests.py`'s output, `--recompute` re-runs the selection from its recorded inputs, `--replay` checks recall and misses, `--runs` checks the verify run):

```bash
python test-impact-selector-skill/scripts/validate_impact_report.py test-impact-selector-skill/examples/impact_report.example.json --selection test-impact-selector-skill/examples/run/C1/selection.json --replay test-impact-selector-skill/examples/run/replay.json --runs test-impact-selector-skill/examples/run/C1/verify/runs.json --recompute
python test-impact-selector-skill/scripts/validate_impact_report.py test-impact-selector-skill/examples/reports/C3.impact_report.json --selection test-impact-selector-skill/examples/run/C3/selection.json --replay test-impact-selector-skill/examples/run/replay.json --recompute
python test-impact-selector-skill/scripts/render_report.py test-impact-selector-skill/examples/impact_report.example.json [--brief]
python test-impact-selector-skill/scripts/select_tests.py --repo test-impact-selector-skill/examples/harbourline --diff test-impact-selector-skill/examples/changes/C1-late-sailing-discount.diff --coverage test-impact-selector-skill/examples/run/coverage_map.json --journeys test-impact-selector-skill/examples/journeys/journey_map.json --history test-impact-selector-skill/examples/history/history.json --out selection.json
python test-impact-selector-skill/scripts/replay.py --repo test-impact-selector-skill/examples/harbourline --history test-impact-selector-skill/examples/history/history.json --coverage test-impact-selector-skill/examples/run/coverage_map.json --journeys test-impact-selector-skill/examples/journeys/journey_map.json --out replay.json
python test-impact-selector-skill/scripts/import_graph.py test-impact-selector-skill/examples/harbourline --reach api/harbourline_api/pricing.py
```

Run these from the repo root: `selection.json` records its inputs relative to the current directory, and `--recompute` resolves them from there. The selector applies each diff to a scratch copy and never edits `examples/harbourline/`. Editing `harbourline/` changes the repo code hash and makes the coverage map stale for the edited files, so regenerate with `commands.sh`. The history and verify runs need Vitest 3.2.4 installed in a copy of `web/` outside the repo; the coverage map came from coverage.py with pytest-cov.

Test data auditor (`examples/clinic-api/` is the sample package with planted test-data problems, all personal details invented; `examples/run/` is the real run, listed in `examples/run/commands.sh`; `--candidates` checks every candidate was promoted, dismissed or handed off, `--repo` re-reads each flagged location so no personal-data or secret value appears in the report or `--evidence`, `--runs` checks each experiment failed where the baseline passed):

```bash
python test-data-auditor-skill/scripts/validate_test_data_report.py test-data-auditor-skill/examples/test_data_report.example.json --candidates test-data-auditor-skill/examples/run/candidates.json --repo test-data-auditor-skill/examples/clinic-api --runs test-data-auditor-skill/examples/run/runs.json --evidence test-data-auditor-skill/examples/run
python test-data-auditor-skill/scripts/render_report.py test-data-auditor-skill/examples/test_data_report.example.json [--brief]
python test-data-auditor-skill/scripts/inventory.py test-data-auditor-skill/examples/clinic-api --out inventory.json
python test-data-auditor-skill/scripts/scan_data.py test-data-auditor-skill/examples/clinic-api --inventory inventory.json --today 2026-11-11 --out candidates.json
python test-data-auditor-skill/scripts/redact_evidence.py <run> --candidates <run>/candidates.json --repo <repo>
python test-data-auditor-skill/scripts/residue.py snapshot|diff ...
```

Findings may be `source: "agent"` with a `basis` (file and line, or evidence), for example a time bomb confirmed by a clock experiment; `residue` covers files tests leave behind. `scripts/clock_shift.mjs` is the JavaScript counterpart of `clock_shift.py`. `inventory.py --data-dir <dir>` (repeatable) adds a test-data tree under a name it does not know, `redact_evidence.py --report <report.json>` honours review dismissals marked `not_personal_data`, `datafiles.py <file> --text` prints the text form findings count lines in (an `.xlsx` workbook as CSV per sheet), and the schema drift output (`drift.json`) holds `unmatched_records` per file plus `unmatched_total`.

The candidates and the report cite lines in `examples/clinic-api/`, and the time-bomb figures count from the audit date `2026-11-11` recorded in the report (pass `--today 2026-11-11` when re-scanning). Editing the package invalidates the example until the run is repeated. The experiments run on a copy with `scripts/clock_shift.py` at its root; the locale run needs Linux or WSL, because Windows ignores `LANG`, so the example's locale run C-1 is carried over from an earlier run.

Dead test finder (`examples/ledger/` is the sample app, a Python core with pytest and a TypeScript client with Vitest; `examples/proposals/` holds one patch per finding; `examples/run/` is the real run, listed in `examples/run/commands.sh`; the validator reads the evidence files named in the report, relative to the report's directory, and `--repo` checks every patch still applies):

```bash
python dead-test-finder-skill/scripts/validate_dead_test_report.py dead-test-finder-skill/examples/dead_test_report.example.json --repo dead-test-finder-skill/examples/ledger
python dead-test-finder-skill/scripts/render_report.py dead-test-finder-skill/examples/dead_test_report.example.json [--brief]
python dead-test-finder-skill/scripts/scan_tests.py dead-test-finder-skill/examples/ledger --out candidates.json
python dead-test-finder-skill/scripts/unified_diff.py check dead-test-finder-skill/examples/proposals/assert-post-entry.diff --root dead-test-finder-skill/examples/ledger
```

The candidates, neutralisation runs and proofs cite lines and test ids in `examples/ledger/`, so editing it invalidates the example until `commands.sh` is rerun. Findings may be `source: "agent"` with a `basis` that `--repo` re-reads; `collect_check.py` takes `--env`, `--scope candidates|--files` and `--reuse`, and lists Playwright specs with `playwright test --list`. That needs a scratch copy of `ledger/client` with `npm install` done (`CLIENT=...`). Neutralisation works in copies outside the repo and restores everything it patches. `collect_check.py`, `similarity.py` and `neutralise.py` take `--package-env PACKAGE:K=V` for one package's runs, and `similarity.json` stores literals as hashes.

Risk-based test planner (`examples/make_sample_repo.py` builds the sample git repository "larder" deterministically, HEAD `d834c7e`, because a `.git` directory can't be shipped; `examples/run/` holds the mined history, complexity, scope and scores, listed in `examples/run/commands.sh`; `--risk` checks the plan's areas against the scores, `--input` checks carried findings keep their source confidence):

```bash
python risk-based-test-planner-skill/scripts/validate_test_plan.py risk-based-test-planner-skill/examples/test_plan.example.json --risk risk-based-test-planner-skill/examples/run/risk.json --input security=risk-based-test-planner-skill/examples/inputs/security_audit.json
python risk-based-test-planner-skill/scripts/validate_test_plan.py risk-based-test-planner-skill/examples/test_plan.one-session.example.json --risk risk-based-test-planner-skill/examples/run/risk.json --input security=risk-based-test-planner-skill/examples/inputs/security_audit.json
python risk-based-test-planner-skill/scripts/render_report.py risk-based-test-planner-skill/examples/test_plan.example.json [--brief | --markdown plan.md]
python risk-based-test-planner-skill/examples/make_sample_repo.py <empty dir>
```

`score_risk.py --finding-area KIND:ID=AREA` carries an input finding on another area when its report cites the wrong file, and several `--input` reports of one kind are allowed. Re-scoring needs the sample repository: build it with `make_sample_repo.py`, then run `commands.sh` with `LARDER=<that dir>`. The history window is fixed with `--as-of 2026-11-01`, so the scores don't depend on today's date. Editing the trimmed inputs in `examples/inputs/` or `data/surfaces.json` changes the scores and invalidates both plans until they are re-scored and re-validated with `--write-summary`.

Exploratory charter writer (`examples/inputs/` holds a gift-card ticket, journey map, risk-plan excerpt and scope excerpt; `examples/run/commands.sh` lists every step; `validate_charters.py` checks each basis excerpt is verbatim from `run/sources.json` and recomputes the ranking; `validate_debrief.py` re-reads the notes so every quote, line, metric and redaction matches them):

```bash
python exploratory-charter-writer-skill/scripts/validate_charters.py exploratory-charter-writer-skill/examples/charters.example.json --sources exploratory-charter-writer-skill/examples/run/sources.json --md-dir exploratory-charter-writer-skill/examples/charters --personas exploratory-charter-writer-skill/examples/inputs/journey_map.json
python exploratory-charter-writer-skill/scripts/validate_debrief.py exploratory-charter-writer-skill/examples/debrief.example.json --notes exploratory-charter-writer-skill/examples/notes/session-01.md --parsed exploratory-charter-writer-skill/examples/run/parsed-session-01.json --charters exploratory-charter-writer-skill/examples/charters.example.json
python exploratory-charter-writer-skill/scripts/render_report.py exploratory-charter-writer-skill/examples/debrief.example.json [--brief]
python exploratory-charter-writer-skill/scripts/parse_notes.py exploratory-charter-writer-skill/examples/notes/session-01.md --out parsed.json
```

The debrief fingerprints `examples/notes/session-01.md` and quotes it by line number, so editing the notes invalidates the debrief. Editing the inputs or `run/draft.json` changes the sources and rankings; rerun `commands.sh`.

Bug reproducer (`examples/tasklist/` is the sample static app with two planted bugs and `examples/reports/` five Jira issues about it; `examples/run/` is a real run, listed in `examples/run/commands.sh`, with screenshots, console logs and Playwright traces; `--run` checks every attempt equals its record, `--tickets` re-checks triage, harmful steps and injected instructions):

```bash
python bug-reproducer-skill/scripts/validate_repro.py bug-reproducer-skill/examples/repro_report.example.json --tickets bug-reproducer-skill/examples/run/tickets.json --run bug-reproducer-skill/examples/run
python bug-reproducer-skill/scripts/render_report.py bug-reproducer-skill/examples/repro_report.example.json [--brief] [--comment TASK-13]
python bug-reproducer-skill/scripts/triage.py --ticket bug-reproducer-skill/examples/run/tickets.json
python bug-reproducer-skill/scripts/dupes.py --ticket bug-reproducer-skill/examples/run/tickets.json --key TASK-14 --candidates bug-reproducer-skill/examples/run/tickets.json
```

Re-running the attempts needs Playwright with Chromium, Firefox and WebKit (`pip install -r bug-reproducer-skill/requirements.txt`) and the app served on loopback (`python -m http.server 8141 --bind 127.0.0.1 --directory bug-reproducer-skill/examples/tasklist`). TASK-12 is a race, so its counts change on every run. The report quotes `examples/tasklist/src/` by line, and `examples/tasklist/tests/due_today_timezone.test.mjs` is the red regression test, meant to fail. Editing the app, the reports or the specs invalidates the example until `commands.sh` is rerun.

API contract tester (`examples/clinic-api/` is the sample stdlib API with planted contract breaks, its OpenAPI 3.1 contract, the previous 3.0 contract and a GraphQL schema; `examples/run/` and `examples/run-graphql/` are real runs with redacted exchanges, listed in `examples/run/commands.sh`; the validator recomputes operations, candidates, ratings and the headline from the run, and scans the exchanges for credentials):

```bash
python api-contract-tester-skill/scripts/validate_report.py api-contract-tester-skill/examples/report.example.json --run api-contract-tester-skill/examples/run/run.json --candidates api-contract-tester-skill/examples/run/candidates.json --contract api-contract-tester-skill/examples/run/contract.json --cases api-contract-tester-skill/examples/run/cases.json --diff api-contract-tester-skill/examples/run/diff.json --repo api-contract-tester-skill/examples/clinic-api
python api-contract-tester-skill/scripts/validate_report.py api-contract-tester-skill/examples/report.graphql.example.json --run api-contract-tester-skill/examples/run-graphql/run.json --candidates api-contract-tester-skill/examples/run-graphql/candidates.json --contract api-contract-tester-skill/examples/run-graphql/contract.json --cases api-contract-tester-skill/examples/run-graphql/cases.json --repo api-contract-tester-skill/examples/clinic-api
python api-contract-tester-skill/scripts/render_report.py api-contract-tester-skill/examples/report.example.json [--brief] [--sarif out.sarif --contract api-contract-tester-skill/examples/run/contract.json]
python api-contract-tester-skill/scripts/load_contract.py api-contract-tester-skill/examples/clinic-api/openapi.yaml --out contract.json
python api-contract-tester-skill/scripts/spec_diff.py api-contract-tester-skill/examples/run/contract.v1.json api-contract-tester-skill/examples/run/contract.json --out diff.json
python api-contract-tester-skill/scripts/validate_report.py api-contract-tester-skill/examples/report.static.example.json --contract api-contract-tester-skill/examples/run/contract.json --cases api-contract-tester-skill/examples/run/cases.json --diff api-contract-tester-skill/examples/run/diff.json --repo api-contract-tester-skill/examples/clinic-api
python api-contract-tester-skill/scripts/handler_map.py --self-test
```

A `not_run` (static-only) report validates without `--run`. `handler_map.py` compares the contract with the route handlers (Next.js, Express, Flask, FastAPI); `--handler-prefix` prepends a mount prefix the reader cannot see. Against a deployed target the sender takes `--on-429 stop-run|stop-path`, `--path-budget`, `--max-requests`, `--resume` and `--cookie-env`.

`commands.sh` runs from `api-contract-tester-skill/`, so the recorded contract sources are relative to it. Re-running needs the server on loopback (`python api-contract-tester-skill/examples/clinic-api/server.py --port 8140`, token `fake-clinic-token` in `CLINIC_TOKEN`), restarted before each sending step because its planted 503 counts calls. Schemathesis is optional (set `ST`). The report cites `examples/clinic-api/server.py` by line, so editing the server, the contracts or the cases invalidates the example until `commands.sh` is rerun.

Performance budget auditor (`examples/bakery/` is the sample site with planted problems, `examples/plan.json` and `examples/budget.json` the inputs; `examples/run/` is a real run, listed in `examples/run/commands.sh`, with traces, filmstrips and coverage; the validator is structural: it recomputes every statistic, status, candidate and rating from the recorded runs, not the timings themselves):

```bash
python performance-budget-auditor-skill/scripts/validate_report.py performance-budget-auditor-skill/examples/perf_report.example.json --measurements performance-budget-auditor-skill/examples/run/measurements.json --candidates performance-budget-auditor-skill/examples/run/candidates.json --budget performance-budget-auditor-skill/examples/budget.json --regressions performance-budget-auditor-skill/examples/run/regressions.json
python performance-budget-auditor-skill/scripts/render_report.py performance-budget-auditor-skill/examples/perf_report.example.json [--brief]
python performance-budget-auditor-skill/scripts/attribute.py performance-budget-auditor-skill/examples/run/measurements.json --budget performance-budget-auditor-skill/examples/budget.json --out candidates.json
python performance-budget-auditor-skill/scripts/fetch_assets.py --check
```

The example's measurements predate `min_runs`/`max_runs`, variants, CDN cache headers and request summaries, which `attribute.py` reads with defaults. Re-measuring needs Playwright with Chromium and Firefox, web-vitals in the cache (`fetch_assets.py`), and the site on loopback (`python performance-budget-auditor-skill/examples/bakery/serve.py`, which refuses a busy port); run `commands.sh` from `performance-budget-auditor-skill/`. Lab numbers depend on the machine, so a rerun changes every value and invalidates the report until it is rebuilt.

Visual regression reviewer (`examples/storefront/` holds two versions of a small shop with planted changes, `examples/change_context.md` the PR description; `examples/run/` is a real run, listed in `examples/run/commands.sh`, with captures, DOM boxes, diff images and crops; the validator checks groups against `groups.json`, crop hashes, and that every changed pixel is accounted for):

```bash
python visual-regression-reviewer-skill/scripts/validate_review.py visual-regression-reviewer-skill/examples/review.example.json --groups visual-regression-reviewer-skill/examples/run/groups.json --diff visual-regression-reviewer-skill/examples/run/diff.json
python visual-regression-reviewer-skill/scripts/render_report.py visual-regression-reviewer-skill/examples/review.example.json [--brief]
python visual-regression-reviewer-skill/scripts/attribute.py visual-regression-reviewer-skill/examples/run/diff.json --pair chromium/1280x800/home.png
```

`diff.json` records image paths relative to `visual-regression-reviewer-skill/`, so run `group.py` and `attribute.py` from there. Re-capturing needs Playwright with Chromium and both versions on loopback (`commands.sh` starts them); the clock and carousel make every capture differ slightly, so a rerun changes the pixel counts and invalidates the review until it is rebuilt.

Cross-browser checker (`examples/travel-site/` is the sample site with planted engine differences and a browserslist; `examples/run/` is a real run in three engines, listed in `examples/run/commands.sh`; the validator reruns the engine comparison on the recorded runs, so it needs the feature data in the cache for usage shares):

```bash
python cross-browser-checker-skill/scripts/validate_report.py cross-browser-checker-skill/examples/report.example.json --runs cross-browser-checker-skill/examples/run/capture/runs.json --candidates cross-browser-checker-skill/examples/run/candidates.json --features cross-browser-checker-skill/examples/run/features.json --targets cross-browser-checker-skill/examples/targets.json
python cross-browser-checker-skill/scripts/validate_report.py cross-browser-checker-skill/examples/report.static.example.json --candidates cross-browser-checker-skill/examples/static-only/candidates.json --features cross-browser-checker-skill/examples/run/features.json --targets cross-browser-checker-skill/examples/targets.json
python cross-browser-checker-skill/scripts/render_report.py cross-browser-checker-skill/examples/report.example.json [--brief]
python cross-browser-checker-skill/scripts/fetch_data.py [--check]
python cross-browser-checker-skill/scripts/feature_scan.py --repo <repo> [--built-css <dir>] --out features.json
python cross-browser-checker-skill/scripts/browserslist_lite.py --repo cross-browser-checker-skill/examples/travel-site
```

`features.json` records the repo path relative to `cross-browser-checker-skill/`, so run `feature_scan.py` and `commands.sh` from there. `compare_engines.py --static-only` writes the candidates of a feature scan alone (no browser), as in the static-only example. Re-capturing needs Playwright with Chromium, Firefox and WebKit; the flaky banner is random, so a rerun changes the flaky counts and invalidates the report until it is rebuilt.

i18n checker (`examples/bookclub/` is the sample site with en, de, fr and ar resources plus an Android resource set, all with planted problems; `examples/run/` is a real run, listed in `examples/run/commands.sh`, with runtime screenshots and the proposed `missing_keys.patch`; the CLDR subsets in `data/` are committed and pinned):

```bash
python i18n-checker-skill/scripts/validate_report.py i18n-checker-skill/examples/report.example.json --candidates i18n-checker-skill/examples/run/candidates.json --keys i18n-checker-skill/examples/run/keys.json --resources i18n-checker-skill/examples/run/resources.json --repo i18n-checker-skill/examples/bookclub --runtime i18n-checker-skill/examples/run/runtime/runtime.json
python i18n-checker-skill/scripts/render_report.py i18n-checker-skill/examples/report.example.json [--brief]
python i18n-checker-skill/scripts/fetch_cldr.py --check
python i18n-checker-skill/scripts/icu_lite.py "{count, plural, one {# book} other {# books}}"
```

The report cites `examples/bookclub/` by file and line, so editing the site or its resources invalidates it until `commands.sh` is rerun from `i18n-checker-skill/` (the runtime step needs Playwright with Chromium).

Resilience prober (`examples/parcel-tracker/` is the sample single-page app with a stub API and planted resilience problems; `examples/flows.json` the flows and scenarios; `examples/run/` is a real run, listed in `examples/run/commands.sh`, with screenshots and a repro script per finding):

```bash
python resilience-prober-skill/scripts/validate_resilience.py resilience-prober-skill/examples/resilience_report.example.json --run resilience-prober-skill/examples/run/run.json --scenarios resilience-prober-skill/examples/run/scenarios.json --candidates resilience-prober-skill/examples/run/candidates.json
python resilience-prober-skill/scripts/render_report.py resilience-prober-skill/examples/resilience_report.example.json [--brief]
python resilience-prober-skill/scripts/plan_scenarios.py resilience-prober-skill/examples/flows.json --plan-only
python resilience-prober-skill/scripts/build_report.py --run <run>/run.json --scenarios <run>/scenarios.json --candidates <run>/candidates.json --flows <flows.json> --promote K1,... --repros <run>/repros --out report.json
```

Re-running needs Playwright with Chromium and the app on loopback (`commands.sh` starts it); rerunning the plan rewrites `planned_at`, so rebuild the report afterwards. The example has 13 scenarios, including a flow whose baseline fails and a fault that never triggers (`not_triggered`). Writes are blocked by default (`read_only`).

Database migration reviewer (`examples/ledger/` is the sample Flask app with Alembic migrations, 0003–0009 pending, and planted problems; `examples/run/` holds the detection and candidates, listed in `examples/run/commands.sh`; the dynamic check was not run, so the example is static only):

```bash
python db-migration-reviewer-skill/scripts/validate_migration_review.py db-migration-reviewer-skill/examples/migration_review.example.json --candidates db-migration-reviewer-skill/examples/run/candidates.json --migrations db-migration-reviewer-skill/examples/run/migrations.json --repo db-migration-reviewer-skill/examples/ledger
python db-migration-reviewer-skill/scripts/render_report.py db-migration-reviewer-skill/examples/migration_review.example.json [--brief]
python db-migration-reviewer-skill/scripts/detect_migrations.py --repo db-migration-reviewer-skill/examples/ledger --since 0002 --out migrations.json
python db-migration-reviewer-skill/scripts/scan_migrations.py --migrations migrations.json --repo db-migration-reviewer-skill/examples/ledger --table-stats db-migration-reviewer-skill/examples/ledger/table_stats.json --out candidates.json
python db-migration-reviewer-skill/scripts/validate_migration_review.py db-migration-reviewer-skill/examples/migration_review.prisma.example.json --candidates db-migration-reviewer-skill/examples/run-prisma/candidates.json --migrations db-migration-reviewer-skill/examples/run-prisma/migrations.json --repo db-migration-reviewer-skill/examples/greenhouse
python db-migration-reviewer-skill/scripts/self_test.py
```

`examples/greenhouse/` is the Prisma sample (its run is in `examples/run-prisma/`, regenerated by its `commands.sh`). Findings may be `source: "agent"` with a `basis` that `--repo` re-reads, and may have direction `down`. `detect_migrations.py --chain <dir>` picks one of several Alembic chains, `--infra <path>` reads the engine version from infrastructure code elsewhere and `--engine-source` records where a given version came from; `scan_migrations.py --base-ref <rev>` also searches the release still running for readers of renamed or dropped columns. Code hits may carry a `rev`, re-read with `git show`, so `--repo` must then be a git checkout.

The candidates and the review cite lines in `examples/ledger/`, and the sizes come from `examples/ledger/table_stats.json`; editing either (or `data/rules.json`) changes the candidates and invalidates the example until `commands.sh` is rerun. `dynamic_check.py` needs psycopg and Docker, Podman or a loopback PostgreSQL; it refuses any other host and only works in a `qa_migrate_` database it creates.

Privacy flow auditor (`examples/clinic-signup/` is the sample sign-up site with stub vendors on made-up `.test` hostnames; `examples/run/` is a real three-state capture, listed in `examples/run/commands.sh`, with every value redacted; `--recompute` re-runs the merge, `--evidence` scans every evidence file for personal data):

```bash
python privacy-flow-auditor-skill/scripts/validate_privacy_audit.py privacy-flow-auditor-skill/examples/privacy_audit.example.json --candidates privacy-flow-auditor-skill/examples/run/candidates.json --capture privacy-flow-auditor-skill/examples/run/capture.json --canaries privacy-flow-auditor-skill/examples/run/canaries.json --evidence privacy-flow-auditor-skill/examples/run --repo privacy-flow-auditor-skill/examples/clinic-signup --recompute
python privacy-flow-auditor-skill/scripts/render_report.py privacy-flow-auditor-skill/examples/privacy_audit.example.json [--brief]
python privacy-flow-auditor-skill/scripts/scan_privacy.py privacy-flow-auditor-skill/examples/clinic-signup --exclude stub-vendors --out static.json
python privacy-flow-auditor-skill/scripts/fetch_lists.py --check
python privacy-flow-auditor-skill/scripts/validate_privacy_audit.py privacy-flow-auditor-skill/examples/privacy_audit.static-only.example.json --candidates privacy-flow-auditor-skill/examples/static-only/candidates.json --repo privacy-flow-auditor-skill/examples/clinic-signup --recompute
```

The static-only example (`run_status: not_run`) is built from `run/static.json` alone. In `flows.json`, `"consent": {"banner": null}` declares an app with no consent banner and `"storage_state"` a signed-in session file; the validator's `--fix-ratings` rewrites each finding's computed rating fields from its inputs. A `domains.json` cached by an older `fetch_lists.py` must be rebuilt by rerunning it, to pick up EasyPrivacy path rules and the pinned shared-hosting suffixes in `data/public-suffix-private.json`.

Run these from the repo root: `candidates.json` records its inputs relative to it. The audit cites request numbers and redacted URLs from `examples/run/capture.json` and lines in `examples/clinic-signup/`; re-running the capture changes the client ids and invalidates the audit until it is rebuilt. Capturing needs Playwright with Chromium. `fetch_lists.py` downloads EasyPrivacy and the Open Cookie Database at the git commits pinned in `data/lists.lock.json` to `~/.cache/qa-skills/privacy-lists` (not committed) and checks their hashes; `--check` verifies the cache without downloading, `--update` pins each list's latest commit (`--write-lock <file>` writes the new lock elsewhere instead of over the repo's). The capture blocks third-party requests (the shared harness default) and records them as attempts; the example's stub vendors are on loopback `*.test` hosts, so nothing in it is blocked.

Mobile app auditor (`examples/harbour-dental-app/` is the sample Kotlin and Compose app with planted barriers, a crash and an ANR; `examples/run/` is a real run on an Android 16 emulator, listed in `examples/run/commands.sh`, with the APK it tested, screenshots, logs and crash replays; the validator checks tools, checks, sample and settings against the run, every screenshot hash, and every candidate triaged):

```bash
python mobile-app-auditor-skill/scripts/validate_mobile_audit.py mobile-app-auditor-skill/examples/mobile_audit.example.json --run mobile-app-auditor-skill/examples/run
python mobile-app-auditor-skill/scripts/render_report.py mobile-app-auditor-skill/examples/mobile_audit.example.json [--brief]
python mobile-app-auditor-skill/scripts/check_tools.py --out tools.json
python mobile-app-auditor-skill/scripts/merge_findings.py --run mobile-app-auditor-skill/examples/run --out candidates.json
```

Re-running needs the Android SDK platform tools and one emulator; run `commands.sh` from the repo root. Exploration, probes and replay change device settings (rotation, font scale, display size, dark mode) and restore them; each script records what it changed and what it put back. A rerun changes screenshots and hashes and invalidates the audit until it is rebuilt. The APK is built from `examples/harbour-dental-app/` (`gradle assembleRelease`, JDK 17, SDK 36); rebuilding changes its hash. `data/wcag-sc.json` and `data/en301549-map.json` are shared with the WCAG auditor (edit them in `shared/data/`).

Release readiness gate (`examples/rc-2026.11.2/` is a release candidate whose evidence is other skills' example reports, listed in place by `reports.json` with provenance in `provenance/`, plus a real CI JUnit run; `make_repo.py` builds its sample git history with fixed ids; `--reports` re-reads every source and compares copied ratings, `--recompute` re-applies the policy to the collection):

```bash
python release-readiness-gate-skill/scripts/validate_gate.py release-readiness-gate-skill/examples/rc-2026.11.2/gate_report.json --reports --recompute release-readiness-gate-skill/examples/rc-2026.11.2/collection.json
python release-readiness-gate-skill/scripts/render_report.py release-readiness-gate-skill/examples/rc-2026.11.2/gate_report.json [--brief | --markdown gate.md]
python release-readiness-gate-skill/scripts/adapters.py <any skill's report>
```

Run these from the repo root: the collection and the report record paths relative to it. Hashes are taken with line endings normalised, so a CRLF checkout matches. The gate hashes the security, WCAG, crash, journey-tests and impact example reports in place, so editing any of them (or `examples/rc-2026.11.2/`) invalidates the gate example until `commands.sh` is rerun (it needs `HS`, an empty directory for the sample repository). The policy is the starter policy with `validator_args`; each source is checked with its own skill's validator.

CI failure triager (`examples/make_repo.py` builds the sample package "tidewater" with a fixed history; `examples/recorded/` holds a failed GitHub Actions run with its re-run attempt and an Azure DevOps build of the same commit, made from real local pytest and pip runs by `examples/record_example_run.py`; `examples/run/` is the normalised run, listed in `examples/commands.sh`; the validator re-extracts the failures and re-applies the classification rules from the run):

```bash
python ci-failure-triager-skill/scripts/validate_triage.py ci-failure-triager-skill/examples/triage.example.json --run ci-failure-triager-skill/examples/run
python ci-failure-triager-skill/scripts/render_report.py ci-failure-triager-skill/examples/triage.example.json [--brief | --defects-md drafts.md]
python ci-failure-triager-skill/scripts/fetch_run.py --system github --from-dir ci-failure-triager-skill/examples/recorded/github --out run/
python ci-failure-triager-skill/scripts/classify.py --run ci-failure-triager-skill/examples/run --out candidates.json
```

The triage quotes `examples/run/logs/` by line and names commits of the sample history, so editing `make_repo.py` or the recordings invalidates it until `commands.sh` is rerun from `ci-failure-triager-skill/` (it needs `TW`, an empty directory for the sample repository). `record_example_run.py` makes new real runs, which change the flaky test's picks and the line numbers. Live fetches need `GITHUB_TOKEN`/`GH_TOKEN` or `AZURE_DEVOPS_EXT_PAT`; `rerun.py` starts CI jobs and refuses without `--authorised-by`.

Test case designer (`examples/SHOP-310.issue.json` is a loyalty-points story whose five criteria need five techniques; `examples/model.json` is the agent's model per criterion; `examples/run/` holds the normalised ticket, the analysis and the generated rows, listed in `examples/commands.sh`; the validator regenerates the rows from the model and checks every row is covered, the model answers the text, and the exports equal the cases):

```bash
python test-case-designer-skill/scripts/validate_cases.py test-case-designer-skill/examples/test_cases.example.json --ticket test-case-designer-skill/examples/run/ticket.json --model test-case-designer-skill/examples/model.json --rows test-case-designer-skill/examples/run/rows.json --xray test-case-designer-skill/examples/xray.json --csv test-case-designer-skill/examples/cases.csv
python test-case-designer-skill/scripts/render_report.py test-case-designer-skill/examples/test_cases.example.json [--brief | --markdown cases.md]
python test-case-designer-skill/scripts/analyse_criteria.py test-case-designer-skill/examples/run/ticket.json --out analysis.json
python test-case-designer-skill/scripts/design_rows.py test-case-designer-skill/examples/model.json --out rows.json
```

The cases fingerprint the normalised ticket and cite row ids generated from `examples/model.json`, so editing the issue, the model or the generators invalidates the example until `commands.sh` is rerun from `test-case-designer-skill/` and the cases are rebuilt. Exports are files for a person to import; nothing is written to a tracker.

Post-deploy validator (`examples/harbour-tickets/server.py` is a small ticket site and API standing in for production on loopback, in releases 1.8.0 and 1.9.0 with planted problems; `examples/runs/` holds a real baseline run of 1.8.0 and a real run of 1.9.0 against it, listed in `examples/commands.sh`; the validator checks the run was read-only, recomputes timings, comparisons, findings and the recommendation, and scans for secrets):

```bash
python post-deploy-validator-skill/scripts/validate_pvt.py post-deploy-validator-skill/examples/pvt_report.example.json --run post-deploy-validator-skill/examples/runs/release-1.9.0/run.json --baseline post-deploy-validator-skill/examples/runs/baseline-1.8.0/run.json --plan post-deploy-validator-skill/examples/pvt_plan.json
python post-deploy-validator-skill/scripts/render_report.py post-deploy-validator-skill/examples/pvt_report.example.json [--brief]
python post-deploy-validator-skill/scripts/recommend.py post-deploy-validator-skill/examples/runs/release-1.9.0/run.json --out report.json
```

Run `commands.sh` from `post-deploy-validator-skill/`: the runs record the plan path relative to it. It starts the sample server itself on port 8160 and sets the sample's fake `smoke-reader` credentials. A rerun changes every timing, so the report is rebuilt; the findings and the recommendation stay the same. The runner refuses anything but GET, HEAD and the named sign-in, and a production target needs `authorised_by` in the plan.

Hypercare anomaly watcher (`examples/hypercare.json` watches a release of "harbour-shop" with four Datadog queries and a Sentry project; `examples/recorded/` is simulated monitoring data in the APIs' shapes, made by `examples/simulate.py` with a fixed seed; `examples/state/` is the watch after seven hourly ticks, listed in `examples/commands.sh`; the validator recomputes every tick from its stored inputs, replays the states and checks the drafts, and `--recorded` re-derives the inputs):

```bash
python hypercare-anomaly-watcher-skill/scripts/validate_watch.py hypercare-anomaly-watcher-skill/examples/state --recorded hypercare-anomaly-watcher-skill/examples/recorded
python hypercare-anomaly-watcher-skill/scripts/render_report.py hypercare-anomaly-watcher-skill/examples/state [--brief]
python hypercare-anomaly-watcher-skill/scripts/watch.py tick --state <state> --now 2026-11-12T06:00:00Z --from-dir hypercare-anomaly-watcher-skill/examples/recorded
```

Run `commands.sh` from `hypercare-anomaly-watcher-skill/`: it regenerates the simulation and the ticks (the same files, since the seed is fixed); the agent's headline, limitations and suggested actions are then added again. Editing `simulate.py`, `hypercare.json` or `detect.py` changes the evaluations. Live watching needs `DD_API_KEY`, `DD_APP_KEY` and `SENTRY_AUTH_TOKEN`; the skill only reads, and never installs the schedule.

Requirements traceability auditor (`examples/make_repo.py` builds the sample package "harbour-loyalty" whose commits name their tickets; `examples/tickets/` holds four Jira issues and `examples/requirements.md` two document requirements; `examples/run/` holds the normalised inputs, a real pytest JUnit run and the evidence, listed in `examples/commands.sh`; the validator rebuilds the matrix from the evidence and the triage, and `--repo` re-reads every file line and commit):

```bash
python requirements-traceability-auditor-skill/scripts/validate_trace.py requirements-traceability-auditor-skill/examples/trace.example.json --requirements requirements-traceability-auditor-skill/examples/run/requirements.json --evidence requirements-traceability-auditor-skill/examples/run/evidence.json --junit requirements-traceability-auditor-skill/examples/run/junit.xml
python requirements-traceability-auditor-skill/scripts/render_report.py requirements-traceability-auditor-skill/examples/trace.example.json [--brief | --csv matrix.csv]
python requirements-traceability-auditor-skill/scripts/build_matrix.py --requirements requirements-traceability-auditor-skill/examples/run/requirements.json --evidence requirements-traceability-auditor-skill/examples/run/evidence.json --triage requirements-traceability-auditor-skill/examples/triage.json --out matrix.json
```

Run `commands.sh` from `requirements-traceability-auditor-skill/` with `HL` set to an empty directory for the sample repository (fixed commit ids). The report cites lines of that repository and its commits, so editing `make_repo.py`, the tickets, the document or the export invalidates the example until `commands.sh` is rerun and the report rebuilt.

CI suite runner (`examples/suites.json` catalogues a GitHub workflow and an Azure DevOps pipeline; `examples/preauth.json` grants staging and UAT runs; `examples/ci-stub/server.py` stands in for both CI APIs on loopback and runs the requested tests for real with pytest; `examples/runs/` holds two real runs against it and the stub's request log, listed in `examples/commands.sh`; the validator checks authority, the catalogue, the polling and the JUnit totals, and `--stub-log` that the CI received exactly the triggers sent):

```bash
cd ci-suite-runner-skill   # the records name the catalogue relative to the skill directory
python scripts/validate_suite_run.py examples/runs/checkout-staging/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
python scripts/validate_suite_run.py examples/runs/smoke-production/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
python scripts/render_report.py examples/runs/checkout-staging/suite_run.json examples/runs/smoke-production/suite_run.json
```

Run `commands.sh` from `ci-suite-runner-skill/`: the records name the catalogue relative to it. It starts the stub on port 8170 with fake tokens; the agent's headline, next steps and limitations are added afterwards, then the records are validated. `run_suite.py trigger` starts real CI jobs when pointed at GitHub or Azure DevOps, and refuses without a covering grant or `--confirmed-by`; production needs the confirmation.

QA orchestrator (`data/orchestrator.json` is the default seven gates; `examples/orchestrator.example.json` is a six-gate example configuration; `examples/state/` is a real run made by `examples/commands.sh`, in which other skills' example reports stand in for one change's evidence; the validator checks gate order, decisions, report hashes and criteria, and `--rerun` runs each step's validator again):

```bash
python qa-orchestrator-skill/scripts/validate_run.py qa-orchestrator-skill/examples/state [--rerun]
python qa-orchestrator-skill/scripts/render_report.py qa-orchestrator-skill/examples/state [--brief | --markdown summary.md]
python qa-orchestrator-skill/scripts/orchestrate.py status --state qa-orchestrator-skill/examples/state
```

Run these and `commands.sh` from the repo root: the run records report paths and validator commands relative to it. The run hashes the readiness, test case, CI suite, triage and release gate example reports, so editing any of them makes the validator report the change until `commands.sh` is rerun. A real run keeps its state in a temporary folder; the example uses `--state-dir` so it can be committed.

Shared files:

```bash
python tools/check_shared.py        # fail if any skill's copy of a shared file has drifted
python tools/check_shared.py --fix  # copy canonical files from shared/ into skills
node shared/scripts/clock_shift.mjs --self-test
python shared/scripts/clock_shift.py --self-test
python shared/scripts/net_guard.py --self-test
node shared/scripts/net_guard.cjs --self-test
python <skill>/scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>
```

`capture-session` opens a visible browser for a person to sign in by hand and saves the session (cookies, localStorage, IndexedDB and sessionStorage); a browser skill's config then takes it as `auth: {"storage_state": file}` and records only that a session was used. `diff_scope.py --last-merges N` scopes the last N merges (or squash-merged commits). `run_tests.py` takes `--hash-path <dir>` (a sibling package added to `code_hash`) and `--record-env NAME`, and marks a run that did not run the tests `errored: true` with an `error`; validators never count an errored run as a pass.

## Shared files

Files used by more than one skill (the Journey Markdown spec `JOURNEY_FORMAT.md`, its front-matter schema, `journey_md.py`, `detect_conventions.py`, `parse_results.py`, `sarif.py`, `fetch_ticket.py`, `run_tests.py`, `stats.py`, `diff_scope.py`, `unified_diff.py`, `browser_harness.py` (read-only, third parties blocked and CSP bypassed by default, with request counts, Retry-After handling and captured sign-in sessions), `png_io.py`, `net_guard.py` and `net_guard.cjs` (network guards for test runs that log a `loaded` line), `py_routes.py` (the ast-based Python route reader for FastAPI, Flask and Azure Functions), `clock_shift.py` and `clock_shift.mjs` (the flaky test hunter keeps its copies in `plugins/`), the WCAG data files `data/wcag-sc.json` and `data/en301549-map.json`, and `data/ambiguity-lexicon.json`) have their canonical copy in `shared/`. Each skill carries an identical copy because skills are installed by copying a single directory. Edit only the copy in `shared/`, then run `python tools/check_shared.py --fix`; the mapping of shared files to skills lives in `tools/check_shared.py`.

Design documents for each skill (with an "As built" section), and the agreed build order, are in `docs/design/`.

## Skill anatomy

Each skill directory follows the same contract, and new skills should too:

- `SKILL.md` — the complete agent instructions, with YAML frontmatter (`name`, `description`, `version`). This is the file agents load; it must be self-contained and vendor-neutral (no model-specific tool syntax).
- `RUBRIC.md` — detailed scoring anchors referenced by SKILL.md.
- `schemas/*.schema.json` — the machine-readable output contract the skill's JSON output must conform to.
- `scripts/` — stdlib-only Python helpers (rendering, validation) so the skill works in any environment without installs.
- `examples/` — a sample output conforming to the schema.
- `README.md` — installation paths per agent platform and usage instructions.

These pieces are coupled: changing scored dimensions, caps, or output fields in `SKILL.md` requires matching updates in `RUBRIC.md`, the JSON schema, the validator, the renderer, and the example.

## Design principles baked into the skills

The test-suite-evaluator skill encodes strict honesty rules that any edits must preserve: unknown evidence is `not_assessed`/`unknown` rather than zero, test-derived inference is never called "code coverage", every score requires evidence and confidence ratings, and critical weaknesses cap the overall score. Do not soften this language when editing.
