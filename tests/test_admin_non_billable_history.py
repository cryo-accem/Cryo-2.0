import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminNonBillableHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "non-billable-history.sqlite3")
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
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, debit_head_status)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)""",
                ("Facility User", "Dr. Somnath Dutta", "facility@example.com",
                 "internal", "Non-billable Sample", "2026-09-10", "Debit Head Pending"),
            )
            conn.commit()
            cur.close()
            conn.close()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def test_non_billable_history_is_completed_without_payment_details(self):
        response = self.client.get("/admin/history")

        self.assertEqual(response.status_code, 200)
        page = response.data.decode()
        self.assertIn("Completed — non-billable", page)
        self.assertNotIn("Debit Head Pending", page)
        self.assertNotIn("Debit head details", page)
        self.assertNotIn("debit_head_details", page)

    def test_non_billable_payment_updates_are_ignored(self):
        page = self.client.get("/admin/history")
        csrf_token = re.search(
            rb'name="_csrf_token" value="([^"]+)"', page.data
        ).group(1).decode()
        response = self.client.post(
            "/admin/payment/imaging/1",
            data={
                "_csrf_token": csrf_token,
                "origin": "internal",
                "status": "Debit Head Verified",
                "debit_head_details": "Should not be saved",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT debit_head_status, debit_head_details FROM bookings WHERE id=?",
                [1],
            )
            booking = cur.fetchone()
            self.assertEqual(booking["debit_head_status"], "Debit Head Pending")
            self.assertIsNone(booking["debit_head_details"])
            cur.close()
            conn.close()


if __name__ == "__main__":
    unittest.main()
