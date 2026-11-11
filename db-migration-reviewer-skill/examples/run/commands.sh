#!/usr/bin/env bash
# The run behind examples/migration_review.example.json. Run from the repository root.
# Static only: the review machine had no Docker or Podman and no loopback PostgreSQL, so
# dynamic_check.py was not run (the report says so). With either available, add the last step.
set -euo pipefail
S=db-migration-reviewer-skill
E=$S/examples

# 1. Tool, engine and the pending set (0001 and 0002 are deployed)
python $S/scripts/detect_migrations.py --repo $E/ledger --since 0002 --out $E/run/migrations.json

# 2. Rule candidates, with code readers and writers, sized from the staging statistics export
python $S/scripts/scan_migrations.py --migrations $E/run/migrations.json --repo $E/ledger \
  --table-stats $E/ledger/table_stats.json --out $E/run/candidates.json

# 3. (not run here) Throwaway database: timings, locks, blocked queries, rollback comparison
# pip install -r $S/requirements.txt
# python $S/scripts/dynamic_check.py --migrations $E/run/migrations.json --candidates $E/run/candidates.json \
#   --mode container --seed-rows 100000 --out $E/run/dynamic.json

# 4. Validate the review against the run
python $S/scripts/validate_migration_review.py $E/migration_review.example.json --candidates $E/run/candidates.json \
  --migrations $E/run/migrations.json --repo $E/ledger
