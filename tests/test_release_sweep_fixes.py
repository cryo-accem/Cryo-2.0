"""Fixes from the pre-release button sweep."""
import glob
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class TemplateSafetyTests(unittest.TestCase):
    def test_no_inline_handler_embeds_template_values(self):
        # A name with a quote inside onsubmit/onclick JavaScript can break out
        # and run code; questions go in data-confirm instead.
        root = os.path.join(os.path.dirname(__file__), "..", "templates")
        offenders = []
        for path in glob.glob(os.path.join(root, "*.html")):
            text = open(path, encoding="utf-8").read()
            if re.search(r'\bon[a-z]+="[^"]*\{\{', text):
                offenders.append(os.path.basename(path))
        self.assertEqual(offenders, [])


class SweepFixTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "sweep.sqlite3")
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
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    def test_only_one_finder_search_can_hold_the_lock(self):
        from blueprints import admin
        with self.app.app_context():
            self.assertTrue(admin._acquire_finder_lock())
            self.assertFalse(admin._acquire_finder_lock())
            admin._release_finder_lock()
            self.assertTrue(admin._acquire_finder_lock())
            admin._release_finder_lock()

    def test_csv_downloads_neutralise_formulas(self):
        import publication_finder
        text = publication_finder.results_csv({"not_on_website": [{
            "title": "=HYPERLINK(\"http://x\")", "authors": "+Evil", "year": 2026,
            "sources": [], "matches": [],
        }]})
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'+Evil", text)

    def test_page_messages_render_as_a_visible_banner(self):
        page = self.client.post(
            "/admin/maintenance/publications/99999/delete",
            data={"_csrf_token": self._csrf()}, follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn('class="flash-message flash-message--error" data-flash', page)
        self.assertIn("That publication was already removed.", page)

    def test_expired_staff_session_goes_to_sign_in_not_a_bare_400(self):
        guest = self.app.test_client()  # no session at all, like an expired one
        response = guest.post("/admin/maintenance/publications/1/delete", data={"_csrf_token": "old"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/"))
        page = guest.get("/admin/").get_data(as_text=True)
        self.assertIn("Your session expired", page)
        preview = self.app.test_client().post("/admin/billing/preview", data={"_csrf_token": "old"})
        self.assertEqual(preview.status_code, 401)
        self.assertIn("session has expired", preview.get_json()["error"])

    def test_expired_public_form_returns_to_the_page(self):
        guest = self.app.test_client()
        response = guest.post("/register", data={"_csrf_token": "old"},
                              headers={"Referer": "http://localhost/register"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/register"))

    def test_edit_follows_actual_grids_for_a_single_grid_type(self):
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO bookings (user_name, pi_name, email, origin, sample_name, grids, days,
               status, completion_date, actual_slots, actual_grids, number_of_grids,
               grid_source, grid_type, grid_breakdown)
               VALUES ('Meena', 'PI', 'm@example.com', 'External', 'S', 2, 1, 'completed',
                       '2026-11-01', 1, 2, 2, 'facility', 'normal_holey_carbon',
                       '{"normal_holey_carbon": 2, "gold_carbon_graphene": 0}')"""
        )
        conn.commit()
        booking = cur.lastrowid
        cur.close()
        conn.close()
        token = re.search(rb'name="_csrf_token" value="([^"]+)"', self.client.get("/admin/history").data).group(1).decode()
        form = {
            "_csrf_token": token, "user_name": "Meena", "pi_name": "PI", "email": "m@example.com",
            "origin": "External", "esm": "", "sample_name": "S", "grids": "2", "days": "1",
            "completion_date": "2026-11-01", "actual_slots": "1", "actual_grids": "3",
            "grid_source": "facility", "grid_type": "normal_holey_carbon",
            "clip_base_source": "facility", "normal_grids": "2", "gold_grids": "0",
        }
        page = self.client.post(f"/admin/history/imaging/{booking}/edit", data=form,
                                follow_redirects=True).get_data(as_text=True)
        self.assertNotIn("must add up", page)
        conn = database.get_db()
        row = conn.execute("SELECT number_of_grids FROM bookings WHERE id=?", (booking,)).fetchone()
        conn.close()
        self.assertEqual(row["number_of_grids"], 3)
        # Dates may be set ahead: a freezing booked for a busy day can be
        # done and completed later on whichever date suits.
        form["completion_date"] = "2026-12-15"
        self.client.post(f"/admin/history/imaging/{booking}/edit", data=form)
        conn = database.get_db()
        stored = conn.execute("SELECT completion_date FROM bookings WHERE id=?", (booking,)).fetchone()[0]
        conn.close()
        self.assertEqual(str(stored)[:10], "2026-12-15")

    def test_monthly_report_counts_grids_clipped_by_the_facility(self):
        from monthly_activity_report import _clipped_by_facility
        self.assertEqual(_clipped_by_facility({"number_of_grids": 3, "clipped_grids": 1}), 2)
        self.assertEqual(_clipped_by_facility({"number_of_grids": 4, "clipped_grids": 0}), 4)

    def test_rejected_booking_keeps_the_entries(self):
        guest = self.app.test_client()
        page = guest.get("/register").data
        token = re.search(rb'name="_csrf_token" value="([^"]+)"', page).group(1).decode()
        response = guest.post("/register", data={
            "_csrf_token": token, "reg_type": "freezing", "user_name": "Asha Rao",
            "pi_name": "PI", "email": "asha@example.com", "origin": "External", "esm": "",
            "sample_name": "Kinase", "grids_freezing": "2", "freezing_date": "2026-11-14",
        }, follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertIn("Enter the institute name.", html)
        self.assertIn('"user_name": "Asha Rao"', html)
        self.assertIn('"reg_type": "freezing"', html)
        conn = database.get_db()
        count = conn.execute("SELECT COUNT(*) FROM freezing_bookings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        # The draft is used once, then cleared.
        self.assertNotIn('"user_name": "Asha Rao"', guest.get("/register").get_data(as_text=True))

    def test_bills_to_send_card_opens_billing_filtered_to_not_sent(self):
        conn = database.get_db()
        for name, sent in (("Unsent One", None), ("Unsent Two", None), ("Sent One", "2026-11-01 10:00:00")):
            conn.execute(
                """INSERT INTO screening_bookings (user_name, pi_name, email, origin, sample_name,
                   grids, status, completion_date, charge_sheet_sent_at)
                   VALUES (?, 'Some PI', 'x@example.com', 'External', 'S', 1, 'completed', '2026-11-01', ?)""",
                (name, sent),
            )
        conn.execute(
            """INSERT INTO screening_bookings (user_name, pi_name, email, origin, sample_name,
               grids, status, completion_date) VALUES ('Free', 'Somnath Dutta', 'f@example.com',
               'Internal', 'S', 1, 'completed', '2026-11-01')"""
        )
        conn.commit()
        conn.close()
        panel = self.client.get("/admin/panel").get_data(as_text=True)
        self.assertIn('href="/admin/history?sheet=unsent"', panel)
        page = self.client.get("/admin/history?sheet=unsent").get_data(as_text=True)
        self.assertIn("data-sheet-filter", page)
        self.assertEqual(page.count('data-sheet="unsent"'), 2)
        self.assertEqual(page.count('data-sheet="sent"'), 1)
        self.assertEqual(page.count('data-sheet="none"'), 1)  # non-billable PI

    def _csrf(self):
        page = self.client.get("/admin/maintenance").data
        return re.search(rb'name="_csrf_token"\s+value="([^"]+)"', page).group(1).decode()


if __name__ == "__main__":
    unittest.main()
