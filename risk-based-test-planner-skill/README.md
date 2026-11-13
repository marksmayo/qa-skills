# Risk-Based Test Planner Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It writes a test plan for a release, sprint or change, with effort spread by risk:
likelihood × impact for every area of the code.

- **Likelihood** comes from the code's history: churn, fix commits and Jira bugs, plus
  complexity, the change in scope and how weak the area's tests are.
- **Impact** comes from the journeys an area serves and what it does: moves money, controls
  access, exposes personal data, handles secrets or credentials, deletes data, or is a
  library's public API or a CLI command.
- **Releases:** in a changesets (or lerna, or semantic-release) monorepo it finds the last
  release tag, reads the pending changesets' bumps, and flags areas carrying a breaking change.

The plan reads the other skills' reports, allocates 90-minute sessions, and names the skill or
test type that covers each risk. It also lists **what will not be tested, and why**, so the
leftover risk is accepted by a person on purpose.

It is strict about evidence:

- Every score cites its evidence: the git command and counts, fix commits by id, the journey and
  its priority, the line a surface pattern matched.
- Missing evidence is `not_assessed`, never low risk. New code with no history is flagged
  `novel`, which raises it.
- An open high finding from another skill makes an area High, and its severity and confidence
  are carried as reported (crash `ranking.priority`, WCAG severity, CVSS severity). One its own
  report only assumes makes the area at least Medium (`unconfirmed_findings`), not High.
- A partial journey map leaves the areas it didn't map `not_assessed`, not low impact; with no
  journey map at all, the plan must say that UI impact rests on surface patterns alone.
- Squash-merged pull requests ("Merged PR 123: fix(x): ...") count as fixes by their own type,
  and Python tests count through the import roots `sys.path` would have (src layouts, service
  directories, pytest `pythonpath`).
- Every area is either planned or in `not_tested`, with the reason and the residual risk.
- The plan never says a release is safe. Exit criteria are conditions to check.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/risk-based-test-planner/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/risk-based-test-planner/`,
or include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/risk-based-test-planner/`,
and reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and git.
Jira is optional, and read-only.

## Usage

> Plan testing for the 1.5 release: we have 16 sessions.

```bash
python scripts/mine_history.py --repo <repo> --out run/history.json
python scripts/complexity.py <repo> --out run/complexity.json
python scripts/release_scope.py --repo <repo> --out run/release.json        # prints the release base, e.g. v1.4
python scripts/diff_scope.py --repo <repo> --base v1.4 --committed-only --out run/scope.json
python scripts/score_risk.py --repo <repo> --history run/history.json --complexity run/complexity.json --scope run/scope.json \
    --release run/release.json --journeys journey_map.json --input security=security_audit.json --out run/risk.json
python scripts/validate_test_plan.py run/test_plan.json --risk run/risk.json --write-summary
python scripts/render_report.py run/test_plan.json --markdown run/test_plan.md
python scripts/score_risk.py --self-test
python scripts/release_scope.py --self-test
```

On Windows, `complexity.py` over a fresh clone can take a minute or more the first time; that is
the antivirus scanning each file as it is opened, and a second run is fast.

| Band | Risk = likelihood × impact | Treatment |
|---|---|---|
| **High** | ≥ 15, or any open critical/high finding | Two or more test types, exploratory charters, a session floor |
| **Medium** | 8 to 14.9, or new code, or a breaking change | A test type plus regression (regression or contract for a breaking change) |
| Low | < 8 | Smoke or existing automation, or not tested with the reason |

## Example

`examples/make_sample_repo.py` builds "larder", a small shop, as a git repository with 42
deterministic, back-dated commits. The example can't ship a `.git` directory.

`examples/run/` holds the mined history, the complexity, the scope and the scores.
`examples/inputs/` holds a trimmed journey map and security audit. `examples/README.md` has the
answer key.

There are two plans:
- `test_plan.example.json` with 16 sessions;
- `test_plan.one-session.example.json`, where the budget is short.

Each has a Markdown version.

```bash
python scripts/validate_test_plan.py examples/test_plan.example.json --risk examples/run/risk.json \
    --input security=examples/inputs/security_audit.json
python scripts/render_report.py examples/test_plan.example.json --brief
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: factors, percentiles, bands, floors, strategy, effort, honesty rules
- `data/surfaces.json`: call-aware patterns for money, access, personal data, secrets and credentials, destructive, compliance, entry-point, public API/CLI, UI page and external surfaces, run on code with comments and strings blanked (SQL in strings, directives and page paths matched as written)
- `schemas/test_plan.schema.json`: output contract
- `scripts/mine_history.py`: churn, authors, fix commits and formatting-only commits from `git log --numstat`
- `scripts/complexity.py`: lines of code and branches per file
- `scripts/release_scope.py`: the release base tag, pending changesets and breaking commits (`--self-test` builds a synthetic workspace)
- `scripts/score_risk.py`: areas (packages, Next.js App Router routes and modules, or source subdirectories; uncovered files as `unassigned`, everything else outside the areas listed in `excluded`), the eight factors with evidence, risk, bands, floors; the input adapters; the effort allocation; `--self-test` for the surface idioms
- `scripts/validate_test_plan.py`: checks the plan against the scores, the inputs and the rules
- `scripts/render_report.py`: terminal report, or the Markdown plan
- `scripts/diff_scope.py`, `fetch_ticket.py`, `detect_conventions.py`: shared helpers
- `examples/`: sample repository builder, inputs, real run and two plans

## Design principles

1. Spend testing where breakage is likely and costly.
2. Every number has its evidence; no evidence is "not assessed", not "low".
3. What isn't tested is written down, with who accepts it.
4. Other skills' findings count at their own confidence.
5. A plan is a set of conditions, never a promise.
