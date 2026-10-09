import datetime
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminCsicProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "csic-projects.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def csrf_token(self):
        response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 200)
        return re.search(
            rb'name="_csrf_token"\s+value="([^"]+)"', response.data
        ).group(1).decode()

    def test_csic_gst_and_gross_are_calculated_and_reported(self):
        current_year = datetime.date.today().year
        response = self.client.post(
            "/admin/csic-projects",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Example CSIC Ltd",
                "year": str(current_year),
                "net_amount": "100000.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CSIC Project GST (18%)", response.data)
        self.assertIn(b"18,000.00", response.data)
        self.assertIn(b"CSIC Project Gross", response.data)
        self.assertIn(b"118,000.00", response.data)
        self.assertIn(b"Example CSIC Ltd", response.data)
        self.assertIn(b"before GST", response.data)
        self.assertIn(b"GST (18%):", response.data)
        self.assertIn(b"Gross total:", response.data)
        self.assertIn(b'class="csic-project-labels"', response.data)
        self.assertIn(b"<span>GST (18%)</span>", response.data)
        self.assertIn(b'<article class="csic-project-row">', response.data)
        self.assertIn(b"Save changes", response.data)

    def test_csic_gst_rounds_half_up_to_two_decimal_places(self):
        current_year = datetime.date.today().year
        response = self.client.post(
            "/admin/csic-projects",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Rounding Test Ltd",
                "year": str(current_year),
                "net_amount": "0.25",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"0.05", response.data)
        self.assertIn(b"0.30", response.data)

    def test_existing_csic_project_can_be_edited_and_totals_are_recalculated(self):
        current_year = datetime.date.today().year
        created = self.client.post(
            "/admin/csic-projects",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Old Company",
                "year": str(current_year),
                "net_amount": "1000.00",
            },
        )
        self.assertEqual(created.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            project_id = conn.execute(
                "SELECT id FROM csic_projects WHERE company_name=?",
                ["Old Company"],
            ).fetchone()["id"]
            conn.close()

        response = self.client.post(
            f"/admin/csic-projects/{project_id}/edit",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Updated Company",
                "year": str(current_year),
                "net_amount": "2500.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Updated Company", response.data)
        self.assertNotIn(b"Old Company", response.data)
        self.assertIn(b"2,500.00", response.data)
        self.assertIn(b"450.00", response.data)
        self.assertIn(b"2,950.00", response.data)
        self.assertIn(b"CSIC project updated.", response.data)

    def test_invalid_csic_edit_does_not_change_the_existing_project(self):
        current_year = datetime.date.today().year
        self.client.post(
            "/admin/csic-projects",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Keep This Company",
                "year": str(current_year),
                "net_amount": "1000.00",
            },
        )
        with self.app.app_context():
            conn = database.get_db()
            project_id = conn.execute(
                "SELECT id FROM csic_projects WHERE company_name=?",
                ["Keep This Company"],
            ).fetchone()["id"]
            conn.close()

        response = self.client.post(
            f"/admin/csic-projects/{project_id}/edit",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Should Not Save",
                "year": str(current_year),
                "net_amount": "-1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Keep This Company", response.data)
        self.assertNotIn(b"Should Not Save", response.data)
        self.assertIn(b"positive net amount", response.data)

    def test_projects_outside_dashboard_range_remain_available_for_editing(self):
        current_year = datetime.date.today().year
        self.client.post(
            "/admin/csic-projects",
            data={
                "_csrf_token": self.csrf_token(),
                "company_name": "Older Project Ltd",
                "year": str(current_year - 1),
                "net_amount": "1000.00",
            },
        )

        response = self.client.get("/admin/panel?range=this_year")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Older Project Ltd", response.data)
        self.assertIn(b"Save changes", response.data)
        self.assertIn(b"CSIC Project Net Revenue</span><strong>\xe2\x82\xb90.00", response.data)


if __name__ == "__main__":
    unittest.main()
