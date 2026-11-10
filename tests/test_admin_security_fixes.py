import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile
from decimal import Decimal
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database
from blueprints import admin as admin_module
from main import create_app


class AdminSecurityFixesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "security-fixes.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        admin_module._login_failures.clear()
        self.addCleanup(admin_module._login_failures.clear)

        self.app = create_app()
        self.app.testing = True
        self.app.config["PAYMENT_PROOF_DIR"] = os.path.join(self.temp_dir.name, "proofs")
        self.client = self.app.test_client()
        conn = database.get_db()
        self.admin_id = conn.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        conn.close()
        self._login_session()

    def _login_session(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_user_id"] = self.admin_id

    def _csrf_token(self):
        response = self.client.get("/admin/history")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def _insert(self, sql, params):
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(sql, params)
        row_id = cur.lastrowid
        conn.commit()
        cur.close()
        conn.close()
        return row_id

    def _query(self, sql, params=()):
        conn = database.get_db()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    def _add_booking(self, status="waiting", origin="External", **extra):
        columns = {
            "user_name": "User", "pi_name": "Some PI", "email": "user@example.com",
            "origin": origin, "esm": "", "sample_name": "Sample", "grids": 2, "days": 1,
            "status": status,
        }
        columns.update(extra)
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        return self._insert(f"INSERT INTO bookings ({names}) VALUES ({marks})", list(columns.values()))

    # ── Fix 3: load routes are POST-only and only load waiting bookings ─────

    def test_load_dc_rejects_get_and_only_loads_waiting_bookings(self):
        waiting_id = self._add_booking("waiting")
        completed_id = self._add_booking("completed", completion_date="2026-09-01")
        token = self._csrf_token()

        get_response = self.client.get(f"/admin/datacollecting/load/{waiting_id}")
        self.assertEqual(get_response.status_code, 405)

        with patch("blueprints.admin.send_email") as send:
            self.client.post(
                f"/admin/datacollecting/load/{completed_id}", data={"_csrf_token": token}
            )
            send.assert_not_called()
        self.assertEqual(
            self._query("SELECT status FROM bookings WHERE id=?", [completed_id])[0]["status"],
            "completed",
        )

        with patch("blueprints.admin.send_email") as send:
            response = self.client.post(
                f"/admin/datacollecting/load/{waiting_id}", data={"_csrf_token": token}
            )
            send.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self._query("SELECT status FROM bookings WHERE id=?", [waiting_id])[0]["status"],
            "ongoing",
        )
        self.assertTrue(self._session_is_admin())

    def test_load_sc_rejects_get(self):
        response = self.client.get("/admin/screening/load/1")
        self.assertEqual(response.status_code, 405)

    def test_waiting_pages_render_load_as_post_forms(self):
        self._add_booking("waiting")
        page = self.client.get("/admin/datacollecting").get_data(as_text=True)
        self.assertRegex(page, r'<form method="post" action="/admin/datacollecting/load/\d+">')
        self.assertNotIn('href="/admin/datacollecting/load/', page)

    # ── Fix 4: every admin/inventory endpoint is allow-listed ──────────────

    def _session_is_admin(self):
        with self.client.session_transaction() as session:
            return bool(session.get("admin_logged_in"))

    def test_grid_booking_link_routes_keep_admin_signed_in(self):
        token = self._csrf_token()
        for path in (
            "/admin/inventory/grids/ACCEM-G-0001/booking-link",
            "/admin/inventory/grids/ACCEM-G-0001/booking-link/remove",
        ):
            self.client.post(path, data={"_csrf_token": token, "service_type": "screening", "booking_id": "999"})
            self.assertTrue(self._session_is_admin(), path)

    def test_all_admin_and_inventory_endpoints_are_allow_listed(self):
        endpoints = {
            rule.endpoint for rule in self.app.url_map.iter_rules()
            if rule.endpoint.startswith(("admin.", "inventory."))
        }
        missing = endpoints - admin_module._ALLOWED_ADMIN_ENDPOINTS - {"admin.login"}
        self.assertEqual(missing, set())

    # ── Fix 6: non-finite slot counts are rejected cleanly ─────────────────

    def test_completion_rejects_infinite_and_nan_slots(self):
        dc_id = self._add_booking("ongoing")
        sc_id = self._insert(
            """INSERT INTO screening_bookings
               (user_name, pi_name, email, origin, esm, sample_name, grids, days, status)
               VALUES ('U', 'P', 'u@example.com', 'External', '', 'S', 2, 1, 'ongoing')""",
            [],
        )
        token = self._csrf_token()
        for value in ("Infinity", "NaN", "-Infinity", "sNaN"):
            for path, table, booking_id in (
                (f"/admin/datacollecting/complete/{dc_id}", "bookings", dc_id),
                (f"/admin/screening/complete/{sc_id}", "screening_bookings", sc_id),
            ):
                response = self.client.post(path, data={
                    "_csrf_token": token,
                    "actual_slots": value,
                    "actual_grids": "2",
                    "grid_source": "self_owned",
                    "clipped_grids": "0",
                })
                self.assertEqual(response.status_code, 302, (value, path))
                status = self._query(f"SELECT status FROM {table} WHERE id=?", [booking_id])[0]["status"]
                self.assertEqual(status, "ongoing")

    # ── Fix 7: combined charge sheet validates input shape ──────────────────

    def test_combined_charge_sheet_rejects_malformed_and_mixed_selections(self):
        internal_id = self._add_booking(
            "completed", origin="Internal", completion_date="2026-09-01",
            actual_slots=1, actual_grids=1, number_of_grids=1, grid_source="self_owned",
        )
        external_id = self._add_booking(
            "completed", origin="External", completion_date="2026-09-01",
            actual_slots=1, actual_grids=1, number_of_grids=1, grid_source="self_owned",
        )
        token = self._csrf_token()
        for payload in ("[1]", '["x"]', '[{"service_key": "imaging", "booking_id": "1; DROP"}]', "{}"):
            response = self.client.post(
                "/admin/charge-sheet/combined",
                data={"_csrf_token": token, "selected_slots": payload},
            )
            self.assertEqual(response.status_code, 302, payload)

        with patch("blueprints.admin.send_email") as send:
            response = self.client.post(
                "/admin/charge-sheet/combined",
                data={"_csrf_token": token, "selected_slots": json.dumps([
                    {"service_key": "imaging", "booking_id": internal_id},
                    {"service_key": "imaging", "booking_id": external_id},
                ])},
                follow_redirects=True,
            )
            send.assert_not_called()
        self.assertIn(b"Internal and external bookings cannot be combined", response.data)

    # ── Fix 5: resend keeps the stored totals and charge sheet ID ──────────

    def test_resend_reuses_stored_totals_and_ignores_form_clip_base_source(self):
        booking_id = self._add_booking(
            "completed", completion_date="2026-09-01", actual_slots=1, actual_grids=2,
            number_of_grids=2, grid_source="self_owned", clip_base_source="facility",
            subtotal="1000.00", gst_amount="180.00", grand_total="1180.00",
            total_billed="1180.00", charge_sheet_id="2026_ACDM_0007",
            charge_sheet_sent_at="2026-09-02 10:00:00", payment_status="Payment Verified",
        )
        token = self._csrf_token()
        with (
            patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF") as make_pdf,
            patch("blueprints.admin.send_email") as send,
        ):
            self.client.post(
                f"/admin/charge-sheet/imaging/{booking_id}",
                data={"_csrf_token": token, "grid_source": "self_owned", "clip_base_source": "self_owned"},
            )
            send.assert_called_once()
        sent_row = make_pdf.call_args.args[0]
        self.assertEqual(sent_row["charge_sheet_id"], "2026_ACDM_0007")
        self.assertEqual(Decimal(str(sent_row["total_billed"])), Decimal("1180.00"))
        stored = self._query(
            "SELECT total_billed, charge_sheet_id, clip_base_source, charge_sheet_sent_at FROM bookings WHERE id=?",
            [booking_id],
        )[0]
        self.assertEqual(Decimal(str(stored["total_billed"])), Decimal("1180.00"))
        self.assertEqual(stored["charge_sheet_id"], "2026_ACDM_0007")
        self.assertEqual(stored["clip_base_source"], "facility")
        self.assertEqual(str(stored["charge_sheet_sent_at"]), "2026-09-02 10:00:00")

    def test_first_send_uses_stored_clip_base_source_not_form(self):
        booking_id = self._add_booking(
            "completed", completion_date="2026-09-01", actual_slots=1, actual_grids=2,
            number_of_grids=2, grid_source="self_owned", clip_base_source="facility",
        )
        token = self._csrf_token()
        with (
            patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF"),
            patch("blueprints.admin.send_email"),
        ):
            self.client.post(
                f"/admin/charge-sheet/imaging/{booking_id}",
                data={"_csrf_token": token, "clip_base_source": "self_owned"},
            )
        stored = self._query(
            "SELECT clip_base_source, clip_base_charge, charge_sheet_sent_at FROM bookings WHERE id=?",
            [booking_id],
        )[0]
        self.assertEqual(stored["clip_base_source"], "facility")
        self.assertEqual(Decimal(str(stored["clip_base_charge"])), Decimal("5000.00"))
        self.assertIsNotNone(stored["charge_sheet_sent_at"])

    # ── Fix 2: archive sends the backup synchronously and keeps unpaid rows ─

    def _enable_email(self):
        self.app.config["GOOGLE_APPS_SCRIPT_URL"] = "https://example.invalid/relay"
        self.app.config["GOOGLE_APPS_SCRIPT_TOKEN"] = "token"

    def test_archive_deletes_nothing_when_backup_email_fails(self):
        self._enable_email()
        paid_id = self._add_booking(
            "completed", completion_date="2026-09-01",
            charge_sheet_sent_at="2026-09-02 10:00:00", payment_status="Payment Verified",
        )
        token = self._csrf_token()
        with (
            patch("blueprints.admin.send_email_sync", return_value=False) as send_sync,
            patch("blueprints.admin.send_email") as send_async,
        ):
            response = self.client.post(
                "/admin/archive-completed", data={"_csrf_token": token}, follow_redirects=True
            )
            send_sync.assert_called_once()
            send_async.assert_not_called()
        self.assertIn(b"no registrations were removed", response.data)
        self.assertEqual(len(self._query("SELECT id FROM bookings WHERE id=?", [paid_id])), 1)

    def test_archive_keeps_unpaid_records_and_strips_password_hashes(self):
        self._enable_email()
        paid_id = self._add_booking(
            "completed", completion_date="2026-09-01",
            charge_sheet_sent_at="2026-09-02 10:00:00", payment_status="Payment Verified",
        )
        unsent_id = self._add_booking("completed", completion_date="2026-09-01")
        unpaid_id = self._add_booking(
            "completed", completion_date="2026-09-01",
            charge_sheet_sent_at="2026-09-02 10:00:00", payment_status="Payment Pending",
        )
        waiting_id = self._add_booking("waiting")
        token = self._csrf_token()
        with patch("blueprints.admin.send_email_sync", return_value=True) as send_sync:
            response = self.client.post(
                "/admin/archive-completed", data={"_csrf_token": token}, follow_redirects=True
            )
        self.assertIn(b"removed 1 completed registrations", response.data)
        self.assertIn(b"2 completed registration(s) were kept", response.data)
        remaining = {row["id"] for row in self._query("SELECT id FROM bookings")}
        self.assertNotIn(paid_id, remaining)
        self.assertTrue({unsent_id, unpaid_id, waiting_id} <= remaining)

        attachments = send_sync.call_args.kwargs["attachments"]
        backup = zipfile.ZipFile(io.BytesIO(attachments[0][2]))
        snapshot_path = os.path.join(self.temp_dir.name, "snapshot.sqlite3")
        with open(snapshot_path, "wb") as handle:
            handle.write(backup.read("cryo-database.sqlite3"))
        snapshot = sqlite3.connect(snapshot_path)
        hashes = {row[0] for row in snapshot.execute("SELECT password_hash FROM users")}
        snapshot.close()
        self.assertEqual(hashes, {""})
        # The live database keeps its credentials.
        live = self._query("SELECT password_hash FROM users WHERE id=?", [self.admin_id])[0]
        self.assertTrue(live["password_hash"])

    def test_csv_backup_rows_exclude_password_hash(self):
        with self.app.test_request_context():
            rows = admin_module._registration_rows()
            csv_text = admin_module._rows_csv(rows)
        self.assertTrue(any(row["source_table"] == "users" for row in rows))
        self.assertTrue(all("password_hash" not in row for row in rows))
        self.assertNotIn("password_hash", csv_text)

    # ── Fix 10: login throttling ───────────────────────────────────────────

    def test_login_locks_after_repeated_failures(self):
        conn = database.get_db()
        conn.execute(
            "INSERT INTO users (username, email, password_hash, role, must_change_password) "
            "VALUES (?, ?, ?, 'admin', 0)",
            ["throttle-admin", "throttle@example.com",
             generate_password_hash("correct horse battery", method="pbkdf2:sha256")],
        )
        conn.commit()
        conn.close()
        client = self.app.test_client()
        page = client.get("/admin/")
        token = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', page.data).group(1).decode()
        for _ in range(5):
            response = client.post("/admin/", data={
                "_csrf_token": token, "username": "throttle-admin", "password": "wrong",
            })
            self.assertEqual(response.status_code, 200)
        locked = client.post("/admin/", data={
            "_csrf_token": token, "username": "throttle-admin", "password": "correct horse battery",
        })
        self.assertEqual(locked.status_code, 429)
        self.assertIn(b"Too many failed sign-in attempts", locked.data)

        admin_module._login_failures.clear()
        allowed = client.post("/admin/", data={
            "_csrf_token": token, "username": "throttle-admin", "password": "correct horse battery",
        })
        self.assertEqual(allowed.status_code, 302)

    # ── Fix 11: sessions must carry a valid admin user id ──────────────────

    def test_session_without_admin_user_id_is_rejected(self):
        with self.client.session_transaction() as session:
            session.pop("admin_user_id")
        response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/"))
        with self.client.session_transaction() as session:
            self.assertNotIn("admin_logged_in", session)

    def test_stale_admin_session_does_not_block_public_pages(self):
        with self.client.session_transaction() as session:
            session["admin_user_id"] = -1
        response = self.client.get("/about")
        self.assertEqual(response.status_code, 200)

    # ── Fix 17: payment proof deletion stays inside PAYMENT_PROOF_DIR ──────

    def test_deleting_booking_does_not_remove_files_outside_proof_dir(self):
        outside = os.path.join(self.temp_dir.name, "outside.txt")
        with open(outside, "w") as handle:
            handle.write("keep me")
        booking_id = self._add_booking(
            "completed", completion_date="2026-09-01", payment_proof_path=outside,
        )
        token = self._csrf_token()
        self.client.post(f"/admin/history/imaging/{booking_id}/delete", data={"_csrf_token": token})
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(self._query("SELECT id FROM bookings WHERE id=?", [booking_id]), [])


