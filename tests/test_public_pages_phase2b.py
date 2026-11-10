import datetime
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from blueprints import public
from main import create_app


class PublicPagesPhase2bTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "phase2b.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def _execute(self, sql, params=()):
        with self.app.app_context():
            conn = database.get_db()
            conn.execute(sql, params)
            conn.commit()
            conn.close()

    # Instructions page -------------------------------------------------
    def test_facility_has_steps_checklist_and_acknowledgement_copy_button(self):
        html = self.client.get("/facility").get_data(as_text=True)
        # Layout details are covered by tests/test_instructions_and_home_stats.py;
        # this test guards the wording carried over from the original page.
        self.assertIn("Before you come", html)
        self.assertIn(
            # The facility's official acknowledgement statement, word for word.
            "We acknowledge DBT-BUILDER Program (BT/INF/22/SP22844/2017) and DST-FIST "
            "(SR/FST/LSII-039/2015) for the Advanced Center for Cryo Electron Microscopy "
            "Facility at IISc, Bangalore.",
            html,
        )
        self.assertRegex(html, r'<button[^>]*data-copy-target="ack-text"')
        self.assertIn("after screening at IISc", html)
        self.assertIn('href="mailto:somnath@iisc.ac.in"', html)
        self.assertIn('href="/rates"', html)
        # Key rules from the original page are all still present.
        for rule in (
            "Only expert users (mainly PIs) should prepare samples or hand over to the EM manager",
            "Only trained users are allowed to operate the microscope",
            "first and last grid positions of the cassette will be left unoccupied",
            "Industrial samples will receive preferential consideration",
            "not provided by the facility",
            "no assurance will be given about grid/data quality",
        ):
            self.assertIn(rule, html)

    # Home statistics ---------------------------------------------------
    def _store_stats(self, publications=99):
        payload = public.build_user_statistics_payload(
            [("Internal", 3), ("External", 1)],
            {"publications": publications, "structures": "85+"},
        )
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self._execute(
            "INSERT OR REPLACE INTO site_stats_cache (cache_key, payload, updated_at) VALUES (?, ?, ?)",
            (public.SITE_STATS_CACHE_KEY, json.dumps(payload), now.strftime("%Y-%m-%d %H:%M:%S")),
        )

    def test_home_renders_stat_tiles_from_embedded_payload(self):
        self._store_stats(publications=123)
        with patch("blueprints.public.start_site_stats_refresh"), \
                patch("blueprints.public.count_site_publications", return_value=123):
            html = self.client.get("/home").get_data(as_text=True)
        self.assertIn('<script type="application/json" id="user-stats-initial">', html)
        self.assertIn('data-stats-state="ready"', html)
        self.assertRegex(html, r'data-stat="users">4</strong>')
        self.assertRegex(html, r'data-stat="publications">123</strong>')
        self.assertRegex(html, r'data-stat="structures">85\+</strong>')
        self.assertEqual(html.count('class="donut-segment"'), 2)
        self.assertNotIn("Loading user data", html)

    def test_home_tiles_exist_when_no_payload_is_stored(self):
        with patch("blueprints.public.start_site_stats_refresh"):
            html = self.client.get("/home").get_data(as_text=True)
        if 'id="user-stats-initial"' in html:
            self.skipTest("a DB-only payload was embedded")
        self.assertIn('id="user-stats-container"', html)
        self.assertIn('data-stat="users"', html)
        self.assertIn('data-stat="publications"', html)

    # Publications --------------------------------------------------------
    def test_publications_have_count_year_headings_and_search(self):
        self._execute(
            "INSERT INTO managed_publications (citation, doi_url, published_year) VALUES (?, ?, ?)",
            ("Example Author. (2026). Example title. Example Journal.", None, 2026),
        )
        html = self.client.get("/publication").get_data(as_text=True)
        items = html.count("data-pub-item")
        self.assertGreater(items, 30)
        self.assertIn(f"{items} publications", html)
        headings = re.findall(r'class="pub-year__heading"[^>]*>([^<]+)<', html)
        years = [h.strip() for h in headings]
        self.assertEqual(years[0], "2026")
        self.assertIn("2025", years)
        self.assertIn("2017", years)
        self.assertEqual(years[-1], "Year not listed")
        numeric = [int(y) for y in years if y.isdigit()]
        self.assertEqual(numeric, sorted(numeric, reverse=True))
        self.assertIn('id="pub-search-input"', html)
        self.assertIn("No matches", html)
        # Search UI and "No matches" are hidden until JS enables them.
        self.assertRegex(html, r'<form class="pub-search"[^>]*hidden')
        self.assertRegex(html, r'<p class="pub-empty"[^>]*hidden')

    # Notices strip --------------------------------------------------------
    def test_notice_strip_shows_date_and_notice_id(self):
        self._execute(
            "INSERT INTO facility_notices (title, message, event_date, is_active) VALUES (?, ?, ?, 1)",
            ("Planned maintenance", "Microscope unavailable.", "2026-11-14"),
        )
        html = self.client.get("/facility").get_data(as_text=True)
        self.assertRegex(html, r'class="facility-notice__item notice-item" data-notice-id="\d+"')
        self.assertIn('<time class="notice-date" datetime="2026-11-14">14 Nov 2026</time>', html)
        self.assertIn('class="facility-notice__dismiss"', html)


if __name__ == "__main__":
    unittest.main()
