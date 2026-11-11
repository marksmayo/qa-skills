# Rubric: CI suite runner

## Authority

| Request | Sent when |
|---|---|
| Unprotected environment | a grant names the suite and the environment and `expires` is on or after the trigger date; or `--confirmed-by` |
| Protected environment | `--confirmed-by` only |
| Anything else | refused: the record shows the request and why, and nothing is sent |

A confirmation names who confirmed and how (at least 10 characters: "Release manager, in chat on
2026-11-12"). Grants never use wildcards. Each record holds every decision (refused, then sent) and at
most one sent trigger.

## Requests

Only the catalogue's pipeline, with its input names; the environment one of the suite's environments;
the tag expression only its markers, `and`, `or`, `not` and parentheses.

## Outcome

| Status | When |
|---|---|
| `not_started` | refused, or the trigger failed, or no run appeared |
| `in_progress` | triggered, not yet completed when last polled |
| `completed` | a poll saw it completed; the conclusion is the CI's (success, failure, cancelled) |
| `timed_out` | still running at the timeout; poll again later, never re-trigger |

## Results

Counted from the JUnit artifacts (shared `parse_results.py`): tests, passed, failed, errors, skipped, and
each failed test with its first message line. No JUnit: `not_collected`. A failing test means the
conclusion is failure and the next steps hand the run to ci-failure-triager.
