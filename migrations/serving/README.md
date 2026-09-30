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

From `data/`, initialize an empty target and publish its first release:

```bash
make publish-serving RELEASE=<id> MIGRATE=1 ROLES=1
```

For an existing database, first review its columns, types, constraints and
indexes against `V001__initial_schema.sql`, then run once:

```bash
make publish-serving RELEASE=<id> MIGRATE=1 MIGRATE_ARGS=--baseline ROLES=1
```

Baseline verifies that the expected tables exist and records V001 without
recreating them. It does **not** prove full schema equivalence or repair an
older schema. Resolve any differences with reviewed migrations first.
`ROLES=1` sets `kea_loader` and `kea_app` passwords from the ignored `.env`;
secrets are deliberately not stored in SQL migration files. Both are owner
steps a publish never takes on its own, and `make deploy` never passes `ROLES`.

V003 makes the Sprint 3 foreign keys deferrable, which a publication needs.
V004 lets the serving gate read identity sequences as `kea_loader`.
V005 adds Sprint 4's tables, early-warning codes and detected-load columns;
its foreign keys are declared deferrable, because V003 never sees later tables.
V006 is the solar+EV household (detect-v2): it adds how much of a day and of a
session the panels hid, makes `kea.mart_charging_session.charger_class`
NULLABLE -- a PV-masked run cannot be told from a portable charger -- and
admits the `solar_soak` charging pattern on `kea.ev_scenario`.
V007 adds `battery_kwh` to `kea.solar_estimate`: each system size is now
estimated with no battery (0) and with one, so a publication writes six rows
per account, and the key and index gain the column.
Add future changes as `V008__description.sql`, `V009__description.sql`, etc.
Schema-qualify every name: a baselined database never ran V001's search_path.
`design/serving_read_contract.json` lists the columns the backend reads; the
`ServingReadContract` test fails if a migration renames, drops or retypes one.
Never edit an applied migration: its checksum is verified on every run.
V001 is the frozen initial DDL; `design/kea_serving_schema.sql` remains the
readable current schema reference. Update that reference with future changes.
Use additive changes, backfill data, deploy compatible API code, and only
then remove old columns in a later release. Each migration commits separately;
failure rolls back that migration, leaving earlier migrations applied.

Build and gate the warehouse, then export and publish one release:

```bash
make build-warehouse
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
The one exception is `make rebuild-all`, which truncates the nine decision
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
