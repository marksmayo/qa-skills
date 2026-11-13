# Rubric: CI failure triager

## Classification rules (in order)

| # | Class | When | Confidence |
|---|---|---|---|
| 0 | `consequence` | an aggregate gate job (its name or script checks other jobs' results) that failed while other jobs in the run failed | `indicated` |
| 1 | `environment` | an environment signature in the message or the log lines (`ENV-*` in `data/signatures.json`) | `confirmed` if the re-run passed, else `indicated` |
| 2 | `flaky` | it passed on the same commit: the re-run, or the same job passing in another run of the same workflow on that commit | `confirmed` |
| 3 | `data` | a data signature (`DATA-*`), or a date in the failure before the run date | `indicated` |
| 3b | `real` | a real signature (`REAL-*`: a toolchain version below a tool's minimum, a compile or type error) | `indicated` |
| 4 | `real` | an assertion or error in a test that failed again on re-run | `confirmed` with an owning change, else `indicated` |
| 4b | `real` | an assertion or error, no re-run made | `indicated` |
| 5 | `unclassified` | none of the above | `low` |

An override needs a reason of 20 characters or more, still needs the class's evidence (consequence: a gate
with other failures; flaky: a pass on the same commit; environment: a signature or a setup step; data: a
signature or a date marker; real: not passed on re-run), and never raises the confidence above what the
evidence gives. The evidence includes the log lines the agent quotes: once they are checked against the
log, the rules are re-applied to them, so an environment signature the extractor missed but the agent
quoted, in a step that passed on re-run, is `confirmed`. Otherwise an override is at most `indicated`.

## Signals

- **Signatures**: `data/signatures.json` (network, registry, rate limit, disk, memory, services, timeouts;
  duplicate keys, missing fixtures, expired dates), each with the log line that matched.
- **Step kind**: `test` for a failing test (JUnit or console summary) or a step that runs tests;
  `setup` for checkout, install, cache, download, toolchain set-up and the like; `other` for anything
  else (build, lint, publish, gates). Only `setup` makes environment legal without a signature.
- **Re-run**: the same test or step in the latest attempt: `passed`, `failed`, `missing`, `not_rerun`, or
  `job_not_rerun` when the attempt carried the job over without running it (GitHub lists every job in a
  later attempt; the re-run ones have a new start time).
- **History**: runs of the same workflow on the same commit, with the outcome of the same job in each.
- **Consequence of**: for an aggregate gate, the failures of the other jobs.
- **Date markers**: dates in the failure earlier than the run's start date.

## Owning change

Commits in `last_green..commit` (the last green run of the same workflow), scored by shared files: traceback frame 3; test file, imported module,
data file the test reads 2 each. Highest score wins; ties go to the most recent and are listed. Nothing
shared: `not_identified`. In a monorepo only commits touching the failing package count. A failing
or last green commit missing from the checkout gives `not_identified` with the fetch command, and the
repository check below scores 0.

## Priority (real failures only)

P1 on the default branch (`main`, `master`, `trunk`, `develop`) or a release or hotfix branch; P2
elsewhere.

## Drafts

| Class | Draft |
|---|---|
| real | `defect` |
| data | `test_data_issue` |
| flaky, environment, consequence, unclassified | none (a next action instead) |

Each draft has a title, summary, steps to reproduce (commands at the failing commit), expected, actual,
the suspect commit when identified, and evidence (log lines, the re-run, the run URL).

## Evidence completeness

Five checks, each 0 or 1, averaged: logs for every failed job; test results for every failed job that runs
tests (JUnit, or failing tests parsed from a Vitest, Jest or node:test console summary); a re-run; a last
green run; the repository for the owning change.

## Downloads that failed

A log or artifact that could not be downloaded is listed in `run.json` `fetch_problems` with its status
(`unavailable`: 404/410, expired or deleted; `fetch_failed`: 401/403 or network; `skipped`: over the size
cap). Each must be named in the triage's limitations.
