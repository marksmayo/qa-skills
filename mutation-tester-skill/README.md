# Mutation Tester Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It runs mutation testing on the code that changed and turns the surviving mutants into a short
list of weak spots in the tests: "test_total_for_a_family runs line 25 but still passes if `+`
becomes `-`". Each finding names the test that should have caught it and the assertion or input
that would, and the suggestion can be proved: the new test passes on the original code and fails
on the mutant.

It is strict about what a mutation run shows:

- A score always comes with its counts and scope: `18 of 29 mutants killed in 3 changed files
  (62.1%)`. A score for changed code is never presented as the codebase's.
- No "well tested". A high score says these mutants died, not that the tests are good.
- A mutant on a line no test runs is a coverage gap, not a weak assertion.
- An equivalent mutant needs an argument that both versions behave the same for every input.
- Timeouts and crashes are never counted as kills.
- The baseline must pass twice first, the tree is hashed before and after, and tests are only
  written to the repo when you ask.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/mutation-tester/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/mutation-tester/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/mutation-tester/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ (the builtin
engine mutates inside f-strings only on 3.12+) and the repository's own test runner. External
engines are optional: mutmut 3 for Python (Linux, macOS or WSL), StrykerJS, Stryker.NET, PIT,
cargo-mutants and the others listed in SKILL.md. Nothing is added to the repo's dependencies.

## Usage

> Run the mutation tester on my branch's changes.

or

> Mutation-test src/billing and tell me which tests are weak. Write the tests too.

```bash
python scripts/detect_tools.py <repo>
python scripts/diff_scope.py --repo <repo> --base main --out run/scope.json
#   or, for a path scope:  python scripts/path_scope.py --repo <repo> --path src/billing --out run/scope.json
python scripts/run_tests.py --preset pytest --cwd <repo> --out run/runs.json --repeat 2 --label baseline --run-prefix G
python scripts/builtin_mutator.py --repo <repo> --scope run/scope.json --run-dir run --tests tests/unit
python scripts/normalize_results.py --scope run/scope.json --repo <repo> --builtin run/builtin.json \
    --coverage run/coverage.json --out run/mutants.json
python scripts/prove_kill.py --repo <repo> --mutants run/mutants.json --mutant B24 --test-file tests/test_x.py \
    --test-code new_test.py --test-name test_new --finding F1 --run-dir run/proof --out run/proofs.json
python scripts/validate_mutation_report.py run/mutation_report.json --mutants run/mutants.json --runs run/runs.json \
    --proofs run/proofs.json --repo <repo> --write-summary
python scripts/render_report.py run/mutation_report.json [--brief]
```

| Priority | Score = criticality × kind × proximity |
|---|---|
| **P0** | ≥ 12 |
| **P1** | ≥ 8 |
| P2 | ≥ 4 |
| P3 | < 4 |

Criticality is 3 for money, auth, permissions, data deletion or a critical journey, 2 for ordinary
logic and 1 for display text. Kind is 3 for a branch or boundary (including a string compared in
a condition), 2 for a value (including an UPPER_CASE string constant) and 1 for other strings.
Proximity is 2 when a unit test of the function ran the line and still passed.

## Output

`mutation_report.json`, described by `schemas/mutation_report.schema.json`:

| Section | Content |
|---|---|
| `metadata` | Scope, engines (version, config source, counts, tree check), coverage, budget |
| `baseline` | The two green runs |
| `mutants` | Every mutant in scope, normalised across engines |
| `findings` | Weak assertions and missing cases (ranked), equivalent and accepted mutants (not ranked) |
| `coverage_gaps` | Lines no test runs, with a suggested test |
| `summary` | Per-engine counts and scores with their text, priorities, evidence completeness |

## Example

`examples/fare-calc/` is a small Python fare calculator and `change.diff` is the change under
review. `examples/run/` holds the real run: the scope, the baseline, both engines' raw results,
per-test coverage, the normalised mutants and four proofs. `examples/run/commands.sh` lists every
step, and `examples/README.md` has the answer key.

```bash
python scripts/validate_mutation_report.py examples/mutation_report.example.json --mutants examples/run/mutants.json \
    --runs examples/run/runs.json --proofs examples/run/proofs.json --repo examples/fare-calc
python scripts/render_report.py examples/mutation_report.example.json --brief
```

The example exercises the builtin engine and mutmut. StrykerJS (JavaScript, TypeScript) runs
through `run_engine.py stryker`, which mutates only the changed line ranges and records the
tree check; `normalize_results.py --stryker ... --stryker-run ...` turns Stryker's test ids into
test names and finds each mutant's function with the shared `diff_scope.py` scanner, and `prove_kill.py` proves Vitest
and Jest tests. These were tested on a real Next.js/Vitest repository; no example in this
directory uses them.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: statuses, scores, triage, ranking, proofs
- `data/operators.json`: common operators, their kind, and each engine's names for them
- `schemas/mutation_report.schema.json`: output contract
- `scripts/detect_tools.py`: configured and available mutation tools
- `scripts/diff_scope.py`: changed files, lines and functions from git or a diff (shared)
- `scripts/run_tests.py`, `scripts/parse_results.py`, `scripts/detect_conventions.py`: test runs and conventions (shared)
- `scripts/path_scope.py`: a path scope (every line of the files under a path) in `scope.json` form
- `scripts/builtin_mutator.py`: stdlib AST mutation engine and per-test line coverage for pytest, limited to `--tests`
- `scripts/run_engine.py`: runs mutmut in a scratch copy, or StrykerJS on the scope's line ranges, and keeps the raw results and the tree check
- `scripts/ts_scan.py`: finds JavaScript/TypeScript tests by name and whether they call a function (for proximity)
- `scripts/normalize_results.py`: one mutant list across engines, limited to the scope
- `scripts/prove_kill.py`: proves a suggested test passes on the original and fails on the mutants
- `scripts/validate_mutation_report.py`: checks the report against the evidence and the rules
- `scripts/render_report.py`: coloured terminal/Markdown report
- `examples/`: sample package, change, real run and report

## Design principles

1. Report the surviving mutants that matter, not a number.
2. Every number carries its counts and its scope.
3. Not covered is not the same as weakly tested.
4. Equivalence is argued, not asserted.
5. Leave the code exactly as it was found.
