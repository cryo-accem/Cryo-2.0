"""Regression tests for bugs found by the end-to-end workflow runs."""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app
from revenue import billed_origin


class BilledOriginTests(unittest.TestCase):
    def test_keeps_origin_when_category_matches_or_is_absent(self):
        self.assertEqual(billed_origin("External", None), "External")
        self.assertEqual(billed_origin("External", "academic"), "External")
        self.assertEqual(billed_origin("Internal", "internal"), "Internal")

    def test_uses_completion_category_when_it_differs(self):
        self.assertEqual(billed_origin("Internal", "industry"), "Industry")
        self.assertEqual(billed_origin("Industry", "academic"), "External")
        self.assertEqual(billed_origin("External", "internal"), "Internal")


class WorkflowRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "e2e-regressions.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            conn = database.get_db()
            session["admin_user_id"] = conn.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    def _execute(self, sql, params=()):
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        last_id = cur.lastrowid
        cur.close()
        conn.close()
        return last_id

    def _csrf(self, path):
        page = self.client.get(path)
        return re.search(rb'name="_csrf_token" value="([^"]+)"', page.data).group(1).decode()

    def _screening(self, status):
        return self._execute(
            """INSERT INTO screening_bookings
               (user_name, pi_name, email, origin, sample_name, grids, status)
               VALUES ('Grid User', 'Grid PI', 'grid@example.com', 'External', 'S', 1, ?)""",
            (status,),
        )

    def test_loaded_screening_with_inventory_grids_cannot_be_deleted(self):
        booking = self._screening("ongoing")
        self._execute(
            "INSERT INTO screening_booking_grids (screening_booking_id, grid_id, linked_at) "
            "VALUES (?, 1, '2026-11-10 10:00:00')",
            (booking,),
        )
        response = self.client.post(
            f"/admin/screening/delete/{booking}",
            data={"_csrf_token": self._csrf("/admin/screening")},
            follow_redirects=True,
        )
        self.assertIn(b"Complete it (store or discard each grid)", response.data)
        conn = database.get_db()
        still_there = conn.execute(
            "SELECT COUNT(*) FROM screening_bookings WHERE id=?", (booking,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(still_there, 1)

    def test_waiting_screening_can_still_be_deleted(self):
        booking = self._screening("waiting")
        self.client.post(
            f"/admin/screening/delete/{booking}",
            data={"_csrf_token": self._csrf("/admin/screening")},
        )
        conn = database.get_db()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM screening_bookings WHERE id=?", (booking,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0)

    def test_failed_charge_sheet_pdf_releases_the_database(self):
        booking = self._execute(
            """INSERT INTO bookings
               (user_name, pi_name, email, origin, sample_name, grids, days, status,
                completion_date, actual_slots, actual_grids, number_of_grids,
                grid_source, grid_type)
               VALUES ('Pdf User', 'Pdf PI', 'pdf@example.com', 'External', 'S', 1, 1,
                       'completed', '2026-09-01', 1, 1, 1, 'self_owned', '')"""
        )
        token = self._csrf("/admin/history")
        self.app.testing = False  # let the exception become a 500 response
        with patch("blueprints.admin.generate_charge_sheet", side_effect=RuntimeError("pdf")), \
                patch("blueprints.admin.send_email") as send:
            response = self.client.post(
                f"/admin/charge-sheet/imaging/{booking}", data={"_csrf_token": token}
            )
        self.app.testing = True
        self.assertEqual(response.status_code, 500)
        send.assert_not_called()
        # A follow-up write must not hit "database is locked", and no sheet ID
        # may have been stored for the failed send.
        self._execute("UPDATE bookings SET sample_name='after' WHERE id=?", (booking,))
        conn = database.get_db()
        row = conn.execute(
            "SELECT charge_sheet_id, charge_sheet_sent_at FROM bookings WHERE id=?", (booking,)
        ).fetchone()
        conn.close()
        self.assertIsNone(row["charge_sheet_id"])
        self.assertIsNone(row["charge_sheet_sent_at"])


if __name__ == "__main__":
    unittest.main()
