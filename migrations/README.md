# Grid Inventory migration

The migrations are versioned and tracked in `inventory_schema_migrations`.
They create inventory tables, indexes, grid type options, identifier
sequences, an optional link from a completed freezing booking to its inventory
batch, optional per-grid blot conditions, and auditable links from screening
and data-collection bookings to selected inventory grids. They preserve
existing inventory history and do not alter booking, billing, user, or other
application tables.
Migration `005_grid_types` activates the four supported grid types and
deactivates other options without deleting types referenced by historical grids.
The active types are Quantifoil holey carbon, Carbon-coated grids, Gold
Quantifoil Gold Grids, and Graphene-coated grids.
Migration `006_existing_stored_grids_frozen` corrects existing grids that have
an active storage location but still carry the legacy `Stored` status to
`Frozen`; their storage-location records are retained.
Migration `007_unlimited_admin_screening` removes the legacy MySQL check
constraint that capped screening bookings at four grids. Public screening and
data-collection forms continue to enforce their four-grid limit in application
validation; admin bookings are not capped.
Migration `009_unfrozen_inventory_grids` allows inventory records with no
freezing batch. Screening and data-collection completion can register and store
additional grids without creating a fictitious freezing record; existing
freezing-linked grids and their batch history are preserved.

Public booking forms can reserve up to four available inventory grids for
screening or data collection. The grid links are stored in the booking-link
tables created by migration `004_booking_grid_links`. Completion of a linked
booking records each grid's confirmed Falcon, box, and position in its
storage-location history within the same transaction as booking completion.

Before applying it to production, back up the configured MySQL database and
confirm `DATABASE_URL` points to the intended database. Then run from the
repository root:

```sh
python migrations/apply_grid_inventory.py
```

The runner applies all migrations in order, selecting the MySQL migration for
the production MySQL connection or the SQLite variant when `DATABASE_URL` is
SQLite. The MySQL constraints require InnoDB
and support generated columns (MySQL 5.7+); they enforce a single active
location per grid and prevent two grids occupying the same active container,
box, and position. Position capacity is deliberately not assumed.

The application applies this migration after the base schema is initialized and
before it starts serving requests. The migration is idempotent, so it can
safely run again when the application restarts. For environments with an
existing deployment, run the manual command above once if the service has not
yet restarted with this application version.

## Migration 008 is intentionally not applied

`008_csic_project_entries` (both dialects) is deliberately left out of the
`MIGRATIONS` tuple in `apply_grid_inventory.py`. Nothing in the application
reads or writes a `csic_project_entries` table, so the runner never creates
it. The files are kept only so the version number is not reused; do not add
008 to the tuple unless a feature starts using that table.

## Idempotency on MySQL

MySQL commits DDL implicitly, so a transaction rollback cannot undo an
`ALTER TABLE` that ran before a later failure. The runner therefore commits
after each MySQL migration and treats "duplicate column" (1060) and
"duplicate key name" (1061) errors as an already-applied step, so a restart
after a partial failure does not crash on the repeated DDL. Keep new MySQL
`ALTER TABLE ... ADD COLUMN` / `CREATE INDEX` migrations to one column or
index per statement so each step can be skipped independently. SQLite still
applies all pending migrations in a single transaction.

## Storage names are case-insensitive on MySQL only

Falcon container, grid box, and position values (`storage_locations`) are
compared using the database's default collation. Production MySQL uses a
case- and accent-insensitive collation (e.g. `utf8mb4_0900_ai_ci`), so `F1`
and `f1` are the same container there and collide on
`uq_storage_active_position`, while the SQLite preview/test database treats
them as different. No schema change is made; normalise or compare names
case-insensitively in application code if consistent behaviour is needed.
