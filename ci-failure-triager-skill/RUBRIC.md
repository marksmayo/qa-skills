# Rubric: CI failure triager

## Classification rules (in order)

| # | Class | When | Confidence |
|---|---|---|---|
| 1 | `environment` | an environment signature in the message or the log lines (`ENV-*` in `data/signatures.json`) | `confirmed` if the re-run passed, else `indicated` |
| 2 | `flaky` | it passed on the same commit: the re-run, or a run of that commit in the history | `confirmed` |
| 3 | `data` | a data signature (`DATA-*`), or a date in the failure before the run date | `indicated` |
| 4 | `real` | an assertion or error in a test that failed again on re-run | `confirmed` with an owning change, else `indicated` |
| 4b | `real` | an assertion or error, no re-run made | `indicated` |
| 5 | `unclassified` | none of the above | `low` |

An override needs a reason of 20 characters or more, still needs the class's evidence (flaky: a pass on
the same commit; environment: a signature or a setup step; data: a signature or a date marker; real: not
passed on re-run), and never raises the confidence above what the evidence gives.

## Signals

- **Signatures**: `data/signatures.json` (network, registry, rate limit, disk, memory, services, timeouts;
  duplicate keys, missing fixtures, expired dates), each with the log line that matched.
- **Step kind**: `test` for a failing JUnit case, `setup` for a failed step outside the tests.
- **Re-run**: the same test or step in the latest attempt: `passed`, `failed`, `missing`, `not_rerun`.
- **History**: runs of the same commit on the branch.
- **Date markers**: dates in the failure earlier than the run's start date.

## Owning change

Commits in `last_green..commit`, scored by shared files: traceback frame 3; test file, imported module,
data file the test reads 2 each. Highest score wins; ties go to the most recent and are listed. Nothing
shared: `not_identified`.

## Priority (real failures only)

P1 on the default branch (`main`, `master`, `trunk`, `develop`) or a release or hotfix branch; P2
elsewhere.

## Drafts

| Class | Draft |
|---|---|
| real | `defect` |
| data | `test_data_issue` |
| flaky, environment, unclassified | none (a next action instead) |

Each draft has a title, summary, steps to reproduce (commands at the failing commit), expected, actual,
the suspect commit when identified, and evidence (log lines, the re-run, the run URL).

## Evidence completeness

Five checks, each 0 or 1, averaged: logs for every failed job; JUnit for every test job; a re-run; a last
green run; the repository for the owning change.
