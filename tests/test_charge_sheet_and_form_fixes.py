import datetime
import json
import os
import re
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from reportlab.platypus import Paragraph, Table

import charge_sheet
import database
from blueprints import admin as admin_module
from inventory_service import facility_today
from main import create_app
from revenue import calculate_charge_sheet


def _next_weekday(days_ahead):
    day = facility_today() + datetime.timedelta(days=days_ahead)
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    return day


def _flowable_text(flowable):
    """Plain text of a reportlab flowable (paragraphs and nested tables)."""
    if isinstance(flowable, Paragraph):
        return flowable.getPlainText()
    if isinstance(flowable, Table):
        return "\n".join(
            " | ".join(_flowable_text(cell) for cell in row) for row in flowable._cellvalues
        )
    if isinstance(flowable, (list, tuple)):
        return " ".join(_flowable_text(item) for item in flowable)
    if isinstance(flowable, str):
        return flowable
    return ""


def _sheet_story(row, service):
    """Run generate_charge_sheet and return the built story's flowables."""
    captured = []

    def fake_build(doc, story, **kwargs):
        captured.extend(story)

    with patch.object(charge_sheet.SimpleDocTemplate, "build", fake_build):
        charge_sheet.generate_charge_sheet(row, service)
    return captured


def _billing_table(story):
    for flowable in story:
        if isinstance(flowable, Table) and "S.No" in _flowable_text(flowable).split("\n")[0]:
            return flowable
    raise AssertionError("billing table not found")


def _sheet_text(row, service):
    return "\n".join(_flowable_text(item) for item in _sheet_story(row, service))


class _AppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "charge-sheet-form-fixes.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        admin_module._login_failures.clear()
        self.addCleanup(admin_module._login_failures.clear)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        conn = database.get_db()
        self.admin_id = conn.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        conn.close()

    def _login(self, client=None):
        with (client or self.client).session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_user_id"] = self.admin_id
            session["must_change_password"] = False

    def _admin_csrf(self):
        response = self.client.get("/admin/history")
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def _insert(self, table, **columns):
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(columns.values()))
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

    def _one(self, sql, params=()):
        rows = self._query(sql, params)
        return rows[0] if rows else None


# ── 1-3: public freezing registration ──────────────────────────────────────

class FreezingRegistrationTests(_AppTestCase):
    def setUp(self):
        super().setUp()
        self.guest = self.app.test_client()

    def _post_freezing(self, **overrides):
        page = self.guest.get("/register")
        token = re.search(rb'name="_csrf_token"\s+value="([^"]+)"', page.data).group(1)
        data = {
            "_csrf_token": token.decode(),
            "reg_type": "freezing",
            "user_name": "Freeze Person",
            "pi_name": "Dr PI",
            "email": "freeze.person@example.test",
            "origin": "External",
            "esm": "Example Institute of Science",
            "sample_name": "Spike protein",
            "grids_freezing": "2",
            "freezing_date": _next_weekday(14).isoformat(),
        }
        data.update(overrides)
        with patch("blueprints.freezing.send_email") as send:
            response = self.guest.post("/register", data=data, follow_redirects=True)
        return response, send

    def _count(self):
        return self._one("SELECT COUNT(*) AS c FROM freezing_bookings")["c"]

    def test_identical_double_submission_is_rejected(self):
        first, first_send = self._post_freezing()
        self.assertEqual(first.status_code, 200)
        first_send.assert_called_once()
        self.assertEqual(self._count(), 1)

        second, second_send = self._post_freezing()
        self.assertIn(b"already have an active freezing booking", second.data)
        second_send.assert_not_called()
        self.assertEqual(self._count(), 1)

        # Email comparison is case-insensitive.
        third, _ = self._post_freezing(email="FREEZE.Person@Example.test")
        self.assertIn(b"already have an active freezing booking", third.data)
        self.assertEqual(self._count(), 1)

    def test_different_second_booking_for_same_person_is_allowed(self):
        self._post_freezing()
        self._post_freezing(sample_name="Different protein")
        self._post_freezing(freezing_date=_next_weekday(21).isoformat())
        self.assertEqual(self._count(), 3)

    def test_duplicate_allowed_once_first_booking_is_no_longer_active(self):
        self._post_freezing()
        conn = database.get_db()
        conn.execute("UPDATE freezing_bookings SET status='completed'")
        conn.commit()
        conn.close()
        self._post_freezing()
        self.assertEqual(self._count(), 2)

    def test_registration_stores_institute(self):
        self._post_freezing()
        booking = self._one("SELECT esm FROM freezing_bookings")
        self.assertEqual(booking["esm"], "Example Institute of Science")

    def test_far_future_dates_are_rejected_server_side(self):
        for value in ("9999-12-31", _next_weekday(370).isoformat()):
            response, send = self._post_freezing(freezing_date=value)
            self.assertIn(b"at most one year in advance", response.data, value)
            send.assert_not_called()
        self.assertEqual(self._count(), 0)

        response, _ = self._post_freezing(freezing_date=_next_weekday(300).isoformat())
        self.assertEqual(self._count(), 1)

    def test_register_page_sets_max_on_freezing_date(self):
        page = self.guest.get("/register").get_data(as_text=True)
        expected = (facility_today() + datetime.timedelta(days=365)).isoformat()
        self.assertRegex(page, rf'id="freezing_date" name="freezing_date" max="{expected}"')


