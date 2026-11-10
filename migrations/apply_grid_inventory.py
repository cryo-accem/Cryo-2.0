"""Apply the additive grid inventory schema migration to the configured database."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import get_db, _is_sqlite_url

# MySQL error codes that mean an additive DDL step already ran. MySQL DDL
# commits implicitly, so a crash after an ALTER but before the version row is
# recorded would otherwise make every later boot fail on the repeated ALTER.
# 1060 = duplicate column name, 1061 = duplicate key/index name.
_MYSQL_ALREADY_APPLIED_ERRORS = {1060, 1061}


MIGRATIONS_DIR = Path(__file__).resolve().parent
MIGRATIONS = (
    "001_grid_inventory",
    "002_freezing_inventory_link",
    "003_per_grid_blot_conditions",
    "004_booking_grid_links",
    "005_grid_types",
    "006_existing_stored_grids_frozen",
    "007_unlimited_admin_screening",
    "009_unfrozen_inventory_grids",
)


def _execute_mysql_idempotent(cursor, statement, version):
    """Run a MySQL statement, skipping ADD COLUMN / ADD INDEX already applied."""
    try:
        cursor.execute(statement)
    except Exception as error:
        code = error.args[0] if getattr(error, "args", None) else None
        if code not in _MYSQL_ALREADY_APPLIED_ERRORS:
            raise
        print(f"Skipped already-applied step in {version}: {error.args[1:]}")


def apply_migration():
    dialect = "sqlite" if _is_sqlite_url() else "mysql"
    connection = get_db()
    cursor = connection.cursor()
    try:
        if dialect == "sqlite":
            cursor.execute("PRAGMA foreign_keys=ON")
        for version in MIGRATIONS:
            if version != MIGRATIONS[0]:
                cursor.execute(
                    "SELECT version FROM inventory_schema_migrations WHERE version=?",
                    [version],
                )
                if cursor.fetchone():
                    continue
            migration_path = MIGRATIONS_DIR / f"{version}.{dialect}.sql"
            statements = [
                statement.strip()
                for statement in migration_path.read_text(encoding="utf-8").split(";")
                if statement.strip()
            ]
            for statement in statements:
                if dialect == "mysql":
                    _execute_mysql_idempotent(cursor, statement, version)
                else:
                    cursor.execute(statement)
            if dialect == "mysql":
                # DDL already auto-committed; persist the version row now so a
                # later failure cannot leave this migration half-recorded.
                connection.commit()
            print(f"Applied {version} ({dialect}) from {migration_path.name}.")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    apply_migration()
