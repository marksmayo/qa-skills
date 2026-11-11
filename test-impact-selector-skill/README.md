# Test Impact Selector Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

Give it a change and it picks the tests to run:

    6 of 11 test files selected for the late-sailing discount in pricing.py, with no blind spots.
    Replay of 12 past changes: 11 of 12 failures selected (91.7%, 95% CI 64.6%-98.5%).

Every selected test comes with its reasons. For example, "test_family_booking_is_created ran base
line(s) 20", an import chain, a journey link, or "failed in 2 of 2 past changes to this file".

Every change that evidence can't bound is listed as a blind spot, with what was done about it:
- a `conftest.py` widens to every test under it;
- a lockfile widens to the full suite;
- a plugin loaded by name widens to its package;
- a stylesheet widens to the e2e and visual tests that render it.

It is strict about what a selection shows:

- Unselected tests are "not selected by this evidence", never "safe to skip".
- Strict mode is the default: every blind spot widens the selection.
- Recall is measured by replaying past changes, with counts and an interval, or it is
  `not_assessed`.
- A stale coverage map is labelled stale.
- Every report says when the full suite still runs.
- It never edits CI configuration. The CI snippet is written somewhere else for you to add.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/test-impact-selector/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/test-impact-selector/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/test-impact-selector/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version.

**Requirements:**
- The scripts need Python 3.9+ and nothing else. Diffs are applied by the script itself, and git
  is needed only for git mode.
- Optional evidence comes from the repo's own tools:
  - coverage.py with pytest-cov for per-test coverage;
  - Jest, Nx or Bazel for related tests;
  - journey-mapper output;
  - past JUnit results.

## Usage

> Which tests do I need to run for my branch?

or

> Pick the tests for change.diff, replay our last month of CI runs, and give me a GitHub Actions step.

```bash
python scripts/import_graph.py <repo> --reach src/pricing.py        # who reaches this file, and how
python scripts/coverage_map.py normalise coverage.json --repo <repo> --package api --out run/coverage_map.json
python scripts/select_tests.py --repo <repo> --base main --coverage run/coverage_map.json \
    --journeys journey_map.json --history history.json --out run/selection.json
python scripts/replay.py --repo <repo> --history history.json --coverage run/coverage_map.json --out run/replay.json
python scripts/build_command.py run/selection.json [--ci github --ci-out run/ci_snippet.yml]
python scripts/validate_impact_report.py run/impact_report.json --selection run/selection.json \
    --replay run/replay.json --recompute --write-summary
python scripts/render_report.py run/impact_report.json [--brief]
```

| Mode | Blind spots |
|---|---|
| **strict** (default) | Every blind spot widens the selection |
| balanced | Only config, fixtures, dynamic loading, deletions and huge changes widen; needs a fresh coverage map and history |
| aggressive | Nothing widens; every blind spot is labelled |

## Output

`impact_report.json`, described by `schemas/impact_report.schema.json`:

| Section | Content |
|---|---|
| `sources` | Each evidence source: ran, not run, stale or not applicable, with the reason |
| `changes` | Each changed file and its kind |
| `selected` | Test files in run order, with strength and reasons |
| `blind_spots` | Kind, action, the tests it widened to |
| `command` | Commands per package and framework |
| `replay` | Recall with its interval, and every miss with why and the fix |
| `verify` | Failures inside and outside the selection from a real run |
| `summary` | Headline, counts, evidence completeness, recall status |

## Example

`examples/harbourline/` is a small monorepo: a Python API with pytest and a TypeScript web
package with Vitest. `examples/changes/` holds five changes:
- a pricing edit;
- a conftest fixture;
- an importlib plugin;
- a lockfile;
- a stylesheet.

`examples/history/` holds 12 earlier changes with real full-run JUnit. `examples/run/commands.sh`
lists every step, and `examples/README.md` has the answer key, including the one miss that
replay finds.

```bash
python scripts/validate_impact_report.py examples/impact_report.example.json --selection examples/run/C1/selection.json \
    --replay examples/run/replay.json --runs examples/run/C1/verify/runs.json
python scripts/render_report.py examples/impact_report.example.json --brief
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: strengths, blind-spot scopes, modes, completeness, recall, honesty rules
- `data/file_kinds.json`: the globs that classify lockfiles, CI, config, fixtures, schemas, assets, docs and test levels
- `schemas/impact_report.schema.json`: output contract
- `scripts/import_graph.py`: file-level import graph for Python and JS/TS, conftest fixtures, dynamic loading sites
- `scripts/coverage_map.py`: per-test coverage (coverage.py contexts, the mutation tester's map) to line -> tests
- `scripts/select_tests.py`: the selection, blind spots and commands
- `scripts/replay.py`: recall from past changes
- `scripts/build_command.py`: commands per framework, splitting, CI snippets
- `scripts/validate_impact_report.py`: checks the report against the evidence and the rules
- `scripts/render_report.py`: coloured terminal/Markdown report
- `scripts/diff_scope.py`, `unified_diff.py`, `run_tests.py`, `parse_results.py`, `detect_conventions.py`, `stats.py`: shared helpers
- `examples/`: sample monorepo, changes, history, real run and reports

## Design principles

1. Every selected test has a reason you can check.
2. What can't be reasoned about is listed, never dropped.
3. Recall is measured from history, or not claimed.
4. The full suite still runs somewhere.
5. Selection is advice: no CI edits, no commits.
