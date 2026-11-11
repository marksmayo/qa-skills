#!/usr/bin/env bash
# How examples/run/ and examples/run-graphql/ were produced (bash, from api-contract-tester-skill/).
# Needs Python 3.9+. Schemathesis is optional: set ST to its `st` executable (the example used 4.33.2 in a
# throwaway venv); without it run_schemathesis.py records not_run and the rest is unchanged.
# The server is restarted before each sending step, because its planted 503 counts calls to /slots.
set -e
export CLINIC_TOKEN=fake-clinic-token          # the sample API's token; obviously fake
ST=${ST:-st}
R=examples/run
G=examples/run-graphql
start_server() { python examples/clinic-api/server.py --port 8140 > /dev/null 2>&1 & SERVER=$!; sleep 1; }
stop_server() { kill $SERVER 2>/dev/null || true; sleep 1; }

python scripts/load_contract.py examples/clinic-api/openapi.yaml --out $R/contract.json
python scripts/load_contract.py examples/clinic-api/openapi.v1.yaml --out $R/contract.v1.json
python scripts/spec_diff.py $R/contract.v1.json $R/contract.json --out $R/diff.json --run-oasdiff examples/clinic-api/openapi.v1.yaml examples/clinic-api/openapi.yaml
python scripts/gen_cases.py $R/contract.json --out $R/cases.json --seed 1611 --exclude cancelAppointment --allow-writes --auth-configured

start_server
python scripts/run_schemathesis.py examples/clinic-api/openapi.yaml --st "$ST" --base-url http://127.0.0.1:8140 --out $R/schemathesis.json \
  --seed 1611 --max-examples 20 --rate 5 --bearer-env CLINIC_TOKEN --exclude-operation-id cancelAppointment
stop_server

# Schemathesis found getClinician returning a clinician without `specialty` (id 3), which the seeded sample
# missed. $R/extra_cases.json reproduces it with our own saved exchange.
python scripts/gen_cases.py $R/contract.json --out $R/cases.json --seed 1611 --exclude cancelAppointment --allow-writes --auth-configured \
  --add-cases $R/extra_cases.json

start_server
python scripts/send_cases.py $R/contract.json $R/cases.json --base-url http://127.0.0.1:8140 --out $R/run.json \
  --bearer-env CLINIC_TOKEN --allow-writes --authorised-by "repo owner (loopback sample app)" --confirm-non-production
stop_server
python scripts/merge_findings.py $R/contract.json $R/cases.json $R/run.json --diff $R/diff.json --schemathesis $R/schemathesis.json --out $R/candidates.json

start_server
python scripts/load_contract.py examples/clinic-api/schema.graphql --introspect-url http://127.0.0.1:8140/graphql \
  --save-introspection $G/introspection.json --out $G/contract.json
python scripts/gen_cases.py $G/contract.json --out $G/cases.json --seed 1611
python scripts/send_cases.py $G/contract.json $G/cases.json --base-url http://127.0.0.1:8140 --out $G/run.json
stop_server
python scripts/merge_findings.py $G/contract.json $G/cases.json $G/run.json --out $G/candidates.json

python scripts/validate_report.py examples/report.example.json --run $R/run.json --candidates $R/candidates.json \
  --contract $R/contract.json --cases $R/cases.json --diff $R/diff.json --repo examples/clinic-api
python scripts/validate_report.py examples/report.graphql.example.json --run $G/run.json --candidates $G/candidates.json \
  --contract $G/contract.json --cases $G/cases.json --repo examples/clinic-api
