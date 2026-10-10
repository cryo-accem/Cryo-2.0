CREATE TABLE IF NOT EXISTS freezing_booking_inventory (
    booking_id INT NOT NULL PRIMARY KEY,
    batch_id INT NOT NULL UNIQUE,
    blot_seconds DECIMAL(8,2) NOT NULL,
    blot_force DECIMAL(8,2) NOT NULL,
    CONSTRAINT fk_freezing_inventory_booking FOREIGN KEY (booking_id)
        REFERENCES freezing_bookings(id) ON DELETE CASCADE,
    CONSTRAINT fk_freezing_inventory_batch FOREIGN KEY (batch_id)
        REFERENCES freezing_batches(batch_id) ON DELETE CASCADE
) ENGINE=InnoDB;

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('002_freezing_inventory_link');
