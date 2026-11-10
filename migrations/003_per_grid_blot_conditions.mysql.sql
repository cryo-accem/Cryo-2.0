-- One column per ALTER so each step can be skipped independently if it was
-- already applied (see _execute_mysql_idempotent in apply_grid_inventory.py).
ALTER TABLE inventory_grids ADD COLUMN blot_seconds DECIMAL(8,2) NULL;
ALTER TABLE inventory_grids ADD COLUMN blot_force DECIMAL(8,2) NULL;

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('003_per_grid_blot_conditions');
