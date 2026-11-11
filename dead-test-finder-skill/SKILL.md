---
name: dead-test-finder
description: Find tests that give false assurance - tests the framework never collects, tests that pass whatever the code does, tests that only check their own mocks, un-awaited async assertions, swallowed failures, duplicates, and skips whose reason has gone - and prove the important ones with real runs. Never-runs is confirmed by the framework's own collect output; can't-fail by neutralising the code under test in a scratch copy and seeing the test still pass; duplicates by identical coverage and values. Each finding gets a patch, and every strengthening patch is proved to pass on the code and fail when it's neutralised. Never deletes or merges tests, never commits, and never claims a suite has "no dead tests".
version: 1.0.0
---

# Dead test finder

## Mission

Find the tests that would stay green if the code they claim to check were broken, and the tests
that never run at all. Each finding gives the evidence (a collect listing, a neutralisation run,
a coverage comparison, a ticket status) and a proposal with a patch. Fix, merge, un-skip or
delete is a person's decision; the skill proves its strengthening fixes but applies them only
when asked.

How this differs from the other skills:

- **test-suite-evaluator** samples tests to judge the suite. This skill checks every test against
  concrete rules and proves the important ones with a run.
- **mutation-tester** makes small changes to product code. This skill uses the extreme case,
  neutralising a whole function, and points to mutation-tester for finer analysis.
- **flaky-test-hunter** looks for tests that fail sometimes. This skill looks for tests that never
  fail, or never run.
- **test-data-auditor** gets very large snapshots referred to it.

Output:

1. `dead_test_report.json`, conforming to `schemas/dead_test_report.schema.json`.
2. The run directory, kept with the report as its evidence:
   - `candidates.json`, `collection.json` and the raw collect output;
   - `runs.json` and its JUnit files;
   - `neutralisations.json`, `similarity.json`, `tickets.json` and `skips.json`.
3. A patch for every proposal, in a proposals directory.
4. A rendered summary for the user.

## Non-negotiable rules

- **"Can't fail" is `confirmed` only by a run.**
  - **How:** in a scratch copy, the function the test calls gets a neutral body (`return None`
    or `return undefined`), and the test still passes.
  - **The raise variant:** the same function is also made to raise, separately. If the test
    fails then, it at least checks that nothing raises; say so.
  - **Without a run:** a test with no assertion is `static`, and reading the code alone is
    `suspected` (never above P2).
- **"Never runs" is confirmed by collection, not by name patterns.** The framework's own list
  command must leave the test out (`pytest --collect-only`, `vitest list`, `jest --listTests`).
  A `.only` left in the code is always P0.
- **A duplicate needs similar code and identical behaviour.**
  - **The test:** AST similarity at or above 0.9 after normalising names and literals, the same
    product lines run, and the same values.
  - **Otherwise:** similar code that runs the same lines with different values is an `overlap`.
- **A stale skip needs evidence.** Either its ticket is done (Jira status category, read-only),
  or it has no ticket and `git blame` says it is older than `skip_age_days`. A conditional skip
  with a stated reason is legitimate and is dismissed with that reason.
- **Never deletes or merges.**
  - **Proposals:** every one has a patch file.
  - **With `apply: true`:** apply only fixes that strengthen a test (the missing assertion or
    `await`, the rename that gets a file collected) and remove a stray `.only`. Then list the
    changed files.
  - **Never:** commit, or write back to Jira.
- **The repository is never changed by a run.**
  - Neutralisation and proof runs work in copies outside the repo, or in a scratch directory you
    name.
  - Every patched file is restored afterwards.
  - The tree hash is checked before and after.
- **No "no dead tests" claim.** The report lists what was scanned, collected and neutralised,
  and what couldn't be (no collect command, services needed). The validator rejects "no dead
  tests", "the suite is healthy" and similar.

## Inputs

| Input | Default | Notes |
|---|---|---|
| Repo | required | |
| Paths | whole suite | |
| Neutralise | on, for flagged candidates only | Ordinary tests the rules don't flag are not neutralised, and the report says so |
| Tickets | none | Jira issues for skip keys, via fetch_ticket.py (REST, a connector export, or a file) |
| `skip_age_days` | 180 | |
| `apply` | false | Strengthening fixes only |
| Critical paths | none | Money, auth, permissions, data loss, or journey-mapper P0/P1 journeys get criticality 3 |

## Step 1: Scan and collect

```bash
python scripts/scan_tests.py <repo> --out <run>/candidates.json
python scripts/collect_check.py <repo> --candidates <run>/candidates.json --run-dir <run> [--cwd client=<copy with node_modules>]
```

`scan_tests.py` lists every test definition, whether or not it would be collected:
- Python is parsed with `ast`;
- JavaScript and TypeScript are parsed with `it`/`test`/`describe` matching.

It flags these candidates:

| Candidate | Rule |
|---|---|
| `never_runs` | File outside `python_files` (read from the pytest config), class outside `python_classes`, `.only` |
| `no_assertion` | No `assert`, assertion call, `expect` or `pytest.raises` |
| `tautology` | `assert True`, `x == x`, `expect(v).toBe(v)` |
| `unreached_assertion` | Assertions inside a loop over a possibly empty iterable, only inside `except`, or after `return` |
| `swallowed_failure` | `except AssertionError: pass` or similar around an assertion; an empty `.catch(() => {})` |
| `async_not_awaited` | `expect(...).resolves/.rejects` neither awaited nor returned; a `done` callback never called |
| `mock_only` | The test patches a function, then asserts on that same function |
| `skipped` | `skip`, `skipif`, a non-strict `xfail`, `it.skip`, `describe.skip`, `todo`; with the reason and any ticket key |
| `duplicate` | Normalised AST similarity ≥ 0.9 |

