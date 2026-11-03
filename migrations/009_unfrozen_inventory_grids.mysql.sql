ALTER TABLE inventory_grids MODIFY COLUMN batch_id INT NULL;
INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('009_unfrozen_inventory_grids');