class SecretKeyConfigTests(unittest.TestCase):
    def test_production_requires_secret_key(self):
        env = {"DATABASE_URL": "mysql://user:pass@db.example/cryo", "SECRET_KEY": ""}
        with patch.dict(os.environ, env):
            with self.assertRaises(RuntimeError):
                create_app()
        with patch.dict(os.environ, {"SECURE_COOKIES": "true", "SECRET_KEY": ""}):
            with self.assertRaises(RuntimeError):
                create_app()

    def test_development_generates_random_secret_key(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = os.path.join(temp_dir.name, "dev.sqlite3")
        env = {"SECRET_KEY": "", "SECURE_COOKIES": "", "DATABASE_URL": ""}
        with patch.dict(os.environ, env), patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}"):
            first = create_app()
            second = create_app()
        self.assertNotEqual(first.secret_key, "change-me-in-production")
        self.assertGreaterEqual(len(first.secret_key), 32)
        self.assertNotEqual(first.secret_key, second.secret_key)


class EmailRelayErrorTests(unittest.TestCase):
    def test_network_errors_return_false_instead_of_raising(self):
        import http.client

        from flask import Flask

        import extensions

        app = Flask("relay-test")
        app.config.update(GOOGLE_APPS_SCRIPT_URL="https://example.invalid", GOOGLE_APPS_SCRIPT_TOKEN="t")
        for error in (TimeoutError("slow"), ConnectionResetError("reset"),
                      http.client.RemoteDisconnected("gone")):
            with patch("urllib.request.urlopen", side_effect=error), patch.object(app.logger, "error"):
                self.assertFalse(extensions._deliver_email(app, "a@example.com", "s", "b", None, None, None))


class AmountWordsTests(unittest.TestCase):
    def test_amount_words_include_paise(self):
        from charge_sheet import _amount_words

        self.assertEqual(_amount_words("41890.00"), "Rupees Forty One Thousand Eight Hundred Ninety Only")
        self.assertEqual(
            _amount_words("1180.50"),
            "Rupees One Thousand One Hundred Eighty and Fifty Paise Only",
        )


if __name__ == "__main__":
    unittest.main()
