#!/usr/bin/env bash
# The two runs behind examples/runs/. Run from ci-suite-runner-skill/.
# examples/ci-stub/server.py stands in for both CI APIs on loopback and runs the requested tests for real
# with pytest. The tokens are the stub's fake ones (it needs a token, any token).
set -euo pipefail
export GITHUB_TOKEN=fake-ci-token AZURE_DEVOPS_EXT_PAT=fake-ci-token
python examples/ci-stub/server.py --port 8170 & STUB=$!; sleep 1
A=http://127.0.0.1:8170
rm -rf examples/runs

# 1. checkout-regression on staging: covered by grant PA-1, so it is triggered without asking
python scripts/run_suite.py plan --catalogue examples/suites.json --suite checkout-regression --environment staging \
    --tags "checkout and not slow" --out examples/runs/checkout-staging --api-base $A/github
python scripts/run_suite.py trigger --run examples/runs/checkout-staging --preauth examples/preauth.json
python scripts/run_suite.py poll --run examples/runs/checkout-staging --poll-seconds 1
python scripts/run_suite.py collect --run examples/runs/checkout-staging

# 2. smoke on production (protected): refused under the pre-authorisation, then sent with a confirmation
python scripts/run_suite.py plan --catalogue examples/suites.json --suite smoke --environment production \
    --out examples/runs/smoke-production --api-base $A/azure
python scripts/run_suite.py trigger --run examples/runs/smoke-production --preauth examples/preauth.json || true
python scripts/run_suite.py trigger --run examples/runs/smoke-production --confirmed-by "Release manager, in chat on 2026-11-12, for the 2.4.0 post-deploy smoke"
python scripts/run_suite.py poll --run examples/runs/smoke-production --poll-seconds 1
python scripts/run_suite.py collect --run examples/runs/smoke-production

curl -s $A/stub/log > examples/runs/stub_log.json     # what the stub received
kill $STUB
# The agent then adds each run's headline, next steps (failed tests go to ci-failure-triager) and limitations, and validates:
#   python scripts/validate_suite_run.py examples/runs/checkout-staging/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
#   python scripts/validate_suite_run.py examples/runs/smoke-production/suite_run.json --preauth examples/preauth.json --stub-log examples/runs/stub_log.json
