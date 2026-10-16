import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
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

    def transcribe(self, audio=b"\x00" * 128, content_type="audio/webm"):
        return self.client.post(
            "/admin/assistant/transcribe",
            data={"audio": (io.BytesIO(audio), "question.webm", content_type)},
            headers={"X-CSRF-Token": "assistant-test-token"},
        )

    def speak(self, text="Freezing availability is eight grids today."):
        return self.client.post(
            "/admin/assistant/speak",
            json={"text": text},
            headers={"X-CSRF-Token": "assistant-test-token"},
        )

    def prepare_slot_action(self, command):
        response = self.ask(command)
        self.assertEqual(response.status_code, 200)
        self.assertIn("action_preview", response.get_json())
        return response.get_json()["action_preview"]

    def confirm_slot_action(self, preview, token=None, draft=None):
        return self.client.post(
            "/admin/assistant/slot/confirm",
            json={
                "token": token or preview["token"],
                "draft": draft or preview["draft"],
            },
            headers={"X-CSRF-Token": "assistant-test-token"},
        )

    def test_mira_does_not_open_automatically_on_dashboard_load(self):
        with self.client.session_transaction() as session:
            session["mira_welcome"] = True

        first_dashboard = self.client.get("/admin/panel")
        second_dashboard = self.client.get("/admin/panel")

        self.assertEqual(first_dashboard.status_code, 200)
        self.assertIn(b'data-admin-assistant', first_dashboard.data)
        self.assertNotIn(b'data-auto-open=', first_dashboard.data)
        self.assertEqual(second_dashboard.status_code, 200)
        self.assertNotIn(b'data-auto-open=', second_dashboard.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("mira_welcome", session)

    def test_voice_booking_preview_does_not_mutate_until_confirmed(self):
        next_weekday = datetime.date.today() + datetime.timedelta(days=1)
        while next_weekday.weekday() >= 5:
            next_weekday += datetime.timedelta(days=1)
        command = (
            "Book freezing slot; user Jane Doe; PI Dr Smith; "
            "email jane@example.test; origin Internal; sample Sample A; "
            f"grids 2; date {next_weekday.isoformat()}"
        )

        preview = self.prepare_slot_action(command)
        conn = database.get_db()
        self.assertIsNone(
            conn.execute(
                "SELECT id FROM freezing_bookings WHERE email=?",
                ["jane@example.test"],
            ).fetchone()
        )
        conn.close()
        self.assertIn("Jane Doe", [item["value"] for item in preview["fields"]])

        with patch("blueprints.freezing.send_email"):
            response = self.confirm_slot_action(preview)
        self.assertEqual(response.status_code, 200)
        self.assertIn("now visible in the admin slots page", response.get_json()["answer"])
        conn = database.get_db()
        booking = conn.execute(
            "SELECT user_name, grids, freezing_date, status FROM freezing_bookings WHERE email=?",
            ["jane@example.test"],
        ).fetchone()
        conn.close()
        self.assertEqual(booking["user_name"], "Jane Doe")
        self.assertEqual(booking["grids"], 2)
        self.assertEqual(booking["freezing_date"], next_weekday.isoformat())
        self.assertEqual(booking["status"], "active")

    def test_mira_understands_plain_language_booking_and_spoken_values(self):
        next_weekday = datetime.date.today() + datetime.timedelta(days=1)
        while next_weekday.weekday() >= 5:
            next_weekday += datetime.timedelta(days=1)
        weekday_name = next_weekday.strftime("%A")

        preview = self.prepare_slot_action(
            "Can you book a freezing slot for Jane Doe; PI Dr Smith; "
            "email jane-plain@example.test; origin IISc; sample Sample A; "
            f"two grids on {weekday_name}"
        )

        preview_values = {item["label"]: item["value"] for item in preview["fields"]}
        self.assertEqual(preview_values["User Name"], "Jane Doe")
        self.assertEqual(preview_values["Grids"], "2")
        self.assertEqual(preview_values["Freezing Date"], next_weekday.isoformat())
        conn = database.get_db()
        self.assertIsNone(
            conn.execute(
                "SELECT id FROM freezing_bookings WHERE email=?",
                ["jane-plain@example.test"],
            ).fetchone()
        )
        conn.close()

    def test_mira_understands_plain_language_slot_edits_and_cancellation(self):
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO freezing_bookings
               (user_name, pi_name, email, origin, sample_name, grids, freezing_date, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
            ["Plain User", "Plain PI", "plain@example.test", "Internal", "Plain Sample", 1, "2030-01-02"],
        )
        slot_id = cur.lastrowid
        conn.commit()
        cur.close()
        conn.close()

        preview = self.prepare_slot_action(
            f"Please change grids for freezing slot {slot_id} to three"
        )
        self.assertEqual(preview["draft"]["value"], "3")
        response = self.confirm_slot_action(preview)
        self.assertEqual(response.status_code, 200)
        conn = database.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT grids FROM freezing_bookings WHERE id=?", [slot_id]
            ).fetchone()["grids"],
            3,
        )
        conn.close()

        cancel_preview = self.prepare_slot_action(f"Cancel freezing {slot_id}")
        self.assertEqual(cancel_preview["action"], "delete")

    def test_voice_screening_booking_uses_confirmed_preview(self):
        preview = self.prepare_slot_action(
            "Book screening slot; user Alex Doe; PI Dr Test PI; "
            "email alex@example.test; origin External; institute IISc; "
            "sample Sample B; grids 1"
        )
        with patch("blueprints.screening.send_email"):
            response = self.confirm_slot_action(preview)
        self.assertEqual(response.status_code, 200)
        conn = database.get_db()
        booking = conn.execute(
            "SELECT user_name, pi_name, origin, sample_name, status FROM screening_bookings WHERE email=?",
            ["alex@example.test"],
        ).fetchone()
        conn.close()
        self.assertEqual(booking["user_name"], "Alex Doe")
        self.assertEqual(booking["pi_name"], "Dr Test PI")
        self.assertEqual(booking["origin"], "External")
        self.assertEqual(booking["sample_name"], "Sample B")
        self.assertEqual(booking["status"], "waiting")

    def test_voice_data_collection_booking_uses_confirmed_preview(self):
        preview = self.prepare_slot_action(
            "Book data collection slot; user Sam Doe; PI Dr Rao; "
            "email sam@example.test; origin Industry; institute Cryo Lab; "
            "sample Sample C; grids 2; days 3"
        )
        with patch("blueprints.imaging.send_email"):
            response = self.confirm_slot_action(preview)
        self.assertEqual(response.status_code, 200)
        conn = database.get_db()
        booking = conn.execute(
            "SELECT user_name, origin, esm, grids, days, status FROM bookings WHERE email=?",
            ["sam@example.test"],
        ).fetchone()
        conn.close()
        self.assertEqual(booking["user_name"], "Sam Doe")
        self.assertEqual(booking["origin"], "Industry")
        self.assertEqual(booking["esm"], "Cryo Lab")
        self.assertEqual(booking["grids"], 2)
        self.assertEqual(booking["days"], 3)
        self.assertEqual(booking["status"], "waiting")

    def test_voice_slot_update_and_delete_require_confirmed_unchanged_preview(self):
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO freezing_bookings
               (user_name, pi_name, email, origin, sample_name, grids, freezing_date, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
            ["Update User", "Update PI", "update@example.test", "Internal", "Update Sample", 1, "2030-01-02"],
        )
        slot_id = cur.lastrowid
        conn.commit()
        cur.close()
        conn.close()

        update_preview = self.prepare_slot_action(
            f"Change freezing slot ID {slot_id} grids to 3"
        )
        response = self.confirm_slot_action(update_preview)
        self.assertEqual(response.status_code, 200)
        conn = database.get_db()
        self.assertEqual(
            conn.execute("SELECT grids FROM freezing_bookings WHERE id=?", [slot_id]).fetchone()["grids"],
            3,
        )
        conn.close()

        delete_preview = self.prepare_slot_action(f"Delete freezing slot ID {slot_id}")
        changed_draft = dict(delete_preview["draft"])
        changed_draft["slot_id"] = slot_id + 1
        tampered = self.confirm_slot_action(delete_preview, draft=changed_draft)
        self.assertEqual(tampered.status_code, 409)
        conn = database.get_db()
        self.assertIsNotNone(
            conn.execute("SELECT id FROM freezing_bookings WHERE id=?", [slot_id]).fetchone()
        )
        conn.close()
        deleted = self.confirm_slot_action(delete_preview)
        self.assertEqual(deleted.status_code, 200)
        conn = database.get_db()
        self.assertIsNone(
            conn.execute("SELECT id FROM freezing_bookings WHERE id=?", [slot_id]).fetchone()
        )
        conn.close()

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
        self.assertIn("enter the actual grids and billing details", answer)
        self.assertEqual(
            response.get_json()["navigation"],
            "/admin/freezing#active-freezing-slots",
        )

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
        self.assertEqual(
            response.get_json()["navigation"],
            "/admin/panel#revenue-dashboard",
        )

    def test_unsupported_daily_revenue_is_not_reported_as_a_precise_total(self):
        response = self.ask("How much revenue did we make today?")

        self.assertEqual(response.status_code, 200)
        self.assertIn("don't track every source at daily granularity", response.get_json()["answer"])

    def test_schedule_requests_navigate_to_the_matching_admin_queue(self):
        cases = {
            "Show me freezing schedule": "/admin/freezing",
            "Open the screening schedule": "/admin/screening",
            "Take me to the data collection schedule": "/admin/datacollecting",
            "Show data collecting schedule": "/admin/datacollecting",
        }
        for question, expected_url in cases.items():
            with self.subTest(question=question):
                response = self.ask(question)
                self.assertEqual(response.status_code, 200)
                result = response.get_json()
                self.assertEqual(result["navigation"], expected_url)
                self.assertIn("active, waiting, and completed slots", result["answer"])

    def test_schedule_request_for_multiple_services_asks_which_one(self):
        response = self.ask("Show me the freezing and screening schedule")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["navigation"])
        self.assertIn("Which schedule", response.get_json()["answer"])

    def test_unknown_question_gets_supported_topics(self):
        response = self.ask("What is the weather today?")

        self.assertEqual(response.status_code, 200)
        self.assertIn("revenue and billing", response.get_json()["answer"])
        self.assertIn("freezing availability", response.get_json()["answer"])
        self.assertIsNone(response.get_json()["navigation"])

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

    def test_transcription_requires_configured_provider_key(self):
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": ""}):
            response = self.transcribe()

        self.assertEqual(response.status_code, 503)
        self.assertIn("Deepgram API key", response.get_json()["error"])

    def test_transcription_rejects_unsupported_audio_format_and_oversized_clips(self):
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}):
            unsupported = self.transcribe(content_type="application/octet-stream")
            empty = self.transcribe(audio=b"")
            oversized = self.transcribe(audio=b"\x00" * (8 * 1024 * 1024 + 1))

        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 413)

    def test_transcription_sends_audio_with_zero_retention_opt_out(self):
        provider_response = io.BytesIO(json.dumps({
            "results": {
                "channels": [{"alternatives": [{"transcript": "  Freezing today?  "}]}]
            }
        }).encode("utf-8"))
        audio_data = b"\x01\x02sample-audio"
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}),
            patch(
                "blueprints.admin.urllib.request.urlopen",
                return_value=provider_response,
            ) as urlopen,
        ):
            response = self.transcribe(audio=audio_data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["transcript"], "Freezing today?")
        provider_request = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(provider_request.full_url).query)
        self.assertEqual(provider_request.data, audio_data)
        self.assertEqual(provider_request.get_header("Authorization"), "Token test-key")
        self.assertEqual(query["mip_opt_out"], ["true"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)

    def test_transcription_reports_provider_quota_exhaustion(self):
        provider_error = urllib.error.HTTPError(
            "https://api.deepgram.com/v1/listen",
            429,
            "quota exceeded",
            {},
            io.BytesIO(b""),
        )
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}),
            patch("blueprints.admin.urllib.request.urlopen", side_effect=provider_error),
        ):
            response = self.transcribe()

        self.assertEqual(response.status_code, 503)
        self.assertIn("free quota is exhausted", response.get_json()["error"])

    def test_transcription_requires_admin_session_and_csrf_token(self):
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}):
            with self.client.session_transaction() as session:
                session.pop("admin_logged_in")
            unauthorized = self.transcribe()
            missing_csrf = self.client.post(
                "/admin/assistant/transcribe",
                data={"audio": (io.BytesIO(b"sample"), "question.webm", "audio/webm")},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(missing_csrf.status_code, 400)

    def test_spoken_answer_requires_configured_provider_key(self):
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": ""}),
            patch("blueprints.admin.urllib.request.urlopen") as urlopen,
        ):
            response = self.speak()

        self.assertEqual(response.status_code, 503)
        self.assertIn("Deepgram API key", response.get_json()["error"])
        urlopen.assert_not_called()

    def test_spoken_answer_rejects_empty_and_oversized_text(self):
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": ""}):
            empty = self.speak(" ")
            oversized = self.speak("x" * 5001)

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 400)

    def test_spoken_answer_reports_provider_quota_exhaustion(self):
        provider_error = urllib.error.HTTPError(
            "https://api.deepgram.com/v1/speak",
            429,
            "quota exceeded",
            {},
            io.BytesIO(b""),
        )
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}),
            patch("blueprints.admin.urllib.request.urlopen", side_effect=provider_error),
        ):
            response = self.speak()

        self.assertEqual(response.status_code, 503)
        self.assertIn("Deepgram quota is exhausted", response.get_json()["error"])

    def test_spoken_answer_reports_provider_error_without_echoing_key_or_text(self):
        provider_error = urllib.error.HTTPError(
            "https://api.deepgram.com/v1/speak",
            422,
            "invalid request",
            {},
            io.BytesIO(json.dumps({
                "detail": {
                    "status": "voice_not_available",
                    "message": "Voice cannot speak: private test phrase",
                },
            }).encode("utf-8")),
        )
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": "private-test-key"}),
            patch(
                "blueprints.admin.urllib.request.urlopen",
                side_effect=provider_error,
            ),
        ):
            response = self.speak("private test phrase")
        error = response.get_json()["error"]
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.status_code, 502)
        self.assertIn("could not generate audio", error)
        self.assertNotIn("private test phrase", error)
        self.assertNotIn("private-test-key", error)

    def test_legacy_mira_welcome_flag_does_not_open_dashboard_assistant(self):
        with self.client.session_transaction() as session:
            session["mira_welcome"] = True

        first_response = self.client.get("/admin/panel")
        second_response = self.client.get("/admin/panel")

        self.assertIn(b'data-admin-assistant', first_response.data)
        self.assertNotIn(b'data-auto-open=', first_response.data)
        self.assertNotIn(b'data-auto-open=', second_response.data)

    def test_spoken_answer_uses_deepgram_helena_voice_when_configured(self):
        audio_data = b"RIFF\x04\x00\x00\x00WAVE"
        provider_response = io.BytesIO(audio_data)
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": "deepgram-test-key"}),
            patch(
                "blueprints.admin.urllib.request.urlopen",
                return_value=provider_response,
            ) as urlopen,
        ):
            response = self.speak("  Freezing availability is eight grids today.  ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        self.assertEqual(response.data, audio_data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        provider_request = urlopen.call_args.args[0]
        self.assertEqual(
            urllib.parse.urlsplit(provider_request.full_url).path,
            "/v1/speak",
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(provider_request.full_url).query)
        self.assertEqual(query["model"], ["aura-2-helena-en"])
        self.assertEqual(query["encoding"], ["linear16"])
        self.assertEqual(query["container"], ["wav"])
        self.assertEqual(provider_request.get_header("Authorization"), "Token deepgram-test-key")
        self.assertEqual(provider_request.get_header("Accept"), "audio/wav")
        self.assertEqual(
            json.loads(provider_request.data),
            {"text": "Freezing availability is eight grids today."},
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)

    def test_spoken_answer_rejects_non_wav_provider_response(self):
        with (
            patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}),
            patch(
                "blueprints.admin.urllib.request.urlopen",
                return_value=io.BytesIO(b"provider error text"),
            ),
        ):
            response = self.speak()

        self.assertEqual(response.status_code, 502)
        self.assertIn("browser cannot play", response.get_json()["error"])

    def test_spoken_answer_requires_admin_session_and_csrf_token(self):
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-key"}):
            with self.client.session_transaction() as session:
                session.pop("admin_logged_in")
            unauthorized = self.speak()
            missing_csrf = self.client.post(
                "/admin/assistant/speak",
                json={"text": "Freezing availability is eight grids today."},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(missing_csrf.status_code, 400)


if __name__ == "__main__":
    unittest.main()
