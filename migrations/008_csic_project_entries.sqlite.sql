CREATE TABLE IF NOT EXISTS csic_project_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(100) NOT NULL,
    company_name VARCHAR(180) NOT NULL,
    sample_count INTEGER NOT NULL,
    sample_details TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);

INSERT OR IGNORE INTO inventory_schema_migrations (version)
    VALUES ('008_csic_project_entries');
