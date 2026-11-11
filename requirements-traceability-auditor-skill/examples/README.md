# Example: harbour-loyalty, release 2026.12

`make_repo.py` builds "harbour-loyalty", a small points package, with five commits that name their
tickets. `tickets/` holds four Jira issues (SHOP-401 to SHOP-404) as a Jira connector returns them.
`requirements.md` is a requirements document with two non-functional requirements. `test-management.json`
maps the Xray test SHOP-T12 to an automated test. `run/` holds the normalised tickets and requirements, a
real pytest JUnit run of the package at HEAD (with the hostname replaced), and the evidence. `triage.json`
is the agent's triage, and `trace.example.json` the report. Every step is in `commands.sh`.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| SHOP-401: tests naming `SHOP-401 AC-1..3`, all passing | verified | verified; 3/3 criteria with explicit tests |
| SHOP-401's comment names `tests/test_points.py`, which also holds SHOP-402's tests | two wrong inferred links | both dropped in triage with reasons |
| SHOP-402: tests for AC-1 and AC-2 only | partially verified; AC-3 (Bronze) gap | partially_verified; "SHOP-402/AC-3 has no explicit test" |
| SHOP-403: linked only through Jira "is tested by SHOP-T12" and the Xray mapping; the test fails (expiry is a month late) | failing, explicit link | failing; evidence: ticket link, test-management entry, and the commit that added the test |
| SHOP-404: a commit and code name it, no test | untested | untested, with implementation evidence (code and commit) |
| REQ-NFR-1 and REQ-NFR-2 in the document | named by nothing | not_traceable (the limitations hand REQ-NFR-1 to the performance budget auditor) |
| Decoy: `test_bulk_import_line` names SHOP-4010 | never linked to SHOP-401 | orphan, "names SHOP-4010, outside this set" |
| `test_legacy_bonus_flag_is_ignored` names nothing | orphan | orphan |

6 of 10 criteria have an explicit test. The attachment `tier-rules.pdf` on SHOP-402 is listed, not read.

```bash
python scripts/validate_trace.py examples/trace.example.json --requirements examples/run/requirements.json --evidence examples/run/evidence.json --junit examples/run/junit.xml
python scripts/render_report.py examples/trace.example.json
```

Run these from `requirements-traceability-auditor-skill/`. With `--repo <the repository built by
make_repo.py>`, the validator also re-reads every file line and commit. The commit ids and the JUnit are
fixed: rerunning `commands.sh` gives the same evidence, apart from the JUnit timestamp.
