# Grid Inventory migration

The migration is additive and safe to rerun. It creates the inventory-only
tables, indexes, grid type options, and identifier sequences; it does not alter
or replace booking, billing, user, or other application tables.

Before applying it to production, back up the configured MySQL database and
confirm `DATABASE_URL` points to the intended database. Then run from the
repository root:

```sh
python migrations/apply_grid_inventory.py
```

The runner selects the MySQL migration for the production MySQL connection or
the SQLite variant when `DATABASE_URL` is SQLite. Run it once in each
environment before using Grid Inventory. The MySQL constraints require InnoDB
and support generated columns (MySQL 5.7+); they enforce a single active
location per grid and prevent two grids occupying the same active container,
box, and position. Position capacity is deliberately not assumed.

The application applies this migration after the base schema is initialized and
before it starts serving requests. The migration is idempotent, so it can
safely run again when the application restarts. For environments with an
existing deployment, run the manual command above once if the service has not
yet restarted with this application version.
