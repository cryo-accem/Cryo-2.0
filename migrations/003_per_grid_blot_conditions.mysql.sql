ALTER TABLE inventory_grids
    ADD COLUMN blot_seconds DECIMAL(8,2) NULL,
    ADD COLUMN blot_force DECIMAL(8,2) NULL;

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('003_per_grid_blot_conditions');
