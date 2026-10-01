import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminFreezingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "freezing-completion.sqlite3")
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
                """INSERT INTO freezing_bookings
                   (user_name, pi_name, email, origin, sample_name, grids,
                    freezing_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
                (
                    "Freezing User",
                    "Freezing PI",
                    "freezing@example.test",
                    "academic",
                    "Mixed-grid sample",
                    3,
                    "2026-10-02",
                ),
            )
            conn.commit()
            cur.close()
            conn.close()

        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def _csrf_token(self):
        response = self.client.get("/admin/freezing")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def test_completion_accepts_facility_grid_breakdown_without_single_type(self):
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                "/admin/freezing/complete/1",
                data={
                    "_csrf_token": self._csrf_token(),
                    "user_category": "academic",
                    "actual_grids": "3",
                    "grid_source": "facility",
                    "grid_type": "",
                    "normal_grids": "2",
                    "gold_grids": "1",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Completed freezing slots", response.data)
        self.assertIn(b"Mixed-grid sample", response.data)
        self.assertNotIn(b"Please select the grid type", response.data)
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute("SELECT status FROM freezing_bookings WHERE id=?", [1])
            self.assertEqual(cur.fetchone()["status"], "completed")
            cur.execute(
                """SELECT actual_grids, grid_type, grid_breakdown
                   FROM completed_freezing WHERE sample_name=?""",
                ["Mixed-grid sample"],
            )
            completed = cur.fetchone()
            self.assertEqual(completed["actual_grids"], 3)
            self.assertEqual(completed["grid_type"], "mixed")
            self.assertEqual(
                completed["grid_breakdown"],
                '{"normal_holey_carbon": 2, "gold_carbon_graphene": 1}',
            )
            cur.close()
            conn.close()


if __name__ == "__main__":
    unittest.main()
