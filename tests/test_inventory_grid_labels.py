"""Grids are shown by sample, grid type and freezing session, not by ACCEM-G codes."""
import re
import unittest

import database
from inventory_service import grid_label_info, humanize_grid_codes
from tests import test_inventory as inventory_tests  # not imported by name: pytest would re-run it

CODE = re.compile(rb"ACCEM-G-\d{4,}")


class GridLabelTests(unittest.TestCase):
    # Reuse the inventory fixtures without re-running every inherited test.
    setUp = inventory_tests.GridInventoryTests.setUp
    csrf_token = inventory_tests.GridInventoryTests.csrf_token
    create_batch = inventory_tests.GridInventoryTests.create_batch
    post_storage = inventory_tests.GridInventoryTests.post_storage

    def _visible_codes(self, html):
        """Grid codes that appear outside URLs, form values and data attributes."""
        stripped = re.sub(rb'(href|action|value|data-grid)="[^"]*"', b"", html)
        stripped = re.sub(rb"<script.*?</script>", b"", stripped, flags=re.S)
        return CODE.findall(stripped)

    def test_label_names_sample_type_and_place_in_session(self):
        first, second = self.create_batch(2, "Bapan", "Spike RBD")
        info = grid_label_info([first, second])
        self.assertEqual(info[second]["grid_number"], 2)
        self.assertEqual(info[second]["session_total"], 2)
        self.assertEqual(
            info[second]["label"], "Spike RBD · Quantifoil holey carbon · grid 2 of 2"
        )
        self.assertTrue(info[first]["session_label"].startswith("Frozen "))
        self.assertIn("Spike RBD", humanize_grid_codes(f"{first} was discarded."))
        self.assertNotIn(first, humanize_grid_codes(f"{first} was discarded."))

    def test_booking_search_api_groups_by_freezing_session(self):
        self.create_batch(2, "Bapan", "Spike RBD")
        self.create_batch(1, "Bapan", "Membrane pump")
        grids = self.app.test_client().get("/inventory/search?user_name=Bapan").get_json()["grids"]
        self.assertEqual(len(grids), 3)
        self.assertEqual(len({grid["session_key"] for grid in grids}), 2)
        for grid in grids:
            self.assertEqual(grid["grid_type_name"], "Quantifoil holey carbon")
            self.assertIn(grid["sample_name"], grid["label"])
            self.assertNotIn("ACCEM-G", grid["label"])

    def test_pages_do_not_show_grid_codes(self):
        first, second = self.create_batch(2, "Bapan", "Spike RBD")
        self.post_storage(first, "Falcon-A", "Box-1", "1")
        pages = {
            "storage": self.client.get("/admin/inventory/storage").data,
            "overview": self.client.get("/admin/inventory/").data,
            "search": self.client.get("/admin/inventory/grids").data,
            "activity": self.client.get("/admin/inventory/activity").data,
            "detail": self.client.get(f"/admin/inventory/grids/{first}").data,
            "lookup": self.app.test_client().get("/inventory?username=Bapan").data,
        }
        for name, html in pages.items():
            self.assertEqual(self._visible_codes(html), [], name)
        self.assertIn(b"Spike RBD", pages["storage"])
        self.assertIn(b"Quantifoil holey carbon", pages["storage"])
        self.assertIn(b"Frozen ", pages["lookup"])


if __name__ == "__main__":
    unittest.main()


class FlexibleNameSearchTests(unittest.TestCase):
    setUp = inventory_tests.GridInventoryTests.setUp
    csrf_token = inventory_tests.GridInventoryTests.csrf_token
    create_batch = inventory_tests.GridInventoryTests.create_batch

    def test_name_rules(self):
        from inventory_service import names_match
        self.assertTrue(names_match("Bapan", "Bapan Mondal"))
        self.assertTrue(names_match("Bapan Mondal", "Bapan"))
        self.assertTrue(names_match("Bapen", "Bapan"))
        self.assertTrue(names_match("dr. bapan mondal", "Bapan Mondal"))
        self.assertFalse(names_match("Bapan", "Sonali Kumari"))
        self.assertFalse(names_match("e", "Bapan"))

    def test_lookup_offers_names_before_showing_grids(self):
        grid = self.create_batch(1, "Bapan Mondal", "Spike RBD")[0]
        guest = self.app.test_client()
        for typed in ("Bapan", "Bapen", "bapan  mondal."):
            page = guest.get("/inventory?username=" + typed.replace(" ", "%20")).data
            self.assertNotIn(grid.encode(), page, typed)  # no grids until confirmed
            self.assertIn(b"Is one of these you?", page, typed)
            self.assertIn(b"username=Bapan+Mondal", page, typed)
            api = guest.get("/inventory/search?user_name=" + typed.replace(" ", "%20")).get_json()
            self.assertEqual(api["grids"], [], typed)
            self.assertEqual(api["suggestions"][0]["name"], "Bapan Mondal", typed)
        confirmed = guest.get("/inventory?username=Bapan%20Mondal&booking_name=Bapan").data
        self.assertIn(grid.encode(), confirmed)

    def test_unrelated_names_get_no_suggestions(self):
        self.create_batch(1, "Bapan Mondal", "Spike RBD")
        api = self.app.test_client().get("/inventory/search?user_name=Sonali").get_json()
        self.assertEqual(api["suggestions"], [])
        self.assertEqual(api["grids"], [])

    def test_booking_under_short_name_can_use_grids_recorded_under_full_name(self):
        import re as _re
        from unittest.mock import patch as _patch
        grid = self.create_batch(1, "Bapan Mondal", "Spike RBD")[0]
        guest = self.app.test_client()
        token = _re.search(rb'name="_csrf_token"\s+value="([^"]+)"', guest.get("/register").data).group(1).decode()
        with _patch("extensions.send_email"):
            guest.post("/register", data={
                "_csrf_token": token, "reg_type": "screening", "user_name": "Bapan",
                "pi_name": "PI Name", "email": "bapan@example.com", "origin": "Internal",
                "esm": "IISc", "sample_name": "Spike RBD", "grids": "1",
                "inventory_grid_codes": [grid],
            })
        conn = database.get_db()
        linked = conn.execute(
            """SELECT b.user_name FROM screening_booking_grids l
               JOIN screening_bookings b ON b.id=l.screening_booking_id
               JOIN inventory_grids g ON g.grid_id=l.grid_id WHERE g.grid_code=?""",
            (grid,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(linked)
        self.assertEqual(linked["user_name"], "Bapan")
