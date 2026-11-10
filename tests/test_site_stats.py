import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from blueprints import public
from main import create_app


EXPECTED_KEYS = {"success", "total", "data", "metrics"}
EXPECTED_ITEM_KEYS = {"label", "value", "percentage", "color", "start", "end"}


class SiteStatsCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "site-stats.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def _store(self, payload, age_hours=0):
        updated = (
            datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            - datetime.timedelta(hours=age_hours)
        )
        conn = database.get_db()
        conn.execute(
            "INSERT OR REPLACE INTO site_stats_cache (cache_key, payload, updated_at) "
            "VALUES (?, ?, ?)",
            [public.SITE_STATS_CACHE_KEY, json.dumps(payload),
             updated.strftime("%Y-%m-%d %H:%M:%S")],
        )
        conn.commit()
        conn.close()

    def _site_publication_count(self):
        with self.app.app_context():
            return public.count_site_publications()

    def _sample_payload(self, publications=99):
        return public.build_user_statistics_payload(
            [("Internal", 3), ("External", 1)],
            {"publications": publications, "structures": "85+"},
        )

    def _assert_shape(self, body):
        self.assertEqual(set(body), EXPECTED_KEYS)
        self.assertTrue(body["success"])
        self.assertEqual(set(body["metrics"]), {"publications", "structures"})
        for item in body["data"]:
            self.assertEqual(set(item), EXPECTED_ITEM_KEYS)

    def test_table_is_created_by_init_db(self):
        conn = database.get_db()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='site_stats_cache'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_fresh_cache_is_served_without_network_or_refresh(self):
        self._store(self._sample_payload())
        self.app.config["SITE_STATS_BACKGROUND_REFRESH"] = True
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen, \
                patch("blueprints.public.start_site_stats_refresh") as refresh:
            response = self.client.get("/api/user-statistics")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self._assert_shape(body)
        # The stored count is replaced by the current Publications-page count.
        self.assertEqual(body["metrics"]["publications"], self._site_publication_count())
        self.assertEqual(body["total"], 4)
        urlopen.assert_not_called()
        refresh.assert_not_called()

    def test_empty_cache_serves_db_only_payload_without_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
            response = self.client.get("/api/user-statistics")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self._assert_shape(body)
        # Baseline Internal 74 + External 64 + Industry 25.
        self.assertEqual(body["total"], 163)
        self.assertEqual(body["metrics"]["publications"], self._site_publication_count())
        urlopen.assert_not_called()

    def test_stale_cache_is_served_and_refreshed_in_background(self):
        self._store(self._sample_payload(publications=10), age_hours=30)
        self.app.config["SITE_STATS_BACKGROUND_REFRESH"] = True
        started = []
        real_start = public.start_site_stats_refresh

        def capture():
            thread = real_start()
            started.append(thread)
            return thread

        with patch("blueprints.public.count_site_publications", return_value=42), \
                patch("blueprints.public.start_site_stats_refresh", side_effect=capture):
            response = self.client.get("/api/user-statistics")
            # Stale user totals are served; the publication count is current.
            self.assertEqual(response.get_json()["total"], 4)
            self.assertEqual(response.get_json()["metrics"]["publications"], 42)
            self.assertEqual(len(started), 1)
            self.assertIsNotNone(started[0])
            started[0].join(timeout=10)

        with self.app.app_context():
            payload, age = public.load_site_stats()
        self.assertEqual(payload["metrics"]["publications"], 42)
        self.assertEqual(payload["total"], 163)
        self.assertLess(age, 120)

    def test_only_one_background_refresh_runs_at_a_time(self):
        self.app.config["SITE_STATS_BACKGROUND_REFRESH"] = True
        with self.app.app_context():
            self.assertTrue(public._site_stats_refresh_lock.acquire(blocking=False))
            try:
                self.assertIsNone(public.start_site_stats_refresh())
            finally:
                public._site_stats_refresh_lock.release()

    def test_refresh_script_stores_payload(self):
        import refresh_site_stats

        with patch("blueprints.public.count_site_publications", return_value=55), \
                patch("refresh_site_stats.refresh_emdb_structures", return_value={"count": 7}) as emdb:
            from flask import Flask
            payload = refresh_site_stats.run(Flask("refresh-test"))
        self._assert_shape(payload)
        with self.app.app_context():
            stored, age = public.load_site_stats()
        self.assertEqual(stored, payload)
        self.assertEqual(stored["metrics"]["publications"], 55)
        self.assertEqual(payload["total"], 163)
        emdb.assert_called_once()
        self.assertLess(age, 120)

    def test_publication_count_matches_publications_page(self):
        with self.app.app_context():
            conn = database.get_db()
            conn.execute(
                "INSERT INTO managed_publications (citation, doi_url, published_year) VALUES (?, ?, ?)",
                ["New Author. (2026). A new facility paper. J Test.", "https://doi.org/10.1/new", 2026],
            )
            # A managed copy of a curated entry (same DOI) is not counted twice.
            conn.execute(
                "INSERT INTO managed_publications (citation, doi_url, published_year) VALUES (?, ?, ?)",
                ["Duplicate citation", "https://doi.org/10.1038/s41467-025-61741-X", 2025],
            )
            conn.commit()
            conn.close()
            payload = public.refresh_site_stats()
        page = self.client.get("/publication").get_data(as_text=True)
        items = page.count("data-pub-item")
        self.assertIn(f"{items} publications", page)
        self.assertEqual(payload["metrics"]["publications"], items)
        self.assertEqual(items, len(public.CURATED_PUBLICATIONS) + 1)

    def test_home_embeds_stored_payload(self):
        payload = self._sample_payload()
        self._store(payload)
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        marker = '<script type="application/json" id="user-stats-initial">'
        self.assertIn(marker, html)
        embedded = html.split(marker, 1)[1].split("</script>", 1)[0]
        expected = dict(payload, metrics=dict(
            payload["metrics"], publications=self._site_publication_count()
        ))
        self.assertEqual(json.loads(embedded), expected)

    def test_home_omits_tag_when_nothing_stored(self):
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="user-stats-initial"', response.data)

    def test_robots_and_sitemap(self):
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn(b"Disallow: /admin", robots.data)
        self.assertIn(b"Sitemap: http://localhost/sitemap.xml", robots.data)

        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertEqual(sitemap.mimetype, "application/xml")
        self.assertIn(b"<loc>http://localhost/home</loc>", sitemap.data)
        self.assertIn(b"<loc>http://localhost/publication</loc>", sitemap.data)
        self.assertNotIn(b"/admin", sitemap.data)


if __name__ == "__main__":
    unittest.main()
