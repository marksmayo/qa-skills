# Dead test finder rubric

Rules for `dead_test_report.json`. The validator enforces everything marked (computed) and every
"must".

## Triage

Every candidate in `candidates.json` must appear exactly once: in a finding's `candidates`, or
in `dismissed` with a reason of 20+ characters.

| Candidate | May become |
|---|---|
| `never_runs` | `never_runs` |
| `no_assertion` | `cant_fail`, `weak` |
| `tautology` | `cant_fail` |
| `unreached_assertion` | `unreached_assertion`, `cant_fail` |
| `swallowed_failure` | `swallowed_failure`, `cant_fail` |
| `async_not_awaited` | `async_not_awaited`, `cant_fail` |
| `mock_only` | `mock_only` |
| `skipped` | `stale_skip` (or dismissed when legitimate, open or unknown) |
| `duplicate` | `duplicate`, `overlap` |

Two candidates can't be dismissed: a stale skip, and a never-runs candidate that the collect
output confirms. The exception is a conditional skip (a skipif, an `importorskip`, or a runtime
`pytest.skip()` inside an `if`, `except` or similar guard) whose stale verdict came from its age,
not from a done ticket: it may be dismissed with the reason the guard is there **(checked)**.
`check_skips.py` no longer makes a conditional skip stale by age, so this only arises with older
skip files.

**A candidate the scanner got wrong** (a runtime `test.skip(condition, reason)` read as a new
test, a name cut at a quote) may be dismissed with `scanner_error: true` and a `basis` citing the
lines that show the misreading: each `{file, line, excerpt}` must match the repo line **(checked
with `--repo`, required for these)**. A stale skip whose ticket is done can't be dismissed this
way.

**Findings found by review** (`source: "agent"`): an issue no candidate covers, for example an
assertion that is commented out in a test the rules didn't parse. Such a finding has `candidates:
[]` and a non-empty `basis`: `{file, line, excerpt}` items (the excerpt must appear on that repo
line, **checked with `--repo`**) or `{evidence: <file in the run directory>}` items (must exist).
Its location must be one of its basis files. Every other rule applies unchanged: it is `suspected`
from reading alone, `static` only if a rule-like check matched, and `confirmed` only with the same
run evidence as any other finding. The renderer labels it "found by review". A finding with
`source: "tool"` (the default) cites at least one candidate.

## Confidence

| Finding | `confirmed` needs |
|---|---|
| `cant_fail`, `mock_only`, and `async_not_awaited`, `swallowed_failure` or `unreached_assertion` when confirmed | A neutralisation of the test: it passed unchanged and passed with every target's neutral body, in runs recorded in `runs.json`. If the raise variant failed, `evidence.note` must say the test checks that nothing raises |
| `never_runs` | The collect output leaves the test out (`collection.json` and its raw output file), or a `.only` marker |
| `duplicate` | `similarity.json` says duplicate: similarity ≥ 0.9, identical coverage and identical values. Otherwise the finding is an `overlap`. A JavaScript pair is `not_assessed` there (no per-test coverage): it can be a `static` `duplicate` only when the bodies are identical apart from variable names (similarity 1.0, the scanner's JavaScript threshold), otherwise an `overlap`, and never `confirmed` |
| `stale_skip` | `skips.json` says stale: a done ticket, or no ticket and older than `skip_age_days` |

`static` means a rule matched and you checked it. `suspected` means your judgement alone, and is
capped at P2.

A `weak` finding whose test passes even when its code raises is really `cant_fail`.

## Ranking (computed)

`score = weight × criticality`

| Category | Weight |
|---|---|
| `never_runs`, `cant_fail` (confirmed) | 4 |
| `cant_fail` (static or suspected), `mock_only`, `async_not_awaited`, `swallowed_failure` | 3 |
| `stale_skip`, `unreached_assertion` | 2 |
| `duplicate`, `overlap`, `weak` | 1 |

Criticality is yours, with a reason:

| Criticality | What the test covers |
|---|---|
| 3 | Money, auth, permissions, data loss, or a journey-mapper P0/P1 journey |
| 2 | Ordinary code |
| 1 | Trivial helpers |

| Priority | Score |
|---|---|
| **P0** | ≥ 12, and always for a `.only` |
| **P1** | ≥ 8 |
| P2 | ≥ 4 |
| P3 | < 4 |

## Proposals

- **Patches:** every proposal except `investigate` has a patch file that applies to the repo.
- **Proofs:** a `proof` must match a patched neutralisation run, which must have:
  - passed unchanged (`original_run_id`);
  - failed in the runs listed as `neutralised_run_ids`.

  The result is `proved` only then.
- **Applying:**
  - `delete` and `merge` are never applied.
  - Only strengthening fixes (`cant_fail`, `async_not_awaited`, `weak`, `never_runs`) may be
    applied, and only with `apply: true`.
  - `files_changed` must list exactly the files the applied patches touch.

## Evidence completeness (computed)

The mean of three shares:
- the packages whose collect command ran;
- the neutralisable candidates that were neutralised, or that had nothing to neutralise;
- the ticket keys whose ticket was read.

## Honesty rules

- **Tree hash:** the working tree is unchanged across the runs (`tree_hash_before` equals
  `tree_hash_after` in the neutralisation and similarity files).
- **Test inventory:** `tests[]` lists exactly the defined tests. `neutralised` is set only where
  a neutralisation ran, and `not_assessed` needs a reason.
- **The headline:** it names the P0 findings.
- **No clean-suite claims:** the validator rejects these and similar:
  - "no dead tests";
  - "the suite is healthy";
  - "all tests are effective";
  - "every test can fail".
