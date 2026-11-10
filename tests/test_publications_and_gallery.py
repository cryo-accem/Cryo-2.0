import os
import re
import tempfile
import unittest
from unittest.mock import patch

from markupsafe import Markup

import database
from blueprints.public import link_emdb_ids
from main import create_app

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


class PublicationsAndGalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "pubgallery.sqlite3")
        patcher = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def _get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIsNone(INLINE_SCRIPT.search(html), "inline <script> found")
        self.assertIsNone(INLINE_HANDLER.search(html), "inline event handler found")
        return html

    def test_publications_grouped_by_year_with_counts_chips_and_script(self):
        with self.app.app_context():
            conn = database.get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO managed_publications (citation, doi_url, published_year) VALUES (?, ?, ?)",
                ("Author A. (2026). Map deposited as EMD-12345. <b>Journal</b>.", None, 2026),
            )
            conn.commit()
            cur.close()
            conn.close()
        html = self._get("/publication")

        total = html.count("data-pub-item")
        self.assertIn(f"{total} publications", html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('id="pub-search-input"', html)
        self.assertRegex(html, r'<script src="[^"]*js/publications\.js[^"]*"[^>]*defer')

        groups = re.findall(r'data-pub-year="(\d+)"', html)
        self.assertEqual(groups[0], "2026")
        self.assertEqual(groups[-1], "0")
        numeric = [int(y) for y in groups if y != "0"]
        self.assertEqual(numeric, sorted(numeric, reverse=True))
        self.assertEqual(len(groups), len(set(groups)))

        # Year jump chips: one per group, each linking to its heading, with counts summing to total.
        chips = re.findall(r'href="#pub-year-(\d+)" data-pub-year-link="\d+">[^<]*<span>(\d+)</span>', html)
        self.assertEqual([year for year, _ in chips], groups)
        self.assertEqual(sum(int(count) for _, count in chips), total)
        for year in groups:
            self.assertIn(f'id="pub-year-{year}"', html)

        # Managed citations stay escaped; EMDB accessions are linked.
        self.assertIn("&lt;b&gt;Journal&lt;/b&gt;", html)
        self.assertIn('href="https://www.ebi.ac.uk/emdb/EMD-12345"', html)

    def test_link_emdb_ids_escapes_plain_text_and_keeps_markup(self):
        self.assertEqual(str(link_emdb_ids("<i>x</i>")), "&lt;i&gt;x&lt;/i&gt;")
        linked = str(link_emdb_ids(Markup("<em>J</em> EMD-1234")))
        self.assertIn("<em>J</em>", linked)
        self.assertIn('href="https://www.ebi.ac.uk/emdb/EMD-1234"', linked)
        self.assertNotIn("emdb", str(link_emdb_ids("EMD-12 is too short")))

    def test_events_gallery_has_lightbox_hooks_without_hijacking_links(self):
        html = self._get("/events")
        self.assertIn('data-lightbox-gallery="events"', html)
        self.assertEqual(html.count("data-lightbox-item"), 3)
        # Separate enlarge buttons, hidden until JS runs; external links untouched.
        self.assertEqual(len(re.findall(r'<button class="gallery-enlarge"[^>]*data-lightbox-open[^>]*hidden', html)), 3)
        self.assertIn('<a class="event-photo" href="https://meetings.embo.org/event/24-cryo-em" target="_blank" rel="noopener">', html)
        self.assertNotRegex(html, r"<a [^>]*>(?:(?!</a>).)*<button", "button nested inside a link")
        gallery = html.split('data-lightbox-gallery="events"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(gallery.count("srcset="), 3)
        self.assertIn("hands-on-cryo-em-training-1920w.webp 1920w", html)
        self.assertRegex(html, r'<script src="[^"]*js/gallery\.js[^"]*"[^>]*defer')

    def test_community_gallery_has_lightbox_hooks_and_keeps_srcset(self):
        html = self._get("/community-gallery")
        self.assertIn('data-lightbox-gallery="community"', html)
        self.assertEqual(html.count("<figure data-lightbox-item>"), 6)
        gallery = html.split('data-lightbox-gallery="community"', 1)[1].split("</section>", 1)[0]
        imgs = re.findall(r"<img [^>]*>", gallery)
        gallery_imgs = [tag for tag in imgs if "srcset=" in tag]
        self.assertEqual(len(gallery_imgs), 6)
        for tag in gallery_imgs:
            self.assertRegex(tag, r'\bsizes="')
            self.assertRegex(tag, r'\bwidth="\d+"')
            self.assertRegex(tag, r'\bheight="\d+"')
        self.assertRegex(html, r'<script src="[^"]*js/gallery\.js[^"]*"[^>]*defer')


if __name__ == "__main__":
    unittest.main()
