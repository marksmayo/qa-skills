# Example: Harbour Tickets 1.9.0

`harbour-tickets/server.py` is a small ferry-ticket site and API that stands in for production on
loopback. It serves a version endpoint, four pages and a sign-in page, a sailings API, a fares API, a
signed-in account API, and a bookings POST that creates something. Pages carry security headers and name
their build in `<html data-build="...">`. `pvt_plan.json` is the team's plan, with eight checks, the
version from `/version` and the deployment id from the home page's HTML (a `regex` reader), security-header
assertions on the home page, and the `smoke-reader` credential set (the sample server's obviously fake
account, `not-a-real-password`).

`runs/baseline-1.8.0/` is a real run of release 1.8.0, the previous run. `runs/release-1.9.0/` is a real
run of 1.9.0 compared against it. Both were made by `commands.sh` on this machine.
`pvt_report.example.json` is the report for 1.9.0.

## Answer key

| Planted in 1.9.0 | Expected | Got |
|---|---|---|
| `/timetable` answers 500 | critical check fails; sent once, its time not compared | F1, critical; `no_go` reason; timing `not_comparable` (a 4 ms error page is not an improvement on 20 ms) |
| `/help`, a public page, redirects to `/login` | fails by default (a public check landing on sign-in), though the login page answers 200 | F2, major; the hop is recorded (`302 -> /login`, query dropped) |
| `/api/fares` returns a child price of -12.25 | the data check `fares[*].price all_gt 0` fails, and the recorded values show it | F3, critical; `no_go` reason |
| `/api/fares` about 300 ms slower | regression against the baseline, another deployment | F4, major (about 33 ms to 323 ms); `baseline_relation: different_deployment` (a1b2c3d to e4f5a6b) |
| `/about` sends an older Content-Security-Policy | the CSP differs between pages (nonces ignored) | F5, minor `header_inconsistent` |
| The plan lists `POST /api/bookings` | refused, never sent | `blocked`, in the notes and the limitations; no request to it is in the run |
| Decoy: `/about` about 30 ms slower | not a regression (under the 50 ms minimum change, though about +200%) | `ok` |
| Decoy: the home page's security headers | all asserted headers present and as expected, no redirect | `pass` |
| Decoy: the signed-in account check | passes with `smoke-reader`; the email is checked to exist but not recorded | `pass`; `actual_shape` only |

The recommendation is `no_go`, for the release manager to decide. The decision is left empty. The version
matched (1.9.0, build e4f5a6b). Neither run's records contain the password, the bearer token or a CSP nonce.

```bash
python scripts/validate_pvt.py examples/pvt_report.example.json --run examples/runs/release-1.9.0/run.json --baseline examples/runs/baseline-1.8.0/run.json
python scripts/render_report.py examples/pvt_report.example.json
```

Run these from `post-deploy-validator-skill/`. Rerunning `commands.sh` makes new timings, so the report is
rebuilt from the new run; the findings and the recommendation stay the same.
