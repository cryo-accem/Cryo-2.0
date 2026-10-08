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


if __name__ == "__main__":
    unittest.main()
