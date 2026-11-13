# Design: `ci-failure-triager-skill`

Status: Built (v1.1.0); see "As built" at the end. Build order: 26.

## Mission

The skill takes a failed CI run (GitHub Actions or Azure DevOps) and says, for each failure, what kind
of failure it is and what to do next:

- **real**: the product or the test's expectation changed, and the failure is repeatable on that commit;
- **flaky**: it failed and passed on identical code;
- **environment**: the runner, network, registry, disk, memory or a service the job needs failed;
- **data**: test data or fixtures are wrong for this run (expired dates, duplicate keys, missing seed);
- **unclassified**: the evidence doesn't decide it.

For real failures it finds the owning change (the commit since the last green run that touched what
failed) and drafts a defect. For data failures it drafts a test-data issue. Drafts go in the result
log; nothing is filed.

How it differs from the other skills:

- **flaky-test-hunter** proves why a test is flaky with controlled experiments. This skill only decides,
  from one run and its re-run, whether a failure is flaky, and hands confirmed flakes to the hunter.
- **bug-reproducer** reproduces a reported bug. This skill starts from a CI failure, and its draft defect
  can be the input to the reproducer.
- **ci-suite-runner** (31) triggers and collects runs. This skill reads a finished run.

## Decisions

Agreed with the user (see `decisions-26-32.md`):

- **GitHub Actions and Azure DevOps.** GitHub through its REST API (`gh api` or a token in
  `GITHUB_TOKEN`); Azure DevOps through its REST API (a PAT in `AZURE_DEVOPS_EXT_PAT`). Both normalise to
  one run shape. A recorded directory of API responses works offline.
- **Re-running failed jobs is allowed** to separate flaky from real. Only failed jobs, once each, on the
  same commit. Re-running is still an outward action, so the skill asks before triggering unless the user
  pre-authorised re-runs (`allow_rerun: true` with who authorised it). The re-run's id and outcome are recorded.
- **Defects are drafted in the result log first**, as Markdown and JSON in the report; never filed in a
  tracker.

## Non-negotiable rules

- **Flaky needs a pass and a fail on the same commit**: the re-run, or the run history on that commit.
  A failure that is never seen passing on that commit is not flaky, however often the test fails elsewhere.
- **Real needs a repeatable failure on that commit**: it failed again on re-run, or no re-run was made and
  the confidence is at most `indicated`.
- **Environment and data need their evidence**: a known signature in the log (with the line), a failure in
  a setup step rather than a test step, or a data marker in the failure (a date before the run date, a
  duplicate key). A signature match alone is `indicated`.
