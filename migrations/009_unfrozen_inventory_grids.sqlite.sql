PRAGMA defer_foreign_keys=ON;
CREATE TABLE inventory_grids_rebuilt (
    grid_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_code TEXT NOT NULL UNIQUE,
    batch_id INTEGER NULL REFERENCES freezing_batches(batch_id),
    sample_id INTEGER NOT NULL REFERENCES inventory_samples(sample_id),
    grid_type_id INTEGER NOT NULL REFERENCES grid_types(grid_type_id),
    clipped INTEGER NOT NULL DEFAULT 0 CHECK (clipped IN (0, 1)),
    clipped_at TEXT NULL,
    clipped_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    clipping_notes TEXT,
    current_status TEXT NOT NULL DEFAULT 'Frozen'
        CHECK (current_status IN ('Frozen','Clipped','Stored','In Data Collection','Discarded')),
    created_at TEXT NOT NULL,
    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    blot_seconds NUMERIC NULL,
    blot_force NUMERIC NULL
);
INSERT INTO inventory_grids_rebuilt
    (grid_id, grid_code, batch_id, sample_id, grid_type_id, clipped, clipped_at,
     clipped_by, clipping_notes, current_status, created_at, created_by,
     blot_seconds, blot_force)
SELECT grid_id, grid_code, batch_id, sample_id, grid_type_id, clipped, clipped_at,
       clipped_by, clipping_notes, current_status, created_at, created_by,
       blot_seconds, blot_force
FROM inventory_grids;
DROP TABLE inventory_grids;
ALTER TABLE inventory_grids_rebuilt RENAME TO inventory_grids;
CREATE INDEX idx_inventory_grids_sample ON inventory_grids(sample_id);
CREATE INDEX idx_inventory_grids_status ON inventory_grids(current_status);
CREATE INDEX idx_inventory_grids_type ON inventory_grids(grid_type_id);
CREATE INDEX idx_inventory_grids_batch ON inventory_grids(batch_id);
INSERT OR IGNORE INTO inventory_schema_migrations (version)
    VALUES ('009_unfrozen_inventory_grids');
