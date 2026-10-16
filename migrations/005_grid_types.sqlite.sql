INSERT OR IGNORE INTO grid_types (grid_type_name, active)
    VALUES ('Quantifoil holey carbon', 1);
INSERT OR IGNORE INTO grid_types (grid_type_name, active)
    VALUES ('Carbon-coated grids', 1);
INSERT OR IGNORE INTO grid_types (grid_type_name, active)
    VALUES ('Gold Quantifoil Gold Grids', 1);
INSERT OR IGNORE INTO grid_types (grid_type_name, active)
    VALUES ('Graphene-coated grids', 1);

UPDATE grid_types SET active=1
WHERE grid_type_name IN (
    'Quantifoil holey carbon',
    'Carbon-coated grids',
    'Gold Quantifoil Gold Grids',
    'Graphene-coated grids'
);
UPDATE grid_types SET active=0
WHERE grid_type_name NOT IN (
    'Quantifoil holey carbon',
    'Carbon-coated grids',
    'Gold Quantifoil Gold Grids',
    'Graphene-coated grids'
);

INSERT OR IGNORE INTO inventory_schema_migrations (version)
    VALUES ('005_grid_types');
