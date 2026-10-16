UPDATE inventory_grids
SET current_status='Frozen'
WHERE current_status='Stored'
  AND EXISTS (
      SELECT 1 FROM storage_locations
      WHERE storage_locations.grid_id=inventory_grids.grid_id
        AND storage_locations.removed_at IS NULL
  );

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('006_existing_stored_grids_frozen');
