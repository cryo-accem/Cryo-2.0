import datetime
import os
import re
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import database
from blueprints.admin import _revenue_dashboard
from main import create_app


class AdminServiceNonBillableTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "non-billable-services.sqlite3")
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
        response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 200)
        return re.search(
            rb'name="_csrf_token"\s+value="([^"]+)"', response.data
        ).group(1).decode()

    def _insert_ongoing_booking(self, table, email):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                f"""INSERT INTO {table}
                    (user_name, pi_name, email, origin, esm, sample_name,
                     grids, days, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ongoing')""",
                ["Test User", "Test PI", email, "academic", "Test Institute",
                 "Test Sample", 3, 1],
            )
            booking_id = cur.lastrowid
            conn.commit()
            cur.close()
            conn.close()
        return booking_id

    def test_screening_and_data_collection_complete_without_billing_fields(self):
        screening_id = self._insert_ongoing_booking(
            "screening_bookings", "screening@example.test"
        )
        data_collection_id = self._insert_ongoing_booking(
            "bookings", "data-collection@example.test"
        )
        token = self._csrf_token()

        with patch("blueprints.admin.send_email"):
            screening_response = self.client.post(
                f"/admin/screening/complete/{screening_id}",
                data={"_csrf_token": token, "actual_slots": "2", "actual_grids": "3"},
            )
            data_collection_response = self.client.post(
                f"/admin/datacollecting/complete/{data_collection_id}",
                data={"_csrf_token": token, "actual_slots": "2", "actual_grids": "3"},
            )

        self.assertEqual(screening_response.status_code, 302)
        self.assertEqual(data_collection_response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            for table, booking_id in (
                ("screening_bookings", screening_id),
                ("bookings", data_collection_id),
            ):
                row = conn.execute(
                    f"""SELECT status, actual_slots, actual_grids, number_of_grids,
                               grid_source, grid_charge, slot_charge, subtotal,
                               gst_amount, grand_total, total_billed,
                               bill_generated_at
                        FROM {table} WHERE id=?""",
                    [booking_id],
                ).fetchone()
                self.assertEqual(row["status"], "completed")
                self.assertEqual(Decimal(str(row["actual_slots"])), Decimal("2"))
                self.assertEqual(row["actual_grids"], 3)
                self.assertEqual(row["number_of_grids"], 3)
                self.assertIsNone(row["grid_source"])
                for field in (
                    "grid_charge", "slot_charge", "subtotal", "gst_amount",
                    "grand_total", "total_billed",
                ):
                    self.assertEqual(Decimal(str(row[field])), Decimal("0"))
                self.assertIsNone(row["bill_generated_at"])
            conn.close()

    def test_completed_screening_and_data_collection_records_have_no_billing_actions(self):
        screening_id = self._insert_ongoing_booking(
            "screening_bookings", "screening@example.test"
        )
        data_collection_id = self._insert_ongoing_booking(
            "bookings", "data-collection@example.test"
        )
        with self.app.app_context():
            conn = database.get_db()
            for table, booking_id in (
                ("screening_bookings", screening_id),
                ("bookings", data_collection_id),
            ):
                conn.execute(
                    f"""UPDATE {table} SET status='completed', completion_date=?,
                               total_billed=1234, grand_total=1234,
                               payment_status='Payment Pending'
                        WHERE id=?""",
                    [datetime.date.today().isoformat(), booking_id],
                )
            conn.commit()
            conn.close()

        token = self._csrf_token()
        page = self.client.get("/admin/history")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Completed \xe2\x80\x94 non-billable", page.data)
        self.assertNotIn(b"Generate &amp; Send", page.data)
        self.assertNotIn(b'<input type="checkbox" data-slot-select', page.data)

        with patch("blueprints.admin.send_email") as send_email:
            send_response = self.client.post(
                f"/admin/charge-sheet/imaging/{data_collection_id}",
                data={"_csrf_token": token, "grid_source": "facility"},
            )
            preview_response = self.client.post(
                f"/admin/charge-sheet/screening/{screening_id}/preview",
                data={"_csrf_token": token, "grid_source": "facility"},
            )
            payment_response = self.client.post(
                f"/admin/payment/imaging/{data_collection_id}",
                data={
                    "_csrf_token": token,
                    "origin": "academic",
                    "status": "Payment Verified",
                },
            )
        self.assertEqual(send_response.status_code, 302)
        self.assertEqual(preview_response.status_code, 302)
        self.assertEqual(payment_response.status_code, 302)
        send_email.assert_not_called()

        with self.app.app_context():
            conn = database.get_db()
            row = conn.execute(
                """SELECT total_billed, grand_total, payment_status,
                          charge_sheet_sent_at
                   FROM bookings WHERE id=?""",
                [data_collection_id],
            ).fetchone()
            self.assertEqual(Decimal(str(row["total_billed"])), Decimal("1234"))
            self.assertEqual(Decimal(str(row["grand_total"])), Decimal("1234"))
            self.assertEqual(row["payment_status"], "Payment Pending")
            self.assertIsNone(row["charge_sheet_sent_at"])
            conn.close()

    def test_revenue_dashboard_excludes_all_screening_and_data_collection_records(self):
        today = datetime.date.today()
        start = today.replace(day=1).isoformat()
        end = today.isoformat()
        with self.app.test_request_context(
            f"/admin/panel?range=custom&start={start}&end={end}"
        ):
            with self.app.app_context():
                conn = database.get_db()
                cur = conn.cursor()
                baseline = _revenue_dashboard(cur)["totals"]
                for table, email in (
                    ("bookings", "data-collection@example.test"),
                    ("screening_bookings", "screening@example.test"),
                ):
                    cur.execute(
                        f"""INSERT INTO {table}
                            (user_name, pi_name, email, origin, sample_name,
                             status, completion_date, actual_slots,
                             actual_grids, number_of_grids, total_billed,
                             grand_total, subtotal)
                            VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)""",
                        ["Test User", "Test PI", email, "academic", "Test Sample",
                         today.isoformat(), 2, 3, 3, "1234.00", "1234.00", "1000.00"],
                    )
                conn.commit()
                after = _revenue_dashboard(cur)["totals"]
                self.assertEqual(after["net"], baseline["net"])
                self.assertEqual(after["gross"], baseline["gross"])
                self.assertEqual(after["billed"], baseline["billed"])
                cur.close()
                conn.close()

    def test_admin_billing_preview_rejects_screening_and_data_collection(self):
        token = self._csrf_token()
        for stage in ("Screening / Clipping", "Data Collection"):
            response = self.client.post(
                "/admin/billing/preview",
                data={
                    "_csrf_token": token,
                    "user_category": "academic",
                    "service_stage": stage,
                    "number_of_grids": "3",
                    "grid_source": "facility",
                    "grid_type": "normal_holey_carbon",
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.get_json()["error"],
                "Screening and data collection are not billed here.",
            )


if __name__ == "__main__":
    unittest.main()
