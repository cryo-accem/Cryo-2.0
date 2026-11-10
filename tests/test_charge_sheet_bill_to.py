"""The charge sheet "To" block is addressed to the PI as on the manual sheets."""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from billing_address import (
    address_lines, bill_to_from_form, build_address_book, missing_bill_to,
    suggested_bill_to,
)
from main import create_app

ADDRESS = {
    "bill_to_name": "Prof. Abhishek Mondal",
    "bill_to_designation": "Associate Professor",
    "bill_to_department": "Solid State and Structural Chemistry Unit",
    "bill_to_institute": "Indian Institute of Science",
    "bill_to_city": "Bangalore 560012",
}


class AddressHelperTests(unittest.TestCase):
    def test_lines_follow_the_manual_sheet_punctuation(self):
        self.assertEqual(address_lines(dict(ADDRESS)), [
            "Prof. Abhishek Mondal,", "Associate Professor,",
            "Solid State and Structural Chemistry Unit,",
            "Indian Institute of Science,", "Bangalore 560012.",
        ])

    def test_legacy_internal_rows_fall_back_to_pi_and_iisc(self):
        lines = address_lines({"origin": "Internal", "pi_name": "A Mondal", "user_name": "U"})
        self.assertEqual(lines, ["A Mondal,", "Indian Institute of Science,", "Bangalore 560012."])

    def test_required_details(self):
        internal = {"origin": "Internal"}
        self.assertEqual(missing_bill_to(ADDRESS, internal), [])
        partial = dict(ADDRESS, bill_to_designation="", bill_to_department="")
        self.assertEqual(missing_bill_to(partial, internal), ["Designation", "Department / Unit"])
        # A company has no designation/department requirement.
        self.assertEqual(missing_bill_to(partial, {"origin": "Industry"}), [])

    def test_address_is_remembered_per_pi(self):
        older = dict(ADDRESS, pi_name="Abhishek  Mondal", origin="Internal")
        book = build_address_book([older])
        fresh = {"pi_name": "abhishek mondal", "origin": "Internal"}
        self.assertEqual(suggested_bill_to(fresh, book), ADDRESS)

    def test_form_values_are_trimmed_and_absent_form_returns_none(self):
        self.assertIsNone(bill_to_from_form({"grid_source": "facility"}))
        form = {key: f"  {value}  " for key, value in ADDRESS.items()}
        self.assertEqual(bill_to_from_form(form), ADDRESS)


class SendChargeSheetBillToTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "bill-to.sqlite3")
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
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO screening_bookings
               (user_name, pi_name, email, origin, sample_name, grids, status,
                completion_date, actual_slots, actual_grids, number_of_grids, grid_source, grid_type)
               VALUES ('Sonali Kumari', 'Abhishek Mondal', 'sonali@example.com', 'Internal', 'S', 3,
                       'completed', '2026-11-10', 1, 3, 3, 'self_owned', '')"""
        )
        conn.commit()
        self.booking = cur.lastrowid
        cur.close()
        conn.close()

    def _csrf(self):
        page = self.client.get("/admin/history")
        return re.search(rb'name="_csrf_token" value="([^"]+)"', page.data).group(1).decode()

    def _stored(self):
        conn = database.get_db()
        row = conn.execute("SELECT * FROM screening_bookings WHERE id=?", (self.booking,)).fetchone()
        conn.close()
        return row

    def test_history_asks_for_the_address(self):
        html = self.client.get("/admin/history").get_data(as_text=True)
        self.assertIn("To (PI billing address)", html)
        self.assertIn("details needed", html)
        self.assertIn('name="bill_to_designation"', html)

    def test_send_is_refused_until_the_address_is_complete(self):
        with patch("blueprints.admin.send_email") as send:
            response = self.client.post(
                f"/admin/charge-sheet/screening/{self.booking}",
                data={"_csrf_token": self._csrf(), "grid_source": "self_owned",
                      "bill_to_name": "Prof. Abhishek Mondal"},
                follow_redirects=True,
            )
        send.assert_not_called()
        self.assertIn(b"Add the PI billing address", response.data)
        self.assertIsNone(self._stored()["charge_sheet_id"])

    def test_send_stores_and_prints_the_address(self):
        captured = {}
        with patch("blueprints.admin.send_email") as send, \
                patch("blueprints.admin.generate_charge_sheet",
                      side_effect=lambda row, service: captured.update(row) or b"%PDF"):
            self.client.post(
                f"/admin/charge-sheet/screening/{self.booking}",
                data={"_csrf_token": self._csrf(), "grid_source": "self_owned", **ADDRESS},
            )
        send.assert_called_once()
        self.assertEqual(address_lines(captured)[0], "Prof. Abhishek Mondal,")
        stored = self._stored()
        for column, value in ADDRESS.items():
            self.assertEqual(stored[column], value)
        # The next history page pre-fills the saved address.
        html = self.client.get("/admin/history").get_data(as_text=True)
        self.assertIn('value="Solid State and Structural Chemistry Unit"', html)


if __name__ == "__main__":
    unittest.main()