# ── 2: institute column migration and completion ───────────────────────────

class FreezingInstituteTests(_AppTestCase):
    def test_init_db_adds_institute_columns_to_existing_tables(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy.sqlite3")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE freezing_bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_name VARCHAR(100), pi_name VARCHAR(100), email VARCHAR(150), "
            "origin VARCHAR(100), sample_name VARCHAR(150), grids INTEGER, "
            "freezing_date DATE, status TEXT DEFAULT 'active', "
            "registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE completed_freezing (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_name VARCHAR(100), pi_name VARCHAR(100), email VARCHAR(150), "
            "origin VARCHAR(100), sample_name VARCHAR(150), grids INTEGER, "
            "freezing_date DATE, completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.commit()
        conn.close()
        with patch.object(database, "DATABASE_URL", f"sqlite:///{legacy_path}"):
            database.init_db()
            database.init_db()  # idempotent
        conn = sqlite3.connect(legacy_path)
        for table in ("freezing_bookings", "completed_freezing"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            self.assertIn("esm", columns, table)
        conn.close()

    def test_institute_carries_through_completion_to_charge_sheet(self):
        self._insert(
            "freezing_bookings", user_name="Freeze Person", pi_name="Dr PI",
            email="freeze@example.test", origin="External", esm="Example Institute",
            sample_name="Sample", grids=2, freezing_date="2026-10-02", status="active",
        )
        self._login()
        token = self._admin_csrf()
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                "/admin/freezing/complete/1",
                data={
                    "_csrf_token": token, "user_category": "academic",
                    "actual_grids": "2", "grid_source": "self_owned", "grid_type": "",
                },
            )
        self.assertEqual(response.status_code, 302)
        completed = self._one("SELECT * FROM completed_freezing")
        self.assertEqual(completed["esm"], "Example Institute")

        text = _sheet_text(dict(completed), "Freezing")
        self.assertIn("Example Institute.", text)
        self.assertNotIn("Not provided", text)

    def test_history_edit_keeps_freezing_institute(self):
        booking_id = self._insert(
            "completed_freezing", user_name="Freeze Person", pi_name="Dr PI",
            email="freeze@example.test", origin="External", esm="Old Institute",
            sample_name="Sample", grids=2, freezing_date="2026-10-02",
            actual_grids=2, number_of_grids=2, grid_source="self_owned",
            service_stage="Freezing / Grid Registration",
        )
        self._login()
        page = self.client.get("/admin/history").get_data(as_text=True)
        self.assertIn('value="Old Institute"', page)
        response = self.client.post(
            f"/admin/history/freezing/{booking_id}/edit",
            data={
                "_csrf_token": self._admin_csrf(), "user_name": "Freeze Person",
                "pi_name": "Dr PI", "email": "freeze@example.test", "origin": "External",
                "esm": "New Institute", "sample_name": "Sample", "grids": "2",
                "actual_grids": "2", "completion_date": "2026-10-02",
                "grid_source": "self_owned", "grid_type": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self._one("SELECT esm FROM completed_freezing WHERE id=?", [booking_id])["esm"],
            "New Institute",
        )


# ── 4, (c), (d): charge sheet PDF content ──────────────────────────────────

class ChargeSheetContentTests(unittest.TestCase):
    def _screening_row(self, **extra):
        row = {
            "id": 7, "user_name": "User", "email": "user@example.test", "origin": "External",
            "esm": "Institute", "charge_sheet_id": "2026_ACDM_0100",
            "completion_date": "2026-11-10", "service_stage": "Screening / Clipping",
            "number_of_grids": 4, "actual_grids": 4, "actual_slots": 1,
            "grid_source": "facility", "grid_type": "normal_holey_carbon",
        }
        charges = calculate_charge_sheet("academic", "Screening / Clipping", 4, "facility",
                                         "normal_holey_carbon", 1)
        row.update(charges)
        row.update(extra)
        return row

    def test_dates_print_as_day_month_year(self):
        self.assertEqual(charge_sheet._display_date("2026-11-10"), "10-11-2026")
        self.assertEqual(charge_sheet._display_date("2026-11-10 09:30:00"), "10-11-2026")
        self.assertEqual(charge_sheet._display_date(datetime.date(2026, 11, 10)), "10-11-2026")
        text = _sheet_text(self._screening_row(), "Screening")
        self.assertIn("10-11-2026", text)
        self.assertNotIn("2026-11-10", text)

    def test_external_lines_show_gst_and_totals_matching_sheet_gst(self):
        row = self._screening_row()
        story = _sheet_story(row, "Screening")
        billing = _billing_table(story)
        lines = billing._cellvalues[1:]
        total_gst = Decimal("0")
        for _, _, _, amount, gst, total in lines:
            amount = Decimal(amount.replace("INR ", "").replace(",", ""))
            gst = Decimal(gst.replace("INR ", "").replace(",", ""))
            total = Decimal(total.replace("INR ", "").replace(",", ""))
            self.assertEqual(total, amount + gst)
            total_gst += gst
        self.assertEqual(total_gst, Decimal(str(row["gst_amount"])))
        self.assertGreater(total_gst, 0)

    def test_csic_sheet_line_gst_adds_up_to_csic_gst(self):
        from revenue import apply_csic_final_amount
        row = self._screening_row()
        row.update(apply_csic_final_amount(row, "50000.00", "industry"))
        row["origin"] = "Industry"
        lines = charge_sheet._billing_lines(row, "Screening")
        shares = charge_sheet._line_gst(lines, row["gst_amount"])
        self.assertEqual(sum(shares, Decimal("0")), Decimal(str(row["gst_amount"])))

    def test_internal_sheet_stays_gst_free(self):
        row = self._screening_row(origin="Internal")
        row.update(calculate_charge_sheet("internal", "Screening / Clipping", 4, "facility",
                                          "normal_holey_carbon", 1))
        story = _sheet_story(row, "Screening")
        billing = _billing_table(story)
        for _, _, _, amount, total, _ in billing._cellvalues[1:]:
            self.assertEqual(amount, total)
        self.assertEqual(Decimal(str(row["gst_amount"])), Decimal("0"))

    def test_freezing_sheet_omits_slots_line(self):
        row = {
            "id": 3, "user_name": "User", "email": "user@example.test", "origin": "External",
            "esm": "Institute", "completed_at": "2026-11-10 10:00:00",
            "service_stage": "Freezing / Grid Registration", "number_of_grids": 2,
            "actual_slots": 1, "grid_source": "self_owned",
        }
        row.update(calculate_charge_sheet("academic", "Freezing / Grid Registration", 2, "self_owned"))
        for origin in ("External", "Industry", "Internal"):
            row["origin"] = origin
            text = _sheet_text(row, "Freezing")
            self.assertNotIn("Actual 24-hour slots", text, origin)
            self.assertIn("FRZ-3", text)
        self.assertIn("Actual 24-hour slots", _sheet_text(self._screening_row(), "Screening"))

    def test_combined_sheet_prefixes_ids_and_lists_grids_per_stage(self):
        freezing = {"id": 1, "combined_service": "Freezing", "number_of_grids": 4}
        screening = {"id": 1, "combined_service": "Screening", "number_of_grids": 4}
        dc = {"id": 1, "combined_service": "Data Collection", "number_of_grids": 4}
        row = self._screening_row(combined_items=[freezing, screening, dc], number_of_grids=12)
        self.assertEqual(charge_sheet._booking_label(row, "Combined Services"), "FRZ-1, SCR-1, DC-1")
        text = _sheet_text(row, "Combined Services")
        self.assertIn("FRZ-1, SCR-1, DC-1", text)
        self.assertIn("FRZ-1: 4; SCR-1: 4; DC-1: 4", text)
        self.assertNotRegex(text, r"Actual grids \| 12\b")


# ── (b), 5: sending charge sheets ──────────────────────────────────────────

class ChargeSheetSendingTests(_AppTestCase):
    def setUp(self):
        super().setUp()
        self._login()

    def _screening(self, **extra):
        columns = dict(
            user_name="Combo User", pi_name="PI", email="combo@example.test", origin="External",
            esm="Institute", sample_name="S", grids=2, days=1, status="completed",
            completion_date="2026-10-05", actual_slots=1, actual_grids=2, number_of_grids=2,
            grid_source="self_owned", clipped_grids=0,
        )
        columns.update(extra)
        return self._insert("screening_bookings", **columns)

    def _dc(self, **extra):
        columns = dict(
            user_name="Combo User", pi_name="PI", email="combo@example.test", origin="External",
            esm="Institute", sample_name="S", grids=2, days=2, status="completed",
            completion_date="2026-10-06", actual_slots=2, actual_grids=2, number_of_grids=2,
            grid_source="self_owned", clipped_grids=0,
        )
        columns.update(extra)
        return self._insert("bookings", **columns)

    def _freezing(self, **extra):
        columns = dict(
            user_name="Combo User", pi_name="PI", email="combo@example.test", origin="External",
            esm="Institute", sample_name="S", grids=2, freezing_date="2026-10-02",
            actual_grids=2, number_of_grids=2, actual_slots=1, grid_source="self_owned",
            service_stage="Freezing / Grid Registration",
        )
        columns.update(extra)
        return self._insert("completed_freezing", **columns)

    def _sequence(self):
        return self._one("SELECT next_number FROM charge_sheet_sequences")["next_number"]

    def test_combined_slots_exclude_freezing_and_ids_are_prefixed(self):
        frz, scr, dc = self._freezing(), self._screening(), self._dc()
        selections = [
            {"service_key": "freezing", "booking_id": frz},
            {"service_key": "screening", "booking_id": scr},
            {"service_key": "imaging", "booking_id": dc},
        ]
        with patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF") as make_pdf, \
                patch("blueprints.admin.send_email") as send:
            response = self.client.post("/admin/charge-sheet/combined", data={
                "_csrf_token": self._admin_csrf(), "selected_slots": json.dumps(selections),
            })
        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        combined = make_pdf.call_args.args[0]
        self.assertEqual(Decimal(combined["actual_slots"]), Decimal("3"))
        text = _sheet_text(combined, "Combined Services")
        self.assertIn(f"FRZ-{frz}, SCR-{scr}, DC-{dc}", text)
        self.assertIn("Actual 24-hour slots | 3", text)

    def _race_next_id(self, table, booking_id):
        """Wrap _next_charge_sheet_id so another request "wins" first."""
        real = admin_module._next_charge_sheet_id

        def racing(cur, origin, completed_date):
            other = database.get_db()
            other.execute(
                f"UPDATE {table} SET charge_sheet_id='2026_ACDM_9999', "
                "charge_sheet_sent_at=CURRENT_TIMESTAMP WHERE id=?",
                [booking_id],
            )
            other.commit()
            other.close()
            return real(cur, origin, completed_date)

        return patch("blueprints.admin._next_charge_sheet_id", side_effect=racing)

    def test_concurrent_first_send_does_not_email_a_second_sheet(self):
        booking_id = self._screening()
        before = self._sequence()
        token = self._admin_csrf()
        with self._race_next_id("screening_bookings", booking_id), \
                patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF"), \
                patch("blueprints.admin.send_email") as send:
            response = self.client.post(
                f"/admin/charge-sheet/screening/{booking_id}",
                data={"_csrf_token": token}, follow_redirects=True,
            )
        self.assertIn(b"already been sent", response.data)
        send.assert_not_called()
        row = self._one("SELECT charge_sheet_id FROM screening_bookings WHERE id=?", [booking_id])
        self.assertEqual(row["charge_sheet_id"], "2026_ACDM_9999")
        self.assertEqual(self._sequence(), before)

    def test_first_send_still_works_and_resend_keeps_id(self):
        booking_id = self._screening()
        token = self._admin_csrf()
        with patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF") as make_pdf, \
                patch("blueprints.admin.send_email") as send:
            self.client.post(f"/admin/charge-sheet/screening/{booking_id}", data={"_csrf_token": token})
            self.client.post(f"/admin/charge-sheet/screening/{booking_id}", data={"_csrf_token": token})
        self.assertEqual(send.call_count, 2)
        first_id = make_pdf.call_args_list[0].args[0]["charge_sheet_id"]
        self.assertEqual(make_pdf.call_args_list[1].args[0]["charge_sheet_id"], first_id)
        self.assertEqual(
            self._one("SELECT charge_sheet_id FROM screening_bookings WHERE id=?", [booking_id])["charge_sheet_id"],
            first_id,
        )
        # Sheet IDs use the completion date's academic year (item 7).
        self.assertTrue(first_id.startswith("2026_ACDM_"))

    def test_concurrent_combined_send_does_not_email_a_second_sheet(self):
        scr, dc = self._screening(), self._dc()
        selections = [
            {"service_key": "screening", "booking_id": scr},
            {"service_key": "imaging", "booking_id": dc},
        ]
        before = self._sequence()
        token = self._admin_csrf()
        with self._race_next_id("bookings", dc), \
                patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF"), \
                patch("blueprints.admin.send_email") as send:
            response = self.client.post("/admin/charge-sheet/combined", data={
                "_csrf_token": token, "selected_slots": json.dumps(selections),
            }, follow_redirects=True)
        self.assertIn(b"billed by another request", response.data)
        send.assert_not_called()
        self.assertIsNone(
            self._one("SELECT charge_sheet_id FROM screening_bookings WHERE id=?", [scr])["charge_sheet_id"]
        )
        self.assertEqual(self._sequence(), before)


# ── (a) CSIC history edit and prebilled grids ──────────────────────────────

class HistoryEditRecalculationTests(_AppTestCase):
    def setUp(self):
        super().setUp()
        self._login()

    def _edit_screening(self, booking_id, **extra):
        row = self._one("SELECT * FROM screening_bookings WHERE id=?", [booking_id])
        data = {
            "_csrf_token": self._admin_csrf(), "user_name": row["user_name"],
            "pi_name": row["pi_name"], "email": row["email"], "origin": row["origin"],
            "esm": row["esm"] or "", "sample_name": row["sample_name"],
            "grids": str(row["grids"]), "days": str(row["days"]),
            "completion_date": str(row["completion_date"])[:10],
            "actual_slots": str(int(Decimal(str(row["actual_slots"])))),
            "actual_grids": str(row["actual_grids"]), "grid_source": row["grid_source"],
            "grid_type": row["grid_type"] or "", "clipped_grids": str(row["clipped_grids"] or 0),
            "clip_base_source": row["clip_base_source"] or "facility",
        }
        data.update(extra)
        return self.client.post(f"/admin/history/screening/{booking_id}/edit", data=data)

    def test_csic_history_edit_keeps_csic_components_consistent(self):
        booking_id = self._insert(
            "screening_bookings", user_name="Industry User", pi_name="PI",
            email="industry@example.test", origin="Industry", esm="Acme Pvt Ltd",
            sample_name="S", grids=2, days=1, status="ongoing",
            registration_date=datetime.date.today().isoformat(),
        )
        with patch("blueprints.admin.send_email"):
            response = self.client.post(f"/admin/screening/complete/{booking_id}", data={
                "_csrf_token": self._admin_csrf(), "user_category": "industry",
                "service_stage": "Screening / Clipping", "actual_slots": "1",
                "actual_grids": "2", "grid_source": "self_owned", "grid_type": "",
                "clipped_grids": "0", "is_csic": "1", "csic_final_amount": "50000.00",
                "csic_discount_reason": "Agreed project rate",
            })
        self.assertEqual(response.status_code, 302)
        fields = ("subtotal", "gst_amount", "grand_total", "total_billed",
                  "csic_adjustment", "csic_original_amount", "csic_final_amount")
        sql = f"SELECT {', '.join(fields)} FROM screening_bookings WHERE id=?"
        completed = self._one(sql, [booking_id])

        response = self._edit_screening(booking_id)
        self.assertEqual(response.status_code, 302)
        edited = self._one(sql, [booking_id])
        expected = {
            "subtotal": Decimal("42372.88"), "gst_amount": Decimal("7627.12"),
            "grand_total": Decimal("50000.00"), "total_billed": Decimal("50000.00"),
            "csic_adjustment": Decimal("-16627.12"), "csic_original_amount": Decimal("69620.00"),
            "csic_final_amount": Decimal("50000.00"),
        }
        for field in fields:
            self.assertEqual(Decimal(str(edited[field])), expected[field], field)
            self.assertEqual(Decimal(str(completed[field])), expected[field], field)

    def test_prebilled_grids_give_the_same_total_everywhere(self):
        expected = calculate_charge_sheet(
            "academic", "Screening / Clipping", 4, "facility", None, 1,
            normal_grids=2, gold_grids=0, prebilled_grids=2,
        )
        full = calculate_charge_sheet(
            "academic", "Screening / Clipping", 4, "facility", "normal_holey_carbon", 1,
        )
        self.assertLess(expected["grand_total"], full["grand_total"])
        breakdown = json.dumps({"normal_holey_carbon": 2, "gold_carbon_graphene": 0,
                                "prebilled_grids": 2})
        # Stored as completion leaves it (totals from the prebilled calculation).
        booking_id = self._insert(
            "screening_bookings", user_name="Pre User", pi_name="PI", email="pre@example.test",
            origin="External", esm="Institute", sample_name="S", grids=4, days=1,
            status="completed", completion_date="2026-10-05", actual_slots=1, actual_grids=4,
            number_of_grids=4, grid_source="facility", grid_type=expected["grid_type"],
            grid_breakdown=breakdown, clipped_grids=0,
            subtotal=str(expected["subtotal"]), gst_amount=str(expected["gst_amount"]),
            grand_total=str(expected["grand_total"]), total_billed=str(expected["total_billed"]),
        )
        total = Decimal(str(expected["grand_total"]))

        with patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF") as make_pdf:
            response = self.client.post(
                f"/admin/charge-sheet/screening/{booking_id}/preview",
                data={"_csrf_token": self._admin_csrf()},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(str(make_pdf.call_args.args[0]["grand_total"])), total)

        response = self._edit_screening(booking_id, normal_grids="2", gold_grids="0")
        self.assertEqual(response.status_code, 302)
        edited = self._one("SELECT grand_total, grid_breakdown FROM screening_bookings WHERE id=?",
                           [booking_id])
        self.assertEqual(Decimal(str(edited["grand_total"])), total)
        self.assertEqual(json.loads(edited["grid_breakdown"])["prebilled_grids"], 2)

        with patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF") as make_pdf, \
                patch("blueprints.admin.send_email"):
            self.client.post(f"/admin/charge-sheet/screening/{booking_id}",
                             data={"_csrf_token": self._admin_csrf()})
        sent_row = make_pdf.call_args.args[0]
        self.assertEqual(Decimal(str(sent_row["grand_total"])), total)
        self.assertEqual(
            Decimal(str(self._one("SELECT grand_total FROM screening_bookings WHERE id=?",
                                  [booking_id])["grand_total"])),
            total,
        )
        self.assertIn("2 billed at freezing", _sheet_text(sent_row, "Screening"))


# ── 6: admin session guard ─────────────────────────────────────────────────

class AdminSessionGuardTests(_AppTestCase):
    def test_signed_in_admin_visiting_login_goes_to_panel_and_stays_signed_in(self):
        self._login()
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/panel"))
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("admin_logged_in"))
        self.assertEqual(self.client.get("/admin/panel").status_code, 200)

    def test_signed_out_login_page_still_renders(self):
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_unlisted_admin_endpoint_is_refused_not_served(self):
        self._login()
        allowed = set(admin_module._ALLOWED_ADMIN_ENDPOINTS) - {"admin.history"}
        with patch.object(admin_module, "_ALLOWED_ADMIN_ENDPOINTS", allowed):
            response = self.client.get("/admin/history")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/", response.headers["Location"])
        self.assertNotIn(b"Edit booking", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("admin_logged_in", session)
            self.assertNotIn("admin_user_id", session)

    def test_every_admin_and_inventory_endpoint_is_allowed(self):
        endpoints = {
            rule.endpoint for rule in self.app.url_map.iter_rules()
            if rule.endpoint.startswith(("admin.", "inventory."))
        }
        self.assertTrue(endpoints)
        self.assertEqual(endpoints - admin_module._ALLOWED_ADMIN_ENDPOINTS, set())


if __name__ == "__main__":
    unittest.main()
