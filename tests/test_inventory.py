import csv
import io
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app
from migrations.apply_grid_inventory import apply_migration


class GridInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "inventory.sqlite3")
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

    def csrf_token(self):
        response = self.client.get("/admin/inventory/register")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def create_batch(self, count=2):
        conn = database.get_db()
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 ORDER BY grid_type_id LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        response = self.client.post(
            "/admin/inventory/register",
            data={
                "_csrf_token": self.csrf_token(),
                "create_sample": "1",
                "sample_name": "Test sample",
                "researcher_name": "Researcher",
                "lab_name": "Example Lab",
                "external_reference": "REF-1",
                "grid_type_id": str(grid_type_id),
                "grid_count": str(count),
                "frozen_at": "2025-01-02T11:30",
                "preparation_notes": "Prepared on grid",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        codes = [
            row["grid_code"] for row in conn.execute(
                "SELECT grid_code FROM inventory_grids ORDER BY grid_id"
            ).fetchall()
        ]
        sample = conn.execute(
            "SELECT sample_code FROM inventory_samples ORDER BY sample_id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertTrue(sample["sample_code"].startswith("ACCEM-S-"))
        return codes

    def post_storage(self, code, container, box, position):
        return self.client.post(
            f"/admin/inventory/grids/{code}/storage",
            data={
                "_csrf_token": self.csrf_token(),
                "falcon_container_id": container,
                "grid_box_name": box,
                "grid_position": position,
                "notes": "",
            },
        )

    def test_migration_is_repeatable_and_preserves_existing_booking_data(self):
        conn = database.get_db()
        inventory_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory_grids'"
        ).fetchone()
        self.assertIsNotNone(inventory_table)
        conn.execute(
            """INSERT INTO bookings (user_name, sample_name, status)
               VALUES ('Existing user', 'Existing sample', 'waiting')"""
        )
        conn.commit()
        conn.close()

        apply_migration()
        apply_migration()

        conn = database.get_db()
        row = conn.execute("SELECT sample_name FROM bookings").fetchone()
        version_count = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_schema_migrations "
            "WHERE version IN ('001_grid_inventory', '002_freezing_inventory_link')"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(row["sample_name"], "Existing sample")
        self.assertEqual(version_count, 2)

    def test_inventory_dashboard_renders_stored_grid_count(self):
        codes = self.create_batch(1)
        response = self.post_storage(codes[0], "FALCON-1", "Box-A", "A1")
        self.assertEqual(response.status_code, 302)

        dashboard = self.client.get("/admin/inventory/")

        self.assertEqual(dashboard.status_code, 200)
        self.assertRegex(
            dashboard.data.decode(),
            r"<span>Stored</span>\s*<strong>1</strong>",
        )

    def test_batch_registration_assigns_unique_grid_ids_and_search_filters_work(self):
        codes = self.create_batch(3)
        self.assertEqual(len(set(codes)), 3)
        self.assertEqual(codes, ["ACCEM-G-0001", "ACCEM-G-0002", "ACCEM-G-0003"])

        response = self.client.get("/admin/inventory/grids?q=Test%20sample")
        self.assertEqual(response.status_code, 200)
        for code in codes:
            self.assertIn(code.encode(), response.data)
        csv_response = self.client.get("/admin/inventory/export.csv?q=Test%20sample")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(b"ACCEM-S-", csv_response.data)

        dashboard = self.client.get("/admin/inventory/")
        self.assertIn(b"Total grids", dashboard.data)
        self.assertIn(b">3</strong>", dashboard.data)

    def test_initial_storage_is_saved_per_grid_and_type_options_are_manageable(self):
        conn = database.get_db()
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        response = self.client.post(
            "/admin/inventory/register",
            data={
                "_csrf_token": self.csrf_token(),
                "create_sample": "1",
                "sample_name": "=1+1",
                "researcher_name": "Formula Test",
                "lab_name": "Safety Lab",
                "grid_type_id": str(grid_type_id),
                "grid_count": "2",
                "frozen_at": "2025-01-02T11:30",
                "store_immediately": "1",
                "storage_0_container": "FALCON-1",
                "storage_0_box": "Box-A",
                "storage_0_position": "A1",
                "storage_1_container": "FALCON-1",
                "storage_1_box": "Box-A",
                "storage_1_position": "A2",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        positions = conn.execute(
            "SELECT grid_position FROM storage_locations ORDER BY storage_location_id"
        ).fetchall()
        statuses = conn.execute(
            "SELECT current_status FROM inventory_grids ORDER BY grid_id"
        ).fetchall()
        conn.close()
        self.assertEqual([row["grid_position"] for row in positions], ["A1", "A2"])
        self.assertEqual([row["current_status"] for row in statuses], ["Stored", "Stored"])

        types_page = self.client.get("/admin/inventory/grid-types")
        self.assertEqual(types_page.status_code, 200)
        created_type = self.client.post(
            "/admin/inventory/grid-types",
            data={"_csrf_token": self.csrf_token(), "action": "create", "grid_type_name": "Custom support grid"},
        )
        self.assertEqual(created_type.status_code, 302)
        conn = database.get_db()
        type_row = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_name='Custom support grid'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(type_row)

        exported = self.client.get("/admin/inventory/export.csv")
        self.assertIn(b"'=1+1", exported.data)

    def test_invalid_batch_and_session_requests_do_not_write_partial_records(self):
        conn = database.get_db()
        type_id = conn.execute("SELECT grid_type_id FROM grid_types LIMIT 1").fetchone()["grid_type_id"]
        conn.close()
        invalid = self.client.post(
            "/admin/inventory/register",
            data={
                "_csrf_token": self.csrf_token(),
                "create_sample": "1",
                "sample_name": "Will not persist",
                "researcher_name": "Researcher",
                "lab_name": "Lab",
                "grid_type_id": str(type_id),
                "grid_count": "201",
                "frozen_at": "2025-01-02T11:30",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        conn = database.get_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) AS count FROM inventory_samples").fetchone()["count"],
            0,
        )
        conn.close()

        valid_grid = self.create_batch(1)[0]
        failed_session = self.client.post(
            "/admin/inventory/sessions",
            data={
                "_csrf_token": self.csrf_token(),
                "start_time": "2025-03-01T09:00",
                "grid_codes": [valid_grid, "ACCEM-G-9999"],
            },
        )
        self.assertEqual(failed_session.status_code, 200)
        conn = database.get_db()
        session_count = conn.execute("SELECT COUNT(*) AS count FROM data_sessions").fetchone()["count"]
        grid_status = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?", [valid_grid]
        ).fetchone()["current_status"]
        conn.close()
        self.assertEqual(session_count, 0)
        self.assertEqual(grid_status, "Frozen")

    def test_existing_sample_can_be_reused_without_duplicate_registration(self):
        self.create_batch(1)
        conn = database.get_db()
        sample = conn.execute(
            "SELECT sample_id FROM inventory_samples ORDER BY sample_id DESC LIMIT 1"
        ).fetchone()
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        response = self.client.post(
            "/admin/inventory/register",
            data={
                "_csrf_token": self.csrf_token(),
                "sample_id": str(sample["sample_id"]),
                "grid_type_id": str(grid_type_id),
                "grid_count": "1",
                "frozen_at": "2025-02-02T10:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        sample_count = conn.execute("SELECT COUNT(*) AS count FROM inventory_samples").fetchone()["count"]
        linked_grids = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_grids WHERE sample_id=?",
            [sample["sample_id"]],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(sample_count, 1)
        self.assertEqual(linked_grids, 2)

    def test_inventory_pages_bulk_clipping_and_activity_export_render(self):
        first, second = self.create_batch()
        for path in (
            "/admin/inventory/",
            "/admin/inventory/grids",
            "/admin/inventory/register",
            "/admin/inventory/storage",
            "/admin/inventory/sessions",
            "/admin/inventory/activity",
            f"/admin/inventory/grids/{first}",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

        bulk = self.client.post(
            "/admin/inventory/grids/bulk-clipping",
            data={
                "_csrf_token": self.csrf_token(),
                "clipped": "1",
                "selected_grid_codes": [first, second, "ACCEM-G-9999"],
            },
            follow_redirects=True,
        )
        self.assertEqual(bulk.status_code, 200)
        self.assertIn(first.encode(), bulk.data)
        self.assertIn(b"ACCEM-G-9999", bulk.data)
        activity = self.client.get("/admin/inventory/activity?format=csv")
        self.assertIn(b"clipping_status_change", activity.data)
        conn = database.get_db()
        updated = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_grids WHERE clipped=1"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(updated, 2)

    def test_invalid_search_filter_is_rejected_and_grid_type_can_be_deactivated(self):
        grid_type = self.client.post(
            "/admin/inventory/grid-types",
            data={
                "_csrf_token": self.csrf_token(),
                "action": "create",
                "grid_type_name": "Retired custom grid",
            },
        )
        self.assertEqual(grid_type.status_code, 302)
        conn = database.get_db()
        type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_name='Retired custom grid'"
        ).fetchone()["grid_type_id"]
        conn.close()
        response = self.client.post(
            "/admin/inventory/grid-types",
            data={
                "_csrf_token": self.csrf_token(),
                "action": "toggle",
                "grid_type_id": str(type_id),
                "active": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        invalid = self.client.get("/admin/inventory/grids?status=Unknown")
        self.assertEqual(invalid.status_code, 302)
        self.assertIn("/admin/inventory/grids", invalid.location)

    def test_clipping_is_individual_and_kept_separate_from_storage_status(self):
        first, second = self.create_batch()
        response = self.client.post(
            f"/admin/inventory/grids/{first}/clipping",
            data={"_csrf_token": self.csrf_token(), "clipped": "1", "notes": "Clipped individually"},
        )
        self.assertEqual(response.status_code, 302)
        self.post_storage(first, "FALCON-1", "Box-A", "A1")
        conn = database.get_db()
        rows = conn.execute(
            "SELECT grid_code, clipped, current_status FROM inventory_grids ORDER BY grid_id"
        ).fetchall()
        conn.close()
        self.assertEqual(rows[0]["clipped"], 1)
        self.assertEqual(rows[0]["current_status"], "Stored")
        self.assertEqual(rows[1]["clipped"], 0)
        self.assertEqual(rows[1]["current_status"], "Frozen")

    def test_grid_type_can_be_corrected_for_one_grid_with_audit_event(self):
        first, second = self.create_batch()
        conn = database.get_db()
        first_type = conn.execute(
            "SELECT grid_type_id FROM inventory_grids WHERE grid_code=?", [first]
        ).fetchone()["grid_type_id"]
        conn.close()
        self.client.post(
            "/admin/inventory/grid-types",
            data={
                "_csrf_token": self.csrf_token(),
                "action": "create",
                "grid_type_name": "Per-grid correction type",
            },
        )
        conn = database.get_db()
        second_type = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_name='Per-grid correction type'"
        ).fetchone()["grid_type_id"]
        conn.close()
        response = self.client.post(
            f"/admin/inventory/grids/{first}/type",
            data={"_csrf_token": self.csrf_token(), "grid_type_id": str(second_type)},
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        first_after = conn.execute(
            "SELECT grid_type_id FROM inventory_grids WHERE grid_code=?", [first]
        ).fetchone()["grid_type_id"]
        second_after = conn.execute(
            "SELECT grid_type_id FROM inventory_grids WHERE grid_code=?", [second]
        ).fetchone()["grid_type_id"]
        event = conn.execute(
            "SELECT old_value, new_value FROM grid_events WHERE grid_id=("
            "SELECT grid_id FROM inventory_grids WHERE grid_code=?) AND event_type='grid_type_change'",
            [first],
        ).fetchone()
        conn.close()
        self.assertNotEqual(first_type, first_after)
        self.assertEqual(second_after, first_type)
        self.assertEqual(event["new_value"], "Per-grid correction type")

    def test_storage_conflict_rolls_back_and_relocation_preserves_history(self):
        first, second = self.create_batch()
        self.assertEqual(self.post_storage(first, "FALCON-1", "Box-A", "A1").status_code, 302)
        self.assertEqual(self.post_storage(second, "FALCON-1", "Box-A", "A1").status_code, 302)
        conn = database.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) AS count FROM storage_locations "
                "WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?) "
                "AND removed_at IS NULL",
                [second],
            ).fetchone()["count"],
            0,
        )
        grid_before = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?", [second]
        ).fetchone()["current_status"]
        conn.close()
        self.assertEqual(grid_before, "Frozen")

        self.post_storage(second, "FALCON-1", "Box-B", "B2")
        self.post_storage(first, "FALCON-1", "Box-B", "B2")
        conn = database.get_db()
        first_active = conn.execute(
            "SELECT grid_box_name, grid_position FROM storage_locations "
            "WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?) "
            "AND removed_at IS NULL",
            [first],
        ).fetchone()
        conn.close()
        self.assertEqual((first_active["grid_box_name"], first_active["grid_position"]), ("Box-A", "A1"))

        self.post_storage(first, "FALCON-1", "Box-C", "C3")
        conn = database.get_db()
        locations = conn.execute(
            """SELECT grid_box_name, grid_position, removed_at FROM storage_locations
               WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?)
               ORDER BY storage_location_id""",
            [first],
        ).fetchall()
        events = conn.execute(
            "SELECT event_type FROM grid_events WHERE grid_id=("
            "SELECT grid_id FROM inventory_grids WHERE grid_code=?)",
            [first],
        ).fetchall()
        conn.close()
        self.assertEqual([row["grid_box_name"] for row in locations], ["Box-A", "Box-C"])
        self.assertIsNotNone(locations[0]["removed_at"])
        self.assertIsNone(locations[1]["removed_at"])
        self.assertIn("storage_relocation", [row["event_type"] for row in events])

    def test_session_can_use_multiple_grids_and_store_or_discard_per_grid(self):
        first, second = self.create_batch()
        started = self.client.post(
            "/admin/inventory/sessions",
            data={
                "_csrf_token": self.csrf_token(),
                "start_time": "2025-03-01T09:00",
                "instrument": "Cryo-EM",
                "grid_codes": [first, second],
            },
        )
        self.assertEqual(started.status_code, 302)
        conn = database.get_db()
        session_row = conn.execute(
            "SELECT session_id, session_code FROM data_sessions"
        ).fetchone()
        conn.close()
        session_id = session_row["session_id"]
        self.assertEqual(self.client.get(f"/admin/inventory/sessions/{session_id}").status_code, 200)

        stored = self.client.post(
            f"/admin/inventory/sessions/{session_id}/complete/{first}",
            data={
                "_csrf_token": self.csrf_token(), "outcome": "store",
                "falcon_container_id": "FALCON-2", "grid_box_name": "Box-A",
                "grid_position": "C1", "notes": "Return to storage",
            },
        )
        discarded = self.client.post(
            f"/admin/inventory/sessions/{session_id}/complete/{second}",
            data={"_csrf_token": self.csrf_token(), "outcome": "discard", "notes": "Low ice quality"},
        )
        self.assertEqual(stored.status_code, 302)
        self.assertEqual(discarded.status_code, 302)
        conn = database.get_db()
        first_state = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?", [first]
        ).fetchone()["current_status"]
        second_state = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?", [second]
        ).fetchone()["current_status"]
        session_state = conn.execute(
            "SELECT session_status FROM data_sessions WHERE session_id=?", [session_id]
        ).fetchone()["session_status"]
        event_types = [
            row["event_type"] for row in conn.execute(
                "SELECT event_type FROM grid_events WHERE grid_id=("
                "SELECT grid_id FROM inventory_grids WHERE grid_code=?)",
                [second],
            ).fetchall()
        ]
        conn.close()
        self.assertEqual(first_state, "Stored")
        self.assertEqual(second_state, "Discarded")
        self.assertEqual(session_state, "completed")
        self.assertIn("discard", event_types)
        self.assertIn("data_collection_completed", event_types)

        second_session = self.client.post(
            "/admin/inventory/sessions",
            data={"_csrf_token": self.csrf_token(), "start_time": "2025-04-01T10:00", "grid_codes": [first]},
        )
        self.assertEqual(second_session.status_code, 302)
        conn = database.get_db()
        sessions_for_grid = conn.execute(
            "SELECT COUNT(*) AS count FROM grid_session_links WHERE grid_id=("
            "SELECT grid_id FROM inventory_grids WHERE grid_code=?)",
            [first],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(sessions_for_grid, 2)

    def test_discarded_grid_cannot_be_stored_and_releases_position(self):
        first, second = self.create_batch()
        self.post_storage(first, "FALCON-3", "Box-1", "D1")
        started = self.client.post(
            "/admin/inventory/sessions",
            data={"_csrf_token": self.csrf_token(), "start_time": "2025-05-01T10:00", "grid_codes": [first]},
        )
        self.assertEqual(started.status_code, 302)
        conn = database.get_db()
        session_id = conn.execute("SELECT session_id FROM data_sessions").fetchone()["session_id"]
        conn.close()
        self.client.post(
            f"/admin/inventory/sessions/{session_id}/complete/{first}",
            data={"_csrf_token": self.csrf_token(), "outcome": "discard", "notes": "Discarded after session"},
        )
        self.post_storage(second, "FALCON-3", "Box-1", "D1")
        conn = database.get_db()
        first = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code='ACCEM-G-0001'"
        ).fetchone()
        location_count = conn.execute(
            "SELECT COUNT(*) AS count FROM storage_locations WHERE removed_at IS NULL"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(first["current_status"], "Discarded")
        self.assertEqual(location_count, 1)
        conn = database.get_db()
        first_history = conn.execute(
            "SELECT COUNT(*) AS count FROM storage_locations WHERE grid_id=("
            "SELECT grid_id FROM inventory_grids WHERE grid_code='ACCEM-G-0001')"
        ).fetchone()["count"]
        first_event_history = conn.execute(
            "SELECT COUNT(*) AS count FROM grid_events WHERE grid_id=("
            "SELECT grid_id FROM inventory_grids WHERE grid_code='ACCEM-G-0001')"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(first_history, 1)
        self.assertGreater(first_event_history, 3)

    def test_inventory_pages_require_admin_and_csv_formula_injection_is_neutralized(self):
        unauthenticated = self.app.test_client().get("/admin/inventory/")
        self.assertEqual(unauthenticated.status_code, 302)

        response = self.client.get("/admin/inventory/export.csv")
        self.assertEqual(response.status_code, 200)
        output = io.StringIO(response.data.decode("utf-8"))
        next(csv.reader(output))
        self.assertIn(b"Grid ID", response.data)


if __name__ == "__main__":
    unittest.main()