- **The owning change is a commit in range** (after the last green run on the branch, up to the failing
  commit), and the files it shares with the failure (traceback frames, the test file, the test's imports)
  are listed. With none shared, it is `not_identified`, never a guess.
- **Log excerpts are quoted with their line numbers** from the downloaded log, and secrets are redacted.
  A log line that looks like a token is never copied.
- **Every failing job and test is triaged exactly once.** Nothing is dropped as noise.
- **No "the build is fine".** A run whose failures are all flaky or environment is reported as such, with
  what would make it pass; the skill never calls the code green.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `run` | required | A GitHub run URL or id (with `owner/repo`), an Azure DevOps build URL or id (with organisation and project), or a recorded directory |
| `repo` | the working tree | A checkout with history, for the owning change |
| `allow_rerun` | false | `true` with `authorised_by`; otherwise the skill asks before each re-run |
| `history` | 20 previous runs on the branch | For the last green run and earlier outcomes of each test |
| `run_date` | the run's start | For data markers such as expired dates |

## Workflow

```bash
python scripts/fetch_run.py --system github --repo owner/name --run-id 123 --out run/    # or --from-dir recorded/
python scripts/extract_failures.py --run run/ --out run/failures.json
python scripts/rerun.py --run run/ --authorised-by "..."                                   # only with permission
python scripts/owning_change.py --run run/ --repo . --out run/changes.json
python scripts/classify.py --run run/ --out run/candidates.json
python scripts/validate_triage.py triage.json --run run/
python scripts/render_report.py triage.json [--brief | --defects-md drafts.md]
```

1. **Fetch** the run, its jobs and steps, their logs, the JUnit artifacts, the previous runs on the branch,
   and the last green one. Normalise to `run.json`.
2. **Extract failures**: failed jobs and steps from the run, failing cases from JUnit, and for a step that
   failed outside the tests, the log lines around the first error.
3. **Re-run** the failed jobs once (with permission), and fetch the new attempt's results.
4. **Find the owning change**: the commits from the last green run to this one, scored by files shared
   with each failure.
5. **Classify** each failure from its signals (`data/signatures.json`, re-run outcome, history, step kind,
   data markers) into candidates with the rule that decided it.
6. **Triage**: the agent confirms or overrides each candidate with a reason, drafts the defects, and writes
   the next action (fix the code, fix the test data, hand to flaky-test-hunter, re-run after the outage).
7. **Validate and render.**

## Rating

| Classification | Decided by | Confidence |
|---|---|---|
| flaky | fail and pass on the same commit (re-run or history) | `confirmed` |
| environment | signature in the log, or a failed setup step, and no assertion failure | `indicated`; `confirmed` when the re-run passed and the signature named an outside service |
| data | a data marker in the failure (date before the run date, duplicate key, missing fixture) | `indicated` |
| real | an assertion or product error that failed again on the re-run | `confirmed` with an owning change; `indicated` without a re-run |
| unclassified | none of the above | - |

**Priority** for real failures: P1 when the failing test is on the default branch or a release branch, P2
otherwise; P0 when the same failure also breaks the build of every later run in the history.

## Output (`triage.schema.json`)

```text
run{system, id, url, attempt, commit, branch, event, started_at, last_green{id, commit}}
reruns[{job_id, new_attempt, outcome, authorised_by, at}]
failures[{id, job, step, test_id?, kind: test|step, classification, confidence, rule, signals[],
          log_excerpt{path, lines[]}, rerun_outcome, history, owning_change{commit, subject, files_shared[],
          basis} | not_identified, draft_defect?, next_action}]
summary{headline, by_classification, defects_drafted, reruns, evidence_completeness}
limitations[]
```

## Validator fails when

- A failing job or JUnit case isn't triaged, or is triaged twice.
- A flaky failure has no pass on the same commit; a real failure passed on re-run.
- An environment or data classification has no signature line, setup step or data marker.
- The owning change is outside the range, or shares no file with the failure.
- A real failure has no draft defect, or a flaky or environment failure has one.
- A log excerpt doesn't match the log at those lines, or contains a token-like value.
- A re-run happened without `authorised_by`, or re-ran a job that hadn't failed.
- The text calls the build green, stable or fine.

## Edge cases

- **Re-run not allowed or not possible** (a fork's run, expired logs): flaky can still come from history;
  otherwise `indicated` at most.
- **Matrix jobs**: each leg is a job; the same test failing in one leg only is a signal for environment or
  platform, recorded as such.
- **Logs expired** (GitHub keeps them 90 days by default): `not_assessed` for those jobs.
- **Monorepo**: the owning change considers only commits touching the failing package.

## Files

```text
ci-failure-triager-skill/
  SKILL.md  RUBRIC.md  README.md
  schemas/triage.schema.json
  data/signatures.json
  scripts/fetch_run.py  extract_failures.py  rerun.py  owning_change.py  classify.py
          validate_triage.py  render_report.py  parse_results.py (shared)
  examples/
```

## Example

`examples/tidewater/` is a small Python package; `make_repo.py` builds its history with fixed ids: a green
run's commit, then commits that change rounding, add a fixture with a booking date, and touch the docs.
`examples/recorded/` holds the API responses of a failed GitHub Actions run and its re-run attempt, and an
Azure DevOps build of the same commit. The logs and JUnit inside them are real local runs (pytest and pip);
the envelopes follow the APIs' shapes, and no real CI was called. Planted:

- a real regression from the rounding commit (fails again on re-run);
- a flaky test (fails, then passes on re-run);
- an environment failure: the dependency install step can't reach the package index;
- a data failure: a fixture booking dated before the run date;
- a decoy: a docs-only commit in range that the owning change must not pick.

## Resolved questions

Agreed with the user before the build (`decisions-26-32.md`).

1. **CI systems?** GitHub Actions and Azure DevOps.
2. **Re-run failed jobs?** Yes, to separate flaky from real.
3. **Defects?** Drafted in the result log first.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Live fetch and re-run are written but not exercised by the example.** The example uses recorded API
  responses built from real local pytest and pip runs (`examples/record_example_run.py`), because no CI
  system was called. `fetch_run.py` makes only GET requests. `rerun.py` refuses without
  `--authorised-by`, never re-runs a run already re-run, and in `--recorded` mode records the existing
  attempt instead of triggering one.
- **Rule order puts environment first**: an environment signature decides it even when the re-run
  passed (`confirmed` then), so a network blip isn't called a flaky test.
- **The owning change scores data files the test names** (a fixture under the test's folder whose name
  appears in the test source), so the fixture commit owns the data failure.
- **Priority** is P1 on the default, release or hotfix branches and P2 elsewhere. The P0 rule (the same
  failure in every later run) is not built.
- **Example results:** four failures, one of each class, matching the plants. The docs-only decoy was
  not picked. The Azure DevOps path, with no re-run, shows the flaky test as `real, indicated`, which is
  why re-runs are asked for.
- **Testing:** 29 of 29 seeded violations are caught, including a re-run without authorisation, a re-run
  of a job that hadn't failed, a token in a log excerpt, and the docs commit as owner.

## As built (v1.1.0): fixes from a live run on a public TypeScript monorepo

Run against four failed GitHub Actions runs of a public pnpm workspace (Vitest, no JUnit), the 1.0.0 skill
could not download a single log and got 0 of 6 excerpts right. Fixed:

- **Live downloads.** GitHub answers job-log and artifact downloads with a 302 to signed blob storage;
  urllib re-sent the bearer token there and the storage host answered 401, so every log was silently
  "missing" and any artifact crashed the fetch. `fetch_run.py` now follows redirects with a handler that
  drops credential headers on any change of scheme, host or port (and refuses https to http except on
  loopback). A failed download is recorded with its status (`unavailable` for 404/410, `fetch_failed`
  otherwise) in `run.json` `fetch_problems`, printed, and the script exits 2 when a log fetch failed; the
  validator requires the triage's limitations to name each such job. Artifacts over `--max-artifact-mb`
  are skipped and recorded. Verified read-only against the public runs; `fetch_run.py --self-test` covers
  it with two loopback servers.
- **History is the same workflow.** It was the 20 newest runs of any workflow on the branch, so the last
  green run could be another workflow on the same commit (an empty range) or missing entirely once 20
  later runs existed. Now: the run's workflow, on the branch, created up to the run, plus every run of the
  workflow on the same commit with its jobs. The last green run prefers the same event and skips a run
  that did not pass the jobs that failed here.
- **Flaky by history is job-level.** A same-commit run counts only if the same job passed in it; a push
  run of the release workflow that ran a different job no longer makes a scheduled publish failure
  "flaky, confirmed".
- **Step excerpts.** GitHub titles a step's log group `Run <script>`, never the step's `name:`, so the
  section was never found and the excerpt was the first "error" anywhere in the job (an action input, a
  script name, the echoed script, a passing test line). Now the group is matched by title when unique and
  otherwise by order of the groups holding `##[error]`; the echoed script is skipped and the excerpt
  anchors on the error lines just before the step's closing `##[error]`. ANSI codes are stripped before
  matching and quoting.
- **Console test results.** Without JUnit, failing tests are read from Vitest, Jest and node:test summaries
  (test id `file > suite > test`, message, frames), so the real rule can fire and the owning change has a
  test file; paths printed relative to a package are matched to the tree, relative TypeScript imports are
  followed, and in a monorepo only commits touching the failing package can own it.
- **Step kind** is `test`, `setup` or `other` (was `setup` for every non-JUnit step, which let any step
  failure be called environment).
- **New class `consequence`** for aggregate gate jobs. A cross-run cause (a release run waiting on another
  run's gate) is still not expressed.
- **Signatures**: TLS handshake timeout, socket hang up, EAI_AGAIN/getaddrinfo, pnpm publish/fetch 404s,
  and two `real` signatures (a toolchain below a tool's minimum version; compile/type errors).
- **Re-runs someone else started** are recorded as `observed` (`rerun.py --observed`) with the run's
  triggering actor and the jobs that actually ran again (a new start time), not as authorised by the triage.
- **Override confidence**: the validator re-applies the rules to the log lines an agent quotes (once they
  match the log), so a quoted signature can carry `confirmed`; otherwise overrides stay at `indicated`.
- **Smaller**: `owning_change.py` writes a not_identified `changes.json` with the fetch command when a
  pull request head is not in the clone; the requested repository and the API's name for it are both kept
  (a renamed repository); evidence completeness counts console-parsed test results; `--defects-md` writes
  no file when there are no drafts.
- **Testing**: `scripts/self_test.py` runs the fetch and extraction self-tests and a synthetic run through
  the whole pipeline with 10 seeded violations. The example's recordings gained workflow and definition
  ids (`record_example_run.py` writes them); its classifications are unchanged.
