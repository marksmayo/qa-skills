#!/usr/bin/env bash
# The watch behind examples/state/. Run from hypercare-anomaly-watcher-skill/.
# The monitoring data is simulated (simulate.py, fixed seed); the ticks run hourly after the 22:00 deploy.
set -euo pipefail
python examples/simulate.py --out examples/recorded
rm -rf examples/state
python scripts/watch.py init --config examples/hypercare.json --state examples/state
for t in 2026-11-11T23 2026-11-12T00 2026-11-12T01 2026-11-12T02 2026-11-12T03 2026-11-12T04 2026-11-12T05; do
  python scripts/watch.py tick --state examples/state --now "$t:00:00Z" --from-dir examples/recorded
done
# The agent then adds the headline, limitations and suggested actions to watch.json and the draft, and rewrites the Markdown:
python scripts/render_report.py examples/state --write-drafts
python scripts/validate_watch.py examples/state --recorded examples/recorded
python scripts/render_report.py examples/state
