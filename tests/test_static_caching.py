import gzip
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class StaticCachingAndCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "static.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def test_versioned_static_is_immutable(self):
        response = self.client.get("/static/css/site.css?v=abc123")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Cache-Control"], "public, max-age=31536000, immutable"
        )

    def test_unversioned_css_revalidates(self):
        response = self.client.get("/static/css/site.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertNotIn("immutable", response.headers["Cache-Control"])

    def test_unversioned_image_gets_one_week(self):
        response = self.client.get("/static/images/cryo.webp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=604800")

    def test_missing_versioned_static_is_not_cached_forever(self):
        response = self.client.get("/static/css/missing.css?v=1")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("immutable", response.headers.get("Cache-Control", ""))

    def test_css_is_gzipped_when_accepted(self):
        plain = self.client.get("/static/css/site.css").get_data()
        response = self.client.get(
            "/static/css/site.css?v=1", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", response.headers.get("Vary", ""))
        self.assertEqual(gzip.decompress(response.get_data()), plain)

    def test_compressed_etag_still_revalidates(self):
        first = self.client.get(
            "/static/css/site.css", headers={"Accept-Encoding": "gzip"}
        )
        etag = first.headers["ETag"]
        self.assertTrue(etag.endswith(':gzip"'))
        second = self.client.get(
            "/static/css/site.css",
            headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
        )
        self.assertEqual(second.status_code, 304)

    def test_non_text_types_are_not_compressed(self):
        response = self.client.get(
            "/static/images/cryo.webp", headers={"Accept-Encoding": "gzip, br"}
        )
        self.assertNotIn("Content-Encoding", response.headers)
        csv_response = self.client.get(
            "/downloads/iisc-users.csv", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertNotIn("Content-Encoding", csv_response.headers)


if __name__ == "__main__":
    unittest.main()
