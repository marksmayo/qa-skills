# clinic-api

A tiny appointment-booking API used as the api-contract-tester example. Stdlib only; all data is
invented and lives in memory.

```bash
CLINIC_TOKEN=fake-clinic-token python server.py --port 8140
```

- `openapi.yaml`: the contract, version 1.5.0 (OpenAPI 3.1)
- `openapi.v1.yaml`: the previous release, 1.4.0 (OpenAPI 3.0), to diff against
- `schema.graphql`: the read-only GraphQL view at `POST /graphql`

The server breaks its contract on purpose; the lines are marked `planted` and listed in
`../README.md`. It returns 429 with `Retry-After: 1` above 20 requests per second.
