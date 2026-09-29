import os
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminPendingPaymentDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "pending-payments.sqlite3")
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
                    completion_date, total_billed, amount_received)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                ("Payment User", "PI Payment", "payment@example.com", "academic",
                 "Sample Payment", "2026-09-02", "1200.00", "200.00"),
            )
            cur.execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed, debit_head_status)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                ("Internal User", "PI Internal", "internal@example.com", "internal",
                 "Sample Internal", "2026-09-03", "900.00", "Debit Head Pending"),
            )
            cur.execute(
                """INSERT INTO completed_freezing
                   (user_name, pi_name, email, origin, sample_name, completed_at,
                    total_billed, payment_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Freezing User", "PI Freeze", "freeze@example.com", "industrial",
                 "Sample Freeze", "2026-09-04", "700.00", "Payment Pending"),
            )
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed, payment_status)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                ("Paid User", "PI Paid", "paid@example.com", "academic",
                 "Sample Paid", "2026-09-05", "500.00", "Payment Verified"),
            )
            cur.execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed, payment_status)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                ("Proof User", "PI Proof", "proof@example.com", "academic",
                 "Sample Proof", "2026-09-06", "800.00", "Payment Proof Received"),
            )
            conn.commit()
            cur.close()
            conn.close()

        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def test_dashboard_tracks_history_pending_statuses_and_omits_updated_statuses(self):
        response = self.client.get("/admin/panel")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pending payment tracking", response.data)
        self.assertIn(b"2 records", response.data)
        self.assertIn(b"Payment balance outstanding", response.data)
        self.assertIn(b"Internal debit head pending", response.data)
        self.assertIn(b"Payment User", response.data)
        self.assertIn(b"Freezing User", response.data)
        self.assertIn(b"Internal User", response.data)
        self.assertIn(b"1,000.00", response.data)
        self.assertNotIn(b"Paid User", response.data)
        self.assertNotIn(b"Proof User", response.data)

    def test_accounting_counts_verified_bills_as_received_when_amount_is_blank(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed, debit_head_status)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                ("Internal Verified", "PI Internal", "internal-verified@example.com",
                 "internal", "Sample Internal Verified", "2026-09-07", "74250.00",
                 "Debit Head Verified"),
            )
            conn.commit()
            cur.close()
            conn.close()

        response = self.client.get(
            "/admin/panel?range=custom&start=2026-09-01&end=2026-09-30"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Amount Received</span><strong>\xe2\x82\xb974,950.00", response.data)
        self.assertIn(b"Outstanding</span><strong>\xe2\x82\xb93,400.00", response.data)


if __name__ == "__main__":
    unittest.main()
