import os
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from flask import Flask

import freezing_reminders
from extensions import _deliver_email


class FreezingReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "freezing-reminders.sqlite3")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE freezing_bookings (
                   id INTEGER PRIMARY KEY,
                   user_name TEXT,
                   pi_name TEXT,
                   email TEXT,
                   origin TEXT,
                   sample_name TEXT,
                   grids INTEGER,
                   freezing_date DATE,
                   status TEXT
               )"""
        )
        conn.executemany(
            """INSERT INTO freezing_bookings
               (id, user_name, pi_name, email, origin, sample_name, grids,
                freezing_date, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (1, "Tomorrow User", "Tomorrow PI", "user1@example.test",
                 "academic", "Tomorrow sample", 3, "2026-10-02", "active"),
                (2, "Today User", "Today PI", "user2@example.test",
                 "internal", "Today sample", 1, "2026-10-01", "active"),
                (3, "Completed User", "Completed PI", "user3@example.test",
                 "academic", "Completed sample", 2, "2026-10-02", "completed"),
                (4, "Other User", "Other PI", "other@example.test",
                 "academic", "Other sample", 4, "2026-10-02", "active"),
            ],
        )
        conn.commit()
        conn.close()
        self.get_db_patch = patch(
            "freezing_reminders.get_db",
            side_effect=self._connect,
        )
        self.get_db_patch.start()
        self.addCleanup(self.get_db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.app = Flask(__name__)
        self.app.config["FREEZING_REMINDER_EMAIL"] = "cryoem.iisc@gmail.com"
        self.app.config["TESTING"] = True
        conn = self._connect()
        conn.execute(
            "UPDATE freezing_bookings SET email=? WHERE id IN (1, 2)",
            ("CRYOEM.IISC@GMAIL.COM",),
        )
        conn.commit()
        conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_day_before_reminder_includes_every_users_active_slot(self):
        with patch("freezing_reminders.send_email_sync", return_value=True) as send:
            sent_count = freezing_reminders.send_freezing_reminders(
                self.app, "day_before", date(2026, 10, 1)
            )

        self.assertEqual(sent_count, 2)
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(args[0], "cryoem.iisc@gmail.com")
        self.assertIn("[HIGH PRIORITY]", args[1])
        self.assertIn("Tomorrow User", args[2])
        self.assertIn("Tomorrow sample", args[2])
        self.assertIn("Other User", args[2])
        self.assertIn("Other sample", args[2])
        self.assertNotIn("Completed User", args[2])
        self.assertEqual(kwargs["priority"], "high")

    def test_reminder_is_not_resent_after_successful_delivery(self):
        with patch("freezing_reminders.send_email_sync", return_value=True) as send:
            first_count = freezing_reminders.send_freezing_reminders(
                self.app, "slot_day", date(2026, 10, 1)
            )
            second_count = freezing_reminders.send_freezing_reminders(
                self.app, "slot_day", date(2026, 10, 1)
            )

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        send.assert_called_once()

    def test_failed_delivery_is_reported_and_can_be_retried(self):
        with patch("freezing_reminders.send_email_sync", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "not accepted"):
                freezing_reminders.send_freezing_reminders(
                    self.app, "slot_day", date(2026, 10, 1)
                )

        with patch("freezing_reminders.send_email_sync", return_value=True) as send:
            sent_count = freezing_reminders.send_freezing_reminders(
                self.app, "slot_day", date(2026, 10, 1)
            )

        self.assertEqual(sent_count, 1)
        send.assert_called_once()

    def test_high_priority_metadata_is_sent_to_mail_relay(self):
        self.app.config["GOOGLE_APPS_SCRIPT_URL"] = "https://relay.example.test"
        self.app.config["GOOGLE_APPS_SCRIPT_TOKEN"] = "test-token"
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"accepted"

        with patch("extensions.urllib.request.urlopen", return_value=response) as urlopen:
            sent = _deliver_email(
                self.app,
                "cryoem.iisc@gmail.com",
                "[HIGH PRIORITY] Freezing slot reminder",
                "Reminder details",
                None,
                None,
                "high",
            )

        self.assertTrue(sent)
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["priority"], "high")
        self.assertEqual(payload["importance"], "high")
        self.assertEqual(payload["headers"]["X-Priority"], "1")


if __name__ == "__main__":
    unittest.main()
