#!/usr/bin/env bash
# How examples/run/ was produced (bash, from bug-reproducer-skill/). Needs Playwright with Chromium,
# Firefox and WebKit, and Node 20+ for the regression test. The app is served on loopback:
#   python -m http.server 8141 --bind 127.0.0.1 --directory examples/tasklist
# Results vary with the machine: TASK-12 is a race, and its counts will differ.
set -e
python scripts/fetch_ticket.py --from-file examples/reports --source connector --out examples/run/tickets.json
python scripts/triage.py --ticket examples/run/tickets.json --out examples/run/triage.json
for k in TASK-11 TASK-12 TASK-13 TASK-14 TASK-15; do
  python scripts/dupes.py --ticket examples/run/tickets.json --key $k --candidates examples/run/tickets.json --out examples/run/dupes-$k.json
done
for k in TASK-11 TASK-12 TASK-14 TASK-15; do
  python scripts/reproduce.py examples/specs/$k.json --out examples/run/attempts/$k.json --artifacts examples/run/artifacts/$k --base examples
done
python scripts/minimise.py examples/specs/TASK-11.json --candidate A1 --out examples/run/minimal/TASK-11.json \
  --artifacts examples/run/artifacts/TASK-11 --base examples
python scripts/minimise.py examples/specs/TASK-12.json --candidate A1 --runs 3 --need 2 --out examples/run/minimal/TASK-12.json \
  --artifacts examples/run/artifacts/TASK-12 --base examples
# The regression test (examples/tasklist/tests/due_today_timezone.test.mjs) was written after TASK-11 reproduced.
python scripts/run_tests.py --preset custom \
  --cmd "node --test --test-reporter=junit --test-reporter-destination={junit} tests/due_today_timezone.test.mjs" \
  --cwd examples/tasklist --out examples/run/regression/runs.json --label "TASK-11 regression test, red" --run-prefix R || true
python scripts/validate_repro.py examples/repro_report.example.json --tickets examples/run/tickets.json --run examples/run
