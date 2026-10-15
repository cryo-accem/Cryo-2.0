CREATE TABLE IF NOT EXISTS screening_booking_grids (
    screening_booking_id INT NOT NULL,
    grid_id INT NOT NULL,
    linked_at DATETIME NOT NULL,
    linked_by INT NULL,
    PRIMARY KEY (screening_booking_id, grid_id),
    INDEX idx_screening_booking_grids_grid (grid_id),
    CONSTRAINT fk_screening_booking_grids_booking
        FOREIGN KEY (screening_booking_id) REFERENCES screening_bookings(id) ON DELETE CASCADE,
    CONSTRAINT fk_screening_booking_grids_grid
        FOREIGN KEY (grid_id) REFERENCES inventory_grids(grid_id),
    CONSTRAINT fk_screening_booking_grids_user
        FOREIGN KEY (linked_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS data_collection_booking_grids (
    booking_id INT NOT NULL,
    grid_id INT NOT NULL,
    linked_at DATETIME NOT NULL,
    linked_by INT NULL,
    PRIMARY KEY (booking_id, grid_id),
    INDEX idx_data_collection_booking_grids_grid (grid_id),
    CONSTRAINT fk_data_collection_booking_grids_booking
        FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
    CONSTRAINT fk_data_collection_booking_grids_grid
        FOREIGN KEY (grid_id) REFERENCES inventory_grids(grid_id),
    CONSTRAINT fk_data_collection_booking_grids_user
        FOREIGN KEY (linked_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('004_booking_grid_links');
