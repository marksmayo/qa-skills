#!/usr/bin/env bash
# The run behind examples/migration_review.prisma.example.json. Run from the repository root.
# Static only, like the ledger run: no Docker, Podman or loopback PostgreSQL on the review machine.
set -euo pipefail
S=db-migration-reviewer-skill
E=$S/examples

# 1. Tool, engine and the pending set. The baseline is deployed (and marked applied with
#    prisma migrate resolve on databases older than the squash); the archived folder is left out
#    of the chain but still feeds the column type history.
python $S/scripts/detect_migrations.py --repo $E/greenhouse --since 20260301000000_baseline --out $E/run-prisma/migrations.json

# 2. Rule candidates, sized from the staging export (keyed by plain table names)
python $S/scripts/scan_migrations.py --migrations $E/run-prisma/migrations.json --repo $E/greenhouse \
  --table-stats $E/greenhouse/table_stats.json --out $E/run-prisma/candidates.json

# 3. Validate the review against the run
python $S/scripts/validate_migration_review.py $E/migration_review.prisma.example.json --candidates $E/run-prisma/candidates.json \
  --migrations $E/run-prisma/migrations.json --repo $E/greenhouse
