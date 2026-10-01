import os
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class PublicManagedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "publications.sqlite3")
        self.database_url = patch.object(
            database, "DATABASE_URL", f"sqlite:///{db_path}"
        )
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True

    def test_managed_publication_uses_the_existing_publication_list(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO managed_publications
                   (citation, doi_url, published_year)
                   VALUES (?, ?, ?)""",
                (
                    "Example Author. (2026). Example research title. Example Journal.",
                    "https://doi.org/10.1234/example",
                    2026,
                ),
            )
            conn.commit()
            cur.close()
            conn.close()

        response = self.app.test_client().get("/publication")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="publication-list"', response.data)
        self.assertIn(b"Example research title. Example Journal.", response.data)
        self.assertIn(
            b'<a href="https://doi.org/10.1234/example" target="_blank" '
            b'rel="noopener noreferrer">https://doi.org/10.1234/example</a>',
            response.data,
        )
        self.assertNotIn(b"Recently added", response.data)
        self.assertEqual(response.data.count(b'class="publication-list"'), 1)
