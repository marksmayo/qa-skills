---
name: db-migration-reviewer
description: Review database migrations before they ship and find the ones that are dangerous in production - locks that block a busy table, table rewrites, data loss and silent narrowing, down migrations that can't roll back, and schema changes that break code still running during a rolling deploy. Reads Alembic, Django, Knex, Flyway, Prisma and raw SQL migrations for PostgreSQL and MySQL, sizes each table from real statistics, searches the code for readers of dropped or renamed columns, and can run the migrations on a throwaway local database to time them, watch the locks and test the rollback. Never runs against production, never edits a migration, and never says "safe to deploy" without saying what was checked.
version: 1.2.0
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
9. **What the rules miss, the review still reports.** A hazard found by reading the migration (SQL inside
   a `DO` block or built in a function, a narrowing the type history can't show) becomes a finding with
   `"source": "agent"` and a `basis` of `{file, line, excerpt}` lines that show it. It cites no candidate,
   is rated like any other finding, and is labelled "found by review".

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | The repository |
| `migrations` | the pending set | `--since <revision>` (everything after the deployed head; for Prisma the folder name of the last applied migration), `--migrations a,b`, or all. An id that matches nothing is an error |
| `chain` | the only one | Alembic: a repository with several script locations (one `env.py` and `versions/` per service) has several chains; detection lists them and `--chain <dir>` reviews one per run |
| `base_ref` | none | The git revision still running during the deploy (the deployed tag or commit). `--base-ref` also searches readers there, so code removed in this release but still running in the old one counts |
| `exclude_dir` | archive folders | Folders that look like archived or squashed history (`archive`, `squashed`, `history`, `old`) are left out of the chain and listed in `excluded`; `--exclude-dir` adds folders, `--include-archived` keeps them |
| `engine` | detected | From URLs in config, infrastructure templates (CloudFormation or Terraform `Engine`/`EngineVersion`, including a parameter's default) and docker-compose or Dockerfile image tags (with or without a registry prefix); the oldest version from the most production-like source, every claim listed; with no version, the oldest the repo claims, marked `assumed`. A version found only in docker-compose, a Dockerfile or CI is a development source and is also `assumed` (with `assumed_reason`) until the user or infrastructure confirms it. Azure Bicep and ARM templates (`Microsoft.DBforPostgreSQL/flexibleServers` `version`, including a param default) are read too; infrastructure in another repository is read with `--infra <path>`, and a version given with `--engine-version` keeps its provenance with `--engine-source` |
| `table_stats` | none | JSON `{"source": ..., "tables": {name: {"rows", "bytes"}}}`, keyed by the plain table name (`Order`, as `pg_class.relname` returns it); quoted names in migrations match it. A table with no statistics but evidently bounded rows may carry a size hint instead: `{"hint": "...", "class": "small"}` (findings on it stay `assumed`) |
| `deploy_model` | `rolling` | `rolling`, `blue_green` or `stop_the_world`; record where it came from. When parts deploy separately, list them in `scope.components` and use the most exposed |
| `dynamic` | `auto` | `container` when Docker or Podman is present, a loopback `--dsn`, or `off`; otherwise static only, and the report says so |
| `seed_rows` | 100 000 | Synthetic rows for tables the candidates name (1 000 for the rest) |
| `other_readers` | none | Other services or reporting queries that use the same database |
| `squawk` | none | Squawk SARIF output, merged as candidates |

## Workflow

Everything is stdlib except the dynamic check, which needs `psycopg` (`requirements.txt`; ask first).

```bash
python scripts/detect_migrations.py --repo <repo> --since <deployed head> --out <run>/migrations.json [--chain <script location>] [--infra ../infra]
python scripts/scan_migrations.py --migrations <run>/migrations.json --repo <repo> --table-stats stats.json --out <run>/candidates.json [--base-ref <deployed tag>] [--squawk squawk.sarif] [--other-readers ../reports]
python scripts/dynamic_check.py --migrations <run>/migrations.json --candidates <run>/candidates.json --mode container --out <run>/dynamic.json
```

Directory walks prune `node_modules`, virtual environments, build output and VCS folders before descending,
so a JavaScript repository with dependencies installed scans in seconds. `python scripts/self_test.py`
checks the parsers and rules against the examples.

1. **Detect.** The tool, the engine and version (and how they were found), the chain and its heads, and
   the pending set. Each migration becomes SQL-shaped statements with lines, its transaction mode, and
   the state of its down migration (`ok`, `pass`, `empty`, `raises`, `missing`). Alembic calls into the
   migration's own helper functions are followed, and f-strings built from literal arguments are resolved.
   Code the parser can't analyse (runtime-built SQL, `RunPython` bodies, `op.get_bind()` and
   `conn.execute` data steps, SQLAlchemy Core statements, functions imported from the application, files
   read at run time, Java migrations) is listed under `not_analysed`, never guessed at; read every entry. For every
   column a pending migration retypes, `type_history` lists the types it had before: earlier migrations,
   archived migrations, and for Prisma `schema.prisma` before the first pending migration (from git).
2. **Scan.** The rules in `data/rules.json` give candidates with their facts: table size and its
   source, whether the table was created in the same release, whether a default is constant, code
   readers of a renamed or dropped column (`find_readers.py`), code that inserts without a column made
   NOT NULL, and whether a `lock_timeout` is set anywhere. Quoted identifiers are compared unquoted, and
   each clause of a multi-clause `ALTER TABLE` is checked on its own. A type change is compared with
   every earlier type on every upgrade path (`from_path`, `from_source`), so a column that is already
   `NUMERIC` on fresh databases but still `DOUBLE PRECISION` on older ones is a narrowing. Down
   migrations are scanned for data loss too (`direction: "down"`). A value renamed in place (`UPDATE t
   SET c = 'new' WHERE c = 'old'`) is checked against code naming the old value, and with `--base-ref`
   every reader search also covers the previous release.
   Then **read the pending migrations yourself** for what no rule matches (SQL in `DO` blocks or
   functions, the `not_analysed` entries, CHECK lists that drop a value) and record it as agent findings
   (rule 9).
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
   Record the **rollback path** (`scope.rollback_path`): read the pipeline. If no stage runs the down
   migrations, or a reviewed down is not `ok`, the real rollback is a backup restore (say which, and what
   it loses: writes since the backup) or rolling forward; never write `down_migrations` unless every
   reviewed down works and something runs it.
7. **Validate and render.**

```bash
python scripts/validate_migration_review.py <run>/migration_review.json --candidates <run>/candidates.json --migrations <run>/migrations.json [--dynamic <run>/dynamic.json] --repo <repo> [--write-summary]
python scripts/render_report.py <run>/migration_review.json [--brief]
```

## Edge cases

- **Data steps** (`RunPython`, `op.execute` loops, `op.get_bind()` with `conn.execute`, Rails data in
  `change`): flag whole-table updates and missing reverses; list the rest as not analysed.
- **Data steps that read repository files** (seed YAML or CSV opened at run time) give a
  `MIGRATION-READS-FILES` candidate (`reproducibility`): when the files are later moved or deleted, the
  step silently does nothing on every database built afterwards. The candidate lists the paths that no
  longer exist.
- **Several Alembic chains** (one script location per service, often with the same ids): review each with
  `--chain`; the lock-timeout check reads that chain's `env.py` only. A shared database between the
  chains is an `other_readers` question: pass the other service's folder.
- **Value renames** (a lookup or enum-like value renamed by UPDATE, or a CHECK list rewritten): the old
  value in code still running breaks as surely as a renamed column. Pass `--base-ref` so the previous
  release's code is searched; a hit there carries `rev` and is re-read with `git show`.
- **Several components** (API, worker or function apps, a frontend deployed by another pipeline): record
  each in `scope.components` with its deploy model and when it deploys relative to the migration, rate
  with the most exposed, and put cross-component ordering ("deploy with the worker change") in the safer
  sequence.
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
- **Prisma.** A migration's id is its folder name (`20260101120000_add_orders`) and the chain is the
  folder named by `prisma.config.*` `migrations.path` (else the one holding `migration_lock.toml`), in
  folder order. Prisma has **no down migrations**: rollback is rolling forward or restoring a backup, so
  the release gets one `IRREVERSIBLE` candidate, not one per migration; a reverse script can be drafted
  with `prisma migrate diff`. `prisma migrate deploy` sends each `migration.sql` as one script, which
  PostgreSQL runs as one transaction: every lock is held until the end of the file, so put a long
  rewrite or index build in a migration of its own (a migration holding only `CREATE INDEX
  CONCURRENTLY` runs outside a transaction). "Pending" means not in `_prisma_migrations`; without
  database access, ask for the last applied folder and pass `--since`. A squashed baseline marked
  applied with `prisma migrate resolve --applied` means older databases took a different path: their
  columns can still have the archived types, so check `type_history` for both paths. A shadow database
  (`postgres-shadow` in docker-compose) is a development aid, not evidence of the production version.
  Drift between `schema.prisma` and the chain is checked with `prisma migrate diff
  --from-migrations <dir> --to-schema-datamodel <schema> --shadow-database-url <url>`; without a shadow
  database, list it under `not_checked`. Readers are searched by column and by Prisma field name
  (`@map`), and writers include `prisma.<model>.create`, `createMany` and `upsert`; a data object that is
  a variable or a spread can't be read and is counted, not guessed.
- **Serverless deploys** (Vercel and similar) usually run `prisma migrate deploy` as a build or release
  step while the previous deployment keeps serving: treat that as `rolling`.

## Output fields

`scope` (repo, tool, engine with source and `assumed_reason`, deploy model with source, `components`,
`rollback_path`, `chain`, migrations reviewed, excluded archive files, checked, not_checked), `metadata` (tools and status, table stats source), `migrations[]` (file, transaction mode,
down state, findings), `findings[]` (rule, category, statement and line, table, column, engine range,
effect, size with source (and `hinted`), hazard, size factor, exposure, score, priority, floor, cap,
confidence, code hits (with `rev` for a hit in the previous release), `direction`, rewrite, verification; `source: "agent"` and `basis` for a finding found by review), `triage[]`, `dynamic` (status and reason, or per-migration timings, locks,
probes, row changes and rollback comparison), `safe_sequence[]`, `headline` (counts, migrations with a
P0, verdict, statement, scope statement), `limitations[]`.

## What the validator enforces

Findings match their candidates (rule, file, line, statement, table, size, code hits); every cited
line exists and names the finding's column or table; an agent finding's basis excerpts are on their
lines. An engine detection marked `assumed` can't be reported as known from the same source. Hazard, size factor, exposure, score, priority, floors and caps are recomputed. Unknown
sizes rate as large; a finding depending on an unknown is `assumed`, and an `assumed` finding is never
P0. Compatibility findings have code hits outside tests and migrations. A `measured` finding has a
dynamic result, and dynamic results come only from a `qa_migrate_` database on loopback with no row
values. Every candidate is triaged once, dismissals use a reason code the facts support, and data-loss
dismissals need zero code readers. The headline counts and verdict are recomputed, it has a scope
statement, and its statement can't contradict the verdict ("safe to deploy", "no findings"). Code hits
at another revision are re-read with `git show`; a hinted size keeps a finding `assumed`; a finding keeps
its candidate's direction; no component may be more exposed than `deploy_model`; and a rollback path of
`down_migrations` needs every reviewed down to be `ok`.
