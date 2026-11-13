#!/usr/bin/env bash
# The real run behind test_plan.example.json, from the repository root. The sample repository is
# rebuilt by make_sample_repo.py (deterministic: HEAD is d834c7e every time) in a scratch directory.
set -euo pipefail
SKILL=risk-based-test-planner-skill
EX=$SKILL/examples
RUN=$EX/run
LARDER=${LARDER:-$(mktemp -d)/larder}

python $EX/make_sample_repo.py "$LARDER"
python $SKILL/scripts/mine_history.py --repo "$LARDER" --days 180 --as-of 2026-11-01 --out $RUN/history.json
python $SKILL/scripts/complexity.py "$LARDER" --out $RUN/complexity.json
# The release base: the nearest release tag merged into HEAD (v1.4 here; no changesets in this sample).
python $SKILL/scripts/release_scope.py --repo "$LARDER" --out $RUN/release.json
python $SKILL/scripts/diff_scope.py --repo "$LARDER" --base v1.4 --committed-only --out $RUN/scope.json
python $SKILL/scripts/score_risk.py --repo "$LARDER" --history $RUN/history.json --complexity $RUN/complexity.json \
    --scope $RUN/scope.json --release $RUN/release.json --journeys $EX/inputs/journey_map.json \
    --input security=$EX/inputs/security_audit.json --out $RUN/risk.json

# The plans add the strategy, what is not tested and why, and the criteria; the computed fields
# (scores, bands, effort, summary) are then filled in and checked, and the Markdown plan written.
for plan in test_plan.example test_plan.one-session.example; do
  python $SKILL/scripts/validate_test_plan.py $EX/$plan.json --risk $RUN/risk.json \
      --input security=$EX/inputs/security_audit.json --write-summary
  python $SKILL/scripts/render_report.py $EX/$plan.json --markdown $EX/$plan.md
done
