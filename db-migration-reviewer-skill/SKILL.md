---
name: db-migration-reviewer
description: Review database migrations before they ship and find the ones that are dangerous in production - locks that block a busy table, table rewrites, data loss and silent narrowing, down migrations that can't roll back, and schema changes that break code still running during a rolling deploy. Reads Alembic, Django, Knex, Flyway, Prisma and raw SQL migrations for PostgreSQL and MySQL, sizes each table from real statistics, searches the code for readers of dropped or renamed columns, and can run the migrations on a throwaway local database to time them, watch the locks and test the rollback. Never runs against production, never edits a migration, and never says "safe to deploy" without saying what was checked.
version: 1.0.0
---

# Database migration reviewer

## Mission

Answer four questions about the pending migrations, before they ship:

- Does this lock a busy table long enough to cause an outage?
- Does it destroy or silently change data?
- Can it be rolled back?
- Will code that's still running during the deploy break against the new schema?

Then give a safer sequence: expand and contract, split over releases.

The code security auditor reads SQL for injection; this skill reads schema changes for availability and
data safety. The test skills check behaviour; this skill checks the deploy itself, the window where old
code, new code and the new schema coexist.

## Non-negotiable rules

1. **Every finding names the migration file, the statement and line, the engine and version range, and
   the lock or data effect** ("ACCESS EXCLUSIVE on transactions while the table is rewritten").
2. **A guess about table size isn't evidence.** Size comes from the user, a statistics export
   (`pg_class.reltuples`, `information_schema.tables`) or the dynamic run. Without one it is `unknown`,
   rated as large, and a finding that depends on it is `assumed`.
3. **Never against production.** The dynamic check runs only in a container it starts or on a loopback
   server, inside a database it creates (`qa_migrate_<random>`) and drops. Any other host is refused,
   unless the user names a disposable non-production copy in `authorised_by`; even then the skill only
   works inside a database it created.
4. **Quote the statement, never the data.** The dynamic check records counts, hashes, lock modes and
   times, never row values.
5. **The skill never edits migrations.** Safer rewrites are suggested code in the report.
6. **"No findings" is not "safe to deploy".** The headline carries a scope statement: which migrations,
   engine and code paths were checked, and what wasn't (runtime-built SQL, stored procedures, other
   services sharing the database, durations if the dynamic check didn't run).
7. **Compatibility findings need code hits.** Hits in tests and in the migrations themselves are listed
   apart and never count.
8. **Every candidate is triaged**: promoted to a finding or dismissed with a reason code. A data-loss
   candidate may only be dismissed as an intended contract step with zero code readers.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | The repository |
