"""Apply the additive grid inventory schema migration to the configured database."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import get_db, _is_sqlite_url


VERSION = "001_grid_inventory"
MIGRATIONS_DIR = Path(__file__).resolve().parent


def apply_migration():
    dialect = "sqlite" if _is_sqlite_url() else "mysql"
    migration_path = MIGRATIONS_DIR / f"{VERSION}.{dialect}.sql"
    statements = [
        statement.strip()
        for statement in migration_path.read_text(encoding="utf-8").split(";")
        if statement.strip()
    ]
    connection = get_db()
    cursor = connection.cursor()
    try:
        if dialect == "sqlite":
            cursor.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
    print(f"Applied {VERSION} ({dialect}) from {migration_path.name}.")


if __name__ == "__main__":
    apply_migration()
