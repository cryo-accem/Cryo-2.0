import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

import database


class AdminSharedEmailTests(unittest.TestCase):
    def test_bootstrap_admin_is_created_once_and_requires_password_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "bootstrap.sqlite3")
            with patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}"), \
                    patch.dict(os.environ, {
                        "BOOTSTRAP_ADMIN_USERNAME": "convenor",
                        "BOOTSTRAP_ADMIN_EMAIL": "shared@example.com",
                        "BOOTSTRAP_ADMIN_PASSWORD": "Temporary-Secret-123!",
                    }):
                database.init_db()
                conn = database.get_db()
                cur = conn.cursor()
                cur.execute(
                    "SELECT username, email, role, password_hash, must_change_password "
                    "FROM users WHERE username=?",
                    ["convenor"],
                )
                user = cur.fetchone()
                self.assertEqual(user["email"], "shared@example.com")
                self.assertEqual(user["role"], "admin")
                self.assertEqual(user["must_change_password"], 1)
                self.assertTrue(
                    check_password_hash(user["password_hash"], "Temporary-Secret-123!")
                )
                cur.execute(
                    "UPDATE users SET must_change_password=? WHERE username=?",
                    [0, "convenor"],
                )
                cur.execute(
                    "UPDATE users SET password_hash=? WHERE username=?",
                    [
                        generate_password_hash(
                            "User-Changed-Password-2026!",
                            method="pbkdf2:sha256",
                        ),
                        "convenor",
                    ],
                )
                conn.commit()
                cur.close()
                conn.close()

                with patch.dict(os.environ, {
                    "BOOTSTRAP_ADMIN_PASSWORD": "A-Different-Secret-2026!",
                }):
                    database.init_db()
                database.init_db()
                conn = database.get_db()
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) AS count, MAX(must_change_password) AS must_change, "
                    "MAX(password_hash) AS password_hash "
                    "FROM users WHERE username=?",
                    ["convenor"],
                )
                result = cur.fetchone()
                self.assertEqual(result["count"], 1)
                self.assertEqual(result["must_change"], 0)
                self.assertTrue(
                    check_password_hash(
                        result["password_hash"], "User-Changed-Password-2026!"
                    )
                )
                cur.close()
                conn.close()

    def test_bootstrap_updates_existing_admin_account_and_forces_password_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "existing-admin.sqlite3")
            with patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}"):
                database.init_db()
                with patch.dict(os.environ, {
                    "BOOTSTRAP_ADMIN_USERNAME": "admin",
                    "BOOTSTRAP_ADMIN_EMAIL": "shared@example.com",
                    "BOOTSTRAP_ADMIN_PASSWORD": "Temporary-Admin-Secret-2026!",
                }):
                    database.init_db()

                conn = database.get_db()
                cur = conn.cursor()
                cur.execute(
                    "SELECT email, password_hash, role, must_change_password "
                    "FROM users WHERE username=?",
                    ["admin"],
                )
                user = cur.fetchone()
                self.assertEqual(user["email"], "shared@example.com")
                self.assertEqual(user["role"], "admin")
                self.assertEqual(user["must_change_password"], 1)
                self.assertTrue(
                    check_password_hash(
                        user["password_hash"], "Temporary-Admin-Secret-2026!"
                    )
                )
                cur.close()
                conn.close()

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

    def test_email_migration_preserves_existing_password_change_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "password-change.sqlite3")
            raw_conn = sqlite3.connect(db_path)
            raw_conn.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(100),
                    email VARCHAR(150) UNIQUE,
                    password_hash VARCHAR(255),
                    role TEXT CHECK(role IN ('user', 'admin')) DEFAULT 'user',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    must_change_password INTEGER NOT NULL DEFAULT 0
                )
            """)
            raw_conn.execute(
                "INSERT INTO users (username, email, password_hash, role, must_change_password) "
                "VALUES (?, ?, ?, ?, ?)",
                ("convenor", "convenor@example.com", "password-hash", "admin", 1),
            )
            raw_conn.commit()
            raw_conn.close()

            with patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}"):
                conn = database.get_db()
                cur = conn.cursor()
                database._drop_users_email_unique(cur)
                database._ensure_password_change_column(cur)
                cur.execute(
                    "UPDATE users SET email=? WHERE username=?",
                    ["shared@example.com", "convenor"],
                )
                conn.commit()
                cur.execute(
                    "SELECT email, must_change_password FROM users WHERE username=?",
                    ["convenor"],
                )
                user = cur.fetchone()
                self.assertEqual(user["email"], "shared@example.com")
                self.assertEqual(user["must_change_password"], 1)
                cur.close()
                conn.close()


if __name__ == "__main__":
    unittest.main()
