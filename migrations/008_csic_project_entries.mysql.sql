CREATE TABLE IF NOT EXISTS csic_project_entries (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    company_name VARCHAR(180) NOT NULL,
    sample_count INT NOT NULL,
    sample_details TEXT NOT NULL,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB;

INSERT IGNORE INTO inventory_schema_migrations (version)
    VALUES ('008_csic_project_entries');
