#!/usr/bin/env bash
# How the example's charters and debrief were made, from the repository root. The draft charters
# (run/draft.json) and the debrief's judgements are written by the agent; everything else is computed.
set -euo pipefail
SKILL=exploratory-charter-writer-skill
EX=$SKILL/examples

python $SKILL/scripts/fetch_ticket.py --from-file $EX/inputs/GIFT-7.issue.json --source connector --out $EX/inputs/ticket.json
python $SKILL/scripts/gather_sources.py --ticket $EX/inputs/ticket.json --journeys $EX/inputs/journey_map.json \
    --plan $EX/inputs/test_plan.json --scope $EX/inputs/scope.json --out $EX/run/sources.json
python $SKILL/scripts/rank_charters.py $EX/run/draft.json --sources $EX/run/sources.json \
    --out $EX/charters.example.json --md-dir $EX/charters
python $SKILL/scripts/validate_charters.py $EX/charters.example.json --sources $EX/run/sources.json --md-dir $EX/charters \
    --personas $EX/inputs/journey_map.json

python $SKILL/scripts/parse_notes.py $EX/notes/session-01.md --out $EX/run/parsed-session-01.json
# debrief.example.json is written from parsed-session-01.json, then checked:
python $SKILL/scripts/validate_debrief.py $EX/debrief.example.json --notes $EX/notes/session-01.md \
    --parsed $EX/run/parsed-session-01.json --charters $EX/charters.example.json
