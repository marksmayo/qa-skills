# Flaky Test Hunter Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It finds tests that pass and fail on the same code, proves what makes each one fail, and
proposes a fix. It reruns the suite many times under controlled variations: reversed and
shuffled order, each test alone, fixed hash seeds, parallel workers, other time zones, a moved
clock, other locales, and CPU load. For order-dependent failures it bisects to the test that pollutes the
state, or to the earlier test a test silently depends on (an enabler).

It is strict about what the runs show:

- Flaky means a pass and a fail on the same code. A test that fails every time is reported
  separately as a real failure.
- Every rate is failures/runs with a 95% Wilson interval: `2/5 (11.8%-76.9%)`.
- A test that never failed is "not observed flaky in 20 runs (upper bound 16.1%)", never "not
  flaky". The validator rejects "flake-free" and "the suite is stable".
- A cause is confirmed only by an experiment that turns the failure off and on, such as a seed
  that always fails against one that always passes, or a polluter pair.
- A retry is never the fix, and product code is never changed. Fixes are diffs to test code,
  proposed unless you ask for them to be applied.
- Every variation and tool is recorded as ran, not run or not applicable, with the reason.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/flaky-test-hunter/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/flaky-test-hunter/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/flaky-test-hunter/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and the
repository's own test runner. Parallel runs need the runner's parallel plugin (pytest-xdist for
pytest); without it the parallel variation is recorded as not run.

## Usage

> Find the flaky tests in this repo with the flaky test hunter. CI's pytest check is required.

or

> These three tests failed on CI last week and passed on rerun: find out why.

```bash
python scripts/scan_pitfalls.py <repo> --out smells.json
python scripts/run_tests.py --preset pytest --cwd <repo> --out runs.json --repeat 20 --label repeat
python scripts/run_tests.py --preset pytest --cwd <repo> --out runs.json --order shuffle --seed 11 --label order_shuffle
python scripts/run_tests.py --preset pytest --cwd <repo> --out runs.json --tests <ids> --label clock_back_1d \
    --env PYTHONPATH=<plugins/, relative to the repo> "PYTEST_ADDOPTS=-p clock_shift" TDA_CLOCK_SHIFT=-1d
python scripts/bisect_polluter.py --runs runs.json --from-run O-1 --victim <case id> --cwd <repo> --out bisect.json
python scripts/bisect_polluter.py --runs runs.json --from-run R-1 --victim <case id> --cwd <repo> --relation enabler --out bisect-enabler.json
python scripts/validate_flaky_report.py flaky_report.json --runs runs.json --smells smells.json --bisect bisect.json --repo <repo> --write-summary
python scripts/render_report.py flaky_report.json [--brief]
```

`run_tests.py` has presets for pytest, Jest, Vitest, Playwright and gotestsum, and a `custom`
preset with `{junit}` and `{tests}` placeholders for anything that writes JUnit XML. For Vitest
and Jest, pass the repo's own test command and environment (`--cmd`, `--env TZ=...`) when its
`npm test` script adds them; SKILL.md has notes on pools, isolation, shuffling and timeouts.

The worst rate that ranks a flake comes from a label with at least 3 runs, or from all runs
pooled, so one failure in a single run cannot rank like 3 failures in 3. `render_report.py --brief`
lists only the flaky and consistently failing tests.

A run that crashed, timed out or hit a pytest collection or usage error is recorded as `errored` and
never counted. In a monorepo, run `run_tests.py` with each package's own Python, keep one `code_hash`
(`--hash-path` adds sibling packages), and let it map pytest ids from an ini file above or below `--cwd`.
`runs.json` records environment variable names, not their values (except the harness's own, such as
the clock shift).

| Priority | Score = rate factor × gate × masking |
|---|---|
| **P0** | ≥ 18 |
| **P1** | ≥ 9; a product race is at least P1 |
| P2 | ≥ 4; suspected causes and CI-history-only flakes are at most P2 |
| P3 | < 4 |

Rate factor 1–4 comes from the Wilson lower bound of the worst observed rate. Gate is 3 when the
check blocks merging, 2 in CI, and 1 for local-only tests. Masking is 2 when the test guards a
critical journey or the flake is a product race. A flake on a non-required CI check that guards no critical
journey therefore tops out at 8 (P2) by design, even at 3/3.

## Output

`flaky_report.json`, described by `schemas/flaky_report.schema.json`:

| Section | Content |
|---|---|
| `tests` | Every test's runs, passes, fails, normal and worst rates with intervals, and status |
| `flakes` | Cause, confidence, the experiment, a passing and a failing run, polluter, fix and ranking |
| `consistently_failing` | Tests that failed every run |
| `static_smells` | Every smell the scanner found and what it led to |
| `variations`, `tools` | What ran and what didn't, with reasons |
| `summary` | Counts, causes, priorities, total runs and evidence completeness |

`runs.json` keeps every run: the command, exit code, duration, variation, code hash and parsed
JUnit cases.

## Example

`examples/ferry-booking/` is a small Python package with five planted flakes, one real bug and
one sleep that never fails. `examples/run/` holds the real pytest runs behind
`examples/flaky_report.example.json`, and `examples/run/commands.sh` lists them in order.

```bash
python scripts/validate_flaky_report.py examples/flaky_report.example.json --runs examples/run/runs.json \
    --smells examples/run/smells.json --bisect examples/run/bisect.json --repo examples/ferry-booking
python scripts/render_report.py examples/flaky_report.example.json --brief
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: status, rates, causes, confidence, ranking and fixes
- `data/pitfalls.json`: static flake smells by language, with cause and fix
- `schemas/flaky_report.schema.json`: output contract
- `scripts/run_tests.py`: runs tests under variations and records each run (shared)
- `scripts/stats.py`: Wilson intervals and rate bands (shared)
- `scripts/parse_results.py`, `scripts/detect_conventions.py`: JUnit parsing and convention detection (shared)
- `scripts/scan_pitfalls.py`: static smells
- `plugins/clock_shift.py`: a pytest plugin that moves the clock for the `clock` variation (the same file as test-data-auditor's; safe with pandas and numpy; `--self-test`)
- `plugins/clock_shift.mjs`: the same for Node (Vitest, Jest), loaded with `NODE_OPTIONS=--import=<file URL>` (shared)
- `scripts/bisect_polluter.py`: finds the test that pollutes an order-dependent victim, or (`--relation enabler`) the one it depends on
- `scripts/validate_flaky_report.py`: recomputes counts from the runs and checks the honesty rules
- `scripts/render_report.py`: coloured terminal/Markdown report
- `examples/`: sample package, real runs and report

## Design principles

1. Flaky is a fact about runs, not about code: a pass and a fail on the same code.
2. Show the counts and the uncertainty, always.
3. A cause is proven by turning the failure off and on.
4. Remove the cause; never retry it away.
5. Never change product code, and never claim a test can't fail.
