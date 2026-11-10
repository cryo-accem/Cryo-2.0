import csv
import io
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
import publication_finder as pf
from blueprints import admin as admin_module
from blueprints import public
from main import create_app


def _epmc(doi, title, year=2024, source="MED", authors="Rao A, Iyer B, Dutta S.", pmid=None):
    return {
        "id": pmid or (doi or title)[-8:], "source": source, "pmid": pmid, "doi": doi,
        "title": title, "authorString": authors, "journalTitle": "J Test" if source != "PPR" else None,
        "pubYear": str(year),
    }


def _query(tag="A", field="acknowledgement", confidence="high", source="Europe PMC"):
    return {
        "id": f"{tag}:{field}", "tag": tag, "field": field,
        "confidence": confidence, "source": source, "query": "q",
    }


def _record(result, query=None):
    return pf._europe_pmc_record(result), query or _query()


class NoNetworkMixin:
    def block_network(self):
        blocker = patch("urllib.request.urlopen", side_effect=AssertionError("network used in test"))
        blocker.start()
        self.addCleanup(blocker.stop)


class MergeTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        self.block_network()

    def test_normalize_doi(self):
        self.assertEqual(pf.normalize_doi("https://doi.org/10.1038/S41467-024-47142-6"), "10.1038/s41467-024-47142-6")
        self.assertEqual(pf.normalize_doi("doi:10.1101/2025.04.25.650598."), "10.1101/2025.04.25.650598")
        self.assertIsNone(pf.normalize_doi(None))
        self.assertIsNone(pf.normalize_doi("https://example.org/paper"))

    def test_records_with_same_doi_merge_and_keep_highest_confidence(self):
        result = _epmc("10.1000/abc", "A cryo-EM structure of a thing")
        merged = pf.merge_records([
            _record(result, _query("A", "fulltext", "medium")),
            _record(dict(result, doi="10.1000/ABC"), _query("A", "acknowledgement", "high")),
            _record(result, _query("B", "fulltext", "high")),
        ])
        self.assertEqual(len(merged), 1)
        candidate = merged[0]
        self.assertEqual(candidate["doi"], "10.1000/abc")
        self.assertEqual(candidate["confidence"], "high")
        self.assertEqual(candidate["matched_queries"], ["A:fulltext", "A:acknowledgement", "B:fulltext"])
        self.assertEqual(pf.describe_matches(candidate), "A: full text, acknowledgement · B: full text")
        self.assertEqual(candidate["doi_url"], "https://doi.org/10.1000/abc")
        self.assertNotIn("_author_keys", candidate)

    def test_medium_only_candidate_stays_medium(self):
        merged = pf.merge_records([_record(_epmc("10.1000/m", "Some title here"), _query("A", "fulltext", "medium"))])
        self.assertEqual(merged[0]["confidence"], "medium")

    def test_similar_titles_with_different_dois_merge(self):
        merged = pf.merge_records([
            _record(_epmc("10.1000/one", "Structural insights into pre-pore intermediates of alpha-hemolysin.")),
            _record(_epmc("10.2000/two", "Structural insights into pre-pore intermediates of &lt;i&gt;alpha&lt;/i&gt;-hemolysin")),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["other_dois"], ["10.2000/two"])

    def test_different_titles_stay_separate(self):
        merged = pf.merge_records([
            _record(_epmc("10.1000/one", "Cryo-EM structure of GABA transporter 1")),
            _record(_epmc("10.1000/two", "Cryo-EM structures of pannexin 1 and 3")),
        ])
        self.assertEqual(len(merged), 2)

    def test_preprint_is_linked_to_published_version(self):
        title = "Cryo-EM reveals the structural heterogeneity of multidrug efflux pumps MdtB and MdtF"
        merged = pf.merge_records([
            _record(_epmc("10.1101/2025.04.25.650598", title, 2025, "PPR")),
            _record(_epmc("10.1128/mbio.02684-25", title + ".", 2026)),
        ])
        self.assertEqual(len(merged), 1)
        primary = merged[0]
        self.assertEqual(primary["doi"], "10.1128/mbio.02684-25")
        self.assertFalse(primary["is_preprint"])
        self.assertEqual([p["doi"] for p in primary["also_preprint"]], ["10.1101/2025.04.25.650598"])
        self.assertEqual(primary["other_dois"], [])

    def test_preprint_with_rewritten_title_links_by_authors(self):
        authors = "Majumder P, Ahmed S, Ahuja P, Athreya A, Penmatsa A."
        merged = pf.merge_records([
            _record(_epmc("10.1101/2022.07.09.499445",
                          "CryoEM structure of QacA, an antibacterial efflux transporter from Staphylococcus aureus",
                          2022, "PPR", authors)),
            _record(_epmc("10.15252/embj.2023113418",
                          "Cryo-EM structure of antibacterial efflux transporter QacA from Staphylococcus aureus reveals a novel extracellular loop",
                          2023, "MED", authors)),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["doi"], "10.15252/embj.2023113418")
        self.assertEqual(merged[0]["also_preprint"][0]["doi"], "10.1101/2022.07.09.499445")

    def test_unrelated_preprint_by_other_authors_is_not_linked(self):
        merged = pf.merge_records([
            _record(_epmc("10.1101/2022.01.01.1", "Structure of QacA transporter", 2022, "PPR", "Smith J, Doe A.")),
            _record(_epmc("10.1000/pub", "Structure of the QacA efflux transporter in membranes", 2023, "MED", "Rao A, Iyer B.")),
        ])
        self.assertEqual(len(merged), 2)
        preprint = next(c for c in merged if c["is_preprint"])
        self.assertTrue(pf.is_preprint_doi(preprint["doi"]))

    def test_correction_is_folded_into_original(self):
        merged = pf.merge_records([
            _record(_epmc("10.1107/corr", "Macromolecular crystallography at Elettra: current and future perspectives. Corrigendum", 2026)),
            _record(_epmc("10.1107/orig", "Macromolecular crystallography at Elettra: current and future perspectives", 2025)),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["doi"], "10.1107/orig")


class CompareTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        self.block_network()

    def test_status_by_doi_title_and_missing(self):
        candidates = pf.merge_records([
            _record(_epmc("10.1000/on-site", "Paper listed by DOI")),
            _record(_epmc("10.1083/jcb.202102035", "Single-particle cryo-EM reveals conformational variability of the oligomeric VCC beta-barrel pore in a lipid bilayer")),
            _record(_epmc("10.1000/new", "A brand new facility paper")),
            _record(_epmc("10.1101/2020.01.01.5", "Preprint of a listed article", 2020, "PPR")),
        ])
        site = [
            {"year": 2024, "text": "Rao A. (2024). Paper listed by DOI. J Test.", "doi": "https://doi.org/10.1000/ON-SITE"},
            {"year": 2021, "html": "Sengupta, N. et al. (2021). Single-particle cryo-EM reveals conformational variability of the oligomeric VCC β-barrel pore in a lipid bilayer. <em>J Cell Biol</em>.", "doi": None},
            {"year": 2020, "text": "Old paper.", "doi": "https://doi.org/10.1101/2020.01.01.5"},
            {"year": 2019, "text": "Never found online.", "doi": "https://doi.org/10.9000/old"},
        ]
        result = pf.compare_with_site(candidates, site)
        self.assertEqual({c["doi"] for c in result["on_website"]},
                         {"10.1000/on-site", "10.1083/jcb.202102035", "10.1101/2020.01.01.5"})
        self.assertEqual([c["doi"] for c in result["not_on_website"]], ["10.1000/new"])
        self.assertEqual(result["not_on_website"][0]["status"], "not_on_website")
        self.assertEqual([e["doi"] for e in result["on_website_not_found_online"]], ["10.9000/old"])
        self.assertEqual(result["on_website_not_found_online"][0]["status"], "on_website_not_found_online")

    def test_published_candidate_matches_site_listing_its_preprint(self):
        candidate = pf.merge_records([
            _record(_epmc("10.1101/2025.05.28.656627", "Viral IRES recruitment to the preinitiation complex", 2025, "PPR")),
            _record(_epmc("10.7554/elife.107788", "Viral IRES recruitment to the preinitiation complex.", 2026)),
        ])
        site = [{"year": 2025, "text": "Das D. Preprint citation.", "doi": "https://doi.org/10.1101/2025.05.28.656627"}]
        result = pf.compare_with_site(candidate, site)
        self.assertEqual(len(result["on_website"]), 1)
        self.assertEqual(result["on_website_not_found_online"], [])

    def test_csv_contains_all_groups(self):
        candidates = pf.merge_records([_record(_epmc("10.1000/new", "A brand new facility paper"))])
        comparison = pf.compare_with_site(candidates, [{"year": 2019, "text": "Old.", "doi": None}])
        rows = list(csv.DictReader(io.StringIO(pf.results_csv(comparison))))
        self.assertEqual([r["status"] for r in rows], ["not_on_website", "on_website_not_found_online"])
        self.assertEqual(rows[0]["doi"], "10.1000/new")
        self.assertEqual(rows[0]["matched"], "A: acknowledgement")


class FindCandidatesTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        self.block_network()
        self.calls = []

    def fake_get_json(self, url, params):
        self.calls.append((url, dict(params)))
        if url == pf.OPENALEX_WORKS_URL:
            return {"meta": {"count": 2}, "results": [
                {"doi": "https://doi.org/10.64898/2026.02.07.704521", "title": "Dengue antibodies",
                 "publication_year": 2026, "type": "preprint", "ids": {},
                 "authorships": [{"author": {"display_name": "Ananya Chatterjee"},
                                  "institutions": [{"display_name": "Indian Institute of Science Bangalore"}],
                                  "raw_affiliation_strings": []}]},
                {"doi": "https://doi.org/10.5000/elsewhere", "title": "Unrelated centre",
                 "publication_year": 2020, "type": "article", "ids": {},
                 "authorships": [{"author": {"display_name": "X Y"},
                                  "institutions": [{"display_name": "Elsewhere University"}],
                                  "raw_affiliation_strings": ["Elsewhere"]}]},
            ]}
        query = params["query"]
        if "National Cryo-EM Facility" in query:
            results = [_epmc("10.1000/ack", "Acknowledged paper")]
        elif query.startswith("ACK_FUND:(\"cryo-EM facility\""):
            results = [_epmc("10.1000/ack", "Acknowledged paper")]
        elif query.startswith("(\"cryo-EM facility\""):
            results = [_epmc("10.1000/ack", "Acknowledged paper"), _epmc("10.1000/text", "Text only paper", 2020)]
        else:
            results = []
        return {"hitCount": len(results), "resultList": {"result": results}, "nextCursorMark": "*"}

    def test_find_candidates_merges_sources_and_flags(self):
        report = {}
        with patch.object(pf, "_get_json", side_effect=self.fake_get_json), \
                patch.dict(os.environ, {"PUBLICATION_FINDER_CONTACT": ""}):
            candidates = pf.find_candidates(report)
        by_doi = {c["doi"]: c for c in candidates}
        self.assertEqual(set(by_doi), {"10.1000/ack", "10.1000/text", "10.64898/2026.02.07.704521"})
        self.assertEqual(by_doi["10.1000/ack"]["confidence"], "high")
        self.assertTrue(by_doi["10.1000/ack"]["mentions_national_facility"])
        self.assertEqual(by_doi["10.1000/text"]["confidence"], "medium")
        dengue = by_doi["10.64898/2026.02.07.704521"]
        self.assertTrue(dengue["is_preprint"])
        self.assertEqual(dengue["sources"], ["OpenAlex"])
        self.assertEqual(dengue["confidence"], "high")
        ids = [q["id"] for q in report["queries"]]
        self.assertIn("A:acknowledgement", ids)
        self.assertIn("flag:national-facility", ids)
        # OpenAlex: centre-name full-text filter only, never free-text "search",
        # and no mailto unless configured.
        openalex = [params for url, params in self.calls if url == pf.OPENALEX_WORKS_URL]
        self.assertEqual(len(openalex), 4)
        for params in openalex:
            self.assertNotIn("search", params)
            self.assertNotIn("mailto", params)
            self.assertTrue(params["filter"].startswith("fulltext.search:\"Advanced Cent"))
        # "ACCEM" is only ever searched together with the institute.
        for url, params in self.calls:
            if url == pf.EUROPE_PMC_SEARCH_URL and "ACCEM" in params["query"]:
                self.assertIn('(IISc OR "Indian Institute of Science")', params["query"])

    def test_contact_is_sent_as_openalex_mailto_when_configured(self):
        with patch.object(pf, "_get_json", side_effect=self.fake_get_json), \
                patch.dict(os.environ, {"PUBLICATION_FINDER_CONTACT": "facility@example.org"}):
            pf.find_candidates()
        openalex = [params for url, params in self.calls if url == pf.OPENALEX_WORKS_URL]
        self.assertTrue(all(params["mailto"] == "facility@example.org" for params in openalex))

    def test_partial_failure_is_reported_and_total_failure_raises(self):
        def flaky(url, params):
            if url == pf.OPENALEX_WORKS_URL:
                raise OSError("timed out")
            return self.fake_get_json(url, params)

        report = {}
        with patch.object(pf, "_get_json", side_effect=flaky):
            candidates = pf.find_candidates(report)
        self.assertTrue(candidates)
        errors = [q for q in report["queries"] if q["error"]]
        self.assertEqual(len(errors), 4)
        with patch.object(pf, "_get_json", side_effect=OSError("down")):
            with self.assertRaises(pf.FinderError):
                pf.find_candidates()

    def test_http_helper_uses_timeout_user_agent_and_retries_once(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timed out")) as urlopen:
            with self.assertRaises(OSError):
                pf._get_json(pf.EUROPE_PMC_SEARCH_URL, {"query": "x"})
        self.assertEqual(urlopen.call_count, 2)
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs["timeout"], pf.REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(request.get_header("User-agent"), "ACCEM-publication-finder/1.0")

    def test_one_crashing_source_does_not_fail_the_search(self):
        import http.client
        calls = []

        def flaky(query):
            calls.append(query)
            if "AFF:" in query:
                raise http.client.IncompleteRead(b"")  # not an OSError
            return 0, []

        with patch.object(pf, "search_europe_pmc", side_effect=flaky), \
                patch.object(pf, "search_openalex_fulltext", return_value=(0, [])):
            report = {}
            pf.find_candidates(report)
        errors = [q for q in report["queries"] if q["error"]]
        self.assertTrue(errors)
        self.assertTrue(all("IncompleteRead" in q["error"] for q in errors))
        self.assertLess(len(errors), len(report["queries"]))

    def test_search_tags_drive_the_queries(self):
        queries = {q["id"]: q for q in pf.build_queries()}
        self.assertEqual(set(pf.SEARCH_TAGS), {"A", "B", "C"})  # C = DBT-BUILDER grant code
        ack_a = queries["A:acknowledgement"]["query"]
        self.assertIn('ACK_FUND:("cryo-EM facility" OR "cryo-electron microscopy facility"', ack_a)
        self.assertIn('ACK_FUND:(IISc OR "Indian Institute of Science")', ack_a)
        self.assertIn("(Bangalore OR Bengaluru)", ack_a)
        self.assertEqual(queries["A:fulltext"]["confidence"], "medium")
        self.assertEqual(queries["B:fulltext"]["confidence"], "high")
        self.assertIn("ACCEM) AND (IISc", queries["B:fulltext"]["query"])


class AdminPublicationFinderTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        self.block_network()
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "finder.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            conn = database.get_db()
            session["admin_user_id"] = conn.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    def csrf(self):
        self.client.get("/publication")
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def candidates(self):
        return pf.merge_records([
            _record(_epmc("10.1000/new-paper", "A brand new facility paper", 2026, authors="Rao A, Iyer B, Dutta S.")),
            _record(_epmc("10.1038/s41467-025-61741-x", "Structural insights into pre-pore intermediates of alpha-hemolysin in the lipidic environment", 2025)),
        ])

    def run_search(self):
        token = self.csrf()
        with patch("publication_finder.find_candidates", return_value=self.candidates()) as finder:
            response = self.client.post("/admin/publications/finder/run", data={"_csrf_token": token})
        return response, finder, token

    def managed_count(self, doi_url=None):
        conn = database.get_db()
        if doi_url:
            row = conn.execute("SELECT COUNT(*) AS n FROM managed_publications WHERE doi_url=?", [doi_url]).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM managed_publications").fetchone()
        conn.close()
        return row["n"]

    def test_endpoints_are_in_admin_allowlist(self):
        for endpoint in ("admin.publication_finder", "admin.run_publication_finder", "admin.dismiss_finder_publication",
                         "admin.add_finder_publication", "admin.download_publication_finder_csv"):
            self.assertIn(endpoint, admin_module._ALLOWED_ADMIN_ENDPOINTS)
        self.login()
        self.assertEqual(self.client.get("/admin/publications/finder").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("admin_logged_in"))

    def test_pages_require_login(self):
        for path in ("/admin/publications/finder", "/admin/publications/finder.csv"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertTrue(response.headers["Location"].endswith("/admin/"), path)
        token = self.csrf()
        with patch("publication_finder.find_candidates") as finder:
            response = self.client.post("/admin/publications/finder/run", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 302)
        finder.assert_not_called()

    def test_page_shows_search_terms_and_sidebar_link(self):
        self.login()
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        self.assertIn("Searching for", html)
        self.assertIn("cryo-EM facility, IISc Bangalore", html)
        self.assertIn("Advanced Center for Cryo-Electron Microscopy, Indian Institute of Science, Bangalore", html)
        self.assertIn("National Cryo-EM Facility", html)
        self.assertIn('href="/admin/publications/finder"', html)
        self.assertIn("No search has been run yet", html)
        self.assertNotRegex(html.split('<main', 1)[1], r'\sstyle="|<script(?![^>]*src=)[^>]*>')

    def test_run_search_requires_csrf(self):
        self.login()
        with patch("publication_finder.find_candidates") as finder:
            response = self.client.post("/admin/publications/finder/run")
        self.assertEqual(response.status_code, 400)
        finder.assert_not_called()

    def test_run_search_stores_results_and_page_groups_them(self):
        self.login()
        response, finder, _ = self.run_search()
        self.assertEqual(response.status_code, 302)
        finder.assert_called_once()
        with self.app.app_context():
            stored = public.load_cache_payload("publication_finder")
        self.assertEqual({c["doi"] for c in stored["candidates"]},
                         {"10.1000/new-paper", "10.1038/s41467-025-61741-x"})
        self.assertIn("ran_at", stored)
        with patch("publication_finder.find_candidates") as again:
            html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        again.assert_not_called()
        not_on_site = html.split('id="not-on-website"', 1)[1].split('id="on-website"', 1)[0]
        on_site = html.split('id="on-website"', 1)[1].split('id="not-found-online"', 1)[0]
        self.assertIn("A brand new facility paper", not_on_site)
        self.assertIn("Add to website", not_on_site)
        self.assertIn("pre-pore intermediates", on_site)
        self.assertNotIn("Add to website", on_site)
        self.assertIn('href="https://doi.org/10.1000/new-paper"', html)
        self.assertIn("A: acknowledgement", html)
        self.assertIn("pubfinder-badge--high", html)

    def test_add_to_website_inserts_managed_publication_once(self):
        self.login()
        _, _, token = self.run_search()
        before = self.client.get("/publication").get_data(as_text=True).count("data-pub-item")
        response = self.client.post("/admin/publications/finder/add",
                                    data={"_csrf_token": token, "key": "10.1000/new-paper"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.managed_count("https://doi.org/10.1000/new-paper"), 1)
        conn = database.get_db()
        row = conn.execute("SELECT citation, published_year, is_visible FROM managed_publications").fetchone()
        conn.close()
        self.assertEqual(row["published_year"], 2026)
        self.assertEqual(row["is_visible"], 1)
        self.assertIn("A brand new facility paper", row["citation"])
        page = self.client.get("/publication").get_data(as_text=True)
        self.assertEqual(page.count("data-pub-item"), before + 1)
        # A second click does not duplicate it; it now shows as on the website.
        self.client.post("/admin/publications/finder/add",
                         data={"_csrf_token": token, "key": "10.1000/new-paper"})
        self.assertEqual(self.managed_count(), 1)
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        on_site = html.split('id="on-website"', 1)[1].split('id="not-found-online"', 1)[0]
        self.assertIn("A brand new facility paper", on_site)

    def test_add_shows_a_success_banner_next_to_the_results(self):
        self.login()
        _, _, token = self.run_search()
        response = self.client.post("/admin/publications/finder/add",
                                    data={"_csrf_token": token, "key": "10.1000/new-paper"})
        self.assertTrue(response.headers["Location"].endswith("/admin/publications/finder#finder-added"))
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        banner = html.split('id="finder-added"', 1)[1].split("</div>\n  </div>", 1)[0]
        self.assertIn("Added to the website", banner)
        self.assertIn("A brand new facility paper", banner)
        self.assertIn('href="/publication"', banner)
        self.assertLess(html.index('id="finder-added"'), html.index('id="not-on-website"'))
        # Shown once: a later visit has no banner.
        self.assertNotIn('id="finder-added"', self.client.get("/admin/publications/finder").get_data(as_text=True))

    def test_add_button_shows_busy_label(self):
        self.login()
        _, _, _ = self.run_search()
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        self.assertIn('class="inline-form" data-busy-label="Adding…"', html)

    def _not_on_website(self):
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        return html, html.split('id="not-on-website"', 1)[1].split('id="on-website"', 1)[0]

    def test_not_ours_hides_a_result_and_it_stays_hidden_after_a_new_search(self):
        self.login()
        _, _, token = self.run_search()
        _, section = self._not_on_website()
        self.assertIn("A brand new facility paper", section)
        self.assertIn("Not ours", section)
        response = self.client.post("/admin/publications/finder/dismiss",
                                    data={"_csrf_token": token, "key": "10.1000/new-paper", "action": "dismiss"})
        self.assertTrue(response.headers["Location"].endswith("#finder-added"))
        html, section = self._not_on_website()
        self.assertNotIn("A brand new facility paper", section)
        banner = html.split('id="finder-added"', 1)[1].split('id="not-on-website"', 1)[0]
        self.assertIn("Removed from the results", banner)
        self.assertIn('value="restore"', banner)  # Undo
        dismissed = html.split('id="dismissed"', 1)[1]
        self.assertIn("A brand new facility paper", dismissed)
        # Running the search again does not bring it back.
        self.run_search()
        _, section = self._not_on_website()
        self.assertNotIn("A brand new facility paper", section)
        # It cannot be added while dismissed, and is left out of the CSV.
        self.client.post("/admin/publications/finder/add",
                         data={"_csrf_token": token, "key": "10.1000/new-paper"})
        self.assertEqual(self.managed_count(), 0)
        self.assertNotIn("10.1000/new-paper", self.client.get("/admin/publications/finder.csv").get_data(as_text=True))

    def test_restore_brings_a_dismissed_result_back(self):
        self.login()
        _, _, token = self.run_search()
        self.client.post("/admin/publications/finder/dismiss",
                         data={"_csrf_token": token, "key": "10.1000/new-paper", "action": "dismiss"})
        self.client.post("/admin/publications/finder/dismiss",
                         data={"_csrf_token": token, "key": "10.1000/new-paper", "action": "restore"})
        html, section = self._not_on_website()
        self.assertIn("A brand new facility paper", section)
        self.assertNotIn('id="dismissed"', html)

    def test_dismissed_papers_do_not_feed_the_emdb_count(self):
        self.login()
        _, _, token = self.run_search()
        self.client.post("/admin/publications/finder/dismiss",
                         data={"_csrf_token": token, "key": "10.1000/new-paper", "action": "dismiss"})
        with self.app.app_context(), patch("emdb_structures.compute_structures",
                                           return_value={"entries": [], "needs_review": [], "excluded": {}}) as compute:
            public.refresh_emdb_structures()
        dois = {c.get("doi") for c in compute.call_args.kwargs["finder_candidates"]}
        self.assertNotIn("10.1000/new-paper", dois)

    def test_add_rejects_unknown_key(self):
        self.login()
        _, _, token = self.run_search()
        self.client.post("/admin/publications/finder/add",
                         data={"_csrf_token": token, "key": "10.9000/not-in-results"})
        self.assertEqual(self.managed_count(), 0)

    def test_maintenance_save_still_validates(self):
        self.login()
        token = self.csrf()
        self.client.post("/admin/maintenance/publications/save", data={
            "_csrf_token": token, "citation": "X", "doi_url": "javascript:alert(1)", "published_year": "2024",
        })
        self.client.post("/admin/maintenance/publications/save", data={
            "_csrf_token": token, "citation": "X", "doi_url": "", "published_year": "1800",
        })
        self.assertEqual(self.managed_count(), 0)
        self.client.post("/admin/maintenance/publications/save", data={
            "_csrf_token": token, "citation": "Valid", "doi_url": "https://doi.org/10.1000/v", "published_year": "2024",
        })
        self.assertEqual(self.managed_count(), 1)

    def test_csv_download(self):
        self.login()
        self.assertEqual(self.client.get("/admin/publications/finder.csv").status_code, 302)
        self.run_search()
        response = self.client.get("/admin/publications/finder.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn("attachment; filename=publication-finder-", response.headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        statuses = {r["doi"]: r["status"] for r in rows if r["doi"]}
        self.assertEqual(statuses["10.1000/new-paper"], "not_on_website")
        self.assertEqual(statuses["10.1038/s41467-025-61741-x"], "on_website")
        self.assertIn("on_website_not_found_online", {r["status"] for r in rows})

    def test_home_tile_shows_curated_publication_count(self):
        with patch("blueprints.public.start_site_stats_refresh"):
            with self.app.app_context():
                public.refresh_site_stats()
            html = self.client.get("/home").get_data(as_text=True)
        expected = len(public.CURATED_PUBLICATIONS)
        self.assertIn(f'data-stat="publications">{expected}</strong>', html)
        self.assertIn("Listed on our Publications page", html)
        self.assertNotIn("OpenAlex", html)
        self.assertIn('href="/publication"', html.split('data-stat="publications"', 1)[1][:400])
        page = self.client.get("/publication").get_data(as_text=True)
        self.assertIn(f"{expected} publications", page)

    def test_check_publications_cli_writes_csv(self):
        import check_publications

        out_path = os.path.join(self.temp_dir.name, "out.csv")
        buffer = io.StringIO()
        with patch("publication_finder.find_candidates", return_value=self.candidates()):
            comparison = check_publications.run(out_path, out=buffer)
        self.assertEqual([c["doi"] for c in comparison["not_on_website"]], ["10.1000/new-paper"])
        text = buffer.getvalue()
        self.assertIn("Not on website: 1", text)
        self.assertIn("A brand new facility paper", text)
        with open(out_path, encoding="utf-8") as handle:
            self.assertIn("10.1000/new-paper", handle.read())


if __name__ == "__main__":
    unittest.main()


class GrantCodeSearchTests(unittest.TestCase):
    def test_dbt_builder_grant_code_is_searched_with_high_confidence(self):
        import publication_finder as pf
        queries = [q for q in pf.build_queries() if q["tag"] == "C"]
        self.assertEqual({q["field"] for q in queries}, {"acknowledgement", "fulltext"})
        self.assertTrue(all(q["confidence"] == "high" for q in queries))
        ack = next(q for q in queries if q["field"] == "acknowledgement")
        self.assertEqual(ack["query"], "ACK_FUND:(SP22844)")
        # The MBU-wide DST-FIST grant is deliberately not a search term.
        self.assertFalse(any("LSII" in q["query"] for q in pf.build_queries()))

