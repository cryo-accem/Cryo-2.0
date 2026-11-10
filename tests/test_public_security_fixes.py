import datetime
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app
from inventory_service import (
    InventoryError, public_inventory_grids, set_grid_type_active,
)


class PublicSecurityFixTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "public-security.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.guest = self.app.test_client()

    # ── helpers ────────────────────────────────────────────────────────────
    def _insert_grid(self, researcher_name, sample_name="Sample A"):
        admin = self.app.test_client()
        conn = database.get_db()
        admin_id = conn.execute(
            "SELECT id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()["id"]
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 ORDER BY grid_type_id LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        with admin.session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_user_id"] = admin_id
        page = admin.get("/admin/inventory/register")
        token = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', page.data).group(1)
        response = admin.post(
            "/admin/inventory/register",
            data={
                "_csrf_token": token.decode(),
                "create_sample": "1",
                "sample_name": sample_name,
                "researcher_name": researcher_name,
                "lab_name": "Example Lab",
                "grid_type_id": str(grid_type_id),
                "grid_count": "1",
                "frozen_at": "2025-01-02T11:30",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        code = conn.execute(
            "SELECT grid_code FROM inventory_grids ORDER BY grid_id DESC LIMIT 1"
        ).fetchone()["grid_code"]
        conn.close()
        return code

    def _post_register(self, **overrides):
        page = self.guest.get("/register")
        token = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', page.data).group(1)
        data = {
            "_csrf_token": token.decode(),
            "reg_type": "screening",
            "user_name": "Alice Example",
            "pi_name": "Dr PI",
            "email": "alice@example.test",
            "origin": "Internal",
            "sample_name": "Protein",
            "grids": "1",
        }
        data.update(overrides)
        with patch("blueprints.screening.send_email"), \
                patch("blueprints.imaging.send_email"), \
                patch("blueprints.freezing.send_email"):
            return self.guest.post("/register", data=data, follow_redirects=True)

    def _count(self, table):
        conn = database.get_db()
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        conn.close()
        return count

    # ── fix 1: grid ownership on booking ───────────────────────────────────
    def test_partial_name_cannot_reserve_someone_elses_grid(self):
        code = self._insert_grid("Rebecca Stone")
        # Single letters and unrelated names are refused.  (Part of the
        # recorded name, e.g. "Rebecca" for "Rebecca Stone", is accepted:
        # people book under shorter names; see test_inventory_grid_labels.)
        for name in ("e", "Ste", "Rachel Green"):
            response = self._post_register(
                user_name=name, email=f"{name.lower().replace(' ', '')}@example.test",
                inventory_grid_codes=[code],
            )
            self.assertEqual(response.status_code, 200, name)
            self.assertIn(b"not associated with the entered username", response.data)
        self.assertEqual(self._count("screening_booking_grids"), 0)
        self.assertEqual(self._count("screening_bookings"), 0)

        response = self._post_register(
            user_name="  rebecca STONE ", email="rebecca@example.test",
            inventory_grid_codes=[code],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._count("screening_booking_grids"), 1)

    # ── fix 2: public inventory lookup ─────────────────────────────────────
    def test_public_inventory_requires_exact_name_and_minimum_length(self):
        code = self._insert_grid("Rebecca Stone")
        self.assertEqual(public_inventory_grids("Rebecca"), [])
        self.assertEqual(
            [row["grid_code"] for row in public_inventory_grids(" REBECCA stone ")],
            [code],
        )
        with self.assertRaises(InventoryError):
            public_inventory_grids("e")

        search = self.guest.get("/inventory/search?user_name=e")
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.get_json()["grids"], [])
        search = self.guest.get("/inventory/search?user_name=ebe")
        self.assertEqual(search.get_json()["grids"], [])
        page = self.guest.get("/inventory?username=e")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(code.encode(), page.data)
        self.assertIn(b"at least 2 characters", page.data)

        search = self.guest.get("/inventory/search?user_name=rebecca%20stone")
        self.assertEqual([g["grid_code"] for g in search.get_json()["grids"]], [code])

    # ── fix 4: numeric form input ──────────────────────────────────────────
    def test_non_numeric_counts_do_not_500(self):
        cases = (
            {"reg_type": "freezing", "grids_freezing": "abc",
             "freezing_date": "2099-01-05"},
            {"reg_type": "datacollecting", "grids": "1", "days": "abc"},
            {"reg_type": "screening", "grids": "abc"},
        )
        for overrides in cases:
            response = self._post_register(**overrides)
            self.assertEqual(response.status_code, 200, overrides)
            self.assertIn(b"Enter a valid number", response.data, overrides)
        self.assertEqual(self._count("freezing_bookings"), 0)
        self.assertEqual(self._count("bookings"), 0)
        self.assertEqual(self._count("screening_bookings"), 0)

    def test_unknown_registration_type_is_rejected(self):
        response = self._post_register(reg_type="bogus")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a valid booking type.", response.data)

    # ── fix 5: email format and column lengths ─────────────────────────────
    def test_invalid_email_and_overlong_fields_are_rejected(self):
        cases = (
            ({"email": "not-an-email"}, b"Enter a valid email address."),
            ({"email": "a@b"}, b"Enter a valid email address."),
            ({"email": "x" * 140 + "@example.test"}, b"150 characters or fewer"),
            ({"user_name": "u" * 101}, b"100 characters or fewer"),
            ({"pi_name": "p" * 101}, b"100 characters or fewer"),
            ({"sample_name": "s" * 151}, b"150 characters or fewer"),
            ({"esm": "i" * 151}, b"150 characters or fewer"),
            ({"user_name": "   "}, b"is required"),
        )
        for overrides, message in cases:
            response = self._post_register(**overrides)
            self.assertEqual(response.status_code, 200, overrides)
            self.assertIn(message, response.data, overrides)
        self.assertEqual(self._count("screening_bookings"), 0)

        response = self._post_register()
        self.assertEqual(self._count("screening_bookings"), 1)

    # ── fix 6: facility (IST) date ─────────────────────────────────────────
    def test_registration_date_uses_facility_timezone(self):
        with patch("blueprints.screening.facility_today",
                   return_value=datetime.date(2030, 3, 4)):
            self._post_register()
        conn = database.get_db()
        stored = conn.execute(
            "SELECT registration_date FROM screening_bookings"
        ).fetchone()["registration_date"]
        conn.close()
        self.assertEqual(str(stored), "2030-03-04")

    # ── fix 9: grid type toggle ────────────────────────────────────────────
    def test_grid_type_toggle_to_current_state_is_not_an_error(self):
        conn = database.get_db()
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        set_grid_type_active(grid_type_id, True)
        set_grid_type_active(grid_type_id, False)
        with self.assertRaises(InventoryError):
            set_grid_type_active(999999, True)

    # ── fix 8: statistics endpoint ─────────────────────────────────────────
    def test_statistics_errors_are_generic(self):
        with patch("blueprints.public.get_user_statistics",
                   side_effect=RuntimeError("secret internal detail")):
            response = self.guest.get("/api/user-statistics")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"secret internal detail", response.data)


if __name__ == "__main__":
    unittest.main()
