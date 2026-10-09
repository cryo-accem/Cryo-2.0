import os
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from blueprints.public import get_slideshow_images
from main import create_app


class PublicSlideshowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "public-assets.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.client = self.app.test_client()

    def test_optimized_webp_replaces_source_image_in_slideshow_listing(self):
        with self.app.app_context():
            images = get_slideshow_images()

        self.assertIn("1c.webp", images)
        self.assertNotIn("1c.jpg", images)
        self.assertEqual(len(images), len(set(images)))

    def test_webp_responses_are_cached_for_one_day(self):
        response = self.client.get("/static/images/iisc_logo.webp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/webp")
        self.assertIn("public", response.headers["Cache-Control"])
        self.assertIn("max-age=86400", response.headers["Cache-Control"])
        self.assertNotIn("no-cache", response.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
