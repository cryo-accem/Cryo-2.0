import os
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class PublicManagedInstrumentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "instruments.sqlite3")
        self.database_url = patch.object(
            database, "DATABASE_URL", f"sqlite:///{db_path}"
        )
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True

    def test_managed_instruments_render_as_individual_equipment_cards(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO managed_instruments
                   (name, category, description, specifications, image_url)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "Added Instrument",
                    "Imaging",
                    "Used for high-resolution imaging.",
                    "200 kV",
                    "/static/images/instrument.png",
                ),
            )
            cur.execute(
                """INSERT INTO managed_instruments
                   (name, category, description, specifications)
                   VALUES (?, ?, ?, ?)""",
                (
                    "Instrument Without Photo",
                    "Preparation",
                    "Supports sample preparation.",
                    "Temperature controlled",
                ),
            )
            conn.commit()
            cur.close()
            conn.close()

        response = self.app.test_client().get("/equipments")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Managed instruments", response.data)
        self.assertEqual(response.data.count(b'class="equipment-tile'), 7)
        self.assertIn(b'src="/static/images/instrument.png"', response.data)
        self.assertIn(b"<h3>Added Instrument</h3>", response.data)
        self.assertIn(b'class="equipment-tile equipment-tile--text-only"', response.data)
        self.assertIn(b"<h3>Instrument Without Photo</h3>", response.data)
        self.assertIn(b"Temperature controlled", response.data)
