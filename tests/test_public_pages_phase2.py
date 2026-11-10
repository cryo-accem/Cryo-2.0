import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import database
from blueprints.public import RATES_WORKED_EXAMPLE, format_inr
from main import create_app
from revenue import CHARGE_CONFIG, calculate_charge_sheet


class PublicPagesPhase2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "phase2.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def test_format_inr_uses_indian_grouping(self):
        self.assertEqual(format_inr(Decimal("12000")), "₹12,000")
        self.assertEqual(format_inr(Decimal("50000")), "₹50,000")
        self.assertEqual(format_inr(Decimal("1234567")), "₹12,34,567")
        self.assertEqual(format_inr(Decimal("125.5")), "₹125.50")

    def test_rates_page_lists_slot_price_for_each_category(self):
        response = self.client.get("/rates")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for config in CHARGE_CONFIG.values():
            self.assertIn(format_inr(config["slot"]), html)
        self.assertIn("Prices exclude GST", html)
        self.assertIn('aria-current="page">Rates</a>', html)

    def test_rates_worked_example_matches_calculation(self):
        charges = calculate_charge_sheet(**RATES_WORKED_EXAMPLE)
        html = self.client.get("/rates").get_data(as_text=True)
        self.assertIn(f"data-rates-example-total>{format_inr(charges['grand_total'])}<", html)

    def test_faq_page_renders(self):
        response = self.client.get("/faq")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertGreaterEqual(html.count('<details class="faq-item">'), 6)
        self.assertIn("first and last grid positions", html)

    def test_footer_has_directions_and_funding_acknowledgement(self):
        html = self.client.get("/faq").get_data(as_text=True)
        self.assertIn(
            'href="https://www.google.com/maps/search/?api=1&amp;query=Indian+Institute+of+Science+Bengaluru"'
            ' target="_blank" rel="noopener"',
            html,
        )
        self.assertIn("Department of Biotechnology (DBT) Infrastructure Grant, Govt of India and DST-FIST", html)
        self.assertIn('href="/faq">FAQ</a>', html)

    def test_sitemap_includes_new_pages(self):
        xml = self.client.get("/sitemap.xml").get_data(as_text=True)
        self.assertIn("/rates</loc>", xml)
        self.assertIn("/faq</loc>", xml)


if __name__ == "__main__":
    unittest.main()
