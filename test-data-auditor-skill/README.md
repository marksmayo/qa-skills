# Test Data Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It audits the data tests depend on (fixtures, factories, seeds, snapshots, HTTP recordings and
inline literals) and finds:

- **time bombs**, such as a membership that expires on a fixed date the code compares with
  today, along with the date the test will start failing;
- **real-looking personal data** that shouldn't be in a repository, including customer extracts
  (a name column across many rows of a CSV, HTML table or workbook);
- **magic ids** borrowed from a seed dump;
- **production hosts and local paths**;
- **shared fixtures** that tests mutate, and module reloads that leak state between tests;
- **unseeded randomness**;
- **fixtures that have drifted** from the model;
- **unused snapshots and files**;
- **literals that depend on the machine locale**;
- **files a test run leaves behind** in the working tree.

The rules cover Python and JavaScript/TypeScript suites: JS and TS are tokenized, so values in
object literals keep their field names.

It then confirms what it can with real runs: a moved clock, reversed order, another locale.

It is strict about what it shows:

- Personal data is never echoed. A finding gives file, line, field and type, and the validator
  re-reads every flagged location to check that no value leaked into the report or the
  evidence.
- Real-looking personal data is treated as real, and is always P0.
- A time bomb is confirmed only by a failing run with a moved clock, or by exact date
  arithmetic.
- No "clean test data" claims. The report says what was scanned and what couldn't be.
- Secrets are handed to the code security auditor (obvious placeholders are dismissed by rule),
  and shared state to the flaky test hunter.
- A problem no rule produced, found by reading or by an experiment, is reported with
  `source: agent` and a basis the validator re-reads.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/test-data-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/test-data-auditor/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/test-data-auditor/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and the
repository's own test runner. Clock experiments use the repo's own clock library, libfaketime,
the included `clock_shift.py` pytest plugin, or `clock_shift.mjs` for Vitest and Jest. Test runs
use the included network guards (`scripts/net_guard.py` for pytest, `scripts/net_guard.cjs` for
Node), which refuse and log every connection that is not loopback.

## Usage

> Audit our test fixtures for personal data and anything that will break by itself.

```bash
python scripts/inventory.py <repo> --out run/inventory.json [--data-dir <dir>]
python scripts/scan_data.py <repo> --inventory run/inventory.json --out run/candidates.json
python scripts/run_tests.py --preset pytest --cwd <copy> --out run/runs.json --label baseline --run-prefix B --repeat 2
python scripts/run_tests.py --preset custom --cmd "python -m pytest -q -p no:cacheprovider -p clock_shift --junitxml={junit}" \
    --cwd <copy> --env TDA_CLOCK_SHIFT=+400d --out run/runs.json --label "clock +400d" --run-prefix S
python scripts/redact_evidence.py run --candidates run/candidates.json --repo <repo> --report run/test_data_report.json
python scripts/validate_test_data_report.py run/test_data_report.json --candidates run/candidates.json --repo <repo> \
    --runs run/runs.json --evidence run --write-summary
python scripts/render_report.py run/test_data_report.json [--brief]
```

| Priority | Score = severity × reach |
|---|---|
| **P0** | ≥ 9, and always for real-looking personal data |
| **P1** | ≥ 6 |
| P2 | ≥ 3 |
| P3 | < 3 |

## Example

`examples/clinic-api/` is a small clinic booking package with a pytest suite and JSON, YAML,
CSV, SQL and HTML-as-.xls fixtures. Every personal detail in it is invented for the example: none of it
belongs to a real person, and the "real-looking" values were made up to trip the rules, so
please don't treat them as anyone's data. `examples/run/` holds the real run (`commands.sh`
lists the steps), and `examples/README.md` has the answer key.

```bash
python scripts/validate_test_data_report.py examples/test_data_report.example.json --candidates examples/run/candidates.json \
    --repo examples/clinic-api --runs examples/run/runs.json --evidence examples/run
python scripts/render_report.py examples/test_data_report.example.json --brief
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: triage, severity, priority, confidence, the never-echo rule
- `data/rules.json`: field names, patterns and thresholds for the rules
- `data/reserved.json`: reserved email domains, fiction phone ranges, test cards, specimen IDs, reserved hosts
- `schemas/test_data_report.schema.json`: output contract
- `scripts/inventory.py`: data sources and the tests that load them
- `scripts/datafiles.py`: JSON, YAML-subset, CSV, SQL, HTML-table and .xlsx records with line numbers (`--text` prints a workbook's text form)
- `scripts/jsparse.py`: JavaScript/TypeScript tokenizer and object-literal records with field names
- `scripts/scan_data.py`: the static rules, written as candidates without values
- `scripts/schema_drift.py`: fixtures against dataclasses, pydantic, SQLAlchemy, JSON Schema and OpenAPI
- `scripts/clock_shift.py`: pytest plugin that moves the clock for time-bomb runs
- `scripts/clock_shift.mjs`: the same for Node runners (Vitest, Jest), loaded with `NODE_OPTIONS=--import` or as a setup file
- `scripts/redact_evidence.py`: removes flagged values from runner output before validation
- `scripts/residue.py`: snapshot and diff of the working tree (and, with `--also home=<dir>`, an isolated home folder), for files a run leaves behind
- `scripts/self_test.py`: synthetic checks of the Go rules, URL credentials, card shapes, language coverage, `--also` and the live-endpoint severity
- `scripts/validate_test_data_report.py`: checks the report against the candidates, the runs and the never-echo rule
- `scripts/render_report.py`: coloured terminal/Markdown report
- `scripts/run_tests.py`, `parse_results.py`, `detect_conventions.py`: shared helpers
- `examples/`: sample package, real run and report

## Design principles

1. Find data that breaks by itself before it breaks.
2. Never repeat the personal data you found.
3. Confirm with a run where a run can show it.
4. Say what was scanned; never call data clean.
5. Hand secrets and flakes to the skills that own them.
