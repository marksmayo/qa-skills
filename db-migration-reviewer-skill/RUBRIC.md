# Rubric: database migration reviewer

## Hazard (from the rule's category, `data/rules.json`)

| Category | Hazard | Examples |
|---|---|---|
| `data_loss` | 4 | DROP TABLE, DROP COLUMN, TRUNCATE, narrowing a type (NUMERIC to INTEGER, BIGINT to INTEGER, TEXT to VARCHAR(n), DOUBLE PRECISION or REAL to NUMERIC(p,s): rounding to s places and overflow at 10^(p-s), NUMERIC(p,s) to a smaller scale or integer part), UPDATE or DELETE without WHERE |
| `blocking_lock` | 3 | CREATE INDEX without CONCURRENTLY, ALTER COLUMN TYPE with a rewrite, ADD COLUMN with a volatile default (or any default before PostgreSQL 11), SET NOT NULL without a validated CHECK, ADD FOREIGN KEY without NOT VALID, MySQL ALGORITHM=COPY |
| `compatibility` | 3 | A rename or drop while code still reads the column; NOT NULL while code inserts without it |
| `irreversible` | 2 | A down migration that is `pass`, empty, raises or is missing (Prisma: one for the release, since it has no down migrations) |
| `lock_queueing` | 2 | DDL with no `lock_timeout`: one slow query makes every later query wait behind the migration |
| `transaction_mistake` | 2 | CREATE INDEX CONCURRENTLY inside a transaction; MySQL DDL that commits half a migration |
| `backfill_cost` | 2 | One UPDATE across a large table in one transaction, unbatched |
| `ordering` | 1 | Two heads; a migration that depends on a later one |
| `reproducibility` | 3 | A data step that reads repository files at run time (seed YAML, CSV): once the files move or are deleted, the step silently does nothing on every database built later |

A value renamed in place (`UPDATE t SET c = 'new' WHERE c = 'old'`, `VALUE-RENAME-WHILE-READ`) is
`compatibility`: code that still compares with `'old'` (in the previous release, during a rolling
deploy) stops matching. Statements in a down migration are rated like any other (`direction: "down"`).

A finding found by review (`source: "agent"`) with a rule from `data/rules.json` takes that rule's
category and hazard; with a rule of its own it takes its category's hazard from this table.

A narrowing counts on any upgrade path: if the column is already the new type on databases built from
a baseline but still the old type on databases older than a squash (archived migrations, or the previous
`schema.prisma`), the finding stands and the effect says which databases lose data.

## Table size

| Class | Rows | Factor |
|---|---|---|
| large | over 1 000 000, or over 1 GB | 3 |
| unknown | no statistics | 3, and the finding is `assumed` |
| medium | 10 000 to 1 000 000 | 2 |
| small | under 10 000, or created in this release | 1 |

`data_loss`, `compatibility` and `reproducibility` always use 3: losing a small table still loses data,
a broken query breaks whatever the table's size, and a step that silently does nothing misses every row.

**Size hint.** Without statistics, a table whose rows are evidently bounded (a lookup or configuration
table written only by seed migrations and an admin tool) can be given a hint in the statistics file:
`{"hint": "seeded configuration table, rows bounded by the seed files", "class": "small"}`. The size is
then `small` (or `medium`) with `hinted: true`, its source says "size hint", and a finding resting on it
stays `assumed`: a hint is a reason, not a measurement.

## Deploy exposure

| Model | Factor |
|---|---|
| `rolling` (old and new code run together) | 3 |
| `blue_green` | 2 |
| `stop_the_world` (maintenance window) | 1 |

`data_loss` always uses 3: a maintenance window makes locks less harmful but doesn't bring data back.
`reproducibility` always uses 1: it bites when a database is built or rebuilt, not in the deploy window.

When parts of the system deploy separately (API, workers, functions, a frontend), list them in
`scope.components` with their own deploy model and order, and rate with the most exposed one that uses
the database.

## Score and priority

`score = hazard x size x exposure`

| Priority | Score |
|---|---|
| P0 | 27 or more |
| P1 | 18 to 26 |
| P2 | 8 to 17 |
| P3 | under 8 |

**Floors.** Unrecoverable data loss (`recoverable: false`: no backup step and no faithful down) is P0
(`unrecoverable_data_loss`). A compatibility break with code hits is at least P1
(`compatibility_with_code_hits`).

**Cap.** An `assumed` finding is at most P1 (`assumed_capped_at_P1`). The floor applies first, then the cap.

**Recording a floor.** `floor` names a floor only when it applies: `unrecoverable_data_loss` on every
unrecoverable data-loss finding, `compatibility_with_code_hits` only when it raised the priority (a
compatibility finding whose score already gives P0 or P1 has `floor: null`). The validator recomputes both.

## Confidence

| Level | Meaning |
|---|---|
| `measured` | The dynamic run showed the lock, the blocked query, the failed rollback or the changed rows |
| `matched` | A rule matched the statement, and the engine version and table size are known |
| `assumed` | A condition rests on an unknown: engine version (including one taken only from docker-compose, a Dockerfile or CI) or table size (including a size hint) |

## Verdict

`do_not_deploy` with any P0; `deploy_with_changes` with any P1 or P2; otherwise
`no_blocking_findings`, which still carries the scope statement and never reads as "safe".

## Dismissal reason codes

| Code | The facts must show |
|---|---|
| `table_created_in_release` | The table is created by a pending migration, so it is empty when locked |
| `constant_default_pg11` | The default is not volatile and the engine is PostgreSQL 11 or later |
| `intended_contract_step` | A planned drop after the code stopped using the column: zero code readers. The only code allowed for data-loss candidates |
| `validated_check_present` | A validated CHECK (col IS NOT NULL) exists before SET NOT NULL |
| `online_schema_tool` | The repo runs this operation through gh-ost, pt-online-schema-change or pgroll |
| `not_applicable_engine_version` | The rule's version range excludes the engine version |
| `duplicate` | Another candidate covers the same statement and effect |
| `false_match` | The statement doesn't do what the rule matched; say why |
