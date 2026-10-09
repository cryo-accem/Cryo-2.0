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

The Render web service applies this migration before starting Gunicorn. The
migration is idempotent, so it can safely run again when the service restarts.
For other environments, apply it manually with the command above after backing
up the configured database.
