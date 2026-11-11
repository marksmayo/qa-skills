# Example: clinic-api

`clinic-api/` is a tiny appointment-booking API (stdlib `http.server`, version 1.5.0) with its
contract `openapi.yaml` (OpenAPI 3.1), the previous release's `openapi.v1.yaml` (OpenAPI 3.0,
1.4.0) and a GraphQL view, `schema.graphql`. All data is invented; the bearer token is
`fake-clinic-token`.

- `run/` is the REST run: contract and cases, the Schemathesis run (4.33.2, safe mode), the added
  case, the sender's run with saved exchanges, the diff and the candidates.
- `run-graphql/` is the GraphQL run, including the fetched introspection.
- `run/commands.sh` lists every step.
- `report.example.json` and `report.graphql.example.json` are the triaged reports.

## Answer key

| Planted | Where | Expected | Got |
|---|---|---|---|
| `/reports/daily` never checks the token | `server.py:109` | `auth_enforced`, Critical | F1, P1, high confidence; Schemathesis also found it (ST401, ST203) |
| Clinician 3 has no `specialty` | `server.py:22` | `schema_conformance` on a 2xx, High | F2, on listClinicians and getClinician (reach 2), P1. The seeded sample never hit getClinician id 3; Schemathesis did (ST108), and an added case reproduced it |
| `date=11/11/2026` raises ValueError | `server.py:144` | `server_error` on invalid input, Medium | F3, P2 |
| Unknown clinician returns an undocumented 404 with another body shape | `server.py:136` | `status_documented`, Medium | F6, 11 cases, P2, fix direction `fix_contract` |
| 400 JSON body sent as `text/html` | `server.py:149` | `content_type`, Low (body parseable) | F9, P3 |
| No maximum length on `patient_name` | `server.py:160` | `negative_accepted`, Medium | F7, P3 (a write, so medium confidence) |
| Every 8th call to `/slots` returns 503 | `server.py:200` | `server_error` on valid input, flaky | F10: 2 of 17 requests, neither repeated; High, low confidence, P3 |
| `dependentRequired` in the Report schema | `openapi.yaml` | contract issue; dailyReport schema `cantTell` | C2; evidence completeness `partial` |
| The Clinician example has `id: c-7` | `openapi.yaml` | contract issue | C1 |
| 1.4.0 → 1.5.0: `room` removed from Appointment, `clinic_id` now a required parameter | `openapi*.yaml` | breaking, High (no major bump) | B1 and B9, among 10 breaking changes |
| GraphQL: clinician 3 resolves `specialty` to null for `Specialty!` | `server.py:230` | `graphql_errors`, High | GraphQL F1, both fields (reach 2), P1 |
| GraphQL: `clinicians(specialty: DENTAL)` errors | `server.py:222` | `graphql_errors`, High | GraphQL F2, P2 |
| GraphQL: the server serves `Clinician.internalNotes` | `server.py:308` | introspection mismatch | GraphQL C1 |

Found but not planted (real behaviour of the sample, kept as findings):

- `clinic_id=0` and `clinician_id=0` are accepted (F4, F5): `isdigit()` accepts "0".
- `unexpected_field` in a booking is accepted against `additionalProperties: false` (F8, with
  `fix_contract` suggested, since the 1.5.0 restriction is itself breaking change B2).

Schemathesis found 8 failures. All 8 are triaged as the same failures as stdlib candidates; the
one stdlib had missed (getClinician id 3) was reproduced with `run/extra_cases.json` first.

```bash
python scripts/validate_report.py examples/report.example.json --run examples/run/run.json --candidates examples/run/candidates.json --contract examples/run/contract.json --cases examples/run/cases.json --diff examples/run/diff.json --repo examples/clinic-api
python scripts/validate_report.py examples/report.graphql.example.json --run examples/run-graphql/run.json --candidates examples/run-graphql/candidates.json --contract examples/run-graphql/contract.json --cases examples/run-graphql/cases.json --repo examples/clinic-api
```

Re-running needs the server on loopback (`python examples/clinic-api/server.py --port 8140`) and
`CLINIC_TOKEN=fake-clinic-token`. Restart it before each sending step: the 503 counts calls.
