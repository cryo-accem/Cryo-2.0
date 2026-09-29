import os
import re
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import database
from main import create_app


class AdminHistoryEditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "history-edit.sqlite3")
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
                   (user_name, pi_name, email, origin, esm, sample_name,
                    grids, days, status, completion_date, actual_slots,
                    actual_grids, number_of_grids, grid_source, grid_type,
                    total_billed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)""",
                ("Original User", "Original PI", "original@example.com",
                 "External", "", "Original sample", 2, 1, "2026-09-01",
                 1, 1, 1, "facility", "normal_holey_carbon", "500.00"),
            )
            cur.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, grids,
                    days, status, completion_date, charge_sheet_sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, CURRENT_TIMESTAMP)""",
                ("Sent User", "Sent PI", "sent@example.com", "External",
                 "Sent sample", 1, 1, "2026-09-02"),
            )
            cur.execute(
                """INSERT INTO completed_freezing
                   (user_name, pi_name, email, origin, sample_name, grids,
                    freezing_date, actual_grids, number_of_grids, service_stage,
                    grid_source, grid_type, grid_breakdown)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Freeze User", "Freeze PI", "freeze@example.com", "External",
                 "Freeze sample", 2, "2026-09-03", 2, 2,
                 "Freezing / Grid Registration", "facility", "mixed",
                 '{"normal_holey_carbon": 1, "gold_carbon_graphene": 1}'),
            )
            conn.commit()
            cur.close()
            conn.close()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def _csrf_token(self):
        response = self.client.get("/admin/history")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def test_history_exposes_edit_before_sending_only(self):
        page = self.client.get("/admin/history").data

        self.assertIn(b"Edit booking before sending", page)
        self.assertIn(b"Save changes &amp; recalculate charges", page)

    def test_edit_updates_slot_details_and_recalculates_charges(self):
        response = self.client.post(
            "/admin/history/imaging/1/edit",
            data={
                "_csrf_token": self._csrf_token(),
                "user_name": "Corrected User",
                "pi_name": "Corrected PI",
                "email": "corrected@example.com",
                "origin": "External",
                "esm": "",
                "sample_name": "Corrected sample",
                "grids": "2",
                "days": "2",
                "completion_date": "2026-09-10",
                "actual_slots": "2",
                "actual_grids": "2",
                "grid_source": "facility",
                "grid_type": "normal_holey_carbon",
                "clipped_grids": "0",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Booking details updated and charges recalculated.", response.data)
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """SELECT user_name, pi_name, email, sample_name, completion_date,
                          actual_slots, actual_grids, total_billed
                   FROM bookings WHERE id=?""",
                [1],
            )
            booking = cur.fetchone()
            self.assertEqual(booking["user_name"], "Corrected User")
            self.assertEqual(booking["pi_name"], "Corrected PI")
            self.assertEqual(booking["email"], "corrected@example.com")
            self.assertEqual(booking["sample_name"], "Corrected sample")
            self.assertEqual(str(booking["completion_date"]), "2026-09-10")
            self.assertEqual(str(booking["actual_slots"]), "2")
            self.assertEqual(booking["actual_grids"], 2)
            self.assertEqual(Decimal(str(booking["total_billed"])), Decimal("38940.00"))
            cur.close()
            conn.close()

    def test_sent_booking_cannot_be_edited(self):
        response = self.client.post(
            "/admin/history/imaging/2/edit",
            data={"_csrf_token": self._csrf_token()},
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute("SELECT user_name FROM bookings WHERE id=?", [2])
            self.assertEqual(cur.fetchone()["user_name"], "Sent User")
            cur.close()
            conn.close()
        page = self.client.get("/admin/history").data
        self.assertEqual(page.count(b"Edit booking before sending"), 2)

    def test_freezing_edit_preserves_and_recalculates_mixed_grid_charges(self):
        response = self.client.post(
            "/admin/history/freezing/1/edit",
            data={
                "_csrf_token": self._csrf_token(),
                "user_name": "Freeze User",
                "pi_name": "Freeze PI",
                "email": "freeze@example.com",
                "origin": "External",
                "sample_name": "Corrected freeze sample",
                "grids": "2",
                "completion_date": "2026-09-04",
                "actual_grids": "2",
                "grid_source": "facility",
                "grid_type": "",
                "normal_grids": "1",
                "gold_grids": "1",
            },
            follow_redirects=True,
        )

        self.assertIn(b"Booking details updated and charges recalculated.", response.data)
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """SELECT sample_name, freezing_date, grid_type, grid_breakdown,
                          total_billed
                   FROM completed_freezing WHERE id=?""",
                [1],
            )
            booking = cur.fetchone()
            self.assertEqual(booking["sample_name"], "Corrected freeze sample")
            self.assertEqual(str(booking["freezing_date"]), "2026-09-04")
            self.assertEqual(booking["grid_type"], "mixed")
            self.assertEqual(
                booking["grid_breakdown"],
                '{"normal_holey_carbon": 1, "gold_carbon_graphene": 1}',
            )
            self.assertEqual(Decimal(str(booking["total_billed"])), Decimal("8850.00"))
            cur.close()
            conn.close()


if __name__ == "__main__":
    unittest.main()
