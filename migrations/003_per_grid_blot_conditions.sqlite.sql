ALTER TABLE inventory_grids ADD COLUMN blot_seconds NUMERIC NULL;
ALTER TABLE inventory_grids ADD COLUMN blot_force NUMERIC NULL;

INSERT OR IGNORE INTO inventory_schema_migrations (version)
    VALUES ('003_per_grid_blot_conditions');
