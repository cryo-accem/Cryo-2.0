import hashlib
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class StaticFingerprintTests(unittest.TestCase):
    """CSS/JS links carry ?v=<content hash> automatically, so edits bust the year-long cache."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = os.path.join(self.temp_dir.name, "fp.sqlite3")
        patcher = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app()
        self.app.testing = True

    def _url(self, filename, **kwargs):
        with self.app.test_request_context():
            from flask import url_for
            return url_for("static", filename=filename, **kwargs)

    def test_css_link_uses_content_hash(self):
        path = os.path.join(self.app.static_folder, "css", "site.css")
        with open(path, "rb") as fh:
            expected = hashlib.sha256(fh.read()).hexdigest()[:12]
        self.assertEqual(self._url("css/site.css"), f"/static/css/site.css?v={expected}")

    def test_hash_changes_when_file_changes(self):
        static_dir = os.path.join(self.temp_dir.name, "static")
        os.makedirs(static_dir)
        asset = os.path.join(static_dir, "app.js")
        with open(asset, "w") as fh:
            fh.write("console.info(1);")
        self.app.static_folder = static_dir
        first = self._url("app.js")
        with open(asset, "w") as fh:
            fh.write("console.info(2);")
        os.utime(asset, (1, 1))  # force a different mtime even on coarse filesystems
        second = self._url("app.js")
        self.assertRegex(first, r"\?v=[0-9a-f]{12}$")
        self.assertNotEqual(first, second)

    def test_manual_version_is_replaced_and_fonts_untouched(self):
        self.assertNotIn("v=old", self._url("js/site.js", v="old"))
        self.assertEqual(
            self._url("fonts/inter-latin-wght-normal.woff2", v="5.1.0"),
            "/static/fonts/inter-latin-wght-normal.woff2?v=5.1.0",
        )

    def test_missing_file_gets_no_version(self):
        self.assertEqual(self._url("js/does-not-exist.js"), "/static/js/does-not-exist.js")

    def test_rendered_page_links_are_fingerprinted_and_cached_for_a_year(self):
        client = self.app.test_client()
        html = client.get("/home").get_data(as_text=True)
        links = re.findall(r'/static/(?:css|js)/[^"\']+\?v=([0-9a-f]{12})', html)
        self.assertGreaterEqual(len(links), 3)
        css = re.search(r'(/static/css/site\.css\?v=[0-9a-f]{12})', html).group(1)
        response = client.get(css)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")


if __name__ == "__main__":
    unittest.main()
