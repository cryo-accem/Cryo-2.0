SET @drop_screening_grid_limit = (
    SELECT IF(
        COUNT(*) > 0,
        'ALTER TABLE screening_bookings DROP CHECK `screening_bookings_chk_1`',
        'SELECT 1'
    )
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'screening_bookings'
      AND CONSTRAINT_NAME = 'screening_bookings_chk_1'
      AND CONSTRAINT_TYPE = 'CHECK'
);
PREPARE drop_screening_grid_limit_stmt FROM @drop_screening_grid_limit;
EXECUTE drop_screening_grid_limit_stmt;
DEALLOCATE PREPARE drop_screening_grid_limit_stmt;
INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('007_unlimited_admin_screening');
