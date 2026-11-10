import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pymysql
from werkzeug.security import check_password_hash

import database
from migrations import apply_grid_inventory


_CLEAN_ENV = {
    "MYSQL_SSL_CA": "",
    "MYSQL_SSL_CA_PEM": "",
    "BOOTSTRAP_ADMIN_USERNAME": "",
    "BOOTSTRAP_ADMIN_EMAIL": "",
    "BOOTSTRAP_ADMIN_PASSWORD": "",
    "PREVIEW_ADMIN_PASSWORD": "",
    "RENDER": "",
}


class MySQLSSLOptionsTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, _CLEAN_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        database._ssl_warning_logged = False

    def test_without_ca_keeps_legacy_options_and_warns_once(self):
        with self.assertLogs("database", level="WARNING") as logs:
            self.assertEqual(database._mysql_ssl_options(), {"ssl": {}})
            self.assertEqual(database._mysql_ssl_options(), {"ssl": {}})
        self.assertEqual(len(logs.records), 1)
        self.assertIn("verification is DISABLED", logs.output[0])

    def test_ca_path_enables_verification(self):
        with tempfile.NamedTemporaryFile(suffix=".pem") as ca_file:
            with patch.dict(os.environ, {"MYSQL_SSL_CA": ca_file.name}):
                options = database._mysql_ssl_options()
        self.assertEqual(options, {"ca": ca_file.name, "check_hostname": True})

    def test_missing_ca_path_fails_loudly(self):
        with patch.dict(os.environ, {"MYSQL_SSL_CA": "/nonexistent/ca.pem"}):
            with self.assertRaises(RuntimeError):
                database._mysql_ssl_options()

    def test_ca_pem_contents_are_written_to_a_private_file(self):
        pem = "-----BEGIN CERTIFICATE-----\\nABC\\n-----END CERTIFICATE-----"
        with patch.dict(os.environ, {"MYSQL_SSL_CA_PEM": pem}):
            options = database._mysql_ssl_options()
            again = database._mysql_ssl_options()
        self.addCleanup(os.unlink, options["ca"])
        self.assertEqual(options, again)
        self.assertTrue(options["check_hostname"])
        with open(options["ca"], encoding="utf-8") as handle:
            self.assertEqual(
                handle.read(),
                "-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n",
            )
        self.assertEqual(os.stat(options["ca"]).st_mode & 0o777, 0o600)

    def test_get_db_passes_ssl_options_to_pymysql(self):
        with tempfile.NamedTemporaryFile(suffix=".pem") as ca_file, \
                patch.dict(os.environ, {"MYSQL_SSL_CA": ca_file.name}), \
                patch.object(database, "DATABASE_URL", "mysql://u:p@db.example.com:1234/app"), \
                patch.object(database.pymysql, "connect") as connect:
            database.get_db()
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["ssl"], {"ca": ca_file.name, "check_hostname": True})
        self.assertEqual(kwargs["host"], "db.example.com")
        self.assertEqual(kwargs["port"], 1234)


class DefaultAdminSeedTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, _CLEAN_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = os.path.join(self.temp_dir.name, "seed.sqlite3")
        url_patch = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        url_patch.start()
        self.addCleanup(url_patch.stop)

    def _admins(self):
        conn = database.get_db()
        try:
            return [
                dict(row) for row in conn.execute(
                    "SELECT username, password_hash, must_change_password "
                    "FROM users WHERE role='admin'"
                ).fetchall()
            ]
        finally:
            conn.close()

    def test_preview_admin_outside_tests_has_random_password_and_forced_change(self):
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": ""}), \
                self.assertLogs("database", level="WARNING") as logs:
            os.environ.pop("PYTEST_CURRENT_TEST")
            database.init_db()
        admins = self._admins()
        self.assertEqual(len(admins), 1)
        self.assertEqual(admins[0]["must_change_password"], 1)
        self.assertFalse(check_password_hash(admins[0]["password_hash"], "admin123"))
        logged = "\n".join(logs.output)
        password = logged.split("temporary password ", 1)[1].split(" ", 1)[0]
        self.assertTrue(check_password_hash(admins[0]["password_hash"], password))

    def test_no_default_admin_on_render(self):
        with patch.dict(os.environ, {"RENDER": "true"}), \
                self.assertLogs("database", level="WARNING") as logs:
            database.init_db()
        self.assertEqual(self._admins(), [])
        self.assertIn("No admin account exists", "\n".join(logs.output))

    def test_bootstrap_admin_suppresses_default_admin(self):
        with patch.dict(os.environ, {
            "BOOTSTRAP_ADMIN_USERNAME": "convenor",
            "BOOTSTRAP_ADMIN_EMAIL": "convenor@example.com",
            "BOOTSTRAP_ADMIN_PASSWORD": "Bootstrap-Secret-2026!",
        }):
            database.init_db()
        self.assertEqual([a["username"] for a in self._admins()], ["convenor"])

    def test_no_default_admin_on_mysql(self):
        cur = MagicMock()
        cur.fetchone.return_value = {"cnt": 0}
        with patch.object(database, "DATABASE_URL", "mysql://u:p@db/app"), \
                self.assertLogs("database", level="WARNING"):
            database._seed_preview_admin_if_missing(cur)
        for call in cur.execute.call_args_list:
            self.assertNotIn("INSERT", call.args[0])


class MySQLMigrationIdempotencyTests(unittest.TestCase):
    def test_duplicate_column_and_key_errors_are_skipped(self):
        for code in (1060, 1061):
            cursor = MagicMock()
            cursor.execute.side_effect = pymysql.err.OperationalError(code, "dup")
            apply_grid_inventory._execute_mysql_idempotent(
                cursor, "ALTER TABLE t ADD COLUMN c INT", "003"
            )

    def test_other_errors_propagate(self):
        cursor = MagicMock()
        cursor.execute.side_effect = pymysql.err.OperationalError(1146, "no table")
        with self.assertRaises(pymysql.err.OperationalError):
            apply_grid_inventory._execute_mysql_idempotent(cursor, "ALTER TABLE t", "003")

    def test_unused_csic_migration_is_not_applied(self):
        self.assertNotIn("008_csic_project_entries", apply_grid_inventory.MIGRATIONS)


if __name__ == "__main__":
    unittest.main()
