"""Overview status tiles open Search grids filtered to that status."""
import unittest

import database

from tests import test_inventory as inventory_tests  # module import: avoid re-collecting its tests


class StatusTileTests(unittest.TestCase):
    setUp = inventory_tests.GridInventoryTests.setUp
    csrf_token = inventory_tests.GridInventoryTests.csrf_token
    create_batch = inventory_tests.GridInventoryTests.create_batch
    post_storage = inventory_tests.GridInventoryTests.post_storage

    def test_overview_tiles_link_to_filtered_search(self):
        html = self.client.get("/admin/inventory/").get_data(as_text=True)
        for status in ("Frozen", "Clipped", "Stored", "In+Data+Collection", "Discarded"):
            self.assertIn(f"/admin/inventory/grids?status={status}#inventory-results", html)
        self.assertIn('href="/admin/inventory/grids#inventory-results"', html)

    def test_search_shows_only_the_chosen_status_with_chips(self):
        first, second = self.create_batch(2, "Bapan", "Spike RBD")
        conn = database.get_db()
        conn.execute("UPDATE inventory_grids SET current_status='Stored' WHERE grid_code=?", (first,))
        conn.commit()
        conn.close()
        html = self.client.get("/admin/inventory/grids?status=Stored").get_data(as_text=True)
        self.assertIn("Showing only <strong>Stored</strong> grids", html)
        self.assertIn(first, html)
        self.assertNotIn(second, html)
        self.assertIn('inventory-status-chip inventory-status--stored is-active', html)
        # Chip counts: one Stored, one Frozen, two in total.
        self.assertRegex(html, r"<span>All grids</span><strong>2</strong>")
        self.assertRegex(html, r"<span>Frozen</span><strong>1</strong>")
        frozen = self.client.get("/admin/inventory/grids?status=Frozen").get_data(as_text=True)
        self.assertIn(second, frozen)
        self.assertNotIn(first, frozen)


if __name__ == "__main__":
    unittest.main()
