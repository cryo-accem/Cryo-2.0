CREATE TABLE IF NOT EXISTS inventory_schema_migrations (
    version TEXT NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grid_types (
    grid_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_type_name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code TEXT NOT NULL UNIQUE,
    sample_name TEXT NOT NULL,
    researcher_name TEXT NOT NULL,
    lab_name TEXT NOT NULL,
    description TEXT,
    external_reference TEXT,
    linked_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_inventory_samples_name ON inventory_samples(sample_name);
CREATE INDEX IF NOT EXISTS idx_inventory_samples_researcher ON inventory_samples(researcher_name);
CREATE INDEX IF NOT EXISTS idx_inventory_samples_reference ON inventory_samples(external_reference);

CREATE TABLE IF NOT EXISTS freezing_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    sample_id INTEGER NOT NULL REFERENCES inventory_samples(sample_id),
    frozen_at TEXT NOT NULL,
    grid_type_id INTEGER NOT NULL REFERENCES grid_types(grid_type_id),
    notes TEXT,
    comments TEXT,
    created_at TEXT NOT NULL,
    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_freezing_batches_sample_time ON freezing_batches(sample_id, frozen_at);

CREATE TABLE IF NOT EXISTS inventory_id_sequences (
    sequence_name TEXT NOT NULL PRIMARY KEY,
    next_value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_grids (
    grid_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_code TEXT NOT NULL UNIQUE,
    batch_id INTEGER NOT NULL REFERENCES freezing_batches(batch_id),
    sample_id INTEGER NOT NULL REFERENCES inventory_samples(sample_id),
    grid_type_id INTEGER NOT NULL REFERENCES grid_types(grid_type_id),
    clipped INTEGER NOT NULL DEFAULT 0 CHECK (clipped IN (0, 1)),
    clipped_at TEXT NULL,
    clipped_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    clipping_notes TEXT,
    current_status TEXT NOT NULL DEFAULT 'Frozen'
        CHECK (current_status IN ('Frozen','Clipped','Stored','In Data Collection','Discarded')),
    created_at TEXT NOT NULL,
    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_inventory_grids_sample ON inventory_grids(sample_id);
CREATE INDEX IF NOT EXISTS idx_inventory_grids_status ON inventory_grids(current_status);
CREATE INDEX IF NOT EXISTS idx_inventory_grids_type ON inventory_grids(grid_type_id);
CREATE INDEX IF NOT EXISTS idx_inventory_grids_batch ON inventory_grids(batch_id);

CREATE TABLE IF NOT EXISTS storage_locations (
    storage_location_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id INTEGER NOT NULL REFERENCES inventory_grids(grid_id),
    falcon_container_id TEXT NOT NULL,
    grid_box_name TEXT NOT NULL,
    grid_position TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    stored_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    removed_at TEXT NULL,
    removed_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_active_grid
    ON storage_locations(grid_id) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_active_position
    ON storage_locations(falcon_container_id, grid_box_name, grid_position)
    WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_storage_lookup
    ON storage_locations(falcon_container_id, grid_box_name, grid_position);

CREATE TABLE IF NOT EXISTS data_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code TEXT NOT NULL UNIQUE,
    booking_id INTEGER NULL REFERENCES bookings(id) ON DELETE SET NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NULL,
    operator_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    instrument TEXT,
    notes TEXT,
    session_status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (session_status IN ('in_progress','completed'))
);
CREATE INDEX IF NOT EXISTS idx_data_sessions_start ON data_sessions(start_time);

CREATE TABLE IF NOT EXISTS grid_session_links (
    grid_id INTEGER NOT NULL REFERENCES inventory_grids(grid_id),
    session_id INTEGER NOT NULL REFERENCES data_sessions(session_id),
    outcome TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (outcome IN ('in_progress','store','discard')),
    notes TEXT,
    recorded_at TEXT NOT NULL,
    recorded_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (grid_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_grid_session_links_session ON grid_session_links(session_id);

CREATE TABLE IF NOT EXISTS grid_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id INTEGER NOT NULL REFERENCES inventory_grids(grid_id),
    event_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    operator_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    event_time TEXT NOT NULL,
    notes TEXT,
    related_session_id INTEGER NULL REFERENCES data_sessions(session_id) ON DELETE SET NULL,
    operation_reference TEXT
);
CREATE INDEX IF NOT EXISTS idx_grid_events_grid_time ON grid_events(grid_id, event_time);
CREATE INDEX IF NOT EXISTS idx_grid_events_time ON grid_events(event_time);

INSERT OR IGNORE INTO grid_types (grid_type_name) VALUES
    ('Quantifoil holey carbon'),
    ('Gold-coated holey carbon'),
    ('Graphene-coated grid'),
    ('Other');
INSERT OR IGNORE INTO inventory_id_sequences (sequence_name, next_value) VALUES
    ('grid', 0),
    ('batch', 0),
    ('session', 0);
INSERT OR IGNORE INTO inventory_schema_migrations (version) VALUES ('001_grid_inventory');
