import datetime
import os
import re
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import database
from charge_sheet import _billing_lines
from main import create_app
from revenue import apply_csic_final_amount, calculate_charge_sheet, is_non_billable_booking


class AdminCsicBillingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "csic-billing.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def _csrf_token(self):
        response = self.client.get("/admin/history")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def _add_completed_booking(self, *, origin="Industry", pi_name="Example PI"):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, email, origin, esm, sample_name, grids, days,
                    status, registration_date, completion_date, actual_slots, actual_grids,
                    number_of_grids, service_stage, grid_source, grid_type, slot_charge,
                    subtotal, gst_amount, grand_total, total_billed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "Industry User",
                    pi_name,
                    "industry@example.test",
                    origin,
                    "Example Institute",
                    "Screening sample",
                    2,
                    1,
                    datetime.date.today().isoformat(),
                    datetime.date.today().isoformat(),
                    1,
                    2,
                    2,
                    "Screening / Clipping",
                    "facility",
                    "normal_holey_carbon",
                    "100000.00",
                    "100000.00",
                    "18000.00",
                    "118000.00",
                    "118000.00",
                ),
            )
            booking_id = cur.lastrowid
            conn.commit()
            cur.close()
            conn.close()
        return booking_id

    def _add_ongoing_booking(self, *, origin="Industry", pi_name="Example PI"):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, email, origin, esm, sample_name, grids, days,
                    status, registration_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ongoing', ?)""",
                (
                    "Screening User",
                    pi_name,
                    "screening@example.test",
                    origin,
                    "Example Institute",
                    "Screening sample",
                    2,
                    1,
                    datetime.date.today().isoformat(),
                ),
            )
            booking_id = cur.lastrowid
            conn.commit()
            cur.close()
            conn.close()
        return booking_id

    def _complete_screening(self, booking_id, **extra):
        data = {
            "_csrf_token": self._csrf_token(),
            "user_category": "industry",
            "service_stage": "Screening / Clipping",
            "actual_slots": "1",
            "actual_grids": "2",
            "grid_source": "self_owned",
            "grid_type": "",
            "clipped_grids": "0",
            **extra,
        }
        with patch("blueprints.admin.send_email"):
            return self.client.post(
                f"/admin/screening/complete/{booking_id}", data=data
            )

    def test_screening_completion_saves_csic_amount_and_rejects_missing_amount(self):
        booking_id = self._add_ongoing_booking()
        response = self._complete_screening(
            booking_id,
            is_csic="1",
            csic_final_amount="10000.00",
            csic_discount_reason="Approved facility discount",
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT status, is_csic, csic_final_amount, csic_original_amount, "
                "csic_adjustment, subtotal, gst_amount, total_billed "
                "FROM screening_bookings WHERE id=?",
                [booking_id],
            ).fetchone()
            audit = conn.execute(
                "SELECT action, calculated_amount, final_amount, discount_reason "
                "FROM csic_booking_audit WHERE service_key='screening' AND booking_id=?",
                [booking_id],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["status"], "completed")
        self.assertEqual(booking["is_csic"], 1)
        self.assertEqual(str(booking["csic_final_amount"]), "10000")
        self.assertEqual(str(booking["total_billed"]), "10000")
        self.assertEqual(str(booking["csic_original_amount"]), "64900")
        self.assertEqual(str(booking["subtotal"]), "8474.58")
        self.assertEqual(str(booking["gst_amount"]), "1525.42")
        self.assertEqual(str(booking["csic_adjustment"]), "-46525.42")
        self.assertEqual(audit["action"], "completion")
        self.assertEqual(audit["discount_reason"], "Approved facility discount")
        self.assertEqual(str(audit["final_amount"]), "10000")
        panel = self.client.get("/admin/panel")
        self.assertIn(
            b"CSIC gross</span><strong>\xe2\x82\xb910,000.00",
            panel.data,
        )

        missing_amount_id = self._add_ongoing_booking()
        response = self._complete_screening(missing_amount_id, is_csic="1")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT status, is_csic, csic_final_amount FROM screening_bookings WHERE id=?",
                [missing_amount_id],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["status"], "ongoing")
        self.assertEqual(booking["is_csic"], 0)
        self.assertIsNone(booking["csic_final_amount"])

    def test_screening_completion_shows_side_by_side_storage_and_billing_panes(self):
        response = self.client.get("/admin/screening")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"completion-pane--storage", response.data)
        self.assertIn(b"completion-pane--billing", response.data)
        self.assertIn(b"Grid box", response.data)
        self.assertIn(b"Final CSIC invoice amount (including GST)", response.data)

    def test_screening_completion_json_errors_allow_retry_and_reject_duplicate_completion(self):
        booking_id = self._add_ongoing_booking()
        token = self._csrf_token()
        invalid_response = self.client.post(
            f"/admin/screening/complete/{booking_id}",
            data={
                "_csrf_token": token,
                "user_category": "industry",
                "service_stage": "Screening / Clipping",
                "actual_slots": "",
                "actual_grids": "2",
                "grid_source": "self_owned",
                "clipped_grids": "0",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIn("Actual slots and grids are required", invalid_response.json["error"])
        with self.app.app_context():
            conn = database.get_db()
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM screening_bookings WHERE id=?", [booking_id]
                ).fetchone()["status"],
                "ongoing",
            )
            conn.close()

        success_response = self._complete_screening(booking_id)
        self.assertEqual(success_response.status_code, 302)
        duplicate_response = self.client.post(
            f"/admin/screening/complete/{booking_id}",
            data={
                "_csrf_token": self._csrf_token(),
                "user_category": "industry",
                "service_stage": "Screening / Clipping",
                "actual_slots": "1",
                "actual_grids": "2",
                "grid_source": "self_owned",
                "clipped_grids": "0",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(duplicate_response.status_code, 400)
        self.assertIn("no longer ongoing", duplicate_response.json["error"])

    def test_named_non_billable_pi_cannot_be_switched_to_csic(self):
        booking_id = self._add_ongoing_booking(pi_name="Professor Somnath Dutta")
        response = self._complete_screening(
            booking_id,
            is_csic="1",
            csic_final_amount="10000.00",
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT status, is_csic, total_billed FROM screening_bookings WHERE id=?",
                [booking_id],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["status"], "ongoing")
        self.assertEqual(booking["is_csic"], 0)
        self.assertIsNone(booking["total_billed"])

    def test_csic_booking_remains_billable_and_uses_the_confirmed_amount(self):
        baseline_revenue = self.client.get("/admin/panel")
        regular_net_pattern = (
            rb'<article class="revenue-primary-stat"><span>Net revenue '
            rb'\(except CSIC\)</span><strong>([^<]+)'
        )
        gross_billed_pattern = rb'<div><span>Gross billed</span><strong>([^<]+)'
        baseline_regular_net = re.search(regular_net_pattern, baseline_revenue.data)
        baseline_gross_billed = re.search(gross_billed_pattern, baseline_revenue.data)
        self.assertIsNotNone(baseline_regular_net)
        self.assertIsNotNone(baseline_gross_billed)
        booking_id = self._add_completed_booking()
        token = self._csrf_token()

        response = self.client.post(
            f"/admin/billing/csic/screening/{booking_id}",
            data={
                "_csrf_token": token,
                "is_csic": "1",
                "csic_final_amount": "70000.00",
                "csic_discount_reason": "Approved industry discount",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CSIC revenue", response.data)
        self.assertIn(b"billable", response.data)
        self.assertIn(b"Generate &amp; Send", response.data)
        self.assertIn(b"Bill not sent", response.data)

        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT is_csic, csic_final_amount, total_billed, grand_total, subtotal, "
                "gst_amount, csic_adjustment "
                "FROM screening_bookings WHERE id=?", [booking_id]
            ).fetchone()
            self.assertEqual(booking["is_csic"], 1)
            self.assertEqual(str(booking["csic_final_amount"]), "70000")
            self.assertEqual(str(booking["total_billed"]), "70000")
            self.assertEqual(str(booking["grand_total"]), "70000")
            self.assertEqual(str(booking["subtotal"]), "59322.03")
            self.assertEqual(str(booking["gst_amount"]), "10677.97")
            self.assertLess(Decimal(str(booking["csic_adjustment"])), 0)
            audit = conn.execute(
                "SELECT action, discount_reason FROM csic_booking_audit "
                "WHERE service_key='screening' AND booking_id=?",
                [booking_id],
            ).fetchone()
            self.assertEqual(audit["action"], "moved_to_csic")
            self.assertEqual(audit["discount_reason"], "Approved industry discount")
            conn.close()

        revenue_page = self.client.get("/admin/panel")
        self.assertIn(
            b"CSIC net revenue</span><strong>\xe2\x82\xb959,322.03",
            revenue_page.data,
        )
        self.assertEqual(
            re.search(regular_net_pattern, revenue_page.data).group(1),
            baseline_regular_net.group(1),
        )
        self.assertEqual(
            re.search(gross_billed_pattern, revenue_page.data).group(1),
            baseline_gross_billed.group(1),
        )

        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/charge-sheet/screening/{booking_id}",
                data={"_csrf_token": token, "grid_source": "facility"},
            )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT total_billed, charge_sheet_sent_at FROM screening_bookings WHERE id=?",
                [booking_id],
            ).fetchone()
            self.assertEqual(str(booking["total_billed"]), "70000")
            self.assertIsNotNone(booking["charge_sheet_sent_at"])
            conn.close()

        payment_response = self.client.post(
            f"/admin/payment/screening/{booking_id}",
            data={
                "_csrf_token": token,
                "payment_received": "1",
                "amount_received": "1.00",
                "transaction_reference": "not tracked",
            },
        )
        self.assertEqual(payment_response.status_code, 302)

        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT is_csic, payment_status, amount_received, charge_sheet_sent_at "
                "FROM screening_bookings WHERE id=?",
                [booking_id],
            ).fetchone()
            self.assertEqual(booking["is_csic"], 1)
            self.assertEqual(booking["payment_status"], "Payment Verified")
            self.assertIsNone(booking["amount_received"])
            self.assertIsNotNone(booking["charge_sheet_sent_at"])
            conn.close()

    def test_csic_toggle_supports_external_academic_but_not_internal(self):
        academic_id = self._add_completed_booking(origin="External")
        response = self.client.post(
            f"/admin/billing/csic/screening/{academic_id}",
            data={
                "_csrf_token": self._csrf_token(),
                "is_csic": "1",
                "csic_final_amount": "50000.00",
                "csic_discount_reason": "Approved academic discount",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            conn = database.get_db()
            academic = conn.execute(
                "SELECT is_csic, csic_final_amount FROM screening_bookings WHERE id=?",
                [academic_id],
            ).fetchone()
            self.assertEqual(academic["is_csic"], 1)
            self.assertEqual(str(academic["csic_final_amount"]), "50000")
            conn.close()

        internal_id = self._add_completed_booking(origin="Internal")
        response = self.client.post(
            f"/admin/billing/csic/screening/{internal_id}",
            data={
                "_csrf_token": self._csrf_token(),
                "is_csic": "1",
                "csic_final_amount": "50000.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Only external academic and industry bookings", response.data)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT is_csic FROM screening_bookings WHERE id=?", [internal_id]
            ).fetchone()
            self.assertEqual(booking["is_csic"], 0)
            conn.close()

    def test_only_somnath_dutta_is_the_named_non_billable_pi_exception(self):
        self.assertTrue(is_non_billable_booking({"pi_name": "Professor Somnath Dutta"}))
        self.assertTrue(is_non_billable_booking({"pi_name": "Dr. Somnath Dutta"}))
        self.assertFalse(is_non_billable_booking({"pi_name": "Professor Somnath Dutta Lab"}))
        self.assertFalse(is_non_billable_booking({"pi_name": "Somnath Research Group"}))
        self.assertFalse(is_non_billable_booking({"pi_name": "Professor Somnath Kumar"}))

    def test_csic_discount_reconciles_invoice_lines_and_gst_to_final_amount(self):
        base = calculate_charge_sheet(
            "industry",
            "Screening / Clipping",
            2,
            "facility",
            "normal_holey_carbon",
            actual_slots=1,
        )
        adjusted = apply_csic_final_amount(base, "70000.00", "industry")
        row = {
            **adjusted,
            "grid_source": "facility",
            "number_of_grids": 2,
        }
        line_total = sum((amount for _, _, amount in _billing_lines(row, "Screening")), start=0)
        self.assertEqual(line_total, adjusted["subtotal"])
        self.assertEqual(adjusted["subtotal"] + adjusted["gst_amount"], Decimal("70000.00"))
        self.assertLess(adjusted["csic_adjustment"], 0)

    def test_admin_panel_keeps_csic_revenue_insight_but_removes_project_controls(self):
        with self.app.app_context():
            conn = database.get_db()
            conn.execute(
                "INSERT INTO csic_projects (company_name, year, net_amount) VALUES (?, ?, ?)",
                ["Historical CSIC Income", datetime.date.today().year, "100000.00"],
            )
            conn.commit()
            conn.close()

        response = self.client.get("/admin/panel")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CSIC net revenue", response.data)
        self.assertIn(b"100,000.00", response.data)
        self.assertNotIn(b"Historical CSIC Income", response.data)
        self.assertNotIn(b"Add CSIC project", response.data)
        self.assertNotIn(b'id="csic-projects"', response.data)

    def test_old_csic_project_entry_routes_are_removed(self):
        response = self.client.post(
            "/admin/csic-projects",
            data={"_csrf_token": self._csrf_token()},
        )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
