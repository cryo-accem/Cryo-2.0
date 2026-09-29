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

    def test_dashboard_bar_chart_uses_the_selected_period_and_date_range(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)""",
                ("Comparison User", "PI Comparison", "comparison@example.com",
                 "academic", "Sample Comparison", "2024-01-15", "37000.00"),
            )
            conn.commit()
            cur.close()
            conn.close()

        response = self.client.get(
            "/admin/panel?range=custom&start=2024-01-01&end=2024-01-31"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monthly net revenue bar chart", response.data)
        self.assertIn(b'title="2024-01: \xe2\x82\xb945,000.00"', response.data)
        self.assertNotIn(b"Revenue change", response.data)

    def test_dashboard_shows_empty_chart_when_selected_period_has_no_revenue(self):
        response = self.client.get(
            "/admin/panel?range=custom&start=2018-01-01&end=2018-01-31"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No completed revenue in this period.", response.data)

    def test_dashboard_sums_supplied_fiscal_year_monthly_revenue(self):
        annual_response = self.client.get(
            "/admin/panel?period=annual&range=custom&start=2025-04-01&end=2026-03-31"
        )
        monthly_response = self.client.get(
            "/admin/panel?period=monthly&range=custom&start=2025-01-01&end=2025-01-31"
        )
        comparison_response = self.client.get(
            "/admin/panel?range=custom&start=2026-01-01&end=2026-01-31"
        )

        self.assertEqual(annual_response.status_code, 200)
        self.assertIn(b"\xe2\x82\xb9553,500.00", annual_response.data)
        self.assertIn(b"Annual net revenue bar chart", annual_response.data)
        self.assertIn(b'title="FY 2025-26: \xe2\x82\xb9553,500.00"', annual_response.data)
        self.assertEqual(monthly_response.status_code, 200)
        self.assertIn(b'title="2025-01: \xe2\x82\xb90.00"', monthly_response.data)
        self.assertEqual(comparison_response.status_code, 200)

    def test_dashboard_uses_supplied_2026_monthly_revenue_and_zeroes(self):
        february_response = self.client.get(
            "/admin/panel?range=custom&start=2026-02-01&end=2026-02-28"
        )
        may_response = self.client.get(
            "/admin/panel?range=custom&start=2026-05-01&end=2026-05-31"
        )

        self.assertEqual(february_response.status_code, 200)
        self.assertIn(b'title="2026-02: \xe2\x82\xb9167,000.00"', february_response.data)
        self.assertEqual(may_response.status_code, 200)
        self.assertIn(b'title="2026-05: \xe2\x82\xb90.00"', may_response.data)

    def test_dashboard_plots_the_specified_custom_year(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)""",
                ("Future Year User", "PI Future", "future@example.com",
                 "academic", "Future sample", "2027-09-15", "8200.00"),
            )
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)""",
                ("After Cutoff User", "PI Future", "after-cutoff@example.com",
                 "academic", "After cutoff sample", "2027-09-30", "500000.00"),
            )
            conn.commit()
            cur.close()
            conn.close()

        response = self.client.get(
            "/admin/panel?period=annual&range=custom&start=2027-01-01&end=2027-09-29"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Annual net revenue bar chart", response.data)
        self.assertIn(b'title="FY 2027-28: \xe2\x82\xb98,200.00"', response.data)

    def test_selected_year_bar_chart_uses_completed_booking_revenue(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)""",
                ("Detailed Prior User", "PI Detailed", "detailed-prior@example.com",
                 "academic", "Detailed prior sample", "2025-09-10", "1000.00"),
            )
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status,
                    completion_date, total_billed)
                   VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)""",
                ("Detailed Current User", "PI Detailed", "detailed-current@example.com",
                 "academic", "Detailed current sample", "2026-09-10", "2000.00"),
            )
            conn.commit()
            cur.close()
            conn.close()

        response = self.client.get(
            "/admin/panel?range=custom&start=2026-09-01&end=2026-09-30"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monthly net revenue bar chart", response.data)
        self.assertIn(b'title="2026-09: \xe2\x82\xb96,100.00"', response.data)


if __name__ == "__main__":
    unittest.main()
