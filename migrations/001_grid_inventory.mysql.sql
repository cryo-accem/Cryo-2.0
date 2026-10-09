CREATE TABLE IF NOT EXISTS inventory_schema_migrations (
    version VARCHAR(80) NOT NULL PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS grid_types (
    grid_type_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    grid_type_name VARCHAR(100) NOT NULL UNIQUE,
    active TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS inventory_samples (
    sample_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    sample_code VARCHAR(24) NOT NULL UNIQUE,
    sample_name VARCHAR(180) NOT NULL,
    researcher_name VARCHAR(150) NOT NULL,
    lab_name VARCHAR(180) NOT NULL,
    description TEXT,
    external_reference VARCHAR(120),
    linked_user_id INT NULL,
    created_at DATETIME NOT NULL,
    created_by INT NULL,
    INDEX idx_inventory_samples_name (sample_name),
    INDEX idx_inventory_samples_researcher (researcher_name),
    INDEX idx_inventory_samples_reference (external_reference),
    CONSTRAINT fk_inventory_samples_user FOREIGN KEY (linked_user_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_inventory_samples_creator FOREIGN KEY (created_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS freezing_batches (
    batch_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    batch_code VARCHAR(24) NOT NULL UNIQUE,
    sample_id INT NOT NULL,
    frozen_at DATETIME NOT NULL,
    grid_type_id INT NOT NULL,
    notes TEXT,
    comments TEXT,
    created_at DATETIME NOT NULL,
    created_by INT NULL,
    INDEX idx_freezing_batches_sample_time (sample_id, frozen_at),
    CONSTRAINT fk_freezing_batches_sample FOREIGN KEY (sample_id)
        REFERENCES inventory_samples(sample_id),
    CONSTRAINT fk_freezing_batches_type FOREIGN KEY (grid_type_id)
        REFERENCES grid_types(grid_type_id),
    CONSTRAINT fk_freezing_batches_creator FOREIGN KEY (created_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS inventory_id_sequences (
    sequence_name VARCHAR(32) NOT NULL PRIMARY KEY,
    next_value INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS inventory_grids (
    grid_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    grid_code VARCHAR(24) NOT NULL UNIQUE,
    batch_id INT NOT NULL,
    sample_id INT NOT NULL,
    grid_type_id INT NOT NULL,
    clipped TINYINT(1) NOT NULL DEFAULT 0,
    clipped_at DATETIME NULL,
    clipped_by INT NULL,
    clipping_notes TEXT,
    current_status ENUM('Frozen','Clipped','Stored','In Data Collection','Discarded')
        NOT NULL DEFAULT 'Frozen',
    created_at DATETIME NOT NULL,
    created_by INT NULL,
    INDEX idx_inventory_grids_sample (sample_id),
    INDEX idx_inventory_grids_status (current_status),
    INDEX idx_inventory_grids_type (grid_type_id),
    INDEX idx_inventory_grids_batch (batch_id),
    CONSTRAINT fk_inventory_grids_batch FOREIGN KEY (batch_id)
        REFERENCES freezing_batches(batch_id),
    CONSTRAINT fk_inventory_grids_sample FOREIGN KEY (sample_id)
        REFERENCES inventory_samples(sample_id),
    CONSTRAINT fk_inventory_grids_type FOREIGN KEY (grid_type_id)
        REFERENCES grid_types(grid_type_id),
    CONSTRAINT fk_inventory_grids_clipper FOREIGN KEY (clipped_by)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_inventory_grids_creator FOREIGN KEY (created_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS storage_locations (
    storage_location_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    grid_id INT NOT NULL,
    falcon_container_id VARCHAR(100) NOT NULL,
    grid_box_name VARCHAR(100) NOT NULL,
    grid_position VARCHAR(40) NOT NULL,
    stored_at DATETIME NOT NULL,
    stored_by INT NULL,
    removed_at DATETIME NULL,
    removed_by INT NULL,
    notes TEXT,
    active_grid_id INT GENERATED ALWAYS AS
        (CASE WHEN removed_at IS NULL THEN grid_id ELSE NULL END) STORED,
    active_container_id VARCHAR(100) GENERATED ALWAYS AS
        (CASE WHEN removed_at IS NULL THEN falcon_container_id ELSE NULL END) STORED,
    active_grid_box_name VARCHAR(100) GENERATED ALWAYS AS
        (CASE WHEN removed_at IS NULL THEN grid_box_name ELSE NULL END) STORED,
    active_grid_position VARCHAR(40) GENERATED ALWAYS AS
        (CASE WHEN removed_at IS NULL THEN grid_position ELSE NULL END) STORED,
    UNIQUE KEY uq_storage_active_grid (active_grid_id),
    UNIQUE KEY uq_storage_active_position
        (active_container_id, active_grid_box_name, active_grid_position),
    INDEX idx_storage_lookup (falcon_container_id, grid_box_name, grid_position),
    CONSTRAINT fk_storage_grid FOREIGN KEY (grid_id) REFERENCES inventory_grids(grid_id),
    CONSTRAINT fk_storage_storer FOREIGN KEY (stored_by)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_storage_remover FOREIGN KEY (removed_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS data_sessions (
    session_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    session_code VARCHAR(24) NOT NULL UNIQUE,
    booking_id INT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME NULL,
    operator_id INT NULL,
    instrument VARCHAR(120),
    notes TEXT,
    session_status ENUM('in_progress','completed') NOT NULL DEFAULT 'in_progress',
    INDEX idx_data_sessions_start (start_time),
    CONSTRAINT fk_inventory_session_booking FOREIGN KEY (booking_id)
        REFERENCES bookings(id) ON DELETE SET NULL,
    CONSTRAINT fk_inventory_session_operator FOREIGN KEY (operator_id)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS grid_session_links (
    grid_id INT NOT NULL,
    session_id INT NOT NULL,
    outcome ENUM('in_progress','store','discard') NOT NULL DEFAULT 'in_progress',
    notes TEXT,
    recorded_at DATETIME NOT NULL,
    recorded_by INT NULL,
    PRIMARY KEY (grid_id, session_id),
    INDEX idx_grid_session_links_session (session_id),
    CONSTRAINT fk_grid_session_grid FOREIGN KEY (grid_id)
        REFERENCES inventory_grids(grid_id),
    CONSTRAINT fk_grid_session_session FOREIGN KEY (session_id)
        REFERENCES data_sessions(session_id),
    CONSTRAINT fk_grid_session_recorder FOREIGN KEY (recorded_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS grid_events (
    event_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    grid_id INT NOT NULL,
    event_type VARCHAR(60) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    operator_id INT NULL,
    event_time DATETIME NOT NULL,
    notes TEXT,
    related_session_id INT NULL,
    operation_reference VARCHAR(64),
    INDEX idx_grid_events_grid_time (grid_id, event_time),
    INDEX idx_grid_events_time (event_time),
    CONSTRAINT fk_grid_events_grid FOREIGN KEY (grid_id)
        REFERENCES inventory_grids(grid_id),
    CONSTRAINT fk_grid_events_operator FOREIGN KEY (operator_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_grid_events_session FOREIGN KEY (related_session_id)
        REFERENCES data_sessions(session_id) ON DELETE SET NULL
) ENGINE=InnoDB;

INSERT IGNORE INTO grid_types (grid_type_name) VALUES
    ('Quantifoil holey carbon'),
    ('Gold-coated holey carbon'),
    ('Graphene-coated grid'),
    ('Other');

INSERT IGNORE INTO inventory_id_sequences (sequence_name, next_value) VALUES
    ('grid', 0),
    ('batch', 0),
    ('session', 0);

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('001_grid_inventory');
