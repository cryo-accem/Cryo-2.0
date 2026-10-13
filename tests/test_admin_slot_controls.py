import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminSlotControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "admin-slots.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def csrf_token(self):
        response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def slot_data(self):
        return {
            "_csrf_token": self.csrf_token(),
            "user_name": "Admin Added User",
            "pi_name": "Admin Added PI",
            "email": "admin-added@example.test",
            "origin": "External",
            "esm": "Example Institute",
            "sample_name": "Admin Added Sample",
            "grids": "2",
        }

    def test_panel_renders_all_slot_creation_forms(self):
        response = self.client.get("/admin/panel")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="create-slot"', html)
        self.assertIn('data-slot-type="datacollecting"', html)
        self.assertIn('data-slot-type="screening"', html)
        self.assertIn('data-slot-type="freezing"', html)
        self.assertIn('name="freezing_date"', html)

    def test_dashboard_can_add_data_collection_and_screening_slots(self):
        data = self.slot_data()
        data["days"] = "3"
        with patch("blueprints.imaging.send_email"):
            response = self.client.post("/admin/panel/slots/datacollecting", data=data)
        self.assertEqual(response.headers["Location"], "/admin/panel#create-slot")

        data = self.slot_data()
        with patch("blueprints.screening.send_email"):
            response = self.client.post("/admin/panel/slots/screening", data=data)
        self.assertEqual(response.headers["Location"], "/admin/panel#create-slot")

        with self.app.app_context():
            conn = database.get_db()
            collecting = conn.execute(
                "SELECT grids, days, status FROM bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            screening = conn.execute(
                "SELECT grids, days, status FROM screening_bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(tuple(collecting), (2, 3, "waiting"))
        self.assertEqual(tuple(screening), (2, 1, "waiting"))

    def test_save_confirmation_and_failure_preserve_form_values(self):
        data = self.slot_data()
        data["days"] = "2"
        with patch("blueprints.imaging.send_email"):
            response = self.client.post(
                "/admin/panel/slots/datacollecting", data=data, follow_redirects=True,
            )
        html = response.get_data(as_text=True)
        self.assertIn("Slot saved successfully.", html)
        self.assertIn("Add another", html)
        self.assertIn('data-slot-type="datacollecting"', html)
        self.assertIn('data-slot-saved="true"', html)
        self.assertIn('data-slot-saved="true"\n    open', html)

        data["origin"] = "invalid"
        response = self.client.post(
            "/admin/panel/slots/datacollecting", data=data, follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn("Your entries are preserved below", html)
        self.assertIn('value="Admin Added User"', html)
        self.assertIn("Choose a valid user origin.", html)

    def test_known_email_lookup_returns_editable_saved_details(self):
        with self.app.app_context():
            conn = database.get_db()
            conn.execute(
                """INSERT INTO completed_freezing
                   (user_name, pi_name, email, origin, sample_name, freezing_date)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("Repeat User", "Repeat PI", "repeat@example.test", "academic", "Old sample", "2030-01-02"),
            )
            conn.commit()
            conn.close()

        response = self.client.post(
            "/admin/panel/known-user",
            json={"email": "REPEAT@EXAMPLE.TEST"},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "known": True,
            "user_name": "Repeat User",
            "pi_name": "Repeat PI",
            "origin": "External",
            "esm": "",
        })

    def test_admin_can_create_over_capacity_freezing_booking(self):
        data = self.slot_data()
        data["grids"] = "9"
        data["freezing_date"] = "2030-01-02"
        with patch("blueprints.freezing.send_email"):
            response = self.client.post("/admin/panel/slots/freezing", data=data)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT grids, status FROM freezing_bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(tuple(booking), (9, "active"))

    def test_freezing_delete_requires_csrf_and_only_deletes_active_slots(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO freezing_bookings
                   (user_name, pi_name, email, origin, sample_name, grids,
                    freezing_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
                ("Active User", "PI", "active@example.test", "Internal", "Sample", 1, "2030-01-02"),
            )
            active_id = cur.lastrowid
            cur.execute(
                """INSERT INTO freezing_bookings
                   (user_name, pi_name, email, origin, sample_name, grids,
                    freezing_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')""",
                ("Completed User", "PI", "completed@example.test", "Internal", "Sample", 1, "2030-01-02"),
            )
            completed_id = cur.lastrowid
            conn.commit()
            conn.close()

        self.assertEqual(self.client.post(f"/admin/freezing/delete/{active_id}").status_code, 400)
        response = self.client.post(
            f"/admin/freezing/delete/{active_id}",
            data={"_csrf_token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            self.assertIsNone(conn.execute(
                "SELECT id FROM freezing_bookings WHERE id=?", [active_id],
            ).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM freezing_bookings WHERE id=?", [completed_id],
            ).fetchone())
            conn.close()


if __name__ == "__main__":
    unittest.main()
