# Dead Test Finder Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It finds tests that give false assurance:

- **tests that never run**: the framework doesn't collect them;
- **tests that can't fail**: they pass with the code under test neutralised;
- **tests that only check their own mocks**;
- **un-awaited async assertions**;
- **swallowed failures**;
- **duplicated tests**;
- **skips whose reason has gone**.

Each finding comes with the evidence and a patch, and every strengthening patch is proved by a
run.

It is strict about what it shows:

- "Never runs" is confirmed by the framework's own collect output, not by file names.
- "Can't fail" is confirmed only by a run: the function under test is reduced to a neutral body
  in a scratch copy, and the test still passes. A separate raising body shows whether the test
  at least checks that nothing raises.
- A duplicate needs identical coverage and values; similar code alone is an overlap.
- A skip is stale only when its ticket is done, or when `git blame` says it is old; a guard skip
  (`if not path.exists(): pytest.skip(...)`) is conditional and never stale by age.
- It never deletes or merges tests. With `apply: true` it applies only strengthening fixes.
- No "no dead tests" claims.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/dead-test-finder/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/dead-test-finder/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/dead-test-finder/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and the
repository's own test runners (pytest, Vitest, Jest or Playwright for listing). Nothing is installed.

## Usage

> Find the dead tests in this repo and show me which ones matter.

```bash
python scripts/scan_tests.py <repo> --out run/candidates.json [--test-files "*.e2e.ts"]
python scripts/collect_check.py <repo> --candidates run/candidates.json --run-dir run [--env DATABASE_URL=<fake> ...] [--package-env api:PYTHONPATH=<dir>] [--scope candidates] [--reuse] [--split-on-error]
python scripts/neutralise.py --repo <repo> --candidates run/candidates.json --run-dir run --test <test id> [--test <id>[case] ...] [--tests-file ids.txt] [--copy-ignore "*.mp4"] [--env K=V ...] [--package-env <package>:K=V]
python scripts/neutralise.py --repo <repo> --candidates run/candidates.json --run-dir run --patch proposals/fix.diff --test <test id>
python scripts/similarity.py --repo <repo> --candidates run/candidates.json --run-dir run [--env K=V ...] [--package-env <package>:K=V]
python scripts/check_skips.py --repo <repo> --candidates run/candidates.json --tickets run/tickets.json --out run/skips.json
python scripts/validate_dead_test_report.py dead_test_report.json --repo <repo> --write-summary
python scripts/render_report.py dead_test_report.json [--brief]
python scripts/self_test.py            # scanner and collect checks on a synthetic repository
python scripts/tree_guard.py --self-test
```

| Priority | Score = weight × criticality |
|---|---|
| **P0** | ≥ 12, and always for a `.only` |
| **P1** | ≥ 8 |
| P2 | ≥ 4 |
| P3 | < 4 |

## Example

`examples/ledger/` is a small bookkeeping app: a Python core with pytest, and a TypeScript client
with Vitest. The report has eight findings, one of them found by review (`source: "agent"`, with a
`basis` the validator re-reads). `examples/proposals/` holds the seven patches, `examples/run/` the real run
(`commands.sh` lists every step), and `examples/README.md` has the answer key.

```bash
python scripts/validate_dead_test_report.py examples/dead_test_report.example.json --repo examples/ledger
python scripts/render_report.py examples/dead_test_report.example.json --brief
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: triage, confidence, ranking, proposals, honesty rules
- `data/rules.json`: patterns, collect defaults, skip markers, thresholds
- `schemas/dead_test_report.schema.json`: output contract
- `scripts/scan_tests.py`: test definitions and candidates (Python `ast`; JS/TS through a lexer for strings, templates, regex literals and comments, with Playwright idioms)
- `scripts/collect_check.py`: defined vs collected, from the frameworks' own list commands (pytest, Vitest, Jest, Playwright), optionally scoped, cached and split around a file that can't be listed
- `scripts/tree_guard.py`: keeps the target's test files byte for byte around every command the scripts run, puts them back and stops if one changed
- `scripts/self_test.py`: the scanner's head, assertion and cleanup rules and collect_check's guard on a synthetic repository
- `scripts/neutralise.py`: neutral and raising bodies in a scratch copy (JS targets through aliases, default imports and re-exports; byte-exact restores), and patch proofs
- `scripts/similarity.py`: per-test line coverage, outcome and hashed literal values for duplicate candidates
- `scripts/check_skips.py`: ticket status and `git blame` age for every skip
- `scripts/validate_dead_test_report.py`: checks the report against the evidence files
- `scripts/render_report.py`: coloured terminal/Markdown report
- `scripts/run_tests.py`, `parse_results.py`, `detect_conventions.py`, `fetch_ticket.py`, `unified_diff.py`, `diff_scope.py`: shared helpers
- `examples/`: sample app, ticket, proposals, real run and report

## Design principles

1. A test that can't fail is shown failing to fail, not argued about.
2. "Never runs" comes from the framework's own list.
3. Every proposal is a patch; every strengthening patch is proved.
4. Delete and merge are a person's decision.
5. Say what was scanned; never call a suite clean.
