import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database


class AdminSharedEmailTests(unittest.TestCase):
    def test_init_db_migrates_unique_user_email_and_preserves_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "legacy.sqlite3")
            raw_conn = sqlite3.connect(db_path)
            raw_conn.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(100),
                    email VARCHAR(150) UNIQUE,
                    password_hash VARCHAR(255),
                    role TEXT CHECK(role IN ('user', 'admin')) DEFAULT 'user',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            raw_conn.execute(
                "INSERT INTO users (username, email, password_hash, role) "
                "VALUES (?, ?, ?, ?)",
                ("admin", "old-admin@example.com", "existing-hash", "admin"),
            )
            raw_conn.execute(
                "INSERT INTO users (username, email, password_hash, role) "
                "VALUES (?, ?, ?, ?)",
                ("convenor", "old-convenor@example.com", "existing-hash", "admin"),
            )
            raw_conn.commit()
            raw_conn.close()

            with patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}"):
                database.init_db()
                conn = database.get_db()
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET email=? WHERE username IN (?, ?)",
                    ["shared@example.com", "admin", "convenor"],
                )
                conn.commit()
                cur.execute(
                    "SELECT username, email, role, password_hash FROM users "
                    "WHERE username IN (?, ?) ORDER BY username",
                    ["admin", "convenor"],
                )
                users = [dict(row) for row in cur.fetchall()]
                self.assertEqual(len(users), 2)
                self.assertEqual({user["email"] for user in users}, {"shared@example.com"})
                self.assertEqual({user["role"] for user in users}, {"admin"})
                self.assertEqual({user["password_hash"] for user in users}, {"existing-hash"})
                cur.close()
                conn.close()

                conn = database.get_db()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO users (username, email, password_hash, role) "
                    "VALUES (?, ?, ?, ?)",
                    ("regular-user-one", "regular@example.com", "hash-one", "user"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    cur.execute(
                        "INSERT INTO users (username, email, password_hash, role) "
                        "VALUES (?, ?, ?, ?)",
                        ("regular-user-two", "regular@example.com", "hash-two", "user"),
                    )
                cur.close()
                conn.close()


if __name__ == "__main__":
    unittest.main()
