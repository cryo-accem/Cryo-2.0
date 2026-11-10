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


class InstructionsAndHomeStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "instructions.sqlite3")
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

    # Instructions page ---------------------------------------------------
    def test_instructions_page_has_six_linked_steps(self):
        response = self.client.get("/facility")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('class="ins-step"'), 6)
        for title in (
            "Contact the EM manager",
            "Share sample details &amp; preliminary data",
            "Grid freezing",
            "Screening",
            "Data collection",
            "Data hand-off &amp; processing",
        ):
            self.assertIn(f"<h3>{title}</h3>", html)
        with self.app.test_request_context():
            from flask import url_for
            links = [
                url_for("register.register"),
                url_for("freezing.freezing_schedule"),
                url_for("screening.screening_list"),
                url_for("imaging.list_view"),
                url_for("public.grid_inventory"),
            ]
        for link in links:
            self.assertIn(f'class="ins-step__link" href="{link}"', html)

    def test_instructions_checklist_acknowledgement_and_contact(self):
        html = self.client.get("/facility").get_data(as_text=True)
        self.assertIn("Good to know", html)
        card = html.split('class="ins-card ins-checklist"', 1)[1].split("</section>", 1)[0]
        groups = re.findall(r'<h3 class="ins-checklist__group">([^<]+)</h3>', card)
        self.assertEqual(groups, ["Before you come", "Facility rules", "Scheduling"])
        self.assertEqual(card.count("<li>"), 7)
        before = card.split("Before you come", 1)[1].split("Facility rules", 1)[0]
        self.assertIn("preliminary purification or biochemical data", before)
        self.assertIn("Bring your own hard drive", before)
        checklist = card
        for fact in (
            "preliminary purification or biochemical data",
            "Bring your own hard drive",
            "Latitude S",
            "mainly PIs",
            "cannot request cryo-freezing, cartridge preparation or cassette transfer",
            "first and last cassette positions",
            "Industrial samples will receive preferential consideration",
        ):
            self.assertIn(fact, checklist)
        self.assertIn('<blockquote id="ack-text">We acknowledge DBT-BUILDER Program (BT/INF/22/SP22844/2017) and DST-FIST (SR/FST/LSII-039/2015) for the Advanced Center for Cryo Electron Microscopy Facility at IISc, Bangalore.</blockquote>', html)
        self.assertRegex(html, r'<button class="ins-ack__copy" type="button" data-copy-target="ack-text">')
        self.assertIn("Copy acknowledgement", html)
        self.assertRegex(html, r'role="status" aria-live="polite" data-copy-status')
        contact = html.split('class="ins-contact"', 1)[1].split("</p>", 1)[0]
        self.assertIn('href="mailto:cryoem.iisc@gmail.com"', contact)
        self.assertIn('href="mailto:somnath@iisc.ac.in"', contact)
        self.assertNotRegex(html.split("<main>", 1)[1], r'\sstyle="')

    def test_instructions_renders_admin_managed_fields(self):
        self._execute(
            "UPDATE managed_pages SET eyebrow=?, title=?, intro=?, additional_content=? WHERE page_key='facility'",
            ("Managed eyebrow", "Managed title", "Managed intro text", "Managed extra <b>notes</b>"),
        )
        with self.app.app_context():
            conn = database.get_db()
            row = conn.execute("SELECT COUNT(*) FROM managed_pages WHERE page_key='facility'").fetchone()
            conn.close()
        if not row[0]:
            self._execute(
                "INSERT INTO managed_pages (page_key, page_name, eyebrow, title, intro, additional_content) "
                "VALUES ('facility', 'Instructions', ?, ?, ?, ?)",
                ("Managed eyebrow", "Managed title", "Managed intro text", "Managed extra <b>notes</b>"),
            )
        html = self.client.get("/facility").get_data(as_text=True)
        self.assertIn("<title>ACCEM | Managed title</title>", html)
        self.assertIn('<div class="eyebrow">Managed eyebrow</div><h1>Managed title</h1>', html)
        self.assertIn("<p>Managed intro text</p>", html)
        self.assertIn('class="managed-page-content prose">Managed extra &lt;b&gt;notes&lt;/b&gt;</div>', html)
        # Managed content sits between the header and the fixed step flow.
        self.assertLess(html.index("managed-page-content"), html.index('class="ins-flow"'))

    # Home statistics -------------------------------------------------------
    def _store_stats(self, stats, publications):
        payload = public.build_user_statistics_payload(
            stats, {"publications": publications, "structures": "85+"}
        )
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self._execute(
            "INSERT OR REPLACE INTO site_stats_cache (cache_key, payload, updated_at) VALUES (?, ?, ?)",
            (public.SITE_STATS_CACHE_KEY, json.dumps(payload), now.strftime("%Y-%m-%d %H:%M:%S")),
        )

    def test_home_renders_four_numbers_server_side(self):
        self._store_stats([("Internal", 74), ("External", 64), ("Industry", 25)], 57)
        with patch("blueprints.public.start_site_stats_refresh"), \
                patch("blueprints.public.count_site_publications", return_value=57):
            html = self.client.get("/home").get_data(as_text=True)
        self.assertIn('<script type="application/json" id="user-stats-initial">', html)
        numbers = html.split('class="stats-e__numbers"', 1)[1].split('class="stats-e__breakdown"', 1)[0]
        self.assertEqual(numbers.count('class="stats-row-tile__value"'), 4)
        self.assertIn('data-stat="users">163</strong>', numbers)
        self.assertIn('data-stat="external">89</strong>', numbers)
        self.assertIn('data-stat="publications">57</strong>', numbers)
        self.assertIn('data-stat="structures">85+</strong>', numbers)
        self.assertEqual(html.count('class="donut-segment"'), 3)
        self.assertRegex(html, r'class="donut-segment"[^>]*color="#[0-9A-Fa-f]{6}" stroke-dasharray="')
        # The public "Download user lists" panel was removed from the home page.
        self.assertNotIn("Download user lists", html)
        for endpoint in ("iisc-users.csv", "academic-users.csv", "industry-users.csv"):
            self.assertNotIn(f'href="/downloads/{endpoint}"', html)
        section = html.split('class="section user-statistics"', 1)[1].split('class="section glance"', 1)[0]
        self.assertNotRegex(section, r'\sstyle="')

    def test_home_stats_degrade_gracefully_without_payload(self):
        with patch("blueprints.public.load_site_stats", return_value=(None, None)), \
                patch("blueprints.public.start_site_stats_refresh"):
            response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('id="user-stats-initial"', html)
        self.assertIn('data-stats-state="pending"', html)
        for key in ("users", "external", "publications"):
            self.assertRegex(html, rf'data-stat="{key}">—</strong>')
        self.assertIn('data-stat="structures">85+</strong>', html)
        self.assertIn("Loading user data", html)
        self.assertNotIn('class="donut-segment"', html)


if __name__ == "__main__":
    unittest.main()
