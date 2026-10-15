# Grid Inventory migration

The migrations are additive and tracked in `inventory_schema_migrations`.
They create inventory tables, indexes, grid type options, identifier
sequences, an optional link from a completed freezing booking to its inventory
batch, optional per-grid blot conditions, and auditable links from screening
and data-collection bookings to selected inventory grids; they do not alter or
replace existing booking, billing, user, or other application tables.

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
