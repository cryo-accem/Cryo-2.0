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
            session["must_change_password"] = False
            conn = database.get_db()
            session["admin_user_id"] = conn.execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    def _csrf_token(self):
        response = self.client.get("/admin/freezing")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Inventory details", response.data)
        self.assertIn(b"Blot seconds", response.data)
        self.assertIn(b"Copy Grid 1 values to all grids", response.data)
        self.assertIn(b"data-researcher=\"Freezing User\"", response.data)
        self.assertIn(b"data-sample=\"Mixed-grid sample\"", response.data)
        self.assertIn(b"Match existing inventory sample", response.data)
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
            "inventory_grid_sample_name": ["Managed sample", "Different sample"],
            "inventory_grid_type_id": [str(type_id) for type_id in grid_type_ids],
            "blot_seconds": ["4.5", "5.25"],
            "blot_force": ["8", "9.5"],
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
            """SELECT g.grid_code, s.sample_id, s.sample_name,
                      g.blot_seconds, g.blot_force,
                      sl.falcon_container_id, sl.grid_box_name,
                      sl.grid_position, g.grid_type_id
               FROM inventory_grids g
               JOIN inventory_samples s ON s.sample_id=g.sample_id
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
            [(row["sample_name"], row["falcon_container_id"], row["grid_box_name"],
              row["grid_position"], row["grid_type_id"], row["blot_seconds"], row["blot_force"])
             for row in grids],
            [("Managed sample", "FALCON-2", "Box-B", "A1", grid_type_ids[0], 4.5, 8),
             ("Different sample", "FALCON-2", "Box-B", "A2", grid_type_ids[1], 5.25, 9.5)],
        )
        self.assertNotEqual(grids[0]["sample_id"], grids[1]["sample_id"])
        inventory_search = self.client.get(
            f"/admin/inventory/grids?q={link['batch_code']}"
        )
        self.assertEqual(inventory_search.status_code, 200)
        self.assertIn(f"Freezing batch {link['batch_code']}".encode(), inventory_search.data)
        self.assertIn(b"2 grids", inventory_search.data)
        self.assertIn(b"Managed sample", inventory_search.data)
        self.assertIn(b"Different sample", inventory_search.data)
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
        second_details = self.client.get(f"/admin/inventory/grids/{grids[1]['grid_code']}")
        self.assertIn(b"5.25", second_details.data)
        self.assertIn(b"9.5", second_details.data)

    def test_freezing_completion_reuses_matching_existing_sample(self):
        grid_type_id = self._inventory_grid_type_id()[0]
        conn = database.get_db()
        conn.execute(
            """INSERT INTO inventory_samples
               (sample_code, sample_name, researcher_name, lab_name, created_at)
               VALUES ('ACCEM-S-01000', 'Mixed-grid sample', 'Freezing User',
                       'Example Lab', '2026-10-01 10:00:00')"""
        )
        sample_id = conn.execute(
            "SELECT sample_id FROM inventory_samples WHERE sample_code='ACCEM-S-01000'"
        ).fetchone()["sample_id"]
        conn.commit()
        conn.close()

        matches = self.client.get(
            "/admin/inventory/api/matching-samples"
            "?researcher=Freezing%20User&sample=Mixed-grid%20sample"
        )
        self.assertEqual(matches.status_code, 200, matches.location)
        self.assertEqual(matches.get_json()[0]["id"], sample_id)

        data = {
            "_csrf_token": self._csrf_token(),
            "user_category": "academic",
            "actual_grids": "1",
            "grid_source": "facility",
            "normal_grids": "1",
            "grid_management": "1",
            "inventory_grid_sample_name": ["Mixed-grid sample"],
            "inventory_sample_id": [str(sample_id)],
            "inventory_grid_type_id": [str(grid_type_id)],
            "blot_seconds": ["4"],
            "blot_force": ["8"],
            "inventory_falcon": ["FALCON-EXISTING"],
            "inventory_box": ["Box-1"],
            "inventory_position": ["A1"],
        }
        with patch("blueprints.admin.send_email"):
            response = self.client.post("/admin/freezing/complete/1", data=data)

        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        sample_count = conn.execute("SELECT COUNT(*) FROM inventory_samples").fetchone()[0]
        linked_sample_id = conn.execute(
            """SELECT g.sample_id FROM inventory_grids g
               JOIN freezing_booking_inventory fbi ON fbi.batch_id=g.batch_id
               WHERE fbi.booking_id=1"""
        ).fetchone()["sample_id"]
        conn.close()
        self.assertEqual(sample_count, 1)
        self.assertEqual(linked_sample_id, sample_id)

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
