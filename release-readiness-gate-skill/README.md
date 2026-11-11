# Release Readiness Gate Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It gathers the reports the other QA skills produced for one release candidate, plus CI JUnit results,
checks each is valid (with its own skill's validator) and was made for this commit and build, applies the
team's release policy, and gives a decision maker one page:

- **the verdict**: `go`, `no_go` or `not_established`, never "safe to release";
- **what blocks**, with the policy rule and the source finding for each;
- **what was waived**, by whom and until when, and which waivers didn't count;
- **what evidence is missing**, and which skill would produce it;
- **the risk being accepted**: non-blocking findings above the policy's threshold, and what each source
  says it didn't test.

It never re-scores a finding, never runs a scanner or a test, and writes nothing but its report.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/release-readiness-gate/`.
To have the gate run the other skills' validators, install them beside it (the default skills root is this
directory's parent), or pass `--skills-root`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/release-readiness-gate/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/release-readiness-gate/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and nothing else;
`git` is used when a checkout is given.

## Usage

> Is release 2026.11.2 ready? The candidate is a7c23de, build ci-4821, a minor release. The reports are
> listed in release/reports.json; the policy and waivers are in release/.

```bash
python scripts/collect_reports.py --reports release/reports.json --candidate release/candidate.json \
    --policy release/release_policy.json --waivers release/waivers.json --repo . --out collection.json
python scripts/apply_policy.py collection.json --out gate_report.json
python scripts/validate_gate.py gate_report.json --reports --recompute collection.json
python scripts/render_report.py gate_report.json --markdown gate_report.md
```

Start a policy from `data/starter_policy.json` (an example: copy it, set `starter` to false, and edit the
rules, required evidence and approvers).

## Example

`examples/rc-2026.11.2/` is a release candidate whose evidence is other skills' example reports (the
security audit, the WCAG audit, the UI crash report, the journey tests, the test impact selection) plus a
real CI JUnit run, with provenance for each, a policy, three waivers and a script that builds the sample
git history. `examples/rc-2026.11.2/README.md` has the answer key.

```bash
python scripts/validate_gate.py examples/rc-2026.11.2/gate_report.json --reports --recompute examples/rc-2026.11.2/collection.json
python scripts/render_report.py examples/rc-2026.11.2/gate_report.json
```

Run these from the repository root: the report records paths relative to it.

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: verdict order, starter rules, required evidence, freshness, waivers, de-duplication
- `schemas/`: `gate_report`, `release_policy`, `waivers`, `provenance`
- `data/report_types.json`: how each skill's report is detected, validated and read
- `data/starter_policy.json`: an example policy
- `scripts/collect_reports.py`: inventory, hashes, validators, provenance and freshness
- `scripts/adapters.py`: per-skill fact extraction into one shape
- `scripts/apply_policy.py`: required evidence, de-duplication, rules, waivers, verdict
- `scripts/validate_gate.py`, `scripts/render_report.py`
- `scripts/parse_results.py`: shared copy (edit it in `shared/`)

## Design principles

1. Every line points to a report, a finding id or a policy rule.
2. Missing, invalid or stale evidence is never a pass.
3. The source skill's rating stands; the policy only decides what blocks.
4. Waivers are written down, approved by the right person, and expire.
