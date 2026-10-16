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
    InventoryError, create_data_session, link_grid_to_booking, public_inventory_grids,
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
            "'004_booking_grid_links', '005_grid_types')"
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
        self.assertEqual(version_count, 4)
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
        self.assertIn("Falcon-A", content)
        self.assertIn("GridBox-1", content)
        self.assertIn(f'aria-label="Position 1, {stored}, Researcher, Frozen"', content)
        self.assertIn(f'aria-label="Position 2, {clipped}, Researcher, Clipped"', content)
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

    def test_public_inventory_and_booking_grid_selection_are_available_without_admin_login(self):
        selected_grid, other_grid = self.create_batch(2)
        unrelated_grid = self.create_batch(
            1, researcher_name="Different Researcher", sample_name="Different sample"
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

        form = guest.get("/register")
        self.assertEqual(form.status_code, 200)
        csrf_match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', form.data)
        self.assertIsNotNone(csrf_match)
        csrf_token = csrf_match.group(1).decode()
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

    def test_username_search_and_screening_picker_include_linked_account_grids_across_samples(self):
        selected_grid = self.create_batch(
            1, researcher_name="Different display name", sample_name="Stored sample"
        )[0]
        unrelated_grid = self.create_batch(
            1, researcher_name="Different researcher", sample_name="Another sample"
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