| `migrations` | the pending set | `--since <revision>` (everything after the deployed head), `--migrations a,b`, or all |
| `engine` | detected | From URLs in config, infrastructure templates (CloudFormation or Terraform `Engine`/`EngineVersion`, including a parameter's default) and docker-compose or Dockerfile image tags (with or without a registry prefix); the oldest version from the most production-like source, every claim listed; with no version, the oldest the repo claims, marked `assumed` |
| `table_stats` | none | JSON `{"source": ..., "tables": {name: {"rows", "bytes"}}}` |
| `deploy_model` | `rolling` | `rolling`, `blue_green` or `stop_the_world`; record where it came from |
| `dynamic` | `auto` | `container` when Docker or Podman is present, a loopback `--dsn`, or `off`; otherwise static only, and the report says so |
| `seed_rows` | 100 000 | Synthetic rows for tables the candidates name (1 000 for the rest) |
| `other_readers` | none | Other services or reporting queries that use the same database |
| `squawk` | none | Squawk SARIF output, merged as candidates |

## Workflow

Everything is stdlib except the dynamic check, which needs `psycopg` (`requirements.txt`; ask first).

```bash
python scripts/detect_migrations.py --repo <repo> --since <deployed head> --out <run>/migrations.json
python scripts/scan_migrations.py --migrations <run>/migrations.json --repo <repo> --table-stats stats.json --out <run>/candidates.json [--squawk squawk.sarif] [--other-readers ../reports]
python scripts/dynamic_check.py --migrations <run>/migrations.json --candidates <run>/candidates.json --mode container --out <run>/dynamic.json
```

1. **Detect.** The tool, the engine and version (and how they were found), the chain and its heads, and
   the pending set. Each migration becomes SQL-shaped statements with lines, its transaction mode, and
   the state of its down migration (`ok`, `pass`, `empty`, `raises`, `missing`). Code the parser can't
   analyse (runtime-built SQL, `RunPython` bodies, Java migrations) is listed, never guessed at.
2. **Scan.** The rules in `data/rules.json` give candidates with their facts: table size and its
   source, whether the table was created in the same release, whether a default is constant, code
   readers of a renamed or dropped column (`find_readers.py`), code that inserts without a column made
   NOT NULL, and whether a `lock_timeout` is set anywhere.
3. **Dynamic (when possible).** Throwaway database, earlier migrations, synthetic seed, then each pending
   migration with lock polling every 100 ms and a concurrent SELECT and INSERT per affected table, then
   the down migration with schema, row-count and row-hash comparison. A finding the run confirms becomes
   `measured`. Measured times scale with the seed: never extrapolate to production without calling it an
   estimate.
4. **Triage.** Promote or dismiss every candidate. Checkable dismissals: `table_created_in_release`
   (the table is empty when locked), `constant_default_pg11` (no rewrite on PostgreSQL 11+),
   `intended_contract_step` (a drop after the code stopped using the column), `validated_check_present`,
   `online_schema_tool` (gh-ost, pt-osc, pgroll handle it), `not_applicable_engine_version`, `duplicate`,
   `false_match`. Say in the reason what the facts show.
5. **Rate** each finding with `RUBRIC.md`: hazard x size x exposure, floors, the `assumed` cap.
6. **Write** for each finding the effect, a safer rewrite, and a verification step someone can run on a
   copy. Lay out a safer sequence: which release each step belongs to (expand, migrate, contract).
7. **Validate and render.**

```bash
python scripts/validate_migration_review.py <run>/migration_review.json --candidates <run>/candidates.json --migrations <run>/migrations.json [--dynamic <run>/dynamic.json] --repo <repo> [--write-summary]
python scripts/render_report.py <run>/migration_review.json [--brief]
```

## Edge cases

- **Data steps** (`RunPython`, `op.execute` loops, Rails data in `change`): flag whole-table updates
  and missing reverses; list the rest as not analysed.
- **Online schema-change tools:** lock rules for operations the tool handles are dismissed with
  `online_schema_tool`.
- **Partitioned tables:** an index on a partitioned parent can't be built CONCURRENTLY (PostgreSQL);
  build it per partition, then attach.
- **Views:** dropping or retyping a column breaks dependent views; search for them in the migrations.
- **Multiple heads** (Alembic) or conflicting leaves (Django) are an ordering finding.
- **MySQL:** DDL commits implicitly, so a multi-statement migration that fails halfway stays half
  applied; type changes copy the table unless the operation is INSTANT or INPLACE.
- **MariaDB and SQLite:** engine-neutral rules only in this version; the report says so.
- **No migration tool:** plain SQL scripts are reviewed as raw SQL.

## Output fields

`scope` (repo, tool, engine with source, deploy model with source, migrations reviewed, checked,
not_checked), `metadata` (tools and status, table stats source), `migrations[]` (file, transaction mode,
down state, findings), `findings[]` (rule, category, statement and line, table, column, engine range,
effect, size with source, hazard, size factor, exposure, score, priority, floor, cap, confidence, code
hits, rewrite, verification), `triage[]`, `dynamic` (status and reason, or per-migration timings, locks,
probes, row changes and rollback comparison), `safe_sequence[]`, `headline` (counts, migrations with a
P0, verdict, statement, scope statement), `limitations[]`.

## What the validator enforces

Findings match their candidates (rule, file, line, statement, table, size, code hits) and every cited
line exists. Hazard, size factor, exposure, score, priority, floors and caps are recomputed. Unknown
sizes rate as large; a finding depending on an unknown is `assumed`, and an `assumed` finding is never
P0. Compatibility findings have code hits outside tests and migrations. A `measured` finding has a
dynamic result, and dynamic results come only from a `qa_migrate_` database on loopback with no row
values. Every candidate is triaged once, dismissals use a reason code the facts support, and data-loss
dismissals need zero code readers. The headline counts and verdict are recomputed, and it has a scope
statement.
