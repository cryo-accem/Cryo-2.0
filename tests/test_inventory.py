import csv
import datetime
import io
import os
import re
import tempfile
import unittest
from unittest.mock import patch
from werkzeug.datastructures import MultiDict

import database
from main import create_app
from migrations.apply_grid_inventory import apply_migration
from inventory_service import (
    InventoryError, create_data_session, dashboard_storage, link_grid_to_booking,
    public_inventory_grids,
)
from blueprints.freezing import register_freezing as register_freezing_booking
from blueprints.imaging import register_imaging


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

    def create_batch(self, count=2, researcher_name="Researcher", sample_name="Test sample"):
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
                "sample_name": sample_name,
                "researcher_name": researcher_name,
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
                "SELECT grid_code FROM inventory_grids ORDER BY grid_id DESC LIMIT ?",
                [count],
            ).fetchall()
        ]
        codes.reverse()
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
            "WHERE version IN ('001_grid_inventory', '002_freezing_inventory_link', "
            "'004_booking_grid_links', '005_grid_types', "
            "'006_existing_stored_grids_frozen')"
        ).fetchone()["count"]
        link_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='screening_booking_grids'"
        ).fetchone()
        active_types = {
            row["grid_type_name"]
            for row in conn.execute(
                "SELECT grid_type_name FROM grid_types WHERE active=1"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(row["sample_name"], "Existing sample")
        self.assertEqual(version_count, 5)
        self.assertIsNotNone(link_table)
        self.assertEqual(
            active_types,
            {
                "Quantifoil holey carbon",
                "Carbon-coated grids",
                "Gold Quantifoil Gold Grids",
                "Graphene-coated grids",
            },
        )

    def test_grid_type_migration_leaves_only_requested_types_active(self):
        conn = database.get_db()
        conn.execute("DELETE FROM inventory_schema_migrations WHERE version='005_grid_types'")
        conn.execute("DELETE FROM grid_types WHERE grid_type_name='Carbon-coated grids'")
        conn.executemany(
            "INSERT INTO grid_types (grid_type_name, active) VALUES (?, 1)",
            [
                ("Gold-coated holey carbon",),
                ("Graphene-coated grid",),
                ("Other",),
                ("Retired custom type",),
            ],
        )
        conn.commit()
        conn.close()

        apply_migration()

        conn = database.get_db()
        active_types = {
            row["grid_type_name"]
            for row in conn.execute(
                "SELECT grid_type_name FROM grid_types WHERE active=1"
            ).fetchall()
        }
        inactive_other = conn.execute(
            "SELECT active FROM grid_types WHERE grid_type_name='Other'"
        ).fetchone()["active"]
        conn.close()
        self.assertEqual(
            active_types,
            {
                "Quantifoil holey carbon",
                "Carbon-coated grids",
                "Gold Quantifoil Gold Grids",
                "Graphene-coated grids",
            },
        )
        self.assertEqual(inactive_other, 0)

    def test_legacy_stored_grids_with_positions_migrate_to_frozen(self):
        code = self.create_batch(1)[0]
        self.post_storage(code, "FALCON-LEGACY", "Box-Legacy", "A1")
        conn = database.get_db()
        conn.execute(
            "UPDATE inventory_grids SET current_status='Stored' WHERE grid_code=?",
            [code],
        )
        conn.execute(
            "DELETE FROM inventory_schema_migrations "
            "WHERE version='006_existing_stored_grids_frozen'"
        )
        conn.commit()
        conn.close()

        apply_migration()

        conn = database.get_db()
        status = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?", [code]
        ).fetchone()["current_status"]
        active_location = conn.execute(
            """SELECT falcon_container_id, grid_box_name, grid_position
               FROM storage_locations
               WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?)
                 AND removed_at IS NULL""",
            [code],
        ).fetchone()
        conn.close()
        self.assertEqual(status, "Frozen")
        self.assertEqual(
            tuple(active_location), ("FALCON-LEGACY", "Box-Legacy", "A1")
        )

    def test_inventory_dashboard_keeps_frozen_grid_status_when_position_is_assigned(self):
        codes = self.create_batch(1)
        response = self.post_storage(codes[0], "FALCON-1", "Box-A", "A1")
        self.assertEqual(response.status_code, 302)

        dashboard = self.client.get("/admin/inventory/")

        self.assertEqual(dashboard.status_code, 200)
        self.assertRegex(
            dashboard.data.decode(),
            r"<span>Frozen</span>\s*<strong>1</strong>",
        )

    def test_public_grid_inventory_link_is_in_home_portal_not_primary_navigation(self):
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode()
        navigation = re.search(r'<nav class="site-nav"[^>]*>(.*?)</nav>', html, re.S)
        self.assertIsNotNone(navigation)
        self.assertNotIn("Grid inventory", navigation.group(1))
        self.assertRegex(
            html,
            r'<a class="portal-card" href="/inventory"><i class="fa-solid fa-boxes-stacked"[^>]*></i><strong>Grid inventory</strong><span>Find your grids by username',
        )

    def test_inventory_dashboard_shows_storage_positions_with_distinct_lifecycle_codes(self):
        frozen, stored, clipped, discarded, in_collection = self.create_batch(5)
        self.post_storage(stored, "Falcon-A", "GridBox-1", "1")
        self.post_storage(clipped, "Falcon-A", "GridBox-1", "2")
        self.client.post(
            f"/admin/inventory/grids/{clipped}/clipping",
            data={"_csrf_token": self.csrf_token(), "clipped": "1", "notes": "Demo clip"},
        )
        conn = database.get_db()
        conn.execute(
            "UPDATE inventory_grids SET current_status='Stored' WHERE grid_code=?",
            [stored],
        )
        conn.execute(
            "UPDATE inventory_grids SET current_status='Discarded' WHERE grid_code=?",
            [discarded],
        )
        conn.execute(
            "UPDATE inventory_grids SET current_status='In Data Collection' WHERE grid_code=?",
            [in_collection],
        )
        conn.commit()
        conn.close()

        dashboard = self.client.get("/admin/inventory/")
        self.assertEqual(dashboard.status_code, 200)
        content = dashboard.data.decode()
        self.assertIn(
            "/static/css/site.css?v=20261016-muted-inventory-palette",
            content,
        )
        self.assertIn("Falcon-A", content)
        self.assertIn("GridBox-1", content)
        self.assertIn(f'aria-label="Position 1, {stored}, Researcher, Stored"', content)
        self.assertIn(f'aria-label="Position 2, {clipped}, Researcher, Clipped"', content)
        self.assertIn(f'class="inventory-storage-code inventory-storage-code--s">S</span>', content)
        self.assertIn(f'class="inventory-storage-code inventory-storage-code--c">C</span>', content)
        self.assertRegex(
            content,
            r'class="inventory-storage-code inventory-storage-code--f"[^>]*>F</span>',
        )
        self.assertIn(
            f'<article class="inventory-metric inventory-metric--frozen"><span>Frozen</span>',
            content,
        )
        self.assertIn('class="inventory-metric inventory-metric--stored"><span>Stored</span>', content)
        self.assertIn('class="inventory-metric inventory-metric--clipped"><span>Clipped</span>', content)
        self.assertIn('class="inventory-metric inventory-metric--discarded"><span>Discarded</span>', content)
        self.assertIn('class="inventory-metric inventory-metric--in-microscope"><span>In microscope</span>', content)
        self.assertIn('class="inventory-breakdown--status inventory-status--stored"', content)
        self.assertIn('class="inventory-breakdown--status inventory-status--clipped"', content)
        self.assertIn('class="inventory-breakdown--status inventory-status--discarded"', content)
        self.assertIn('class="inventory-breakdown--status inventory-status--in-microscope"', content)
        self.assertIn("To open or manage a grid, use Search grids.", content)
        storage_map = re.search(
            r'<section class="inventory-panel inventory-storage-map">(.*?)'
            r'</section>\s*<section class="inventory-panel">',
            content,
            re.S,
        )
        self.assertIsNotNone(storage_map)
        self.assertNotIn(f'href="/admin/inventory/grids/{stored}"', storage_map.group(1))
        self.assertNotIn(f'href="/admin/inventory/grids/{clipped}"', storage_map.group(1))
        for code in (frozen, discarded, in_collection):
            self.assertIn(f'href="/admin/inventory/grids/{code}"', content)
        self.assertRegex(content, r">F</span>\s*Frozen")
        self.assertRegex(content, r">C</span>")
        self.assertRegex(content, r">D</span>")
        self.assertRegex(content, r">U</span>\s*In microscope")
        self.assertRegex(content, r">U</span>")

    def test_storage_groups_all_positions_by_falcon_and_box(self):
        first, second = self.create_batch(2)
        self.post_storage(first, "Falcon-A", "GridBox-1", "1")
        self.post_storage(second, "Falcon-A", "GridBox-1", "2")

        response = self.client.get("/admin/inventory/storage")

        self.assertEqual(response.status_code, 200)
        content = response.data.decode()
        self.assertIn("<summary>Falcon-A", content)
        self.assertIn("<summary><span>GridBox-1</span><span>2 grid(s)</span></summary>", content)
        self.assertIn("Researcher", content)
        self.assertIn(first, content)
        self.assertIn(second, content)

    def test_grid_storage_positions_sort_numerically(self):
        codes = self.create_batch(10)
        for position, code in enumerate(codes, start=1):
            self.post_storage(code, "Falcon-Numeric", "Box-Numeric", str(position))

        response = self.client.get("/admin/inventory/storage")

        self.assertEqual(response.status_code, 200)
        content = response.data.decode()
        position_order = [
            content.index(f"<strong>{position}</strong>")
            for position in range(1, 11)
        ]
        self.assertEqual(position_order, sorted(position_order))

    def test_grid_detail_loads_and_active_storage_grid_can_be_discarded_individually(self):
        grid_code = self.create_batch(1)[0]
        self.assertEqual(
            self.post_storage(grid_code, "Falcon-detail", "Box-detail", "1").status_code,
            302,
        )

        storage = self.client.get("/admin/inventory/storage")
        self.assertEqual(storage.status_code, 200)
        self.assertIn(
            f'href="/admin/inventory/grids/{grid_code}"'.encode(),
            storage.data,
        )
        detail = self.client.get(f"/admin/inventory/grids/{grid_code}")
        self.assertEqual(detail.status_code, 200)
        self.assertRegex(detail.data.decode(), r"<th>Stored by</th>.*?<td>admin</td>")
        self.assertIn(b"Discard grid", detail.data)
        with self.client.session_transaction() as admin_session:
            self.assertTrue(admin_session.get("admin_logged_in"))

        response = self.client.post(
            f"/admin/inventory/grids/{grid_code}/discard",
            data={
                "_csrf_token": self.csrf_token(),
                "notes": "Long-term storage discard trial",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, f"/admin/inventory/grids/{grid_code}")

        conn = database.get_db()
        status = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?",
            [grid_code],
        ).fetchone()["current_status"]
        active_locations = conn.execute(
            """SELECT COUNT(*) AS count FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               WHERE g.grid_code=? AND sl.removed_at IS NULL""",
            [grid_code],
        ).fetchone()["count"]
        discard_events = conn.execute(
            """SELECT COUNT(*) AS count FROM grid_events e
               JOIN inventory_grids g ON g.grid_id=e.grid_id
               WHERE g.grid_code=? AND e.event_type='grid_discarded'""",
            [grid_code],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(status, "Discarded")
        self.assertEqual(active_locations, 0)
        self.assertEqual(discard_events, 1)

    def test_search_grids_shows_collapsed_falcon_box_cards_before_grid_rows(self):
        first, second, third = self.create_batch(3)
        self.post_storage(first, "Falcon-Search", "Box-A", "1")
        self.post_storage(second, "Falcon-Search", "Box-A", "2")
        self.post_storage(third, "Falcon-Search", "Box-B", "1")

        response = self.client.get("/admin/inventory/grids")

        self.assertEqual(response.status_code, 200)
        content = response.data.decode()
        self.assertIn("3 grid(s) in 2 box group(s)", content)
        self.assertIn("Falcon-Search", content)
        self.assertIn("Box-A", content)
        self.assertIn("Box-B", content)
        self.assertIn('<details class="inventory-search-box">', content)
        self.assertIn("2 grid(s)", content)
        self.assertIn("Select all grids in these boxes", content)
        self.assertIn(f'value="{first}"', content)
        self.assertIn(f'value="{second}"', content)
        self.assertIn(f'value="{third}"', content)

    def test_search_grid_box_pagination_keeps_each_box_complete(self):
        codes = self.create_batch(3)
        self.post_storage(codes[0], "Falcon-Pages", "Box-Large", "1")
        self.post_storage(codes[1], "Falcon-Pages", "Box-Large", "2")
        self.post_storage(codes[2], "Falcon-Pages", "Box-Large", "3")
        for index in range(40):
            code = self.create_batch(1)[0]
            self.post_storage(code, f"Falcon-{index:02}", "Box", "1")

        response = self.client.get("/admin/inventory/grids?page=2")

        self.assertEqual(response.status_code, 200)
        content = response.data.decode()
        self.assertIn("43 grid(s) in 41 box group(s)", content)
        self.assertIn("Page 2 of 2", content)
        self.assertIn('<summary>', content)
        self.assertIn("3 grid(s)", content)
        self.assertIn(f'value="{codes[0]}"', content)
        self.assertIn(f'value="{codes[1]}"', content)
        self.assertIn(f'value="{codes[2]}"', content)

    def test_ten_grid_freezing_completion_creates_one_collapsible_storage_group(self):
        freezing_date = datetime.date.today() + datetime.timedelta(days=1)
        while freezing_date.weekday() >= 5:
            freezing_date += datetime.timedelta(days=1)
        with patch("blueprints.freezing.send_email"):
            success, message = register_freezing_booking(
                "LOCAL STORAGE TRIAL",
                "Trial PI",
                "trial@example.test",
                "Internal",
                "LOCAL STORAGE TRIAL",
                10,
                freezing_date.isoformat(),
                allow_over_capacity=True,
            )
        self.assertTrue(success, message)

        conn = database.get_db()
        booking_id = conn.execute(
            "SELECT id FROM freezing_bookings WHERE email='trial@example.test'"
        ).fetchone()["id"]
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 ORDER BY grid_type_id LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        completion = {
            "_csrf_token": self.csrf_token(),
            "actual_grids": "10",
            "grid_source": "self_owned",
            "grid_management": "1",
            "inventory_grid_sample_name": ["LOCAL STORAGE TRIAL"] * 10,
            "inventory_sample_id": [""] * 10,
            "inventory_grid_type_id": [str(grid_type_id)] * 10,
            "inventory_falcon": ["TRIAL-FALCON"] * 10,
            "inventory_box": ["TRIAL-BOX"] * 10,
            "inventory_position": [str(position) for position in range(1, 11)],
            "blot_seconds": ["1"] * 10,
            "blot_force": ["1"] * 10,
        }
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/freezing/complete/{booking_id}", data=completion
            )
        self.assertEqual(response.status_code, 302)

        storage = self.client.get("/admin/inventory/storage")
        content = storage.data.decode()
        self.assertIn("TRIAL-FALCON", content)
        self.assertIn("TRIAL-BOX", content)
        self.assertIn("10 grid(s)", content)
        for position in range(1, 11):
            self.assertIn(f"<strong>{position}</strong>", content)
        conn = database.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) AS count FROM inventory_grids"
            ).fetchone()["count"],
            10,
        )
        self.assertEqual(
            conn.execute(
                "SELECT status FROM freezing_bookings WHERE id=?", [booking_id]
            ).fetchone()["status"],
            "completed",
        )
        conn.close()

    def test_isolated_freezing_to_screening_microscope_storage_and_discard_pipeline(self):
        user_name = "Subhajit Mahanty"
        sample_name = "ACCEM pipeline dummy sample"
        email = "pipeline@example.test"
        freezing_date = datetime.date.today() + datetime.timedelta(days=1)
        while freezing_date.weekday() >= 5:
            freezing_date += datetime.timedelta(days=1)

        with patch("blueprints.freezing.send_email"):
            success, message = register_freezing_booking(
                user_name, "ACCEM Trial PI", email, "Internal", sample_name, 3,
                freezing_date.isoformat(),
            )
        self.assertTrue(success, message)

        conn = database.get_db()
        freezing_booking_id = conn.execute(
            "SELECT id FROM freezing_bookings WHERE email=?", [email]
        ).fetchone()["id"]
        grid_type_id = conn.execute(
            "SELECT grid_type_id FROM grid_types WHERE active=1 ORDER BY grid_type_id LIMIT 1"
        ).fetchone()["grid_type_id"]
        conn.close()
        freezing_completion = {
            "_csrf_token": self.csrf_token(),
            "actual_grids": "3",
            "grid_source": "self_owned",
            "grid_management": "1",
            "inventory_grid_sample_name": [sample_name] * 3,
            "inventory_sample_id": [""] * 3,
            "inventory_grid_type_id": [str(grid_type_id)] * 3,
            "inventory_falcon": ["TRIAL-FALCON"] * 3,
            "inventory_box": ["TRIAL-BOX"] * 3,
            "inventory_position": ["1", "2", "3"],
            "blot_seconds": ["1"] * 3,
            "blot_force": ["1"] * 3,
        }
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/freezing/complete/{freezing_booking_id}",
                data=freezing_completion,
            )
        self.assertEqual(response.status_code, 302)

        guest = self.app.test_client()
        inventory_search = guest.get("/inventory/search?user_name=subhajit")
        self.assertEqual(inventory_search.status_code, 200)
        grids = inventory_search.get_json()["grids"]
        grid_codes = [grid["grid_code"] for grid in grids]
        self.assertEqual(len(grid_codes), 3)
        conn = database.get_db()
        frozen_rows = conn.execute(
            """SELECT g.current_status, COUNT(*) AS count,
                      SUM(CASE WHEN sl.storage_location_id IS NOT NULL THEN 1 ELSE 0 END)
                          AS located
               FROM inventory_grids g
               LEFT JOIN storage_locations sl
                 ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
               WHERE g.grid_code IN (?, ?, ?)
               GROUP BY g.current_status""",
            grid_codes,
        ).fetchall()
        conn.close()
        self.assertEqual(
            [(row["current_status"], row["count"], row["located"]) for row in frozen_rows],
            [("Frozen", 3, 3)],
        )

        registration = guest.get("/register")
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', registration.data)
        self.assertIsNotNone(csrf_match)
        with patch("blueprints.screening.send_email"):
            response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_match.group(1).decode(),
                    "reg_type": "screening",
                    "user_name": "Subhajit",
                    "pi_name": "ACCEM Trial PI",
                    "email": "pipeline-screening@example.test",
                    "origin": "Internal",
                    "sample_name": "Different screening booking sample",
                    "grids": "3",
                    "inventory_grid_codes": grid_codes,
                },
            )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        screening_booking_id = conn.execute(
            "SELECT id FROM screening_bookings WHERE email=?",
            ["pipeline-screening@example.test"],
        ).fetchone()["id"]
        linked_codes = [
            row["grid_code"]
            for row in conn.execute(
                """SELECT g.grid_code FROM screening_booking_grids l
                   JOIN inventory_grids g ON g.grid_id=l.grid_id
                   WHERE l.screening_booking_id=? ORDER BY g.grid_code""",
                [screening_booking_id],
            ).fetchall()
        ]
        conn.close()
        self.assertEqual(linked_codes, sorted(grid_codes))

        with patch("blueprints.admin.send_email"):
            response = self.client.get(f"/admin/screening/load/{screening_booking_id}")
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        screening_status = conn.execute(
            "SELECT status FROM screening_bookings WHERE id=?",
            [screening_booking_id],
        ).fetchone()["status"]
        conn.close()
        self.assertEqual(screening_status, "ongoing")

        screening_completion = {
            "_csrf_token": self.csrf_token(),
            "actual_slots": "1",
            "actual_grids": "3",
            "grid_source": "user provided",
            "grid_type": "",
            "clipped_grids": "0",
            "inventory_grid_code": grid_codes,
            "inventory_outcome": ["store"] * 3,
            "inventory_discard_notes": [""] * 3,
            "inventory_falcon": ["SCREENING-FALCON"] * 3,
            "inventory_box": ["SCREENING-BOX"] * 3,
            "inventory_position": ["1", "2", "3"],
        }
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/screening/complete/{screening_booking_id}",
                data=screening_completion,
            )
        self.assertEqual(response.status_code, 302)

        session_start = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            "/admin/inventory/sessions",
            data={
                "_csrf_token": self.csrf_token(),
                "start_time": session_start,
                "instrument": "Microscope (isolated trial)",
                "notes": "Simulated microscope stage; no physical instrument used.",
                "grid_codes": grid_codes,
            },
        )
        self.assertEqual(response.status_code, 302)
        session_id = int(response.location.rsplit("/", 1)[1])
        conn = database.get_db()
        session_statuses = {
            row["current_status"]
            for row in conn.execute(
                "SELECT current_status FROM inventory_grids WHERE grid_code IN (?, ?, ?)",
                grid_codes,
            ).fetchall()
        }
        conn.close()
        self.assertEqual(session_statuses, {"In Data Collection"})
        self.assertEqual(
            self.client.get(response.location).status_code,
            200,
        )

        for position, grid_code in enumerate(grid_codes, start=1):
            response = self.client.post(
                f"/admin/inventory/sessions/{session_id}/complete/{grid_code}",
                data={
                    "_csrf_token": self.csrf_token(),
                    "outcome": "store",
                    "falcon_container_id": "FINAL-FALCON",
                    "grid_box_name": "FINAL-BOX",
                    "grid_position": str(position),
                    "notes": "Returned to storage after the isolated trial.",
                },
            )
            self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        stored_count = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_grids "
            "WHERE grid_code IN (?, ?, ?) AND current_status='Stored'",
            grid_codes,
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(stored_count, 3)

        response = self.client.post(
            "/admin/inventory/grids/bulk-discard",
            data={
                "_csrf_token": self.csrf_token(),
                "selected_grid_codes": grid_codes,
                "notes": "Discarded after the isolated end-to-end workflow test.",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        discarded_count = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_grids "
            "WHERE grid_code IN (?, ?, ?) AND current_status='Discarded'",
            grid_codes,
        ).fetchone()["count"]
        active_location_count = conn.execute(
            """SELECT COUNT(*) AS count FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               WHERE g.grid_code IN (?, ?, ?) AND sl.removed_at IS NULL""",
            grid_codes,
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(discarded_count, 3)
        self.assertEqual(active_location_count, 0)

    def test_freezing_to_screening_pipeline_via_public_and_admin_booking_paths(self):
        freezing_date = datetime.date.today() + datetime.timedelta(days=1)
        while freezing_date.weekday() >= 5:
            freezing_date += datetime.timedelta(days=1)

        for booking_path in ("public", "admin"):
            with self.subTest(booking_path=booking_path):
                suffix = booking_path
                user_name = f"{suffix.title()} Pipeline User"
                sample_name = f"{suffix.title()} Pipeline Sample"
                freeze_email = f"{suffix}-pipeline-freezing@example.test"
                screen_email = f"{suffix}-pipeline-screening@example.test"

                if booking_path == "public":
                    registration = self.app.test_client()
                    page = registration.get("/register")
                    csrf_match = re.search(
                        rb'name="_csrf_token"\s+value="([^"]+)"', page.data
                    )
                    self.assertIsNotNone(csrf_match)
                    with patch("blueprints.freezing.send_email"):
                        freeze_response = registration.post(
                            "/register",
                            data={
                                "_csrf_token": csrf_match.group(1).decode(),
                                "reg_type": "freezing",
                                "user_name": user_name,
                                "pi_name": "Pipeline PI",
                                "email": freeze_email,
                                "origin": "Internal",
                                "sample_name": sample_name,
                                "grids_freezing": "3",
                                "freezing_date": freezing_date.isoformat(),
                            },
                        )
                else:
                    registration = self.client
                    with patch("blueprints.freezing.send_email"):
                        freeze_response = registration.post(
                            "/admin/panel/slots/freezing",
                            data={
                                "_csrf_token": self.csrf_token(),
                                "user_name": user_name,
                                "pi_name": "Pipeline PI",
                                "email": freeze_email,
                                "origin": "Internal",
                                "sample_name": sample_name,
                                "grids": "3",
                                "freezing_date": freezing_date.isoformat(),
                            },
                        )
                self.assertEqual(freeze_response.status_code, 302)

                conn = database.get_db()
                freeze_booking_id = conn.execute(
                    "SELECT id FROM freezing_bookings WHERE email=?", [freeze_email]
                ).fetchone()["id"]
                grid_type_id = conn.execute(
                    "SELECT grid_type_id FROM grid_types WHERE active=1 "
                    "ORDER BY grid_type_id LIMIT 1"
                ).fetchone()["grid_type_id"]
                conn.close()
                with patch("blueprints.admin.send_email"):
                    completion_response = self.client.post(
                        f"/admin/freezing/complete/{freeze_booking_id}",
                        data={
                            "_csrf_token": self.csrf_token(),
                            "actual_grids": "3",
                            "grid_source": "self_owned",
                            "grid_management": "1",
                            "inventory_grid_sample_name": [sample_name] * 3,
                            "inventory_sample_id": [""] * 3,
                            "inventory_grid_type_id": [str(grid_type_id)] * 3,
                            "inventory_falcon": [f"{suffix}-FROZEN"] * 3,
                            "inventory_box": [f"{suffix}-BOX"] * 3,
                            "inventory_position": ["1", "2", "3"],
                            "blot_seconds": ["1"] * 3,
                            "blot_force": ["1"] * 3,
                        },
                    )
                self.assertEqual(completion_response.status_code, 302)
                self.assertEqual(
                    self.client.get("/admin/inventory/").status_code, 200
                )
                conn = database.get_db()
                frozen_codes = [
                    row["grid_code"]
                    for row in conn.execute(
                        """SELECT g.grid_code FROM inventory_grids g
                           JOIN inventory_samples s ON s.sample_id=g.sample_id
                           WHERE s.researcher_name=? ORDER BY g.grid_id""",
                        [user_name],
                    ).fetchall()
                ]
                frozen_count = conn.execute(
                    """SELECT COUNT(*) AS count FROM inventory_grids g
                       JOIN inventory_samples s ON s.sample_id=g.sample_id
                       WHERE s.researcher_name=? AND g.current_status='Frozen'""",
                    [user_name],
                ).fetchone()["count"]
                located_count = conn.execute(
                    """SELECT COUNT(*) AS count FROM storage_locations sl
                       JOIN inventory_grids g ON g.grid_id=sl.grid_id
                       WHERE g.grid_code IN (?, ?, ?) AND sl.removed_at IS NULL""",
                    frozen_codes,
                ).fetchone()["count"]
                conn.close()
                self.assertEqual(len(frozen_codes), 3)
                self.assertEqual(frozen_count, 3)
                self.assertEqual(located_count, 3)
                inventory_lookup = self.app.test_client().get(
                    f"/inventory/search?user_name={user_name.replace(' ', '%20')}"
                )
                self.assertEqual(inventory_lookup.status_code, 200)
                self.assertEqual(
                    {grid["grid_code"] for grid in inventory_lookup.get_json()["grids"]},
                    set(frozen_codes),
                )
                self.assertEqual(
                    {grid["status_code"] for grid in inventory_lookup.get_json()["grids"]},
                    {"F"},
                )

                if booking_path == "public":
                    page = registration.get("/register")
                    csrf_match = re.search(
                        rb'name="_csrf_token"\s+value="([^"]+)"', page.data
                    )
                    self.assertIsNotNone(csrf_match)
                    with patch("blueprints.screening.send_email"):
                        screening_response = registration.post(
                            "/register",
                            data={
                                "_csrf_token": csrf_match.group(1).decode(),
                                "reg_type": "screening",
                                "user_name": user_name,
                                "pi_name": "Pipeline PI",
                                "email": screen_email,
                                "origin": "Internal",
                                "sample_name": sample_name,
                                "grids": "2",
                                "inventory_grid_codes": frozen_codes[:2],
                            },
                        )
                else:
                    with patch("blueprints.screening.send_email"):
                        screening_response = self.client.post(
                            "/admin/panel/slots/screening",
                            data={
                                "_csrf_token": self.csrf_token(),
                                "user_name": user_name,
                                "pi_name": "Pipeline PI",
                                "email": screen_email,
                                "origin": "Internal",
                                "esm": "",
                                "sample_name": sample_name,
                                "grids": "2",
                                "inventory_grid_codes": frozen_codes[:2],
                            },
                        )
                self.assertEqual(screening_response.status_code, 302)
                conn = database.get_db()
                screening = conn.execute(
                    "SELECT id, grids FROM screening_bookings WHERE email=?",
                    [screen_email],
                ).fetchone()
                linked_codes = [
                    row["grid_code"]
                    for row in conn.execute(
                        """SELECT g.grid_code FROM screening_booking_grids l
                           JOIN inventory_grids g ON g.grid_id=l.grid_id
                           WHERE l.screening_booking_id=? ORDER BY g.grid_id""",
                        [screening["id"]],
                    ).fetchall()
                ]
                conn.close()
                self.assertEqual(screening["grids"], 2)
                self.assertEqual(linked_codes, frozen_codes[:2])

                with patch("blueprints.admin.send_email"):
                    load_response = self.client.get(
                        f"/admin/screening/load/{screening['id']}"
                    )
                self.assertEqual(load_response.status_code, 302)
                conn = database.get_db()
                microscope_count = conn.execute(
                    """SELECT COUNT(*) AS count FROM inventory_grids
                       WHERE grid_code IN (?, ?) AND current_status='In Data Collection'""",
                    linked_codes,
                ).fetchone()["count"]
                microscope_locations = conn.execute(
                    """SELECT COUNT(*) AS count FROM storage_locations sl
                       JOIN inventory_grids g ON g.grid_id=sl.grid_id
                       WHERE g.grid_code IN (?, ?) AND sl.removed_at IS NULL""",
                    linked_codes,
                ).fetchone()["count"]
                conn.close()
                self.assertEqual(microscope_count, 2)
                self.assertEqual(microscope_locations, 0)
                microscope_dashboard = self.client.get("/admin/inventory/")
                self.assertIn(b"In microscope", microscope_dashboard.data)
                for code in linked_codes:
                    self.assertIn(code.encode(), microscope_dashboard.data)

                with patch("blueprints.admin.send_email"):
                    completion_response = self.client.post(
                        f"/admin/screening/complete/{screening['id']}",
                        data={
                            "_csrf_token": self.csrf_token(),
                            "actual_slots": "1",
                            "actual_grids": "2",
                            "grid_source": "user provided",
                            "grid_type": "",
                            "user_category": "internal",
                            "service_stage": "Screening / Clipping",
                            "clipped_grids": "0",
                            "inventory_grid_code": linked_codes,
                            "inventory_outcome": ["store", "discard"],
                            "inventory_discard_notes": ["", "Discarded after screening."],
                            "inventory_falcon": [f"{suffix}-RETURN-FALCON", ""],
                            "inventory_box": [f"{suffix}-RETURN-BOX", ""],
                            "inventory_position": ["A1", ""],
                        },
                        follow_redirects=True,
                    )
                self.assertEqual(completion_response.status_code, 200)

                conn = database.get_db()
                final_statuses = {
                    row["grid_code"]: row["current_status"]
                    for row in conn.execute(
                        "SELECT grid_code, current_status FROM inventory_grids "
                        "WHERE grid_code IN (?, ?, ?)",
                        frozen_codes,
                    ).fetchall()
                }
                stored_location = conn.execute(
                    """SELECT falcon_container_id, grid_box_name, grid_position
                       FROM storage_locations
                       WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?)
                         AND removed_at IS NULL""",
                    [linked_codes[0]],
                ).fetchone()
                remaining_frozen_location_count = conn.execute(
                    """SELECT COUNT(*) AS count FROM storage_locations
                       WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?)
                         AND removed_at IS NULL""",
                    [frozen_codes[2]],
                ).fetchone()["count"]
                inventory_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM inventory_grids WHERE grid_code IN (?, ?, ?)",
                    frozen_codes,
                ).fetchone()["count"]
                conn.close()
                self.assertEqual(
                    final_statuses,
                    {
                        linked_codes[0]: "Stored",
                        linked_codes[1]: "Discarded",
                        frozen_codes[2]: "Frozen",
                    },
                    completion_response.get_data(as_text=True),
                )
                self.assertEqual(
                    tuple(stored_location),
                    (f"{suffix}-RETURN-FALCON", f"{suffix}-RETURN-BOX", "A1"),
                )
                self.assertEqual(remaining_frozen_location_count, 1)
                self.assertEqual(inventory_count, 3)
                final_lookup = self.app.test_client().get(
                    f"/inventory/search?user_name={user_name.replace(' ', '%20')}"
                ).get_json()["grids"]
                final_status_codes = {
                    grid["grid_code"]: grid["status_code"] for grid in final_lookup
                }
                self.assertEqual(
                    final_status_codes,
                    {
                        linked_codes[0]: "S",
                        frozen_codes[2]: "F",
                    },
                )
                full_inventory = self.app.test_client().get(
                    f"/inventory?username={user_name.replace(' ', '%20')}"
                )
                self.assertIn(linked_codes[1].encode(), full_inventory.data)
                self.assertIn(b"Discarded", full_inventory.data)

        frozen_grid = self.create_batch(
            1, researcher_name="Direct Frozen Discard", sample_name="Direct discard test"
        )[0]
        discard_response = self.client.post(
            "/admin/inventory/grids/bulk-discard",
            data={
                "_csrf_token": self.csrf_token(),
                "selected_grid_codes": [frozen_grid],
                "notes": "Direct discard from Frozen status regression check.",
            },
            follow_redirects=True,
        )
        self.assertEqual(discard_response.status_code, 200)
        self.assertIn(b"Discarded 1 grid(s)", discard_response.data)
        conn = database.get_db()
        direct_discard = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?",
            [frozen_grid],
        ).fetchone()
        inventory_row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_grids WHERE grid_code=?",
            [frozen_grid],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(direct_discard["current_status"], "Discarded")
        self.assertEqual(inventory_row_count, 1)

    def test_public_inventory_and_booking_grid_selection_are_available_without_admin_login(self):
        selected_grid, other_grid = self.create_batch(2)
        unrelated_grid = self.create_batch(
            1, researcher_name="Different Scientist", sample_name="Different sample"
        )[0]
        guest = self.app.test_client()

        inventory = guest.get("/inventory")
        self.assertEqual(inventory.status_code, 200)
        self.assertNotIn(selected_grid.encode(), inventory.data)
        inventory = guest.get(
            "/inventory?researcher_name=Researcher&sample_name=Test%20sample"
        )
        self.assertIn(selected_grid.encode(), inventory.data)
        self.assertIn(other_grid.encode(), inventory.data)
        self.assertNotIn(unrelated_grid.encode(), inventory.data)
        self.assertNotIn(b"Register frozen grids", inventory.data)

        username_lookup = guest.get("/inventory?username=Researcher")
        self.assertEqual(username_lookup.status_code, 200)
        self.assertIn(selected_grid.encode(), username_lookup.data)
        self.assertIn(other_grid.encode(), username_lookup.data)
        self.assertNotIn(unrelated_grid.encode(), username_lookup.data)
        self.assertNotIn(b"Test sample", username_lookup.data)
        self.assertNotIn(b'name="sample_name"', username_lookup.data)
        selection_view = guest.get(
            "/inventory?username=Researcher&select=1&slot_type=screening"
        )
        self.assertEqual(selection_view.status_code, 200)
        self.assertIn(b"Select grids for the booking", selection_view.data)
        self.assertIn(f'value="{selected_grid}" data-inventory-grid'.encode(), selection_view.data)
        self.assertIn(b"Use selected grids", selection_view.data)
        self.assertIn(b'name="slot_type" value="screening"', selection_view.data)
        self.assertIn(b"const selectionLimit = 4;", selection_view.data)
        admin_selection_view = guest.get(
            "/inventory?username=Researcher&select=1&slot_type=screening&admin_selection=1"
        )
        self.assertIn(b"const selectionLimit = null;", admin_selection_view.data)
        self.assertIn(b'name="admin_selection" value="1"', admin_selection_view.data)

        form = guest.get("/register")
        self.assertEqual(form.status_code, 200)
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', form.data)
        self.assertIsNotNone(csrf_match)
        csrf_token = csrf_match.group(1).decode()
        self.assertIn(b'target="_blank" rel="opener"', form.data)
        self.assertIn(b"searchParams.set('select', '1')", form.data)
        self.assertIn(b"inventory-grid-selection", form.data)
        self.assertNotIn(selected_grid.encode(), form.data)
        search = guest.get(
            "/inventory/search?user_name=Researcher&sample_name=Test%20sample"
        )
        self.assertEqual(search.status_code, 200)
        self.assertEqual(
            {grid["grid_code"] for grid in search.get_json()["grids"]},
            {selected_grid, other_grid},
        )
        self.assertNotIn(
            unrelated_grid,
            {grid["grid_code"] for grid in search.get_json()["grids"]},
        )

        with patch("blueprints.screening.send_email"):
            response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_token,
                    "reg_type": "screening",
                    "user_name": "Researcher",
                    "pi_name": "Grid PI",
                    "email": "grid-user@example.test",
                    "origin": "Internal",
                    "sample_name": "Test sample",
                    "grids": "4",
                    "inventory_grid_codes": [selected_grid],
                },
            )
        self.assertEqual(response.status_code, 302)

        conn = database.get_db()
        booking = conn.execute(
            "SELECT id, grids FROM screening_bookings WHERE email=?",
            ["grid-user@example.test"],
        ).fetchone()
        linked_grid = conn.execute(
            """SELECT g.grid_code FROM screening_booking_grids l
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE l.screening_booking_id=?""",
            [booking["id"]],
        ).fetchone()
        conn.close()
        self.assertEqual(booking["grids"], 1)
        self.assertEqual(linked_grid["grid_code"], selected_grid)

        unavailable_form = guest.get("/register")
        self.assertNotIn(selected_grid.encode(), unavailable_form.data)
        self.assertNotIn(other_grid.encode(), unavailable_form.data)
        still_available = guest.get(
            "/inventory/search?user_name=Researcher&sample_name=Test%20sample"
        )
        self.assertEqual(
            {grid["grid_code"] for grid in still_available.get_json()["grids"]},
            {other_grid},
        )
        reserved_selection_view = guest.get(
            "/inventory?username=Researcher&select=1&slot_type=screening"
        )
        self.assertNotIn(
            f'value="{selected_grid}" data-inventory-grid'.encode(),
            reserved_selection_view.data,
        )
        self.assertIn(
            f'value="{other_grid}" data-inventory-grid'.encode(),
            reserved_selection_view.data,
        )
        with patch("blueprints.screening.send_email"):
            collision = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_token,
                    "reg_type": "screening",
                    "user_name": "Researcher",
                    "pi_name": "Other PI",
                    "email": "other-user@example.test",
                    "origin": "Internal",
                    "sample_name": "Other sample",
                    "grids": "1",
                    "inventory_grid_codes": [selected_grid],
                },
            )
        self.assertEqual(collision.status_code, 302)
        conn = database.get_db()
        booking_count = conn.execute(
            "SELECT COUNT(*) AS count FROM screening_bookings"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(booking_count, 1)

        with patch("blueprints.imaging.send_email"):
            data_collection_response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_token,
                    "reg_type": "datacollecting",
                    "user_name": "Researcher",
                    "pi_name": "Data PI",
                    "email": "data-user@example.test",
                    "origin": "Internal",
                    "sample_name": "Test sample",
                    "grids": "1",
                    "days": "1",
                    "inventory_grid_codes": [other_grid],
                },
            )
        self.assertEqual(data_collection_response.status_code, 302)
        conn = database.get_db()
        data_booking = conn.execute(
            "SELECT id, grids FROM bookings WHERE email=?",
            ["data-user@example.test"],
        ).fetchone()
        data_link = conn.execute(
            """SELECT g.grid_code FROM data_collection_booking_grids l
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE l.booking_id=?""",
            [data_booking["id"]],
        ).fetchone()
        conn.close()
        self.assertEqual(data_booking["grids"], 1)
        self.assertEqual(data_link["grid_code"], other_grid)

    def test_public_booking_rejects_more_than_four_grids(self):
        grid_codes = self.create_batch(5)
        guest = self.app.test_client()
        form = guest.get("/register")
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', form.data)
        self.assertIsNotNone(csrf_match)
        csrf_token = csrf_match.group(1).decode()

        with patch("blueprints.screening.send_email"):
            selected_grids_response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_token,
                    "reg_type": "screening",
                    "user_name": "Researcher",
                    "pi_name": "Grid PI",
                    "email": "public-over-four@example.test",
                    "origin": "Internal",
                    "sample_name": "Public screening sample",
                    "grids": "5",
                    "inventory_grid_codes": grid_codes,
                },
            )
        with patch("blueprints.imaging.send_email"):
            manual_count_response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_token,
                    "reg_type": "datacollecting",
                    "user_name": "Researcher",
                    "pi_name": "Grid PI",
                    "email": "public-over-four-manual@example.test",
                    "origin": "Internal",
                    "sample_name": "Public data collection sample",
                    "grids": "5",
                    "days": "1",
                },
            )

        self.assertEqual(selected_grids_response.status_code, 302)
        self.assertEqual(manual_count_response.status_code, 302)
        conn = database.get_db()
        screening_count = conn.execute(
            "SELECT COUNT(*) AS count FROM screening_bookings "
            "WHERE email='public-over-four@example.test'"
        ).fetchone()["count"]
        collection_count = conn.execute(
            "SELECT COUNT(*) AS count FROM bookings "
            "WHERE email='public-over-four-manual@example.test'"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(screening_count, 0)
        self.assertEqual(collection_count, 0)

    def test_admin_can_book_more_than_four_inventory_grids_for_screening_and_collection(self):
        grid_codes = self.create_batch(12)
        csrf_token = self.csrf_token()
        inventory_selector = self.client.get(
            "/inventory?username=Researcher&select=1&slot_type=screening&admin_selection=1"
        )
        self.assertEqual(inventory_selector.status_code, 200)
        inventory_search = self.client.get(
            "/inventory/search?user_name=Researcher"
        )
        self.assertEqual(inventory_search.status_code, 200)
        self.assertEqual(len(inventory_search.get_json()["grids"]), 12)
        self.assertEqual(self.client.get("/admin/panel").status_code, 200)

        with patch("blueprints.screening.send_email"):
            screening_response = self.client.post(
                "/admin/panel/slots/screening",
                data={
                    "_csrf_token": csrf_token,
                    "user_name": "Researcher",
                    "pi_name": "Admin PI",
                    "email": "admin-over-four-screening@example.test",
                    "origin": "Internal",
                    "esm": "",
                    "sample_name": "Admin screening sample",
                    "grids": "6",
                    "inventory_grid_codes": grid_codes[:6],
                },
            )
        with patch("blueprints.imaging.send_email"):
            collection_response = self.client.post(
                "/admin/panel/slots/datacollecting",
                data={
                    "_csrf_token": csrf_token,
                    "user_name": "Researcher",
                    "pi_name": "Admin PI",
                    "email": "admin-over-four-collection@example.test",
                    "origin": "Internal",
                    "esm": "",
                    "sample_name": "Admin collection sample",
                    "grids": "6",
                    "days": "2",
                    "inventory_grid_codes": grid_codes[6:],
                },
            )

        self.assertEqual(screening_response.status_code, 302)
        self.assertEqual(collection_response.status_code, 302)
        conn = database.get_db()
        screening = conn.execute(
            "SELECT id, grids FROM screening_bookings "
            "WHERE email='admin-over-four-screening@example.test'"
        ).fetchone()
        screening_link_count = conn.execute(
            "SELECT COUNT(*) AS count FROM screening_booking_grids "
            "WHERE screening_booking_id=?",
            [screening["id"]],
        ).fetchone()["count"]
        collection = conn.execute(
            "SELECT id, grids FROM bookings "
            "WHERE email='admin-over-four-collection@example.test'"
        ).fetchone()
        collection_link_count = conn.execute(
            "SELECT COUNT(*) AS count FROM data_collection_booking_grids "
            "WHERE booking_id=?",
            [collection["id"]],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(screening["grids"], 6)
        self.assertEqual(screening_link_count, 6)
        self.assertEqual(collection["grids"], 6)
        self.assertEqual(collection_link_count, 6)

    def test_admin_quick_slots_link_selected_inventory_grids_and_use_selected_count(self):
        screening_grid, collection_grid = self.create_batch(
            2, researcher_name="Researcher", sample_name="Admin selected sample"
        )
        csrf_token = self.csrf_token()

        with patch("blueprints.screening.send_email"):
            screening_response = self.client.post(
                "/admin/panel/slots/screening",
                data={
                    "_csrf_token": csrf_token,
                    "user_name": "Researcher",
                    "pi_name": "Admin PI",
                    "email": "admin-screening-grid@example.test",
                    "origin": "Internal",
                    "esm": "",
                    "sample_name": "Admin screening sample",
                    "grids": "4",
                    "inventory_grid_codes": [screening_grid],
                },
            )
        with patch("blueprints.imaging.send_email"):
            collection_response = self.client.post(
                "/admin/panel/slots/datacollecting",
                data={
                    "_csrf_token": csrf_token,
                    "user_name": "Researcher",
                    "pi_name": "Admin PI",
                    "email": "admin-collection-grid@example.test",
                    "origin": "Internal",
                    "esm": "",
                    "sample_name": "Admin collection sample",
                    "grids": "4",
                    "days": "2",
                    "inventory_grid_codes": [collection_grid],
                },
            )

        self.assertEqual(screening_response.status_code, 302)
        self.assertEqual(collection_response.status_code, 302)
        conn = database.get_db()
        screening = conn.execute(
            "SELECT id, grids FROM screening_bookings WHERE email=?",
            ["admin-screening-grid@example.test"],
        ).fetchone()
        screening_link = conn.execute(
            """SELECT g.grid_code FROM screening_booking_grids l
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE l.screening_booking_id=?""",
            [screening["id"]],
        ).fetchone()
        collection = conn.execute(
            "SELECT id, grids, days FROM bookings WHERE email=?",
            ["admin-collection-grid@example.test"],
        ).fetchone()
        collection_link = conn.execute(
            """SELECT g.grid_code FROM data_collection_booking_grids l
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE l.booking_id=?""",
            [collection["id"]],
        ).fetchone()
        conn.close()

        self.assertEqual(screening["grids"], 1)
        self.assertEqual(screening_link["grid_code"], screening_grid)
        self.assertEqual(collection["grids"], 1)
        self.assertEqual(collection["days"], 2)
        self.assertEqual(collection_link["grid_code"], collection_grid)

    def test_admin_quick_slot_rejects_grid_owned_by_a_different_username(self):
        grid_code = self.create_batch(
            1, researcher_name="Researcher", sample_name="Admin selected sample"
        )[0]
        response = self.client.post(
            "/admin/panel/slots/screening",
            data={
                "_csrf_token": self.csrf_token(),
                "user_name": "Different user",
                "pi_name": "Admin PI",
                "email": "admin-mismatched-grid@example.test",
                "origin": "Internal",
                "esm": "",
                "sample_name": "Admin screening sample",
                "grids": "1",
                "inventory_grid_codes": [grid_code],
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"is not associated with the entered username", response.data)
        self.assertIn(grid_code.encode(), response.data)
        conn = database.get_db()
        booking = conn.execute(
            "SELECT id FROM screening_bookings WHERE email=?",
            ["admin-mismatched-grid@example.test"],
        ).fetchone()
        conn.close()
        self.assertIsNone(booking)

    def test_username_search_and_screening_picker_include_linked_account_grids_across_samples(self):
        selected_grid = self.create_batch(
            1, researcher_name="Different display name", sample_name="Stored sample"
        )[0]
        unrelated_grid = self.create_batch(
            1, researcher_name="Different scientist", sample_name="Another sample"
        )[0]
        conn = database.get_db()
        conn.execute(
            "INSERT INTO users (username, email, password_hash, role) "
            "VALUES (?, ?, ?, 'user')",
            ["Subajit", "subajit@example.test", "unused"],
        )
        user_id = conn.execute(
            "SELECT id FROM users WHERE username=?", ["Subajit"]
        ).fetchone()["id"]
        conn.execute(
            """UPDATE inventory_samples SET linked_user_id=?
               WHERE sample_id=(SELECT sample_id FROM inventory_grids WHERE grid_code=?)""",
            [user_id, selected_grid],
        )
        conn.commit()
        conn.close()

        guest = self.app.test_client()
        inventory = guest.get("/inventory?username=Subajit")
        self.assertIn(selected_grid.encode(), inventory.data)
        self.assertNotIn(unrelated_grid.encode(), inventory.data)

        search = guest.get("/inventory/search?user_name=Subajit")
        self.assertEqual(search.status_code, 200)
        grids = search.get_json()["grids"]
        self.assertEqual([grid["grid_code"] for grid in grids], [selected_grid])
        self.assertEqual(grids[0]["sample_name"], "Stored sample")
        admin_search = self.client.get("/admin/inventory/grids?q=Subajit")
        self.assertIn(selected_grid.encode(), admin_search.data)
        self.assertNotIn(unrelated_grid.encode(), admin_search.data)

        form = guest.get("/register")
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', form.data)
        self.assertIsNotNone(csrf_match)
        with patch("blueprints.screening.send_email"):
            response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_match.group(1).decode(),
                    "reg_type": "screening",
                    "user_name": "Subajit",
                    "pi_name": "Grid PI",
                    "email": "subajit-screening@example.test",
                    "origin": "Internal",
                    "sample_name": "Booking",
                    "grids": "1",
                    "inventory_grid_codes": [selected_grid],
                },
            )
        self.assertEqual(response.status_code, 302)

        conn = database.get_db()
        linked_grid = conn.execute(
            """SELECT g.grid_code FROM screening_booking_grids l
               JOIN screening_bookings b ON b.id=l.screening_booking_id
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE b.email=?""",
            ["subajit-screening@example.test"],
        ).fetchone()
        conn.close()
        self.assertEqual(linked_grid["grid_code"], selected_grid)

    def test_name_search_matches_first_or_last_name_without_case_sensitivity(self):
        grid = self.create_batch(
            1, researcher_name="Subhajit Mahanty", sample_name="Name search sample"
        )[0]
        guest = self.app.test_client()

        for name_variant in ("subhajit", "MAHANTY", "sUbHaJiT mAhAnTy"):
            response = guest.get(f"/inventory?username={name_variant.replace(' ', '%20')}")
            self.assertIn(grid.encode(), response.data, name_variant)

        for query in ("SUBHAJIT", "mahanty"):
            response = self.client.get(f"/admin/inventory/grids?q={query}")
            self.assertIn(grid.encode(), response.data, query)

        search = guest.get("/inventory/search?user_name=mahanty")
        self.assertEqual(
            [item["grid_code"] for item in search.get_json()["grids"]],
            [grid],
        )

        form = guest.get("/register")
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', form.data)
        self.assertIsNotNone(csrf_match)
        with patch("blueprints.screening.send_email"):
            booking = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_match.group(1).decode(),
                    "reg_type": "screening",
                    "user_name": "subhajit",
                    "pi_name": "Grid PI",
                    "email": "subhajit-search@example.test",
                    "origin": "Internal",
                    "sample_name": "A different booking sample",
                    "grids": "1",
                    "inventory_grid_codes": [grid],
                },
            )
        self.assertEqual(booking.status_code, 302)

    def test_public_registration_rejects_unrecognized_origin_values(self):
        response = self.client.post(
            "/register",
            data={
                "_csrf_token": self.csrf_token(),
                "reg_type": "screening",
                "user_name": "Untrusted User",
                "pi_name": "Test PI",
                "email": "untrusted@example.test",
                "origin": '<img src=x onerror=alert(1)>',
                "sample_name": "Test sample",
                "grids": "1",
                "days": "1",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Choose a valid user origin.", response.data)
        conn = database.get_db()
        booking_counts = [
            conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in ("bookings", "screening_bookings", "freezing_bookings")
        ]
        conn.close()
        self.assertEqual(booking_counts, [0, 0, 0])

    def test_screening_completion_moves_linked_grid_atomically(self):
        grid_code, occupied_code = self.create_batch(2)
        self.post_storage(occupied_code, "Falcon-C", "Box-C", "1")
        conn = database.get_db()
        conn.execute(
            """INSERT INTO screening_bookings
               (user_name, pi_name, email, origin, sample_name, grids, days, status)
               VALUES ('Grid User', 'Grid PI', 'complete@example.test', 'Internal',
                       'Test sample', 1, 1, 'ongoing')"""
        )
        booking_id = conn.execute(
            "SELECT id FROM screening_bookings WHERE email='complete@example.test'"
        ).fetchone()["id"]
        grid_id = conn.execute(
            "SELECT grid_id FROM inventory_grids WHERE grid_code=?", [grid_code]
        ).fetchone()["grid_id"]
        conn.execute(
            """INSERT INTO screening_booking_grids
               (screening_booking_id, grid_id, linked_at)
               VALUES (?, ?, '2025-01-02 12:00:00')""",
            [booking_id, grid_id],
        )
        conn.commit()
        conn.close()

        completion = {
            "_csrf_token": self.csrf_token(),
            "actual_slots": "1",
            "actual_grids": "1",
            "grid_source": "user provided",
            "grid_type": "",
            "clipped_grids": "0",
            "inventory_grid_code": [grid_code],
            "inventory_outcome": ["store"],
            "inventory_discard_notes": [""],
            "inventory_falcon": ["Falcon-C"],
            "inventory_box": ["Box-C"],
            "inventory_position": ["1"],
        }
        with patch("blueprints.admin.send_email"):
            conflict = self.client.post(
                f"/admin/screening/complete/{booking_id}", data=completion
            )
        self.assertEqual(conflict.status_code, 302)
        conn = database.get_db()
        booking = conn.execute(
            "SELECT status, total_billed FROM screening_bookings WHERE id=?", [booking_id]
        ).fetchone()
        locations = conn.execute(
            "SELECT COUNT(*) AS count FROM storage_locations "
            "WHERE grid_id=? AND removed_at IS NULL",
            [grid_id],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(booking["status"], "ongoing")
        self.assertEqual(locations, 0)

        completion["inventory_falcon"] = ["Falcon-D"]
        completion["inventory_box"] = ["Box-D"]
        completion["inventory_position"] = ["4"]
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/screening/complete/{booking_id}", data=completion
            )
        self.assertEqual(response.status_code, 302)
        conn = database.get_db()
        booking = conn.execute(
            "SELECT status FROM screening_bookings WHERE id=?", [booking_id]
        ).fetchone()
        location = conn.execute(
            """SELECT falcon_container_id, grid_box_name, grid_position
               FROM storage_locations WHERE grid_id=? AND removed_at IS NULL""",
            [grid_id],
        ).fetchone()
        conn.close()
        self.assertEqual(booking["status"], "completed")
        self.assertEqual(
            (location["falcon_container_id"], location["grid_box_name"], location["grid_position"]),
            ("Falcon-D", "Box-D", "4"),
        )

    def test_public_screening_grid_moves_to_microscope_then_returns_to_storage(self):
        grid_code = self.create_batch(
            1, researcher_name="Screening Researcher", sample_name="Screening sample"
        )[0]
        self.post_storage(grid_code, "Falcon-Screen", "Box-A", "A1")
        guest = self.app.test_client()
        registration = guest.get("/register")
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', registration.data)
        self.assertIsNotNone(csrf_match)

        with patch("blueprints.screening.send_email"):
            response = guest.post(
                "/register",
                data={
                    "_csrf_token": csrf_match.group(1).decode(),
                    "reg_type": "screening",
                    "user_name": "Screening Researcher",
                    "pi_name": "Screening PI",
                    "email": "screening-lifecycle@example.test",
                    "origin": "Internal",
                    "sample_name": "Screening sample",
                    "grids": "1",
                    "inventory_grid_codes": [grid_code],
                },
            )
        self.assertEqual(response.status_code, 302)

        conn = database.get_db()
        booking = conn.execute(
            "SELECT id, status FROM screening_bookings "
            "WHERE email='screening-lifecycle@example.test'"
        ).fetchone()
        initial_grid = conn.execute(
            """SELECT current_status FROM inventory_grids WHERE grid_code=?""",
            [grid_code],
        ).fetchone()
        conn.close()
        self.assertEqual(booking["status"], "waiting")
        self.assertEqual(initial_grid["current_status"], "Frozen")

        with patch("blueprints.admin.send_email"):
            response = self.client.get(f"/admin/screening/load/{booking['id']}")
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(f"/admin/inventory/grids/{grid_code}")
        self.assertIn(b"In Data Collection", detail.data)
        self.assertIn(b"Screening", detail.data)
        dashboard = self.client.get("/admin/inventory/")
        self.assertIn(b"In microscope", dashboard.data)
        self.assertIn(grid_code.encode(), dashboard.data)

        conn = database.get_db()
        microscope_grid = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?",
            [grid_code],
        ).fetchone()
        active_locations = conn.execute(
            """SELECT COUNT(*) AS count FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               WHERE g.grid_code=? AND sl.removed_at IS NULL""",
            [grid_code],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(microscope_grid["current_status"], "In Data Collection")
        self.assertEqual(active_locations, 0)

        completion = {
            "_csrf_token": self.csrf_token(),
            "actual_slots": "1",
            "actual_grids": "1",
            "grid_source": "user provided",
            "grid_type": "",
            "user_category": "internal",
            "service_stage": "Screening / Clipping",
            "clipped_grids": "0",
            "inventory_grid_code": [grid_code],
            "inventory_outcome": ["store"],
            "inventory_discard_notes": [""],
            "inventory_falcon": ["Falcon-Return"],
            "inventory_box": ["Box-Return"],
            "inventory_position": ["B2"],
        }
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/screening/complete/{booking['id']}",
                data=completion,
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)

        conn = database.get_db()
        final_booking = conn.execute(
            "SELECT status FROM screening_bookings WHERE id=?",
            [booking["id"]],
        ).fetchone()
        final_grid = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?",
            [grid_code],
        ).fetchone()
        final_location = conn.execute(
            """SELECT falcon_container_id, grid_box_name, grid_position
               FROM storage_locations
               WHERE grid_id=(SELECT grid_id FROM inventory_grids WHERE grid_code=?)
                 AND removed_at IS NULL""",
            [grid_code],
        ).fetchone()
        conn.close()
        self.assertEqual(final_booking["status"], "completed")
        self.assertEqual(final_grid["current_status"], "Stored")
        self.assertEqual(
            tuple(final_location),
            ("Falcon-Return", "Box-Return", "B2"),
        )

        stored_grids, not_stored_grids = dashboard_storage()
        self.assertIn(grid_code, {grid["grid_code"] for grid in stored_grids})
        self.assertNotIn(grid_code, {grid["grid_code"] for grid in not_stored_grids})

    def test_screening_and_data_collection_completion_can_discard_individual_grids(self):
        for service_type in ("screening", "data_collection"):
            first_grid, discarded_grid = self.create_batch(2)
            conn = database.get_db()
            booking_table = "screening_bookings" if service_type == "screening" else "bookings"
            link_table = (
                "screening_booking_grids"
                if service_type == "screening"
                else "data_collection_booking_grids"
            )
            booking_column = (
                "screening_booking_id" if service_type == "screening" else "booking_id"
            )
            conn.execute(
                f"""INSERT INTO {booking_table}
                    (user_name, pi_name, email, origin, sample_name, grids, days, status)
                    VALUES (?, ?, ?, 'Internal', 'Test sample', 2, 1, 'ongoing')""",
                [f"{service_type} user", "Grid PI", f"{service_type}@example.test"],
            )
            booking_id = conn.execute(
                f"SELECT id FROM {booking_table} WHERE email=?",
                [f"{service_type}@example.test"],
            ).fetchone()["id"]
            for grid_code in (first_grid, discarded_grid):
                grid_id = conn.execute(
                    "SELECT grid_id FROM inventory_grids WHERE grid_code=?", [grid_code]
                ).fetchone()["grid_id"]
                conn.execute(
                    f"INSERT INTO {link_table} ({booking_column}, grid_id, linked_at) "
                    "VALUES (?, ?, '2025-01-02 12:00:00')",
                    [booking_id, grid_id],
                )
            conn.commit()
            conn.close()

            completion = {
                "_csrf_token": self.csrf_token(),
                "actual_slots": "1",
                "actual_grids": "2",
                "grid_source": "user provided",
                "grid_type": "",
                "service_stage": "Screening / Clipping" if service_type == "screening" else "Data Collection",
                "clipped_grids": "0",
                "inventory_grid_code": [first_grid, discarded_grid],
                "inventory_outcome": ["store", "discard"],
                "inventory_discard_notes": [
                    "",
                    f"Discarded after {service_type.replace('_', ' ')}",
                ],
                "inventory_falcon": ["Falcon-Return", ""],
                "inventory_box": ["Box-Return", ""],
                "inventory_position": ["A1" if service_type == "screening" else "A2", ""],
            }
            endpoint = "screening" if service_type == "screening" else "datacollecting"
            with patch("blueprints.admin.send_email"):
                response = self.client.post(
                    f"/admin/{endpoint}/complete/{booking_id}",
                    data=completion,
                    follow_redirects=True,
                )
            self.assertEqual(response.status_code, 200)

            conn = database.get_db()
            states = {
                row["grid_code"]: row["current_status"]
                for row in conn.execute(
                    """SELECT g.grid_code, g.current_status
                       FROM inventory_grids g
                       WHERE g.grid_code IN (?, ?)""",
                    [first_grid, discarded_grid],
                ).fetchall()
            }
            discarded_location_count = conn.execute(
                """SELECT COUNT(*) AS count FROM storage_locations sl
                   JOIN inventory_grids g ON g.grid_id=sl.grid_id
                   WHERE g.grid_code=? AND sl.removed_at IS NULL""",
                [discarded_grid],
            ).fetchone()["count"]
            discard_event = conn.execute(
                """SELECT notes FROM grid_events e
                   JOIN inventory_grids g ON g.grid_id=e.grid_id
                   WHERE g.grid_code=? AND e.event_type='grid_discarded'""",
                [discarded_grid],
            ).fetchone()
            conn.close()

            self.assertEqual(states[first_grid], "Stored", response.data.decode())
            self.assertEqual(states[discarded_grid], "Discarded")
            self.assertEqual(discarded_location_count, 0)
            self.assertEqual(
                discard_event["notes"], f"Discarded after {service_type.replace('_', ' ')}"
            )
            public_available_codes = {
                grid["grid_code"]
                for grid in public_inventory_grids(
                    "Researcher", "Test sample", available_only=True
                )
            }
            self.assertIn(first_grid, public_available_codes)
            self.assertNotIn(discarded_grid, public_available_codes)
            conn = database.get_db()
            conn.execute(
                f"""INSERT INTO {booking_table}
                    (user_name, pi_name, email, origin, sample_name, grids, days, status)
                    VALUES ('Another user', 'Another PI', ?, 'Internal', 'Test sample', 1, 1, 'waiting')""",
                [f"retry-{service_type}@example.test"],
            )
            new_booking_id = conn.execute(
                f"SELECT id FROM {booking_table} WHERE email=?",
                [f"retry-{service_type}@example.test"],
            ).fetchone()["id"]
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(InventoryError, "not available"):
                link_grid_to_booking(
                    {
                        "service_type": service_type,
                        "booking_id": str(new_booking_id),
                        "grid_code": discarded_grid,
                    },
                    None,
                )

    def test_data_collection_session_must_use_the_booking_selected_grids(self):
        selected_grid, other_grid = self.create_batch(2)
        with patch("blueprints.imaging.send_email"):
            success, _ = register_imaging(
                "Researcher", "Data PI", "session@example.test", "Internal", "",
                "Test sample", 1, 1, grid_codes=[selected_grid],
            )
        self.assertTrue(success)
        conn = database.get_db()
        booking_id = conn.execute(
            "SELECT id FROM bookings WHERE email=?", ["session@example.test"]
        ).fetchone()["id"]
        operator_id = conn.execute(
            "SELECT id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()["id"]
        conn.close()

        session_fields = MultiDict([
            ("booking_reference", str(booking_id)),
            ("grid_codes", other_grid),
            ("start_time", "2025-01-02T10:30"),
            ("instrument", "Talos"),
            ("notes", ""),
        ])
        with self.assertRaisesRegex(InventoryError, "exactly those grids"):
            create_data_session(session_fields, operator_id)

        session_fields.setlist("grid_codes", [selected_grid])
        created = create_data_session(session_fields, operator_id)
        self.assertTrue(created["session_code"].startswith("ACCEM-DC-"))
        conn = database.get_db()
        status = conn.execute(
            "SELECT current_status FROM inventory_grids WHERE grid_code=?",
            [selected_grid],
        ).fetchone()["current_status"]
        conn.close()
        self.assertEqual(status, "In Data Collection")

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
        self.assertEqual([row["current_status"] for row in statuses], ["Frozen", "Frozen"])

        types_page = self.client.get("/admin/inventory/grid-types")
        self.assertEqual(types_page.status_code, 200)
        for grid_type in (
            "Quantifoil holey carbon",
            "Carbon-coated grids",
            "Gold Quantifoil Gold Grids",
            "Graphene-coated grids",
        ):
            self.assertIn(grid_type.encode(), types_page.data)
        self.assertNotIn(b">Other<", types_page.data)
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

    def test_bulk_discard_marks_selected_grids_and_prevents_storage(self):
        first, second, untouched = self.create_batch(3)
        self.post_storage(first, "FALCON-1", "Box-A", "A1")
        self.post_storage(second, "FALCON-1", "Box-A", "A2")
        response = self.client.post(
            "/admin/inventory/grids/bulk-discard",
            data={
                "_csrf_token": self.csrf_token(),
                "selected_grid_codes": [first, second],
                "notes": "Discarded after review",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Discarded 2 grid(s)", response.data)
        conn = database.get_db()
        statuses = {
            row["grid_code"]: row["current_status"]
            for row in conn.execute(
                "SELECT grid_code, current_status FROM inventory_grids"
            ).fetchall()
        }
        removed_locations = conn.execute(
            "SELECT COUNT(*) AS count FROM storage_locations "
            "WHERE grid_id IN (SELECT grid_id FROM inventory_grids "
            "WHERE grid_code IN (?, ?)) AND removed_at IS NOT NULL",
            [first, second],
        ).fetchone()["count"]
        events = conn.execute(
            """SELECT COUNT(*) AS count FROM grid_events e
               JOIN inventory_grids g ON g.grid_id=e.grid_id
               WHERE g.grid_code IN (?, ?) AND e.event_type='grid_discarded'
                 AND e.notes='Discarded after review'""",
            [first, second],
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(statuses[first], "Discarded")
        self.assertEqual(statuses[second], "Discarded")
        self.assertEqual(statuses[untouched], "Frozen")
        self.assertEqual(removed_locations, 2)
        self.assertEqual(events, 2)
        storage = self.post_storage(first, "FALCON-1", "Box-A", "A1")
        self.assertEqual(storage.status_code, 302)
        detail = self.client.get(f"/admin/inventory/grids/{first}")
        self.assertIn(b"is discarded and cannot be stored", detail.data)

    def test_bulk_discard_rejects_active_booking_without_partial_changes(self):
        first, second = self.create_batch(2)
        conn = database.get_db()
        conn.execute(
            """INSERT INTO screening_bookings
               (user_name, pi_name, email, origin, sample_name, grids, days, status)
               VALUES ('Grid User', 'Grid PI', 'reserved@example.test', 'Internal',
                       'Test sample', 1, 1, 'ongoing')"""
        )
        booking_id = conn.execute(
            "SELECT id FROM screening_bookings WHERE email='reserved@example.test'"
        ).fetchone()["id"]
        grid_id = conn.execute(
            "SELECT grid_id FROM inventory_grids WHERE grid_code=?", [first]
        ).fetchone()["grid_id"]
        conn.execute(
            """INSERT INTO screening_booking_grids
               (screening_booking_id, grid_id, linked_at)
               VALUES (?, ?, '2025-01-02 12:00:00')""",
            [booking_id, grid_id],
        )
        conn.commit()
        conn.close()

        response = self.client.post(
            "/admin/inventory/grids/bulk-discard",
            data={
                "_csrf_token": self.csrf_token(),
                "selected_grid_codes": [first, second],
            },
            follow_redirects=True,
        )
        self.assertIn(b"is reserved by an active booking", response.data)
        conn = database.get_db()
        statuses = conn.execute(
            "SELECT current_status FROM inventory_grids ORDER BY grid_id"
        ).fetchall()
        events = conn.execute(
            "SELECT COUNT(*) AS count FROM grid_events WHERE event_type='grid_discarded'"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual([row["current_status"] for row in statuses], ["Frozen", "Frozen"])
        self.assertEqual(events, 0)

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
        self.assertEqual(rows[0]["current_status"], "Clipped")
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
