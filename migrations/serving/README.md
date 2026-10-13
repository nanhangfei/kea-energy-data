# Serving migrations and publication

The pipeline publishes into the PostgreSQL database the application reads. Ownership is split by
schema:

| Owner | Schemas | Changed by |
|---|---|---|
| this pipeline | `common`, `metering`, `billing`, `catalogue`, `kea`, `data_ops` | the migrations in this directory |
| the application | `app` | the application's own Alembic history |

`data_ops` holds the migration history (`schema_migration`) and the release log
(`serving_publication`).

## Setup

Two connections to the **same database**, in `.env`:

```dotenv
KEA_MIGRATION_DSN=postgresql://MIGRATOR:PASSWORD@HOST:5432/kea   # DDL-capable owner
KEA_PUBLISH_DSN=postgresql://kea_loader:PASSWORD@HOST:5432/kea   # the publisher
```

Both hosts must be reachable from the pipeline's Docker network. Credentials stay in `.env`, never in
SQL files.

**Locally**, the database is `kea-db` from [`docker-compose.serving.yml`](../../docker-compose.serving.yml),
started with `make serving-db` and reached from containers as `host.docker.internal:5432`. To reuse an
existing database instead of starting empty, set `KEA_DB_VOLUME` in `.env` to its Docker volume (for
example `infra_kea-pgdata`), stop the old container, then run `make serving-db`.

## First run

Empty database:

```bash
make publish-serving RELEASE=<id> MIGRATE=1 ROLES=1
```

Existing database that already has the tables: review its columns, types, constraints and indexes
against `V001__initial_schema.sql`, then baseline once:

```bash
make publish-serving RELEASE=<id> MIGRATE=1 MIGRATE_ARGS=--baseline ROLES=1
```

Baselining checks the expected tables exist and records V001 without recreating them. It does not
prove the schemas are equivalent; fix any differences with reviewed migrations first.

`ROLES=1` sets the `kea_loader` and `kea_app` passwords from `.env`. Migrating and setting roles are
owner steps that a publish never takes on its own; `make deploy` migrates but never sets roles.

## Writing a migration

- Name it `V009__description.sql`, `V010__description.sql`, and so on.
- **Never edit an applied migration.** Checksums are verified on every run, and `make ci` checks it.
- **Schema-qualify every name.** A baselined database never ran V001's `search_path`.
- **Declare new foreign keys `DEFERRABLE INITIALLY IMMEDIATE`.** The publication replaces parent tables
  with constraints deferred.
- **Change in compatible steps:** add, backfill, deploy compatible application code, and only then
  remove old columns in a later release.
- **Update [`design/kea_serving_schema.sql`](../../design/kea_serving_schema.sql)**, the readable
  reference for the current schema.
- **Keep [`design/serving_read_contract.json`](../../design/serving_read_contract.json) true.** It lists
  every column the application reads; the `ServingReadContract` test fails if a migration renames,
  drops or retypes one.

Each migration commits separately: a failure rolls back that migration and leaves earlier ones applied.

| Migration | Change |
|---|---|
| V001 | initial schema (frozen) |
| V002 | publication history |
| V003 | deferrable foreign keys, required by the publication |
| V004 | lets the serving gate read identity sequences as `kea_loader` |
| V005 | detected loads, solar and EV estimates, hardship early warning |
| V006 | solar + EV households: masked-run columns, nullable `charger_class`, the `solar_soak` pattern |
| V007 | `battery_kwh` on `kea.solar_estimate` |
| V008 | `battery_dispatch` on `kea.solar_estimate` (surplus-only or grid-charged battery) |

## Publishing

```bash
make build-warehouse                    # build and gate the warehouse
make publish-serving RELEASE=<new-id>   # export, publish, gate
```

**One transaction.** The publisher verifies migration checksums, loads every base table and meter
partition, computes forecasts, recommendations, hardship outputs and solar and EV estimates, checks
bill reproduction, creates pending in-app alerts, validates outputs and foreign keys, and records the
release. It sends no messages.

**Guarantees:**
- A failed publication rolls back every table change.
- Re-running a release id that succeeded is a no-op.
- Migration and publication share an advisory lock, so they never run concurrently.
- Decision history and application data are retained across releases. `make rebuild-all` is the one
  exception: it clears the decision tables first so a demo starts from a single generation, and it
  refuses a `KEA_MIGRATION_DSN` that is not local.

**Operational caveats:**
- Export paths are mutable and their `_SUCCESS` markers are per table, not a release manifest. Don't
  run scheduled exports during a publish, and use a new release id whenever the export changes.
- A large publication holds table and partition locks that can delay application reads. Schedule a
  maintenance window in production.
- Deleting accounts referenced by retained history can fail foreign-key checks. That needs an explicit
  retention policy, not automatic deletion.
- Rolled-back transactions can leave harmless gaps in sequences.

## Operations

Recent releases:

```sql
SELECT release_id, published_at, row_counts
FROM data_ops.serving_publication
ORDER BY published_at DESC;
```

**Recovering from a bad release that succeeded:** restore a reviewed earlier export and publish it under
a new release id. Lake exports are mutable and do not provide rollback, so keep database backups. A
failed publication never undoes migrations that already committed.
