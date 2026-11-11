# Database Migration Reviewer Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It reviews database migrations before they ship and finds the ones that are dangerous in production:

- **Locks and rewrites**: indexes built without CONCURRENTLY, type changes that rewrite the table,
  SET NOT NULL scans, foreign keys validated in one go, defaults that rewrite, MySQL copy ALTERs,
  DDL with no lock timeout.
- **Data loss**: drops, truncates, type narrowing, updates without WHERE.
- **Rollback**: down migrations that are `pass`, empty, raise or are missing.
- **Compatibility**: renames and drops while code still reads the column, NOT NULL while code still
  inserts without it, found by searching the code (tests and migrations don't count).

Each finding names the file, line, statement, engine version range and the lock or data effect, is
rated by table size (from real statistics, never guessed) and deploy model, and comes with a safer
rewrite and a way to verify it. The report lays out a safer sequence over releases.

It can also run the migrations on a throwaway local PostgreSQL (a container it starts, or a loopback
server) to time them, watch the locks, probe for blocked queries and test the rollback. It never
connects to anything else, never prints row values, and never edits a migration.

Supported: Alembic, Django, Knex, Flyway, Prisma and raw SQL; PostgreSQL and MySQL rules (MariaDB and SQLite
get engine-neutral rules only).

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/db-migration-reviewer/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/db-migration-reviewer/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/db-migration-reviewer/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+; the dynamic check
needs `psycopg` (`pip install -r requirements.txt`) and Docker, Podman or a loopback PostgreSQL.

## Usage

> Review the migrations in this release before we deploy. 0042 is what's in production; we deploy with a rolling restart.

```bash
python scripts/detect_migrations.py --repo . --since 0042 --out run/migrations.json
python scripts/scan_migrations.py --migrations run/migrations.json --repo . --table-stats stats.json --out run/candidates.json
python scripts/dynamic_check.py --migrations run/migrations.json --candidates run/candidates.json --mode container --out run/dynamic.json
python scripts/validate_migration_review.py run/migration_review.json --candidates run/candidates.json --migrations run/migrations.json --repo .
python scripts/render_report.py run/migration_review.json [--brief]
```

A statistics file for PostgreSQL (row counts only):

```sql
SELECT relname, reltuples::bigint AS rows, pg_total_relation_size(oid) AS bytes
FROM pg_class WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace;
```

## Example

`examples/ledger/` is a small Flask and SQLAlchemy app with Alembic migrations against PostgreSQL 16.4
and planted problems; `examples/run/` is the run (listed in `examples/run/commands.sh`);
`examples/migration_review.example.json` is the review. `examples/README.md` has the answer key.

```bash
python scripts/validate_migration_review.py examples/migration_review.example.json --candidates examples/run/candidates.json --migrations examples/run/migrations.json --repo examples/ledger
python scripts/render_report.py examples/migration_review.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: hazard, size, exposure, priority, floors, confidence, dismissal codes
- `schemas/migration_review.schema.json`: output contract
- `data/rules.json`: the rule catalogue (category, hazard, engine, effect, safer rewrite)
- `data/images.lock.json`: database images for the dynamic check
- `scripts/detect_migrations.py`: tool, engine, chain, pending set, statements with lines, down state
- `scripts/split_sql.py`: statement splitter (quotes, comments, dollar quoting, DELIMITER)
- `scripts/scan_migrations.py`: rules to candidates, with facts; merges Squawk SARIF
- `scripts/find_readers.py`: code that reads a column, or inserts without it
- `scripts/dynamic_check.py`: throwaway database, timings, locks, blocked queries, rollback comparison
- `scripts/validate_migration_review.py`, `scripts/render_report.py`
- `scripts/diff_scope.py`, `scripts/sarif.py`: shared copies (edit them in `shared/`)
- `examples/`: the app, the run and the review

## Design principles

1. Size and engine come from evidence; unknowns are rated as the worst case and labelled.
2. Think about the deploy window, when old code meets the new schema.
3. Quote statements, never data; never touch a database the skill didn't create.
4. Say what was checked; never call a migration safe.