`collect_check.py` runs each framework's list command and keeps the raw output. Every
definition that isn't listed is `not_collected`.

## Step 2: Neutralise the flagged tests

```bash
python scripts/neutralise.py --repo <repo> --candidates <run>/candidates.json --run-dir <run> \
    --test <id> [--test <id> ...] [--target <id>=module.function] [--cwd client=<copy>]
```

Run this for every `no_assertion`, `tautology`, `unreached_assertion`, `swallowed_failure`,
`async_not_awaited` and `mock_only` candidate.

The targets are the product functions the test imports and calls; use `--target` to name another.
Each test runs three times:
- once unchanged;
- once per target with a neutral body;
- once per target with a raising body.

| Result | Meaning |
|---|---|
| Passes with the neutral body | It can't fail on that code: `cant_fail` (or `mock_only` when it mocks its subject), confirmed |
| Fails with the neutral body | It can fail. Keep the static finding (e.g. an un-awaited assertion that today's runner happens to check), with that result as evidence |
| Nothing to neutralise | It calls no product code: usually a smoke test, so `weak` |

## Step 3: Duplicates and skips

```bash
python scripts/similarity.py --repo <repo> --candidates <run>/candidates.json --run-dir <run>
python scripts/fetch_ticket.py --from-file <issue.json> --source connector --out <run>/tickets.json    # or a REST fetch
python scripts/check_skips.py --repo <repo> --candidates <run>/candidates.json --tickets <run>/tickets.json --out <run>/skips.json
```

`similarity.py` runs each candidate pair with a per-test line tracer and compares the lines run
and the literal values.

`check_skips.py` gives each skip a verdict:

| Verdict | When |
|---|---|
| `stale` | The ticket is done, or the skip has no ticket and is older than `skip_age_days` |
| `open` | The ticket is still open |
| `legitimate` | A conditional skip with a reason |
| `unknown` | No ticket file, no git history |

## Step 4: Review, propose, prove

Every candidate becomes a finding or is dismissed with a reason, for example a legitimate
conditional skip.

| Finding | From |
|---|---|
| `never_runs` | A never-runs candidate that collection confirms, or a `.only` |
| `cant_fail` | A test that passed neutralised, or a tautology |
| `mock_only`, `async_not_awaited`, `swallowed_failure`, `unreached_assertion` | The matching candidate |
| `stale_skip` | A stale verdict |
| `duplicate` or `overlap` | The similarity verdict |
| `weak` | A smoke test you keep, with a note. It becomes `cant_fail` instead if it passes even when the code raises |

Write a patch for each proposal (`fix`, `merge`, `unskip`, `delete`). Prove every strengthening
patch:

```bash
python scripts/neutralise.py ... --patch proposals/<fix>.diff --test <id after the patch> [--target ...]
```

The patched test must pass unchanged and fail with the code neutralised.

Give each finding a criticality with a reason: 3 for money, auth, permissions, data loss or a
journey-mapper P0/P1 journey, 2 for ordinary code, 1 for trivial helpers.

## Step 5: Rank, report, validate

Each finding's priority is computed:
- **Score:** category weight × criticality.
- **Weights:**

  | Category | Weight |
  |---|---|
  | `never_runs`, confirmed `cant_fail` | 4 |
  | `mock_only`, `async_not_awaited`, `swallowed_failure`, unconfirmed `cant_fail` | 3 |
  | `stale_skip`, `unreached_assertion` | 2 |
  | `duplicate`, `overlap`, `weak` | 1 |

- **Bands:** P0 at 12 or more, P1 at 8, P2 at 4, otherwise P3.
- **Overrides:** a `.only` is always P0, and a `suspected` finding is never above P2.

```bash
python scripts/validate_dead_test_report.py <run>/../dead_test_report.json --repo <repo> --write-summary
python scripts/render_report.py <run>/../dead_test_report.json [--brief]
```

The report's `metadata.evidence_files` and patch paths are relative to the report's directory.
Write the headline yourself, naming the P0 findings, and fix every error rather than editing
computed fields.

Show the user:
1. the headline;
2. the ranked findings, each with its evidence and whether the fix is proved;
3. what was dismissed and why;
4. what couldn't be collected or neutralised.

## Report fields

| Field | Content |
|---|---|
| `metadata` | Repo, frameworks and collect commands, `apply`, `skip_age_days`, platform, evidence files |
| `tools[]` | Each tool ran, not run, errored or not applicable, with the reason |
| `collection` | Defined, collected, not collected, not assessed |
| `tests[]` | Every defined test: scanned, neutralised or not assessed (with the reason) |
| `findings[]` | Category, candidates, tests, location, confidence, why, criticality and reason, evidence (neutralisation, collection, similarity or skip), proposal (action, patch, rationale, proof), `applied`; weight, score and priority are computed |
| `dismissed[]` | Candidates set aside, with the reason |
| `summary` | Headline, tests defined, collected and neutralised, counts by category and priority, evidence completeness |
| `limitations[]`, `files_changed[]` | What the scan can't see; files changed with `apply: true` |
