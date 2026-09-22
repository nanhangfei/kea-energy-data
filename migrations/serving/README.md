# Serving migrations and publication

The pipeline publishes directly to the PostgreSQL database used by the API.
The Data team owns `common`, `metering`, `billing`, `catalogue`, `kea`, and
the new `data_ops` migration/publication metadata schema. Backend Alembic
continues to own its application tables.

Configure `data/.env` with two explicit connections to the **same database**:

```dotenv
KEA_MIGRATION_DSN=postgresql://MIGRATOR:PASSWORD@HOST:5432/kea
KEA_PUBLISH_DSN=postgresql://LOADER:PASSWORD@HOST:5432/kea
```

Use a DDL-capable owner for migrations and a loader with the existing
`kea_loader` privileges for publication. These hosts must be reachable from
the data Docker network. Keep credentials outside Git.

Locally the database is `kea-db` from `../infra` (`make db` at the repository
root). Containers on the data network reach it as `host.docker.internal:5432`;
every target that talks to it adds `--add-host=host.docker.internal:host-gateway`,
which is what makes that name resolve on Linux. `kea-source-db`, in this
project's own stack, holds only the four source systems.
`pipeline/migrations/2026-09-12_drop_serving_copy.sql` removes the old copy of
the served schemas from an existing volume.

From `data/`, initialize an empty target:

```bash
make migrate-serving
make configure-serving-roles
```

For an existing database, first review its columns, types, constraints and
indexes against `V001__initial_schema.sql`, then run once:

```bash
make migrate-serving ARGS=--baseline
make configure-serving-roles
```

Baseline verifies that the expected tables exist and records V001 without
recreating them. It does **not** prove full schema equivalence or repair an
older schema. Resolve any differences with reviewed migrations first.
The second command sets `kea_loader` and `kea_app` passwords from the ignored
`.env`; secrets are deliberately not stored in SQL migration files.

V003 makes the Sprint 3 foreign keys deferrable, which a publication needs.
Add future changes as `V004__description.sql`, `V005__description.sql`, etc.
Never edit an applied migration: its checksum is verified on every run.
V001 is the frozen initial DDL; `design/kea_serving_schema.sql` remains the
readable current schema reference. Update that reference with future changes.
Use additive changes, backfill data, deploy compatible API code, and only
then remove old columns in a later release. Each migration commits separately;
failure rolls back that migration, leaving earlier migrations applied.

Prepare a complete, quality-checked export using the existing warehouse flow:

```bash
make dbt-build
make export-warehouse
make publish-serving RELEASE=sprint3-2026-09-11-01
```

Publication uses the existing MinIO export and does not require a PostgreSQL
dump. Stop scheduled exports while publishing: the current export paths are
mutable, and their `_SUCCESS` markers are per table, not a release manifest.
An immutable, versioned export manifest is a future improvement. Use a new
release identifier whenever the source export changes.

The publisher checks migration checksums, loads all base tables and meter
partitions, computes forecasts/recommendations/hardship, verifies bill
reproduction, creates pending in-app alerts, validates required outputs and foreign keys, and records the
release in one PostgreSQL transaction. It sends no messages. A failed data
publication rolls back all its table changes. Retrying a successful release
identifier is a no-op. Migration and publication processes share an advisory
lock; legacy loaders must not run concurrently with this workflow.

Existing recommendation/assessment history and application data are retained.
Removing accounts referenced by retained history can fail foreign-key checks;
that requires an explicit retention policy, not automatic history deletion.
The one exception is `make rebuild-demo`, which truncates the six decision
tables as the owner before it republishes, so a demo starts from a single
generation. It refuses a `KEA_MIGRATION_DSN` whose host is not local.
The transaction may block API reads while it holds table/partition locks.
Schedule a maintenance window for large loads. PostgreSQL sequence increments
may leave harmless gaps after rollback.

Inspect successful releases:

```sql
SELECT release_id, published_at, row_counts
FROM data_ops.serving_publication ORDER BY published_at DESC;
```

To recover from a bad *successful* data release, restore a reviewed previous
export and publish under a new release ID. Keep backups; current mutable lake
exports do not provide automatic historical rollback. A failed publication
does not undo already committed schema migrations.
