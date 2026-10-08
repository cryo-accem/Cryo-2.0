import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from blueprints.admin import _next_charge_sheet_id
from main import create_app


class GlobalChargeSheetSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "charge-sequence.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()

    def test_bill_number_starts_at_76_and_continues_across_categories(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            date = datetime.date(2026, 9, 28)

            internal_id = _next_charge_sheet_id(cur, "internal", date)
            academic_id = _next_charge_sheet_id(cur, "external", date)
            industrial_id = _next_charge_sheet_id(cur, "industrial", date)

            self.assertTrue(internal_id.endswith("_0076"))
            self.assertTrue(academic_id.endswith("_0077"))
            self.assertTrue(industrial_id.endswith("_0078"))
            self.assertEqual(len({internal_id, academic_id, industrial_id}), 3)
            conn.commit()
            cur.close()
            conn.close()

    def test_existing_category_counters_are_not_reused(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO charge_sheet_sequences "
                "(academic_year, category, next_number) VALUES (?, ?, ?)",
                [2026, "internal", 90],
            )
            database._ensure_global_charge_sheet_sequence(cur)
            next_id = _next_charge_sheet_id(cur, "external", datetime.date(2026, 9, 28))
            self.assertTrue(next_id.endswith("_0091"))
            conn.commit()
            cur.close()
            conn.close()


if __name__ == "__main__":
    unittest.main()
