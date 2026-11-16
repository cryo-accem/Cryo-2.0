import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import database
from main import create_app
from revenue import estimate_study_cost


class EstimateStudyCostTests(unittest.TestCase):
    def test_academic_study_matches_rate_card(self):
        result = estimate_study_cost("academic", 8, 2, 3)
        # 8 grids x 3000 (C-clip + base ring included) + 2 x 8/4 x 1000 handling
        # + 3 x 12000 slots
        self.assertEqual(result["subtotal"], Decimal("64000.00"))
        self.assertEqual(result["gst"], Decimal("11520.00"))
        self.assertEqual(result["grand_total"], Decimal("75520.00"))
        self.assertEqual(result["per_sample"], Decimal("37760.00"))

    def test_internal_has_no_gst_and_ignores_processing(self):
        result = estimate_study_cost("internal", 4, 1, 1, processing_requested=True)
        self.assertEqual(result["gst"], Decimal("0.00"))
        self.assertFalse(result["processing_applied"])
        self.assertEqual(result["grand_total"], Decimal("16000") + 500 + 500 + 3000)

    def test_industry_processing_added(self):
        result = estimate_study_cost("industry", 4, 1, 1, processing_requested=True)
        self.assertTrue(result["processing_applied"])
        self.assertEqual(result["subtotal"], Decimal("24000") + 2000 + 2000 + 50000 + 20000)

    def test_zero_slots_is_freezing_only(self):
        result = estimate_study_cost("external", 4, 1, 0, grid_source="self_owned")
        self.assertEqual(result["subtotal"], Decimal("1000.00"))

    def test_rejects_fewer_grids_than_samples(self):
        with self.assertRaises(ValueError):
            estimate_study_cost("academic", 1, 2, 1)

    def test_rejects_non_integer_counts(self):
        for bad in ("", "1.5", "-2", "abc"):
            with self.assertRaises(ValueError):
                estimate_study_cost("academic", bad, 1, 1)


class ProformaPageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "proforma.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def _login(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            conn = database.get_db()
            session["admin_user_id"] = conn.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    def test_requires_login(self):
        response = self.client.get("/admin/proforma")
        self.assertEqual(response.status_code, 302)

    def test_shows_form_and_estimate(self):
        self._login()
        response = self.client.get("/admin/proforma")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Proforma estimate", response.data)
        response = self.client.get(
            "/admin/proforma?calculate=1&user_name=Test+User&user_category=academic"
            "&number_of_grids=8&number_of_samples=2&number_of_slots=3"
            "&grid_source=facility&grid_type=normal_holey_carbon&clip_base_source=facility"
        )
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Test User", text)
        self.assertIn("₹75,520", text)

    def test_shows_validation_error(self):
        self._login()
        response = self.client.get(
            "/admin/proforma?calculate=1&user_name=X&user_category=academic"
            "&number_of_grids=1&number_of_samples=3&number_of_slots=1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"grids cannot be fewer than samples", response.data)


if __name__ == "__main__":
    unittest.main()
