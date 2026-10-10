CREATE TABLE IF NOT EXISTS freezing_booking_inventory (
    booking_id INTEGER NOT NULL PRIMARY KEY REFERENCES freezing_bookings(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL UNIQUE REFERENCES freezing_batches(batch_id) ON DELETE CASCADE,
    blot_seconds NUMERIC NOT NULL,
    blot_force NUMERIC NOT NULL
);

INSERT OR IGNORE INTO inventory_schema_migrations (version)
    VALUES ('002_freezing_inventory_link');
