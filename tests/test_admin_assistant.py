import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app


class AdminAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "assistant.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "assistant-test-token"
            session["admin_logged_in"] = True
            conn = database.get_db()
            session["admin_user_id"] = conn.execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    def ask(self, question):
        return self.client.post(
            "/admin/assistant/query",
            json={"question": question},
            headers={"X-CSRF-Token": "assistant-test-token"},
        )

    def test_freezing_answer_reads_active_and_completed_log_entries(self):
        today = datetime.date.today().isoformat()
        conn = database.get_db()
        conn.execute(
            """INSERT INTO freezing_bookings
               (user_name, pi_name, sample_name, grids, freezing_date, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            ["Researcher One", "PI One", "Sample One", 3, today],
        )
        conn.execute(
            """INSERT INTO freezing_bookings
               (user_name, pi_name, sample_name, grids, freezing_date, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            ["Researcher Two", "PI Two", "Sample Two", 2, today],
        )
        conn.execute(
            """INSERT INTO completed_freezing
               (user_name, sample_name, grids, actual_grids, freezing_date)
               VALUES (?, ?, ?, ?, ?)""",
            ["Completed Researcher", "Completed Sample", 4, 3, today],
        )
        conn.commit()
        conn.close()

        response = self.ask("What freezing is available today?")

        self.assertEqual(response.status_code, 200)
        self.assertIn("microphone=(self)", response.headers["Permissions-Policy"])
        answer = response.get_json()["answer"]
        self.assertIn("3 of 8 daily grid places remain", answer)
        self.assertIn("2 active booking(s) reserve 5 grid(s)", answer)
        self.assertIn("Researcher One, Sample One, 3 grid(s)", answer)
        self.assertIn("Completed log: 1 booking(s), 3 actual grid(s)", answer)

    def test_revenue_answer_uses_dashboard_calculation_for_period(self):
        current_year = datetime.date.today().year
        conn = database.get_db()
        conn.execute(
            "INSERT INTO csic_projects (company_name, year, net_amount) VALUES (?, ?, ?)",
            ["Example Company", current_year, 1234.50],
        )
        conn.commit()
        conn.close()

        response = self.ask("Give me a revenue summary this year")

        self.assertEqual(response.status_code, 200)
        answer = response.get_json()["answer"]
        self.assertIn("Revenue summary for this year to date", answer)
        self.assertIn("CSIC project record(s)", answer)
        self.assertIn("₹1,234.50", answer)

    def test_unsupported_daily_revenue_is_not_reported_as_a_precise_total(self):
        response = self.ask("How much revenue did we make today?")

        self.assertEqual(response.status_code, 200)
        self.assertIn("don't track every source at daily granularity", response.get_json()["answer"])

    def test_unknown_question_gets_supported_topics(self):
        response = self.ask("What is the weather today?")

        self.assertEqual(response.status_code, 200)
        self.assertIn("revenue and billing", response.get_json()["answer"])
        self.assertIn("freezing availability", response.get_json()["answer"])

    def test_invalid_and_oversized_questions_are_rejected(self):
        empty = self.ask("  ")
        oversized = self.ask("x" * 501)

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 400)

    def test_endpoint_requires_admin_session_and_csrf_token(self):
        with self.client.session_transaction() as session:
            session.pop("admin_logged_in")
        unauthorized = self.ask("Summarize revenue")
        missing_csrf = self.client.post(
            "/admin/assistant/query",
            json={"question": "Summarize revenue"},
        )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(missing_csrf.status_code, 400)


if __name__ == "__main__":
    unittest.main()
