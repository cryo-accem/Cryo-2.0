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

    def _csrf_token(self):
        response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create a slot", response.data)
        self.assertIn(b"Add data collecting slot", response.data)
        self.assertIn(b"Add screening slot", response.data)
        self.assertIn(b"Add freezing slot", response.data)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def test_admin_session_survives_public_pages(self):
        public_page = self.client.get("/about")
        admin_page = self.client.get("/admin/panel")

        self.assertEqual(public_page.status_code, 200)
        self.assertEqual(admin_page.status_code, 200)

    def test_create_slot_options_are_collapsed_below_csic_projects(self):
        response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        csic_projects_position = html.index('id="csic-projects"')
        create_slot_position = html.index('<details class="admin-slot-panel" id="create-slot"')

        self.assertGreater(create_slot_position, csic_projects_position)
        self.assertNotIn('<details class="admin-slot-panel" id="create-slot" data-known-user-url="/admin/panel/known-user" data-slot-type="" data-slot-saved="false" open', html)
        self.assertIn('<summary class="admin-slot-panel__toggle">', html)
        self.assertIn('href="#create-slot"', html)
        self.assertIn('role="group" aria-label="Choose slot type" hidden', html)
        self.assertIn('data-slot-type="datacollecting"', html)
        self.assertIn('data-slot-type="screening"', html)
        self.assertIn('data-slot-type="freezing"', html)
        self.assertIn('class="admin-slot-grid"', html)
        self.assertEqual(
            len(re.findall(r'<section class="full" data-admin-inventory-picker', html)),
            2,
        )
        self.assertEqual(html.count("/inventory/search"), 2)
        self.assertEqual(html.count('target="_blank" rel="opener">Search full inventory by username</a>'), 2)
        self.assertEqual(html.count('url.searchParams.set("admin_selection", "1")'), 1)
        self.assertEqual(html.count("Optional · no grid limit"), 2)
        self.assertNotIn(".slice(0, 4)", html)
        self.assertNotIn("slotsRemaining", html)
        self.assertNotRegex(html, r'<input name="(?:grids|days)"[^>]*\bmax=')
        self.assertNotIn("Grids (maximum", html)

    def _slot_data(self):
        return {
            "_csrf_token": self._csrf_token(),
            "user_name": "Admin Added User",
            "pi_name": "Admin Added PI",
            "email": "admin-added@example.test",
            "origin": "External",
            "esm": "Example Institute",
            "sample_name": "Admin Added Sample",
            "grids": "2",
        }

    def test_dashboard_can_add_data_collection_slot(self):
        data = self._slot_data()
        data["days"] = "3"
        with patch("blueprints.imaging.send_email"):
            response = self.client.post("/admin/panel/slots/datacollecting", data=data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/panel#create-slot")
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT user_name, grids, days, status FROM bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["user_name"], "Admin Added User")
        self.assertEqual(booking["grids"], 2)
        self.assertEqual(booking["days"], 3)
        self.assertEqual(booking["status"], "waiting")

    def test_csic_project_entry_saves_only_record_details_without_booking_or_revenue(self):
        response = self.client.post(
            "/admin/panel/slots/csic_project",
            data={
                "_csrf_token": self._csrf_token(),
                "user_name": "Project Contact",
                "company_name": "Example Cryo Company",
                "sample_count": "5",
                "sample_details": "Five samples for the CSIC project.",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CSIC project entry saved.", response.data)
        self.assertIn(b"No booking or billing calculation was created.", response.data)
        self.assertIn(b"Example Cryo Company", response.data)
        with self.app.app_context():
            conn = database.get_db()
            entry = conn.execute(
                """SELECT username, company_name, sample_count, sample_details
                   FROM csic_project_entries"""
            ).fetchone()
            self.assertEqual(entry["username"], "Project Contact")
            self.assertEqual(entry["company_name"], "Example Cryo Company")
            self.assertEqual(entry["sample_count"], 5)
            self.assertEqual(entry["sample_details"], "Five samples for the CSIC project.")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM screening_bookings").fetchone()[0],
                0,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM csic_projects").fetchone()[0], 0)
            conn.close()

    def test_csic_project_entry_requires_positive_whole_sample_count(self):
        response = self.client.post(
            "/admin/panel/slots/csic_project",
            data={
                "_csrf_token": self._csrf_token(),
                "user_name": "Project Contact",
                "company_name": "Example Cryo Company",
                "sample_count": "0",
                "sample_details": "Sample details",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sample count must be a whole number greater than zero.", response.data)
        with self.app.app_context():
            conn = database.get_db()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM csic_project_entries").fetchone()[0],
                0,
            )
            conn.close()

    def test_success_reopens_form_with_confirmation_and_add_another(self):
        data = self._slot_data()
        data["days"] = "3"
        with patch("blueprints.imaging.send_email"):
            response = self.client.post(
                "/admin/panel/slots/datacollecting",
                data=data,
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="create-slot" data-known-user-url="/admin/panel/known-user" data-slot-type="datacollecting" data-slot-saved="true" open', html)
        self.assertIn("Slot saved successfully.", html)
        self.assertIn("Add another", html)
        self.assertIn('id="admin-slot-form-datacollecting" method="post"', html)

    def test_failed_save_preserves_values_and_reopens_selected_form(self):
        data = self._slot_data()
        data["days"] = "3"
        data["origin"] = "Unrecognized"
        response = self.client.post(
            "/admin/panel/slots/datacollecting",
            data=data,
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-slot-type="datacollecting" data-slot-saved="false" open', html)
        self.assertIn("Choose a valid user origin.", html)
        self.assertIn('name="user_name" maxlength="100" value="Admin Added User"', html)
        self.assertIn('name="email" type="email" maxlength="150" value="admin-added@example.test"', html)
        self.assertIn('name="origin" required><option value="">Select origin</option>', html)
        self.assertIn("Choose a valid user origin.", html)

    def test_known_email_lookup_returns_saved_values_and_supports_completed_freezing(self):
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
            headers={"X-CSRF-Token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "known": True,
                "user_name": "Repeat User",
                "pi_name": "Repeat PI",
                "origin": "External",
                "esm": "",
            },
        )

    def test_known_email_lookup_requires_admin_and_valid_email(self):
        csrf_token = self._csrf_token()
        invalid = self.client.post(
            "/admin/panel/known-user",
            json={"email": "not-an-email"},
            headers={"X-CSRF-Token": csrf_token},
        )
        with self.client.session_transaction() as session:
            session.pop("admin_logged_in")
        unauthorized = self.client.post(
            "/admin/panel/known-user",
            json={"email": "known@example.test"},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(unauthorized.status_code, 401)

    def test_known_email_lookup_returns_json_when_admin_session_has_expired(self):
        csrf_token = self._csrf_token()
        with self.client.session_transaction() as session:
            session["admin_user_id"] = -1

        response = self.client.post(
            "/admin/panel/known-user",
            json={"email": "known@example.test"},
            headers={"X-CSRF-Token": csrf_token, "Accept": "application/json"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {"error": "Your admin session has expired. Please sign in again."},
        )

    def test_admin_data_collection_slot_can_exceed_standard_grid_and_day_limits(self):
        data = self._slot_data()
        data["grids"] = "5"
        data["days"] = "5"
        with patch("blueprints.imaging.send_email"):
            response = self.client.post("/admin/panel/slots/datacollecting", data=data)

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT grids, days FROM bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["grids"], 5)
        self.assertEqual(booking["days"], 5)

    def test_dashboard_can_add_screening_slot(self):
        with patch("blueprints.screening.send_email"):
            response = self.client.post(
                "/admin/panel/slots/screening",
                data=self._slot_data(),
            )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT esm, grids, days, status FROM screening_bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["esm"], "Example Institute")
        self.assertEqual(booking["grids"], 2)
        self.assertEqual(booking["days"], 1)
        self.assertEqual(booking["status"], "waiting")

    def test_admin_screening_slot_can_exceed_standard_grid_limit(self):
        data = self._slot_data()
        data["grids"] = "5"
        with patch("blueprints.screening.send_email"):
            response = self.client.post("/admin/panel/slots/screening", data=data)

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT grids FROM screening_bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["grids"], 5)

    def test_dashboard_can_add_freezing_slot(self):
        data = self._slot_data()
        data["freezing_date"] = "2030-01-02"
        with patch("blueprints.freezing.send_email"):
            response = self.client.post("/admin/panel/slots/freezing", data=data)

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT grids, freezing_date, status FROM freezing_bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["grids"], 2)
        self.assertEqual(booking["freezing_date"], "2030-01-02")
        self.assertEqual(booking["status"], "active")

    def test_admin_freezing_slot_can_exceed_grid_and_daily_capacity_limits(self):
        data = self._slot_data()
        data["grids"] = "9"
        data["freezing_date"] = "2030-01-02"
        with patch("blueprints.freezing.send_email"):
            response = self.client.post("/admin/panel/slots/freezing", data=data)

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            booking = conn.execute(
                "SELECT grids FROM freezing_bookings WHERE email=?",
                ["admin-added@example.test"],
            ).fetchone()
            conn.close()
        self.assertEqual(booking["grids"], 9)

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
            cur.close()
            conn.close()

        freezing_page = self.client.get("/admin/freezing")
        self.assertEqual(freezing_page.status_code, 200)
        self.assertIn(b"Delete slot", freezing_page.data)
        self.assertEqual(
            self.client.post(f"/admin/freezing/delete/{active_id}").status_code,
            400,
        )
        response = self.client.post(
            f"/admin/freezing/delete/{active_id}",
            data={"_csrf_token": self._csrf_token()},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            conn = database.get_db()
            self.assertIsNone(
                conn.execute("SELECT id FROM freezing_bookings WHERE id=?", [active_id]).fetchone()
            )
            self.assertIsNotNone(
                conn.execute("SELECT id FROM freezing_bookings WHERE id=?", [completed_id]).fetchone()
            )
            conn.close()

    def test_data_collection_and_screening_delete_require_post_and_csrf(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bookings (user_name, email, sample_name, status)
                   VALUES (?, ?, ?, 'waiting')""",
                ("Delete DC", "delete-dc@example.test", "Sample"),
            )
            data_collection_id = cur.lastrowid
            cur.execute(
                """INSERT INTO screening_bookings (user_name, email, sample_name, status)
                   VALUES (?, ?, ?, 'waiting')""",
                ("Delete Screening", "delete-screening@example.test", "Sample"),
            )
            screening_id = cur.lastrowid
            conn.commit()
            cur.close()
            conn.close()

        for path in (
            f"/admin/datacollecting/delete/{data_collection_id}",
            f"/admin/screening/delete/{screening_id}",
        ):
            self.assertEqual(self.client.get(path).status_code, 405)
            self.assertEqual(self.client.post(path).status_code, 400)

        token = self._csrf_token()
        for path in (
            f"/admin/datacollecting/delete/{data_collection_id}",
            f"/admin/screening/delete/{screening_id}",
        ):
            self.assertEqual(
                self.client.post(path, data={"_csrf_token": token}).status_code,
                302,
            )


if __name__ == "__main__":
    unittest.main()
