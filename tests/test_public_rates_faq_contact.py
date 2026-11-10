import os
import re
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import database
from blueprints.freezing import GRID_LIMIT_PER_DAY
from blueprints.public import RATES_FREEZING_EXAMPLE, RATES_WORKED_EXAMPLE, format_inr, public_rate_table
from main import create_app
from revenue import CHARGE_CONFIG, PROCESSING_RATE, calculate_charge_sheet

KEYS = ("internal", "academic", "industry")
# Every amount must equal the billing configuration (billing code is authoritative).
EXPECTED_SHEET = {
    "slot": [CHARGE_CONFIG[k]["slot"] for k in KEYS],
    "handling_freezing": [CHARGE_CONFIG[k]["handling_per_4"] for k in KEYS],
    "grid_normal": [CHARGE_CONFIG[k]["grid"]["normal_holey_carbon"] for k in KEYS],
    "grid_gold": [CHARGE_CONFIG[k]["grid"]["gold_carbon_graphene"] for k in KEYS],
    "clip_base": [CHARGE_CONFIG[k]["clip_base"] for k in KEYS],
    "handling_clipping": [CHARGE_CONFIG[k]["handling_per_4"] for k in KEYS],
    "processing": ["Not applicable", "Not applicable", PROCESSING_RATE],
}


class PublicRatesFaqContactTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "phase2d.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def _html(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_new_pages_return_200(self):
        for path in ("/rates", "/faq", "/contact"):
            self._html(path)

    def test_rate_table_matches_billing_config(self):
        table = public_rate_table()
        self.assertEqual(
            table["categories"], ["Internal (IISc)", "Academic (other educational institutes)", "Industry"]
        )
        rows = {row["id"]: row for row in table["rows"]}
        for row_id, expected in EXPECTED_SHEET.items():
            self.assertEqual(rows[row_id]["values"], expected, row_id)
        self.assertEqual(
            [row["id"] for row in table["rows"]],
            ["slot", "grid_normal", "grid_gold", "clip_base",
             "clip_base_note", "processing", "handling_freezing", "handling_clipping"],
        )
        self.assertEqual(table["gst"], {"internal": "No GST", "academic": "plus 18% GST",
                                        "industry": "plus 18% GST"})
        # Sanity: the published numbers the owner signed off.
        self.assertEqual(rows["slot"]["values"], [Decimal("3000"), Decimal("12000"), Decimal("50000")])
        self.assertEqual(rows["clip_base"]["values"], [Decimal("2500"), Decimal("2500"), Decimal("4000")])
        self.assertEqual(rows["handling_freezing"]["values"], [Decimal("500"), Decimal("1000"), Decimal("2000")])
        self.assertEqual(rows["handling_freezing"]["row_note"], "Charged on the freezing booking.")
        self.assertIn("in addition to the C-clip and base ring", rows["handling_clipping"]["row_note"])

    def test_worked_example_matches_billing_function(self):
        self.assertEqual(RATES_WORKED_EXAMPLE["user_category"], "academic")
        charges = calculate_charge_sheet(**RATES_WORKED_EXAMPLE)
        self.assertEqual(charges["subtotal"], Decimal("23500.00"))
        self.assertEqual(charges["gst"], Decimal("4230.00"))
        self.assertEqual(charges["grand_total"], Decimal("27730.00"))
        html = self._html("/rates")
        self.assertIn("data-rates-example-subtotal>₹23,500<", html)
        self.assertIn("data-rates-example-gst>₹4,230<", html)
        self.assertIn("data-rates-example-total>₹27,730<", html)
        freezing = calculate_charge_sheet(**RATES_FREEZING_EXAMPLE)
        self.assertEqual(freezing["subtotal"], Decimal("1000.00"))
        self.assertEqual(freezing["gst"], Decimal("180.00"))
        self.assertEqual(freezing["grand_total"], Decimal("1180.00"))
        self.assertIn("data-rates-freezing-example-total>₹1,180<", html)
        for label in ("Grid Charge", "Handling Charge", "C-clip + Base ring", "Slot Charge"):
            self.assertIn(f"<dt>{label} ", html)

    def test_rates_page_renders_charge_sheet(self):
        html = self._html("/rates")
        self.assertIn(">Charge sheet</h2>", html)
        for row_id, expected in EXPECTED_SHEET.items():
            match = re.search(rf'<tr[^>]*data-rate-row="{row_id}">(.*?)</tr>', html, re.S)
            self.assertIsNotNone(match, row_id)
            cells = re.findall(r'class="rates-sheet__amount">([^<]*)<', match.group(1))
            self.assertEqual(
                cells, [v if isinstance(v, str) else format_inr(v) for v in expected], row_id
            )
            # GST applies to the academic and industry cells that carry an amount.
            gst_cells = sum(1 for v in expected[1:] if not isinstance(v, str))
            self.assertEqual(match.group(1).count("plus 18% GST"), gst_cells, row_id)
        self.assertIn(
            "Charged for each grid that is not already clipped. Not charged if your lab "
            "provides the C-clip and base ring.", html)
        self.assertNotIn("or your own", html)
        self.assertNotIn("with C-clip and base ring", html)
        self.assertNotIn(">Free<", html)
        self.assertIn("Handling charge \u2014 Grid freezing (Glow discharger &amp; Vitrobot)", html)
        self.assertIn("Final amount is as per the issued charge sheet.", html)
        self.assertIn("Rates current as of", html)

    def test_nav_has_rates_with_aria_current(self):
        html = self._html("/rates")
        self.assertRegex(html, r'<a href="/rates" class="active" aria-current="page">Rates</a>')
        other = self._html("/faq")
        self.assertIn('<a href="/rates">Rates</a>', other)
        nav = re.search(r'<nav class="site-nav".*?</nav>', other, re.S).group(0)
        self.assertEqual(nav.count("<a "), 12)  # 10 page links (6 inside the Facility / Using ACCEM menus) + Book slots + staff lock

    def test_faq_is_accessible_and_script_free(self):
        html = self._html("/faq")
        main = html[html.index("<main>"):html.index("</main>")]
        self.assertNotIn("<script", main)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn(" style=", main)
        count = main.count('<details class="faq-item">')
        self.assertGreaterEqual(count, 8)
        self.assertLessEqual(count, 12)
        self.assertEqual(count, main.count("<summary>"))
        self.assertIn(f"at most {GRID_LIMIT_PER_DAY} grids per day", main)
        self.assertIn('href="/rates"', main)
        self.assertIn('href="/inventory"', main)

    def test_contact_page_has_emails_and_maps_link(self):
        html = self._html("/contact")
        main = html[html.index("<main>"):html.index("</main>")]
        self.assertIn('href="mailto:cryoem.iisc@gmail.com"', main)
        self.assertIn('href="mailto:somnath@iisc.ac.in"', main)
        self.assertIn("Advanced Centre for Cryo-Electron Microscopy<br>Indian Institute of Science<br>Bengaluru 560012", main)
        self.assertIn(
            'href="https://www.google.com/maps/search/?api=1&amp;query=Indian+Institute+of+Science+Bengaluru"'
            ' target="_blank" rel="noopener"', main,
        )
        self.assertNotIn("<iframe", main)
        self.assertIn('href="/faq"', main)
        self.assertIn('href="/register"', main)

    def test_footer_links_and_funding(self):
        html = self._html("/home")
        footer = html[html.index('<footer class="site-footer">'):html.index("</footer>")]
        for href in ('href="/faq"', 'href="/rates"', 'href="/contact"'):
            self.assertIn(href, footer)
        self.assertIn("Department of Biotechnology (DBT) Infrastructure Grant, Govt of India and DST-FIST", footer)

    def test_sitemap_lists_new_pages(self):
        xml = self._html("/sitemap.xml")
        for path in ("/rates", "/faq", "/contact"):
            self.assertIn(f"{path}</loc>", xml)


if __name__ == "__main__":
    unittest.main()


class DataProcessingIndustryOnlyTests(unittest.TestCase):
    """Data processing / image analysis is billed to industry users only."""

    def test_only_industry_is_charged_for_processing(self):
        for category, expected in (("internal", "0.00"), ("academic", "0.00"), ("industry", "20000.00")):
            with self.subTest(category=category):
                result = calculate_charge_sheet(
                    category, "Data Collection", 1, "self_owned", actual_slots=1,
                    processing_requested=True, clip_base_source="self_owned",
                )
                self.assertEqual(str(result["processing_charge"]), expected)

    def test_industry_processing_carries_gst(self):
        without = calculate_charge_sheet("industry", "Data Collection", 1, "self_owned",
                                         actual_slots=1, clip_base_source="self_owned")
        with_processing = calculate_charge_sheet("industry", "Data Collection", 1, "self_owned",
                                                 actual_slots=1, processing_requested=True,
                                                 clip_base_source="self_owned")
        self.assertEqual(with_processing["grand_total"] - without["grand_total"], Decimal("23600.00"))
