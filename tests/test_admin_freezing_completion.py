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
        self.assertIn(b"Grid management (optional)", response.data)
        self.assertIn(b"Blot seconds", response.data)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def _inventory_grid_type_id(self):
        with self.app.app_context():
            conn = database.get_db()
            rows = conn.execute(
                "SELECT grid_type_id FROM grid_types WHERE active=1 "
                "ORDER BY grid_type_id LIMIT 2"
            ).fetchall()
            conn.close()
        return [row["grid_type_id"] for row in rows]

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
            self.assertEqual(
                cur.execute("SELECT COUNT(*) FROM freezing_booking_inventory").fetchone()[0],
                0,
            )
            cur.close()
            conn.close()

    def test_grid_management_registers_and_links_each_actual_grid(self):
        grid_type_ids = self._inventory_grid_type_id()
        self.assertEqual(len(grid_type_ids), 2)
        data = {
            "_csrf_token": self._csrf_token(),
            "user_category": "academic",
            "actual_grids": "2",
            "grid_source": "facility",
            "grid_type": "",
            "normal_grids": "2",
            "grid_management": "1",
            "inventory_sample_name": "Managed sample",
            "inventory_grid_type_id": [str(type_id) for type_id in grid_type_ids],
            "blot_seconds": "4.5",
            "blot_force": "8",
            "inventory_falcon": ["FALCON-2", "FALCON-2"],
            "inventory_box": ["Box-B", "Box-B"],
            "inventory_position": ["A1", "A2"],
        }

        with patch("blueprints.admin.send_email"):
            response = self.client.post("/admin/freezing/complete/1", data=data)

        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        completed = conn.execute(
            "SELECT status FROM freezing_bookings WHERE id=1"
        ).fetchone()
        link = conn.execute(
            """SELECT fbi.blot_seconds, fbi.blot_force, s.sample_name, fb.batch_code
               FROM freezing_booking_inventory fbi
               JOIN freezing_batches fb ON fb.batch_id=fbi.batch_id
               JOIN inventory_samples s ON s.sample_id=fb.sample_id
               WHERE fbi.booking_id=1"""
        ).fetchone()
        grids = conn.execute(
            """SELECT g.grid_code, sl.falcon_container_id, sl.grid_box_name,
                      sl.grid_position, g.grid_type_id
               FROM inventory_grids g
               JOIN freezing_booking_inventory fbi ON fbi.batch_id=g.batch_id
               JOIN storage_locations sl ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
               WHERE fbi.booking_id=1 ORDER BY g.grid_id"""
        ).fetchall()
        conn.close()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(link["blot_seconds"], 4.5)
        self.assertEqual(link["blot_force"], 8)
        self.assertEqual(link["sample_name"], "Managed sample")
        self.assertEqual(len(grids), 2)
        self.assertEqual(
            [(row["falcon_container_id"], row["grid_box_name"], row["grid_position"])
             + (row["grid_type_id"],)
             for row in grids],
            [("FALCON-2", "Box-B", "A1", grid_type_ids[0]),
             ("FALCON-2", "Box-B", "A2", grid_type_ids[1])],
        )
        with patch("blueprints.admin.send_email"):
            retry = self.client.post(
                "/admin/freezing/complete/1", data=data, follow_redirects=True
            )
        self.assertIn(b"That freezing booking is no longer active.", retry.data)
        conn = database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_grids").fetchone()[0], 2)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM freezing_booking_inventory").fetchone()[0],
            1,
        )
        conn.close()
        details = self.client.get(f"/admin/inventory/grids/{grids[0]['grid_code']}")
        self.assertEqual(details.status_code, 200)
        self.assertIn(b"Blot seconds", details.data)
        self.assertIn(b"4.5", details.data)

    def test_invalid_grid_position_rolls_back_billing_and_inventory(self):
        grid_type_id = self._inventory_grid_type_id()[0]
        data = {
            "_csrf_token": self._csrf_token(),
            "user_category": "academic",
            "actual_grids": "2",
            "grid_source": "facility",
            "normal_grids": "2",
            "grid_management": "1",
            "inventory_sample_name": "Should roll back",
            "inventory_grid_type_id": [str(grid_type_id), str(grid_type_id)],
            "blot_seconds": "4",
            "blot_force": "8",
            "inventory_falcon": ["FALCON-2", "FALCON-2"],
            "inventory_box": ["Box-B", "Box-B"],
            "inventory_position": ["A1", ""],
        }

        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                "/admin/freezing/complete/1", data=data, follow_redirects=True
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Grid position is required.", response.data)
        conn = database.get_db()
        self.assertEqual(
            conn.execute("SELECT status FROM freezing_bookings WHERE id=1").fetchone()["status"],
            "active",
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM completed_freezing").fetchone()["n"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM inventory_samples").fetchone()["n"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM inventory_grids").fetchone()["n"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
