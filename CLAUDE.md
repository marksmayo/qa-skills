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

# Validate an evaluation JSON against the required structure
python test-suite-evaluator-skill/scripts/validate_evaluation.py evaluation.json
```

`examples/evaluation.example.json` is a valid sample input for both scripts.

Journey mapper (run from the repo root; `examples/journeys/` is a complete sample):

```bash
python journey-mapper-skill/scripts/extract_surface.py <repo> --out surface.json
python journey-mapper-skill/scripts/validate_journey_map.py journey-mapper-skill/examples/journeys/journey_map.json
python journey-mapper-skill/scripts/write_journeys.py journey-mapper-skill/examples/journeys/journey_map.json --out journey-mapper-skill/examples/journeys
python journey-mapper-skill/scripts/journey_md.py lint journey-mapper-skill/examples/journeys --strict
python journey-mapper-skill/scripts/render_report.py journey-mapper-skill/examples/journeys/journey_map.json
```

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
```

The example report quotes source lines of `examples/pantry/src/` by file and line, and its triage covers exactly the candidates in `examples/run/candidates.json`; editing either invalidates the example. `probe_web.py` and `strict_overlay.py` need the app built (`npm install && npm run build` in a copy of `examples/pantry/` outside the repo) and are not needed to validate the example.

Code security auditor (`examples/invoicer/` is the sample, deliberately vulnerable Flask app; the scanner outputs in `examples/run/`, the PoC evidence in `examples/evidence/` and the regression runs in `examples/regression/` are real; `--candidates` checks every candidate and dependency advisory was triaged):

```bash
python code-security-auditor-skill/scripts/validate_security_audit.py code-security-auditor-skill/examples/security_audit.example.json --repo code-security-auditor-skill/examples/invoicer --candidates code-security-auditor-skill/examples/run/candidates.json
python code-security-auditor-skill/scripts/render_report.py code-security-auditor-skill/examples/security_audit.example.json [--brief] [--sarif out.sarif]
python code-security-auditor-skill/scripts/scan_pitfalls.py code-security-auditor-skill/examples/invoicer
python code-security-auditor-skill/scripts/recon.py code-security-auditor-skill/examples/invoicer
python code-security-auditor-skill/scripts/run_poc.py code-security-auditor-skill/examples/pocs/sqli-search.json --check
python code-security-auditor-skill/scripts/cvss4.py "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N"
```

The example report quotes source lines of `examples/invoicer/` by file and line, re-reads the planted secret to check its value never appears in the report or evidence, and its triage covers exactly the candidates in `examples/run/candidates.json`; editing either invalidates the example. Re-running the PoCs needs the app running locally (`pip install -r requirements.txt && flask --app invoicer run --port 5057` in a copy outside the repo); the scanners themselves are optional and only `run_scanners.py` uses them. `scripts/cvss4.py` is a port of the FIRST CVSS v4.0 calculator (BSD-2-Clause); keep its licence header.

WCAG auditor (`examples/harbour-dental/site/` is the sample site with planted barriers; `examples/run/` is a real run against it, including screenshots and accessibility-tree snapshots; `--run` checks tool statuses and the check matrix against `run.json`, `--candidates` checks every candidate was triaged):

```bash
python wcag-auditor-skill/scripts/validate_audit.py wcag-auditor-skill/examples/audit.example.json --run wcag-auditor-skill/examples/run --candidates wcag-auditor-skill/examples/run/candidates.json
python wcag-auditor-skill/scripts/render_report.py wcag-auditor-skill/examples/audit.example.json [--brief]
python wcag-auditor-skill/scripts/normalize.py wcag-auditor-skill/examples/run --out items.json
python wcag-auditor-skill/scripts/merge_findings.py items.json --run wcag-auditor-skill/examples/run --out candidates.json
python wcag-auditor-skill/scripts/fetch_engines.py --check
```

The example report's triage covers exactly the candidates in `examples/run/candidates.json`, and its findings cite screenshots in `examples/run/screenshots/`; regenerating the run (or editing `data/rule-map.json` or the merge logic) changes the candidates and invalidates the example. Re-running the audit needs Playwright (`pip install -r wcag-auditor-skill/requirements.txt && python -m playwright install chromium`), the engines from `fetch_engines.py`, and the site served on loopback (`python -m http.server 8137 --bind 127.0.0.1 --directory wcag-auditor-skill/examples/harbour-dental/site`). `data/rule-map.json` is generated from the pinned engines' own rule metadata by `scripts/build_rule_map.py` (needs Playwright and the engines; `--check` compares without writing); regenerate it whenever `data/engines.lock.json` changes.

Shared files:

```bash
python tools/check_shared.py        # fail if any skill's copy of a shared file has drifted
python tools/check_shared.py --fix  # copy canonical files from shared/ into skills
```

## Shared files

Files used by more than one skill (the Journey Markdown spec `JOURNEY_FORMAT.md`, its front-matter schema, `journey_md.py`, `detect_conventions.py`, `parse_results.py` and `sarif.py`) have their canonical copy in `shared/`. Each skill carries an identical copy because skills are installed by copying a single directory. Edit only the copy in `shared/`, then run `python tools/check_shared.py --fix`; the mapping of shared files to skills lives in `tools/check_shared.py`.

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
