import csv
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import database
import emdb_structures as es
from blueprints import admin as admin_module
from blueprints import public
from main import create_app

TALOS = "FEI TALOS ARCTICA"


def row(emdb_id, doi="", country="India", microscope=TALOS, depositors="Dutta S", paper="Paper", year="2024"):
    return {
        "emdb_id": emdb_id, "title": f"Structure {emdb_id}", "release_date": "2024-05-01T00:00:00Z",
        "microscope_name": microscope, "grant_country": country, "xref_DOI": doi,
        "xref_PUBMED": "", "primary_citation_title": paper, "primary_citation_year": year,
        "primary_citation_author_string": "", "dep_author_name": depositors,
    }


SITE = [
    {"year": 2024, "text": "Site paper one.", "doi": "https://doi.org/10.1000/site-1", "origin": "curated"},
    {"year": 2023, "text": "Site paper two.", "doi": "https://doi.org/10.1000/site-2", "origin": "managed"},
]
FINDER = [
    {"doi": "10.1000/found-high", "title": "Found high", "confidence": "high", "pmid": None, "also_preprint": [], "other_dois": []},
    {"doi": "10.1000/found-medium", "title": "Found medium", "confidence": "medium", "pmid": None, "also_preprint": [], "other_dois": []},
    {"doi": "10.1000/site-1", "title": "Site paper one", "confidence": "high", "pmid": "123456", "also_preprint": [], "other_dois": []},
]


def fake_search(query, fields=es.FIELDS):
    """Stand-in for the EMDB API, keyed on the query text."""
    if query.startswith("xref_DOI:"):
        rows = []
        if "10.1000/site-1" in query:
            rows += [row("EMD-1001", "10.1000/site-1", depositors="Rao A"), row("EMD-1002", "10.1000/site-1", depositors="Rao A")]
        if "10.1000/found-high" in query:
            rows.append(row("EMD-4001", "10.1000/found-high", microscope="TITAN KRIOS", depositors="Iyer B"))
        if "10.1000/found-medium" in query:
            rows.append(row("EMD-4999", "10.1000/found-medium", depositors="Other X"))
        return rows
    if query.startswith("xref_PUBMED:"):
        return [row("EMD-1001", "10.1000/site-1", depositors="Rao A")] if "123456" in query else []
    if query.startswith("author_orcid:"):
        return [row("EMD-1001", "10.1000/site-1"), row("EMD-2001", paper="")]
    if query.startswith('dep_author_name:"Dutta S"'):
        # The API filter is India-only, but the code re-checks the country.
        return [row("EMD-2001"), row("EMD-2002", microscope="TECNAI"), row("EMD-9001", country="United States")]
    if query.startswith('dep_author_name:"Prasad VM"'):
        return [row("EMD-3001", depositors="Prasad VM"), row("EMD-3002", microscope="TITAN KRIOS", depositors="Prasad VM")]
    if query.startswith('dep_author_name:"Hussain T"'):
        return [row("EMD-1002", "10.1000/site-1", depositors="Hussain T")]
    if query.startswith("emdb_id:"):
        return [row("EMD-5001", depositors="Somebody Z")] if "EMD-5001" in query else []
    if query.startswith("microscope_name:"):
        return [
            row("EMD-1001"), row("EMD-3001", depositors="Prasad VM"),
            row("EMD-5001", depositors="Somebody Z"), row("EMD-5002", depositors="Other Q"),
            row("EMD-6001", depositors="Francis J, Pathri AK, Chowdhury S"),
            row("EMD-6002", depositors="Chowdhury S"),
        ]
    raise AssertionError(f"unexpected query {query}")


class ComputeTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("urllib.request.urlopen", side_effect=AssertionError("network used in test"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def compute(self, **kwargs):
        with patch.object(es, "search", side_effect=fake_search) as search:
            result = es.compute_structures(SITE, FINDER, **kwargs)
        return result, [call.args[0] for call in search.call_args_list]

    def test_union_dedup_sources_and_exclusions(self):
        result, queries = self.compute()
        entries = {e["emdb_id"]: e for e in result["entries"]}
        self.assertEqual(set(entries), {"EMD-1001", "EMD-1002", "EMD-2001", "EMD-2002", "EMD-3001", "EMD-4001"})
        self.assertEqual(entries["EMD-1001"]["sources"], ["website_paper", "orcid"])
        self.assertEqual(entries["EMD-1002"]["sources"], ["website_paper", "iisc_group"])
        self.assertEqual(entries["EMD-1002"]["groups"], ["Tanweer Hussain"])
        self.assertEqual(entries["EMD-2001"]["sources"], ["orcid", "dutta_india"])
        self.assertEqual(entries["EMD-2002"]["sources"], ["dutta_india"])  # any microscope for Dutta
        self.assertEqual(entries["EMD-4001"]["sources"], ["acknowledgement"])
        self.assertEqual(entries["EMD-1001"]["url"], "https://www.ebi.ac.uk/emdb/EMD-1001")
        # Non-India "Dutta S", Prasad on another microscope, medium-confidence finder paper.
        for excluded in ("EMD-9001", "EMD-3002", "EMD-4999"):
            self.assertNotIn(excluded, entries)
        self.assertFalse(any("found-medium" in q for q in queries))
        self.assertTrue(any(q.startswith("xref_PUBMED:") and "123456" in q for q in queries))
        # Needs review: Talos + India, not counted, CCMB group hidden.
        self.assertEqual([e["emdb_id"] for e in result["needs_review"]], ["EMD-5002", "EMD-5001"])
        self.assertEqual(result["excluded"], {"Chowdhury S (CSIR-CCMB, Hyderabad)": 2})

    def test_dois_are_batched_into_one_query(self):
        _, queries = self.compute()
        site_queries = [q for q in queries if q.startswith("xref_DOI:") and "site-1" in q]
        self.assertEqual(len(site_queries), 1)
        self.assertIn('"10.1000/site-2"', site_queries[0])

    def test_approved_and_dismissed_ids(self):
        result, _ = self.compute(approved_ids=["EMD-5001"], dismissed_ids=["EMD-5002"])
        entries = {e["emdb_id"]: e for e in result["entries"]}
        self.assertEqual(entries["EMD-5001"]["sources"], ["approved"])
        self.assertEqual(result["needs_review"], [])

    def test_failure_raises(self):
        with patch.object(es, "search", side_effect=OSError("down")):
            with self.assertRaises(es.EmdbError):
                es.compute_structures(SITE, None)

    def test_new_since_last_check(self):
        result, _ = self.compute()
        first = es.build_payload(result, previous=None, checked_at="t1")
        self.assertEqual(first["count"], 6)
        self.assertEqual(first["new_ids"], [])  # nothing is "new" on the first check
        previous = dict(first, ids=[i for i in first["ids"] if i != "EMD-4001"])
        second = es.build_payload(result, previous=previous, checked_at="t2")
        self.assertEqual(second["new_ids"], ["EMD-4001"])
        self.assertEqual(second["previous_ids"], previous["ids"])

    def test_approve_and_dismiss_payload_helpers(self):
        result, _ = self.compute()
        payload = es.build_payload(result)
        approved, ok = es.approve_entry(payload, "EMD-5001")
        self.assertTrue(ok)
        self.assertEqual(approved["count"], payload["count"] + 1)
        self.assertIn("EMD-5001", approved["ids"])
        self.assertEqual([e["emdb_id"] for e in approved["needs_review"]], ["EMD-5002"])
        dismissed, ok = es.dismiss_entry(approved, "EMD-5002")
        self.assertTrue(ok)
        self.assertEqual(dismissed["needs_review"], [])
        self.assertEqual(dismissed["count"], approved["count"])
        _, ok = es.approve_entry(dismissed, "EMD-1234")
        self.assertFalse(ok)

    def test_group_by_paper_and_csv(self):
        result, _ = self.compute()
        payload = es.build_payload(result)
        groups = es.group_by_paper(payload["entries"])
        site_group = next(g for g in groups if g["paper"]["doi"] == "10.1000/site-1")
        self.assertEqual({e["emdb_id"] for e in site_group["entries"]}, {"EMD-1001", "EMD-1002"})
        rows = list(csv.DictReader(io.StringIO(es.payload_csv(payload))))
        self.assertEqual(sum(r["status"] == "counted" for r in rows), 6)
        self.assertEqual(sum(r["status"] == "needs_review" for r in rows), 2)

    def test_accem_groups_constant(self):
        depositors = {g["depositor"] for g in es.ACCEM_GROUPS}
        self.assertTrue({"Dutta S", "Prasad VM", "Hussain T"} <= depositors)
        self.assertIn("Chowdhury S", {g["depositor"] for g in es.EXCLUDED_GROUPS})


class EmdbAdminAndHomeTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("urllib.request.urlopen", side_effect=AssertionError("network used in test"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "emdb.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.app.app_context():
            conn = database.get_db()
            conn.execute(
                "INSERT INTO managed_publications (citation, doi_url, published_year) VALUES (?, ?, ?)",
                ["Site paper one.", "https://doi.org/10.1000/site-1", 2024],
            )
            conn.commit()
            conn.close()
            public.save_cache_payload(public.PUBLICATION_FINDER_CACHE_KEY, {"candidates": FINDER})

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

    def check_now(self):
        token = self.csrf()
        with patch.object(es, "search", side_effect=fake_search):
            response = self.client.post("/admin/publications/finder/emdb/check", data={"_csrf_token": token})
        return response, token

    def stored(self, key=public.EMDB_STRUCTURES_CACHE_KEY):
        with self.app.app_context():
            return public.load_cache_payload(key)

    def test_endpoints_allowlisted_and_login_required(self):
        for endpoint in ("admin.check_emdb_structures", "admin.review_emdb_entry", "admin.download_emdb_csv"):
            self.assertIn(endpoint, admin_module._ALLOWED_ADMIN_ENDPOINTS)
        self.assertEqual(self.client.get("/admin/publications/finder/emdb.csv").status_code, 302)
        token = self.csrf()
        with patch.object(es, "search", side_effect=AssertionError("must not run")):
            response = self.client.post("/admin/publications/finder/emdb/check", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.stored())

    def test_check_now_stores_and_page_shows_sections(self):
        self.login()
        response, _ = self.check_now()
        self.assertEqual(response.status_code, 302)
        payload = self.stored()
        self.assertEqual(payload["count"], 6)
        self.assertEqual(len(payload["needs_review"]), 2)
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        section = html.split('id="emdb"', 1)[1]
        self.assertIn("Structures (EMDB)", section)
        self.assertIn('href="https://www.ebi.ac.uk/emdb/EMD-1001"', section)
        self.assertIn("Count as ACCEM", section)
        self.assertIn("Dismiss", section)
        self.assertIn("CSIR-CCMB", section)
        self.assertNotIn("New since last check", section)
        # A second check with one more entry highlights it as new.
        def more(query, fields=es.FIELDS):
            rows = fake_search(query, fields)
            if query.startswith("author_orcid:"):
                rows.append(row("EMD-7777", paper="Brand new"))
            return rows
        token = self.csrf()
        with patch.object(es, "search", side_effect=more):
            self.client.post("/admin/publications/finder/emdb/check", data={"_csrf_token": token})
        self.assertEqual(self.stored()["new_ids"], ["EMD-7777"])
        html = self.client.get("/admin/publications/finder").get_data(as_text=True)
        self.assertIn("New since last check", html)
        self.assertIn("emdb-row--new", html)

    def test_check_failure_keeps_previous_count(self):
        self.login()
        self.check_now()
        token = self.csrf()
        with patch.object(es, "search", side_effect=OSError("down")):
            response = self.client.post("/admin/publications/finder/emdb/check", data={"_csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored()["count"], 6)

    def test_approve_and_dismiss_flow(self):
        self.login()
        _, token = self.check_now()
        response = self.client.post("/admin/publications/finder/emdb/review",
                                    data={"_csrf_token": token, "emdb_id": "EMD-5001", "action": "approve"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored()["count"], 7)
        self.client.post("/admin/publications/finder/emdb/review",
                         data={"_csrf_token": token, "emdb_id": "EMD-5002", "action": "dismiss"})
        state = self.stored(public.EMDB_REVIEW_CACHE_KEY)
        self.assertEqual(state, {"approved": ["EMD-5001"], "dismissed": ["EMD-5002"]})
        self.assertEqual(self.stored()["needs_review"], [])
        # Unknown ids / actions are rejected.
        self.client.post("/admin/publications/finder/emdb/review",
                         data={"_csrf_token": token, "emdb_id": "EMD-1001", "action": "approve"})
        self.client.post("/admin/publications/finder/emdb/review",
                         data={"_csrf_token": token, "emdb_id": "EMD-5001", "action": "delete"})
        self.assertEqual(self.stored()["count"], 7)
        # The next refresh keeps the decisions.
        self.check_now()
        payload = self.stored()
        self.assertEqual(payload["count"], 7)
        self.assertIn("EMD-5001", payload["ids"])
        self.assertEqual(payload["needs_review"], [])
        # Home tile follows the stored count.
        with patch("blueprints.public.start_site_stats_refresh"):
            html = self.client.get("/home").get_data(as_text=True)
        self.assertIn('data-stat="structures">7</strong>', html)

    def test_csv_download(self):
        self.login()
        self.check_now()
        response = self.client.get("/admin/publications/finder/emdb.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual({r["status"] for r in rows}, {"counted", "needs_review"})

    def test_home_tile_fallback_and_stored_count(self):
        with patch("blueprints.public.start_site_stats_refresh"):
            html = self.client.get("/home").get_data(as_text=True)
        self.assertIn('data-stat="structures">85+</strong>', html)
        self.assertIn("EMDB entries from ACCEM-linked research", html)
        with self.app.app_context():
            public.save_cache_payload(public.EMDB_STRUCTURES_CACHE_KEY, {"count": 95, "ids": [], "entries": []})
            public.refresh_site_stats()
        with patch("blueprints.public.start_site_stats_refresh"):
            html = self.client.get("/home").get_data(as_text=True)
            body = self.client.get("/api/user-statistics").get_json()
        self.assertIn('data-stat="structures">95</strong>', html)
        self.assertEqual(body["metrics"]["structures"], 95)
        embedded = html.split('id="user-stats-initial">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(embedded)["metrics"]["structures"], 95)

    def test_refresh_script_survives_emdb_failure(self):
        import refresh_site_stats
        from flask import Flask

        with patch.object(es, "search", side_effect=OSError("down")):
            payload = refresh_site_stats.run(Flask("refresh-test"))
        self.assertEqual(payload["metrics"]["structures"], "85+")
        with patch.object(es, "search", side_effect=fake_search):
            payload = refresh_site_stats.run(Flask("refresh-test"))
        self.assertEqual(payload["metrics"]["structures"], 6)

    def test_check_structures_cli(self):
        import check_structures

        out_path = os.path.join(self.temp_dir.name, "structures.csv")
        buffer = io.StringIO()
        with patch.object(es, "search", side_effect=fake_search):
            payload = check_structures.run(out_path, out=buffer)
        self.assertEqual(payload["count"], 6)
        self.assertIn("Structures counted: 6", buffer.getvalue())
        self.assertIn("Needs review (not counted): 2", buffer.getvalue())
        self.assertEqual(self.stored()["count"], 6)
        with open(out_path, encoding="utf-8") as handle:
            self.assertIn("EMD-1001", handle.read())


if __name__ == "__main__":
    unittest.main()
