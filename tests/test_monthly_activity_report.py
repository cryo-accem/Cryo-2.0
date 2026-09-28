import os
import tempfile
import unittest
from unittest.mock import patch
from io import BytesIO

from docx import Document

import database
from main import create_app


class MonthlyActivityReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "report-test.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.app.test_request_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, origin, sample_name, status, completion_date,
                    actual_grids, number_of_grids, grid_source, clipped_grids,
                    total_billed, grand_total, amount_received)
                   VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Data User", "PI One", "internal", "Sample A", "2024-04-10",
                 3, 3, "facility", 2, "1000.00", "1000.00", "500.00"),
            )
            cur.execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, origin, sample_name, status, completion_date,
                    actual_grids, number_of_grids, grid_source, clipped_grids,
                    clipping_charge, total_billed, grand_total, amount_received)
                   VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Screen User", "PI Two", "academic", "Sample B", "2024-04-15",
                 2, 2, "self_owned", 1, "250.00", "2000.00", "2000.00", "1000.00"),
            )
            cur.execute(
                """INSERT INTO completed_freezing
                   (user_name, pi_name, origin, sample_name, grids, freezing_date,
                    completed_at, actual_grids, number_of_grids, grid_source,
                    total_billed, grand_total, amount_received)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Freeze User", "PI Three", "internal", "Sample C", 4, "2024-04-20",
                 "2024-04-20", 4, 4, "facility", "500.00", "500.00", "500.00"),
            )
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, origin, sample_name, status, completion_date,
                    actual_grids, number_of_grids, grid_source, clipped_grids,
                    total_billed, grand_total)
                   VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)""",
                ("Outside Period", "PI Four", "internal", "Sample D", "2024-05-10",
                 8, 8, "facility", 4, "9000.00", "9000.00"),
            )
            conn.commit()
            cur.close()
            conn.close()

    def test_custom_month_range_downloads_editable_report_with_activity_and_totals(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        response = self.client.get(
            "/admin/activity-report.docx?range=custom&start_date=2024-04-01&end_date=2024-04-30"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertTrue(response.data.startswith(b"PK"))
        self.assertIn("2024-04-01-to-2024-04-30.docx", response.headers["Content-Disposition"])

        document = Document(BytesIO(response.data))
        content = "\n".join(
            [paragraph.text for paragraph in document.paragraphs]
            + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        )
        self.assertIn("Completed freezing records", content)
        self.assertIn("Completed clipping records", content)
        self.assertIn("Completed screening records", content)
        self.assertIn("Completed data collection records", content)
        self.assertIn("Facility-provided grids used", content)
        self.assertIn("Outside/user-provided grids used", content)
        self.assertIn("INR 126,220.00", content)
        self.assertIn("Sample A", content)
        self.assertIn("Sample B", content)
        self.assertIn("Sample C", content)
        self.assertNotIn("Sample D", content)

    def test_report_requires_admin_session(self):
        response = self.client.get("/admin/activity-report.docx")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/", response.headers["Location"])

    def test_invalid_custom_range_returns_validation_error(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        response = self.client.get(
            "/admin/activity-report.docx?range=custom&start_date=2024-05-01&end_date=2024-04-30"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"start date", response.data)

    def test_custom_date_range_excludes_other_dates_in_the_same_month(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

        response = self.client.get(
            "/admin/activity-report.docx?range=custom&start_date=2024-04-11&end_date=2024-04-19"
        )

        self.assertEqual(response.status_code, 200)
        document = Document(BytesIO(response.data))
        content = "\n".join(
            [paragraph.text for paragraph in document.paragraphs]
            + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        )
        self.assertIn("Sample B", content)
        self.assertNotIn("Sample A", content)
        self.assertNotIn("Sample C", content)
        self.assertIn("INR 2,000.00", content)


if __name__ == "__main__":
    unittest.main()
