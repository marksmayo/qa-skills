#!/usr/bin/env bash
# The runs behind pvt_report.example.json. Run from post-deploy-validator-skill/.
# The "production" target is examples/harbour-tickets/server.py on loopback, first as release 1.8.0 (the
# baseline run), then as 1.9.0. The smoke-reader credentials are the sample server's obviously fake ones.
set -euo pipefail
export PVT_SMOKE_READER_USERNAME=smoke-reader PVT_SMOKE_READER_PASSWORD=not-a-real-password
serve() { python examples/harbour-tickets/server.py --release "$1" --port 8160 & SERVER=$!; sleep 1; }

serve 1.8.0
python scripts/run_pvt.py examples/pvt_plan.json --expect-version 1.8.0 --out examples/runs/baseline-1.8.0
kill $SERVER; sleep 1

serve 1.9.0
python scripts/run_pvt.py examples/pvt_plan.json --expect-version 1.9.0 --out examples/runs/release-1.9.0 \
    --baseline examples/runs/baseline-1.8.0/run.json
kill $SERVER

python scripts/recommend.py examples/runs/release-1.9.0/run.json --decision-owner "Release manager" --out examples/pvt_report.example.json
# (the agent then writes the headline, titles and limitations)
python scripts/validate_pvt.py examples/pvt_report.example.json --run examples/runs/release-1.9.0/run.json --baseline examples/runs/baseline-1.8.0/run.json
python scripts/render_report.py examples/pvt_report.example.json
