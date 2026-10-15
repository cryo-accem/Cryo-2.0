CREATE TABLE IF NOT EXISTS screening_booking_grids (
    screening_booking_id INTEGER NOT NULL REFERENCES screening_bookings(id) ON DELETE CASCADE,
    grid_id INTEGER NOT NULL REFERENCES inventory_grids(grid_id),
    linked_at TEXT NOT NULL,
    linked_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (screening_booking_id, grid_id)
);

CREATE INDEX IF NOT EXISTS idx_screening_booking_grids_grid
    ON screening_booking_grids(grid_id);

CREATE TABLE IF NOT EXISTS data_collection_booking_grids (
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    grid_id INTEGER NOT NULL REFERENCES inventory_grids(grid_id),
    linked_at TEXT NOT NULL,
    linked_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (booking_id, grid_id)
);

CREATE INDEX IF NOT EXISTS idx_data_collection_booking_grids_grid
    ON data_collection_booking_grids(grid_id);

INSERT OR IGNORE INTO inventory_schema_migrations (version)
    VALUES ('004_booking_grid_links');
