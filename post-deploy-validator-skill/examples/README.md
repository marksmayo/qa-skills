# Example: Harbour Tickets 1.9.0

`harbour-tickets/server.py` is a small ferry-ticket site and API that stands in for production on
loopback. It serves a version endpoint, three pages, a sailings API, a fares API, a signed-in account API,
and a bookings POST that creates something. `pvt_plan.json` is the team's plan, with seven checks and the
`smoke-reader` credential set (the sample server's obviously fake account, `not-a-real-password`).

`runs/baseline-1.8.0/` is a real run of release 1.8.0, the previous run. `runs/release-1.9.0/` is a real
run of 1.9.0 compared against it. Both were made by `commands.sh` on this machine.
`pvt_report.example.json` is the report for 1.9.0.

## Answer key

| Planted in 1.9.0 | Expected | Got |
|---|---|---|
| `/timetable` answers 500 | critical check fails | F1, critical; `no_go` reason |
| `/api/fares` returns a child price of -12.25 | the data check `fares[*].price all_gt 0` fails, and the recorded values show it | F2, critical; `no_go` reason |
| `/api/fares` about 300 ms slower | regression against the baseline | F3, major (about 37 ms to 331 ms) |
| The plan lists `POST /api/bookings` | refused, never sent | `blocked`, in the notes and the limitations; no request to it is in the run |
| Decoy: `/about` about 30 ms slower | not a regression (under the 50 ms minimum change, though +193%) | `ok` |
| Decoy: the signed-in account check | passes with `smoke-reader`; the email is checked to exist but not recorded | `pass`; `actual_shape` only |

The recommendation is `no_go`, for the release manager to decide. The decision is left empty. The version
matched (1.9.0). Neither run's records contain the password or the bearer token.

```bash
python scripts/validate_pvt.py examples/pvt_report.example.json --run examples/runs/release-1.9.0/run.json --baseline examples/runs/baseline-1.8.0/run.json
python scripts/render_report.py examples/pvt_report.example.json
```

Run these from `post-deploy-validator-skill/`. Rerunning `commands.sh` makes new timings, so the report is
rebuilt from the new run; the findings and the recommendation stay the same.
