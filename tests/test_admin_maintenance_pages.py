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

    def test_facility_notice_can_be_created_edited_hidden_and_deleted(self):
        maintenance = self.client.get("/admin/maintenance")
        self.assertEqual(maintenance.status_code, 200)
        self.assertIn(b"Facility notifications", maintenance.data)
        with self.client.session_transaction() as session:
            csrf_token = session["_csrf_token"]

        created = self.client.post(
            "/admin/maintenance/notices/save",
            data={
                "_csrf_token": csrf_token,
                "title": "Cryo-EM workshop",
                "message": "Registration is open for <strong>everyone</strong>.",
                "event_date": "2026-11-12",
                "link_url": "https://example.test/workshop",
                "is_active": "1",
            },
        )
        self.assertEqual(created.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            notice_id = conn.execute(
                "SELECT id FROM facility_notices WHERE title='Cryo-EM workshop'"
            ).fetchone()["id"]
            conn.close()

        public_page = self.client.get("/home")
        self.assertIn(b"Cryo-EM workshop", public_page.data)
        self.assertIn(b"Registration is open for &lt;strong&gt;everyone&lt;/strong&gt;.", public_page.data)
        self.assertIn(b'rel="noopener noreferrer"', public_page.data)
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        edited = self.client.post(
            "/admin/maintenance/notices/save",
            data={
                "_csrf_token": csrf_token,
                "id": str(notice_id),
                "title": "Cryo-EM workshop",
                "message": "The workshop is now full.",
                "event_date": "2026-11-12",
                "link_url": "",
            },
        )
        self.assertEqual(edited.status_code, 302)
        hidden_page = self.client.get("/home")
        self.assertNotIn(b"Cryo-EM workshop", hidden_page.data)
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        deleted = self.client.post(
            f"/admin/maintenance/notices/{notice_id}/delete",
            data={"_csrf_token": csrf_token},
        )
        self.assertEqual(deleted.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            remaining = conn.execute(
                "SELECT COUNT(*) AS count FROM facility_notices"
            ).fetchone()["count"]
            conn.close()
        self.assertEqual(remaining, 0)

    def test_facility_notice_rejects_unsafe_links(self):
        maintenance = self.client.get("/admin/maintenance")
        self.assertEqual(maintenance.status_code, 200)
        with self.client.session_transaction() as session:
            csrf_token = session["_csrf_token"]
        response = self.client.post(
            "/admin/maintenance/notices/save",
            data={
                "_csrf_token": csrf_token,
                "title": "Unsafe link",
                "message": "This should not be published.",
                "link_url": "javascript:alert(1)",
                "is_active": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM facility_notices"
            ).fetchone()["count"]
            conn.close()
        self.assertEqual(count, 0)
