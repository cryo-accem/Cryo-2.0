import os
import re
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash

import database
from main import create_app


class ForcedAdminPasswordChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "password-change.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET must_change_password=? WHERE username=?",
                [1, "admin"],
            )
            conn.commit()
            cur.close()
            conn.close()

    def test_temporary_password_login_requires_change_before_admin_access(self):
        login_page = self.client.get("/admin/")
        csrf_token = re.search(
            rb'name="_csrf_token" value="([^"]+)"', login_page.data
        ).group(1).decode()
        login = self.client.post(
            "/admin/",
            data={
                "_csrf_token": csrf_token,
                "username": "admin",
                "password": "admin123",
            },
        )
        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/admin/change-password"))
        self.assertEqual(self.client.get("/admin/panel").status_code, 302)
        self.assertIn(
            "/admin/change-password",
            self.client.get("/admin/panel").headers["Location"],
        )

        change_page = self.client.get("/admin/change-password")
        self.assertEqual(change_page.status_code, 200)
        change_csrf = re.search(
            rb'name="_csrf_token" value="([^"]+)"', change_page.data
        ).group(1).decode()
        new_password = "Trusted-ACCEM-Password-2026!"
        changed = self.client.post(
            "/admin/change-password",
            data={
                "_csrf_token": change_csrf,
                "current_password": "admin123",
                "new_password": new_password,
                "confirm_password": new_password,
            },
        )
        self.assertEqual(changed.status_code, 302)
        self.assertTrue(changed.headers["Location"].endswith("/admin/panel"))
        self.assertEqual(self.client.get("/admin/panel").status_code, 200)

        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT password_hash, must_change_password FROM users WHERE username=?",
                ["admin"],
            )
            user = cur.fetchone()
            self.assertTrue(check_password_hash(user["password_hash"], new_password))
            self.assertEqual(user["must_change_password"], 0)
            cur.close()
            conn.close()

    def test_short_or_mismatched_new_password_is_rejected(self):
        login_page = self.client.get("/admin/")
        csrf_token = re.search(
            rb'name="_csrf_token" value="([^"]+)"', login_page.data
        ).group(1).decode()
        self.client.post(
            "/admin/",
            data={
                "_csrf_token": csrf_token,
                "username": "admin",
                "password": "admin123",
            },
        )
        change_page = self.client.get("/admin/change-password")
        change_csrf = re.search(
            rb'name="_csrf_token" value="([^"]+)"', change_page.data
        ).group(1).decode()
        response = self.client.post(
            "/admin/change-password",
            data={
                "_csrf_token": change_csrf,
                "current_password": "admin123",
                "new_password": "short",
                "confirm_password": "different",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"at least 12 characters", response.data)


if __name__ == "__main__":
    unittest.main()
