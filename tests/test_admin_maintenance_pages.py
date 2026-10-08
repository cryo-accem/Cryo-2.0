import os
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminMaintenancePagesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "maintenance-pages.sqlite3")
        self.database_url = patch.object(
            database, "DATABASE_URL", f"sqlite:///{db_path}"
        )
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def test_maintenance_hides_editable_public_pages(self):
        response = self.client.get("/admin/maintenance")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Editable public pages", response.data)
        self.assertIn(b"Managed publications", response.data)
        self.assertIn(b"Managed instruments", response.data)

    def test_public_page_save_route_is_removed(self):
        self.client.get("/admin/maintenance")
        with self.client.session_transaction() as session:
            csrf_token = session["_csrf_token"]
        response = self.client.post(
            "/admin/maintenance/pages/save",
            data={"_csrf_token": csrf_token},
        )

        self.assertEqual(response.status_code, 404)
