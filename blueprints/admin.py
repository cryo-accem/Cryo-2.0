import datetime
import csv
import io
import base64
import json
import os
import sqlite3
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from decimal import Decimal, InvalidOperation
from flask import (
    Blueprint, current_app, render_template, request, send_file, Response, jsonify,
    redirect, url_for, session, flash,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from monthly_activity_report import build_monthly_activity_report
from database import get_db
from database import _is_sqlite_url
from extensions import send_email, send_email_sync
from revenue import (
    calculate_booking_revenue, calculate_charge_sheet, is_non_billable_booking,
    parse_number_of_grids,
)
from blueprints.freezing import complete_freezing_booking
from charge_sheet import generate_charge_sheet
from normalization import normalize_pi_name, clean_display_name, preferred_pi_label
from blueprints.public import PI_USERS
from historical_revenue import (
    HISTORICAL_REVENUE,
    HISTORICAL_CATEGORY_TOTALS, HISTORICAL_CATEGORY_BY_MONTH,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
BACKUP_EMAIL = "cryoem.iisc@gmail.com"
_CHARGE_SHEET_CATEGORY_CODES = {
    "internal": "INT",
    "academic": "ACDM",
    "industrial": "INDY",
}

_ALLOWED_ADMIN_ENDPOINTS = {
    "admin.panel",
    "admin.download_pi_users_csv",
    "admin.download_activity_report",
    "admin.change_password",
    "admin.logout",
    "admin.datacollecting",
    "admin.load_dc",
    "admin.complete_dc",
    "admin.delete_dc",
    "admin.freezing_admin",
    "admin.complete_freezing",
    "admin.screening_admin",
    "admin.load_sc",
    "admin.complete_sc",
    "admin.delete_sc",
    "admin.history",
    "admin.send_charge_sheet",
    "admin.preview_charge_sheet",
    "admin.delete_completed_booking",
    "admin.edit_completed_booking",
    "admin.send_combined_charge_sheet",
    "admin.update_payment",
    "admin.send_payment_reminder",
    "admin.download_payment_proof",
    "admin.download_registrations_csv",
    "admin.download_database_backup",
    "admin.archive_completed_registrations",
    "admin.billing_preview",
    "admin.add_csic_project",
    "admin.delete_csic_project",
    "admin.maintenance",
    "admin.save_publication",
    "admin.delete_publication",
    "admin.save_instrument",
    "admin.delete_instrument",
    "static",
}


# ── Session guard ────────────────────────────────────────────────────────────

@admin_bp.before_app_request
def check_admin_session():
    if "admin_logged_in" in session and request.endpoint:
        if session.get("admin_user_id") is not None:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT must_change_password FROM users WHERE id=? AND role=?",
                [session["admin_user_id"], "admin"],
            )
            user = cur.fetchone()
            cur.close()
            conn.close()
            if not user:
                session.clear()
                return redirect(url_for("admin.login"))
            if user["must_change_password"]:
                session["must_change_password"] = True
        if session.get("must_change_password") and request.endpoint not in {
            "admin.change_password",
            "admin.logout",
            "static",
        }:
            return redirect(url_for("admin.change_password"))
        if request.endpoint not in _ALLOWED_ADMIN_ENDPOINTS:
            session.pop("admin_logged_in", None)


# ── Login / Logout ───────────────────────────────────────────────────────────

@admin_bp.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]

        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=?", [username])
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["admin_logged_in"] = True
            session["admin_user_id"] = user["id"]
            if user["must_change_password"]:
                session["must_change_password"] = True
                return redirect(url_for("admin.change_password"))
            return redirect(url_for("admin.panel"))

        flash("Invalid username or password.", "login")
    return render_template("admin.html")


@admin_bp.route("/change-password", methods=["GET", "POST"])
def change_password():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    if not session.get("must_change_password"):
        return redirect(url_for("admin.panel"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT password_hash FROM users WHERE id=? AND role=?",
            [session.get("admin_user_id"), "admin"],
        )
        user = cur.fetchone()
        if not user or not check_password_hash(user["password_hash"], current_password):
            cur.close()
            conn.close()
            flash("Enter your current temporary password correctly.", "password")
            return render_template("admin_change_password.html")
        if len(new_password) < 12:
            cur.close()
            conn.close()
            flash("Choose a password with at least 12 characters.", "password")
            return render_template("admin_change_password.html")
        if new_password != confirm_password:
            cur.close()
            conn.close()
            flash("The new password and confirmation do not match.", "password")
            return render_template("admin_change_password.html")

        cur.execute(
            "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
            [generate_password_hash(new_password, method="pbkdf2:sha256"), 0, session["admin_user_id"]],
        )
        conn.commit()
        cur.close()
        conn.close()
        session.pop("must_change_password", None)
        flash("Your password has been changed.", "success")
        return redirect(url_for("admin.panel"))

    return render_template("admin_change_password.html")


@admin_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("public.index"))


@admin_bp.route("/billing/preview", methods=["GET", "POST"])
def billing_preview():
    """Return an authoritative preview for the admin billing form."""
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        charges = calculate_charge_sheet(
            request.values.get("user_category", ""),
            request.values.get("service_stage", ""),
            request.values.get("number_of_grids", request.values.get("actual_grids", "")),
            request.values.get("grid_source", ""),
            request.values.get("grid_type", ""),
            request.values.get("actual_slots", "1") or "1",
            request.values.get("processing_requested") == "1",
            request.values.get("clipped_grids", "0") or "0",
            request.values.get("normal_grids") or None,
            request.values.get("gold_grids") or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({key: str(value) for key, value in charges.items() if key not in {"user_category", "service_stage", "grid_source", "grid_type"}})


# ── Main Dashboard ───────────────────────────────────────────────────────────

@admin_bp.route("/panel")
def panel():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    conn = get_db()
    cur = conn.cursor()
    pi_counts = _pi_overview_counts(cur)
    if len(pi_counts) > 6:
        other_count = sum(item["value"] for item in pi_counts[6:])
        pi_counts = pi_counts[:6] + [{"label": "Other PIs", "value": other_count}]
    cur.execute("SELECT COUNT(*) AS count FROM bookings WHERE status='waiting'")
    collecting_waiting = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) AS count FROM screening_bookings WHERE status='waiting'")
    screening_waiting = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) AS count FROM freezing_bookings WHERE status='active'")
    freezing_active = cur.fetchone()["count"]
    pending_payments = _dashboard_pending_payments(cur)
    revenue = _revenue_dashboard(cur)
    cur.close()
    conn.close()
    pi_colors = ["#167da5", "#32a889", "#d58b42", "#7d6bb5", "#c15c73", "#5d86bd"]
    pi_total = sum(item["value"] for item in pi_counts)
    pi_chart = []
    pi_start = 0
    for index, item in enumerate(pi_counts):
        pi_end = pi_start + (item["value"] / pi_total * 100 if pi_total else 0)
        pi_chart.append({**item, "start": pi_start, "end": pi_end, "color": pi_colors[index % len(pi_colors)]})
        pi_start = pi_end
    waiting_chart = [
        {"label": "Data collecting", "value": collecting_waiting, "color": "#d05b63"},
        {"label": "Freezing", "value": freezing_active, "color": "#d58b42"},
        {"label": "Screening", "value": screening_waiting, "color": "#7d6bb5"},
    ]
    waiting_total = sum(item["value"] for item in waiting_chart)
    waiting_start = 0
    for item in waiting_chart:
        waiting_end = waiting_start + (item["value"] / waiting_total * 100 if waiting_total else 0)
        item["start"], item["end"] = waiting_start, waiting_end
        waiting_start = waiting_end
    return render_template(
        "admin_panel.html",
        pi_chart=pi_chart,
        pi_total=pi_total,
        waiting_chart=waiting_chart,
        waiting_total=waiting_total,
        pending_payments=pending_payments,
        revenue=revenue,
        updated_at=datetime.datetime.now().strftime("%d %b %Y, %H:%M"),
    )


def _dashboard_pending_payments(cur):
    pending_rows = []
    cur.execute(
        """SELECT service_key, booking_id, COUNT(*) AS reminder_count
           FROM payment_reminder_history
           GROUP BY service_key, booking_id"""
    )
    reminder_counts = {
        (row["service_key"], row["booking_id"]): row["reminder_count"]
        for row in cur.fetchall()
    }
    sources = (
        ("bookings", "Data collection", "completion_date", "completed"),
        ("screening_bookings", "Screening", "completion_date", "completed"),
        ("completed_freezing", "Freezing", "completed_at", None),
    )
    for table, service, completion_column, booking_status in sources:
        where = " WHERE status='completed'" if booking_status else ""
        cur.execute(
            f"SELECT * FROM {table}{where} ORDER BY {completion_column} DESC"
        )
        for row in _history_rows(cur.fetchall()):
            if is_non_billable_booking(row):
                continue
            internal = (row["origin"] or "").strip().casefold() == "internal"
            tracking_status = (
                row["debit_head_status"] if internal else row["payment_status"]
            )
            expected_status = "Debit Head Pending" if internal else "Payment Pending"
            if tracking_status != expected_status:
                continue

            billed = _money(row.get("total_billed") or row.get("grand_total"))
            received = _money(row.get("amount_received"))
            pending_rows.append({
                **row,
                "service_label": service,
                "service_key": (
                    "imaging" if table == "bookings"
                    else "screening" if table == "screening_bookings"
                    else "freezing"
                ),
                "completion_value": row.get(completion_column),
                "tracking_label": "Debit head pending" if internal else "Payment pending",
                "pending_amount": max(Decimal("0.00"), billed - received),
                "reminder_count": reminder_counts.get(
                    (
                        "imaging" if table == "bookings"
                        else "screening" if table == "screening_bookings"
                        else "freezing",
                        row["id"],
                    ),
                    0,
                ),
            })

    pending_rows.sort(
        key=lambda item: str(item["completion_value"] or ""), reverse=True
    )
    return {
        "rows": pending_rows,
        "payment_count": sum(
            item["tracking_label"] == "Payment pending" for item in pending_rows
        ),
        "payment_amount": sum(
            (
                item["pending_amount"]
                for item in pending_rows
                if item["tracking_label"] == "Payment pending"
            ),
            Decimal("0.00"),
        ),
        "debit_head_count": sum(
            item["tracking_label"] == "Debit head pending" for item in pending_rows
        ),
    }


@admin_bp.route("/payment-reminder/<service_key>/<int:booking_id>", methods=["POST"])
def send_payment_reminder(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        flash("Unknown booking type.")
        return redirect(url_for("admin.panel"))

    table = table_info[0]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE id=?", [booking_id])
    booking_row = cur.fetchone()
    cur.close()
    conn.close()

    if not booking_row:
        flash("That booking could not be found.")
        return redirect(url_for("admin.panel"))
    booking = _history_rows([booking_row])[0]
    if is_non_billable_booking(booking):
        flash("This booking does not require a payment reminder.")
        return redirect(url_for("admin.panel"))

    internal = (booking.get("origin") or "").strip().casefold() == "internal"
    tracking_status = (
        booking.get("debit_head_status") if internal
        else booking.get("payment_status")
    )
    expected_status = "Debit Head Pending" if internal else "Payment Pending"
    if tracking_status != expected_status:
        flash("This booking is no longer marked as pending.")
        return redirect(url_for("admin.panel"))

    recipient = (booking.get("email") or "").strip()
    if not recipient:
        flash("A reminder could not be sent because this booking has no email address.")
        return redirect(url_for("admin.panel"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) AS reminder_count
           FROM payment_reminder_history
           WHERE service_key=? AND booking_id=?""",
        [service_key, booking_id],
    )
    reminder_number = cur.fetchone()["reminder_count"] + 1
    cur.close()
    conn.close()

    billed = _money(booking.get("total_billed") or booking.get("grand_total"))
    received = _money(booking.get("amount_received"))
    balance = max(Decimal("0.00"), billed - received)
    charge_sheet_id = booking.get("charge_sheet_id") or "not yet issued"
    service_label = table_info[1]
    completion_date = booking.get(table_info[2]) or "not available"
    greeting_name = booking.get("user_name") or "there"

    if internal:
        subject = f"Reminder {reminder_number}: debit-head documents for {charge_sheet_id}"
        message = (
            f"Dear {greeting_name},\n\n"
            "I hope you are doing well. This is a gentle reminder regarding the "
            "completed service below. When convenient, could you please share the "
            "debit-head document, duly signed by your PI, along with the charge "
            "sheet at your earliest convenience?\n\n"
        )
    else:
        subject = f"Reminder {reminder_number}: payment details for {charge_sheet_id}"
        message = (
            f"Dear {greeting_name},\n\n"
            "I hope you are doing well. This is a gentle reminder regarding the "
            "completed service below. When convenient, could you please share the "
            "payment details or proof of payment (for example, the transaction "
            "reference or receipt) against the charge sheet?\n\n"
        )

    message += (
        f"Service: {service_label}\n"
        f"Sample: {booking.get('sample_name') or 'Not provided'}\n"
        f"Completed: {completion_date}\n"
        f"Charge sheet: {charge_sheet_id}\n"
        f"Amount billed: ₹{billed:,.2f}\n"
        f"Amount received: ₹{received:,.2f}\n"
        f"Balance pending: ₹{balance:,.2f}\n\n"
        "Please let us know if you have already shared these details or need any "
        "assistance. Thank you for your kind cooperation.\n\n"
        "Warm regards,\n"
        "Cryo-EM Team"
    )
    if not send_email_sync(recipient, subject, message):
        flash(
            f"Payment reminder {reminder_number} could not be sent. "
            "Please try again later.",
            "error",
        )
        return redirect(url_for("admin.panel"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO payment_reminder_history
           (service_key, booking_id, reminder_number)
           VALUES (?, ?, ?)""",
        [service_key, booking_id, reminder_number],
    )
    conn.commit()
    cur.close()
    conn.close()
    flash(f"Payment reminder {reminder_number} sent to {recipient}.", "success")
    return redirect(url_for("admin.panel"))


def _pi_overview_counts(cur):
    pi_users = {
        normalize_pi_name(label): {
            "label": clean_display_name(label),
            "users": {f"historical:{normalize_pi_name(label)}:{index}" for index in range(count)},
        }
        for label, count in PI_USERS
    }
    pi_users.update({
        "historical-external": {"label": "Historical external academic users", "users": {f"historical:external:{i}" for i in range(64)}},
        "historical-industry": {"label": "Historical industry users", "users": {f"historical:industry:{i}" for i in range(25)}},
    })
    recent_users = set()
    for table in ("bookings", "screening_bookings", "freezing_bookings"):
        cur.execute(f"SELECT pi_name, user_name, email FROM {table} WHERE pi_name IS NOT NULL AND pi_name <> ''")
        for row in cur.fetchall():
            pi_name = clean_display_name(row["pi_name"])
            pi_key = normalize_pi_name(pi_name)
            identity = (row["email"] or row["user_name"] or "").strip().casefold()
            if not identity or identity in recent_users:
                continue
            recent_users.add(identity)
            entry = pi_users.setdefault(pi_key, {"label": pi_name, "users": set()})
            entry["label"] = preferred_pi_label(entry["label"], pi_name)
            entry["users"].add(f"recent:{identity}")
    return sorted(
        ({"label": entry["label"], "value": len(entry["users"])} for entry in pi_users.values()),
        key=lambda item: (-item["value"], item["label"]),
    )


@admin_bp.route("/panel/pi-users.csv")
def download_pi_users_csv():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    conn = get_db()
    cur = conn.cursor()
    rows = _pi_overview_counts(cur)
    cur.close()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["PI / Group", "Users"])
    writer.writerows((row["label"], row["value"]) for row in rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=users-by-pi.csv"},
    )


@admin_bp.route("/csic-projects", methods=["POST"])
def add_csic_project():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    company_name = request.form.get("company_name", "").strip()
    year_text = request.form.get("year", "").strip()
    amount_text = request.form.get("net_amount", "").strip()
    if not company_name or len(company_name) > 180:
        flash("Enter a company name of 1 to 180 characters.", "error")
        return redirect(url_for("admin.panel"))
    if not re.fullmatch(r"\d{4}", year_text) or not 1900 <= int(year_text) <= 2100:
        flash("Enter a valid four-digit year between 1900 and 2100.", "error")
        return redirect(url_for("admin.panel"))
    if not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", amount_text):
        flash("Enter a positive net amount with up to two decimal places.", "error")
        return redirect(url_for("admin.panel"))

    amount = Decimal(amount_text)
    if amount <= 0 or amount > Decimal("9999999999.99"):
        flash("Net amount must be greater than zero and no more than ₹9,999,999,999.99.", "error")
        return redirect(url_for("admin.panel"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO csic_projects (company_name, year, net_amount) VALUES (?, ?, ?)",
        [company_name, int(year_text), str(amount.quantize(Decimal("0.01")))],
    )
    conn.commit()
    cur.close()
    conn.close()
    flash("CSIC project added to revenue and slot usage.", "success")
    return redirect(url_for("admin.panel"))


@admin_bp.route("/csic-projects/<int:project_id>/delete", methods=["POST"])
def delete_csic_project(project_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM csic_projects WHERE id=?", [project_id])
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    flash("CSIC project removed." if deleted else "CSIC project not found.", "success" if deleted else "error")
    return redirect(url_for("admin.panel"))


def _content_url_is_safe(value):
    if not value:
        return True
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} or value.startswith("/static/")


@admin_bp.route("/maintenance")
def maintenance():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM managed_publications ORDER BY COALESCE(published_year, 0) DESC, id DESC")
    publications = cur.fetchall()
    cur.execute("SELECT * FROM managed_instruments ORDER BY id DESC")
    instruments = cur.fetchall()
    edit_publication = None
    edit_instrument = None
    publication_id = request.args.get("edit_publication", type=int)
    instrument_id = request.args.get("edit_instrument", type=int)
    if publication_id:
        cur.execute("SELECT * FROM managed_publications WHERE id=?", [publication_id])
        edit_publication = cur.fetchone()
    if instrument_id:
        cur.execute("SELECT * FROM managed_instruments WHERE id=?", [instrument_id])
        edit_instrument = cur.fetchone()
    cur.close()
    conn.close()
    return render_template(
        "admin_maintenance.html",
        publications=publications,
        instruments=instruments,
        edit_publication=edit_publication,
        edit_instrument=edit_instrument,
    )


@admin_bp.route("/maintenance/publications/save", methods=["POST"])
def save_publication():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    citation = request.form.get("citation", "").strip()
    doi_url = request.form.get("doi_url", "").strip()
    year_text = request.form.get("published_year", "").strip()
    publication_id = request.form.get("id", type=int)
    if not citation:
        flash("Publication citation is required.", "error")
        return redirect(url_for("admin.maintenance"))
    if not _content_url_is_safe(doi_url):
        flash("Publication link must use HTTPS, HTTP, or a local /static/ path.", "error")
        return redirect(url_for("admin.maintenance"))
    try:
        published_year = int(year_text) if year_text else None
        if published_year is not None and not 1900 <= published_year <= 2200:
            raise ValueError
    except ValueError:
        flash("Publication year must be between 1900 and 2200.", "error")
        return redirect(url_for("admin.maintenance"))

    conn = get_db()
    cur = conn.cursor()
    if publication_id:
        cur.execute(
            "UPDATE managed_publications SET citation=?, doi_url=?, published_year=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [citation, doi_url or None, published_year, publication_id],
        )
        message = "Publication updated."
    else:
        cur.execute(
            "INSERT INTO managed_publications (citation, doi_url, published_year) VALUES (?, ?, ?)",
            [citation, doi_url or None, published_year],
        )
        message = "Publication added."
    conn.commit()
    cur.close()
    conn.close()
    flash(message, "success")
    return redirect(url_for("admin.maintenance"))


@admin_bp.route("/maintenance/publications/<int:publication_id>/delete", methods=["POST"])
def delete_publication(publication_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM managed_publications WHERE id=?", [publication_id])
    conn.commit()
    cur.close()
    conn.close()
    flash("Publication removed.", "success")
    return redirect(url_for("admin.maintenance"))


@admin_bp.route("/maintenance/instruments/save", methods=["POST"])
def save_instrument():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    specifications = request.form.get("specifications", "").strip()
    image_url = request.form.get("image_url", "").strip()
    instrument_id = request.form.get("id", type=int)
    if not name or not description:
        flash("Instrument name and description are required.", "error")
        return redirect(url_for("admin.maintenance"))
    if not _content_url_is_safe(image_url):
        flash("Image link must use HTTPS, HTTP, or a local /static/ path.", "error")
        return redirect(url_for("admin.maintenance"))

    conn = get_db()
    cur = conn.cursor()
    if instrument_id:
        cur.execute(
            """UPDATE managed_instruments SET name=?, category=?, description=?,
               specifications=?, image_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            [name, category or None, description, specifications or None, image_url or None, instrument_id],
        )
        message = "Instrument updated."
    else:
        cur.execute(
            """INSERT INTO managed_instruments
               (name, category, description, specifications, image_url)
               VALUES (?, ?, ?, ?, ?)""",
            [name, category or None, description, specifications or None, image_url or None],
        )
        message = "Instrument added."
    conn.commit()
    cur.close()
    conn.close()
    flash(message, "success")
    return redirect(url_for("admin.maintenance"))


@admin_bp.route("/maintenance/instruments/<int:instrument_id>/delete", methods=["POST"])
def delete_instrument(instrument_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM managed_instruments WHERE id=?", [instrument_id])
    conn.commit()
    cur.close()
    conn.close()
    flash("Instrument removed.", "success")
    return redirect(url_for("admin.maintenance"))


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _revenue_components(row):
    gst = _money(row["gst_amount"])
    subtotal = _money(row["subtotal"])
    if not subtotal:
        subtotal = sum(
            (_money(row[key]) for key in (
                "slot_charge", "freezing_charge", "clipping_charge",
                "handling_charge", "processing_charge",
            )),
            Decimal("0"),
        )
    gross = _money(row["grand_total"] or row["total_billed"])
    if not gross:
        gross = subtotal + gst
    return gross - gst, gst, gross


def _fiscal_year_label(year, month):
    fiscal_start_year = year if month >= 4 else year - 1
    return f"FY {fiscal_start_year}-{(fiscal_start_year + 1) % 100:02d}"


def _date_one_year_earlier(value):
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _smooth_chart_path(points):
    if len(points) < 2:
        return ""

    secants = [
        (points[index + 1][1] - points[index][1])
        / (points[index + 1][0] - points[index][0])
        for index in range(len(points) - 1)
    ]
    tangents = [secants[0]]
    for previous, following in zip(secants, secants[1:]):
        tangents.append(
            0 if previous * following <= 0 else (previous + following) / 2
        )
    tangents.append(secants[-1])

    for index, secant in enumerate(secants):
        if secant == 0:
            tangents[index] = tangents[index + 1] = 0
            continue
        alpha = tangents[index] / secant
        beta = tangents[index + 1] / secant
        magnitude = alpha * alpha + beta * beta
        if magnitude > 9:
            scale = 3 / magnitude ** 0.5
            tangents[index] = scale * alpha * secant
            tangents[index + 1] = scale * beta * secant

    path = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    for index, secant in enumerate(secants):
        start = points[index]
        end = points[index + 1]
        one_third_dx = (end[0] - start[0]) / 3
        control_1 = (start[0] + one_third_dx, start[1] + tangents[index] * one_third_dx)
        control_2 = (end[0] - one_third_dx, end[1] - tangents[index + 1] * one_third_dx)
        path += (
            f" C {control_1[0]:.2f} {control_1[1]:.2f},"
            f" {control_2[0]:.2f} {control_2[1]:.2f},"
            f" {end[0]:.2f} {end[1]:.2f}"
        )
    return path


def _revenue_dashboard(cur):
    today = datetime.date.today()
    preset = request.args.get("range", "this_year")
    period = request.args.get("period", "monthly")
    if period not in {"weekly", "monthly", "annual"}:
        period = "monthly"
    start = end = None
    if preset == "this_month":
        start = today.replace(day=1)
        end = today
    elif preset == "last_month":
        start = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        end = today.replace(day=1) - datetime.timedelta(days=1)
    elif preset == "last_3_months":
        start = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        start = (start.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        end = today
    elif preset == "this_year":
        start = today.replace(month=1, day=1)
        end = today
    elif preset == "custom":
        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        if not start or not end or start > end:
            start = end = None
            preset = "all"

    comparison_start = start or today.replace(month=1, day=1)
    comparison_end = end or today
    previous_start = _date_one_year_earlier(comparison_start)
    previous_end = _date_one_year_earlier(comparison_end)
    comparison_current = {}
    comparison_previous = {}
    comparison_months = []
    historical_start = start.strftime("%Y-%m") if start else None
    historical_end = end.strftime("%Y-%m") if end else None
    month_start = comparison_start.replace(day=1)
    last_month = comparison_end.replace(day=1)
    while month_start <= last_month:
        previous_month_start = _date_one_year_earlier(month_start)
        current_key = month_start.strftime("%Y-%m")
        previous_key = previous_month_start.strftime("%Y-%m")
        comparison_current[current_key] = Decimal("0")
        comparison_previous[previous_key] = Decimal("0")
        comparison_months.append((month_start, previous_month_start))
        if month_start.month == 12:
            month_start = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_start = month_start.replace(month=month_start.month + 1)
    rows = []
    detailed_revenue_years = set()
    for table, service in (("bookings", "Data Collection"), ("screening_bookings", "Screening")):
        cur.execute(
            f"""            SELECT pi_name, origin, completion_date, actual_slots, actual_grids,
                       number_of_grids, slot_charge, freezing_charge, clipping_charge,
                       handling_charge, subtotal, processing_charge, gst_amount,
                       grand_total, total_billed, amount_received, payment_status,
                       debit_head_status
                FROM {table}
                WHERE status='completed'"""
        )
        for row in cur.fetchall():
            if is_non_billable_booking(row):
                continue
            completion_date = _parse_date(str(row["completion_date"])[:10])
            if not completion_date:
                continue
            detailed_revenue_years.add(completion_date.year)
            net_revenue = _revenue_components(row)[0]
            if comparison_start <= completion_date <= comparison_end:
                key = completion_date.strftime("%Y-%m")
                if key in comparison_current:
                    comparison_current[key] += net_revenue
            if previous_start <= completion_date <= previous_end:
                key = completion_date.strftime("%Y-%m")
                if key in comparison_previous:
                    comparison_previous[key] += net_revenue
            if (start and completion_date < start) or (end and completion_date > end):
                continue
            rows.append((row, service, completion_date))
    cur.execute(
        """SELECT pi_name, origin, completed_at AS completion_date, NULL AS actual_slots,
                  actual_grids, number_of_grids, slot_charge, freezing_charge, clipping_charge,
                  handling_charge, subtotal, processing_charge, gst_amount,
                  grand_total, total_billed, amount_received, payment_status,
                  debit_head_status
           FROM completed_freezing"""
    )
    for row in cur.fetchall():
        if is_non_billable_booking(row):
            continue
        completion_date = _parse_date(str(row["completion_date"])[:10])
        if not completion_date:
            continue
        detailed_revenue_years.add(completion_date.year)
        net_revenue = _revenue_components(row)[0]
        if comparison_start <= completion_date <= comparison_end:
            key = completion_date.strftime("%Y-%m")
            if key in comparison_current:
                comparison_current[key] += net_revenue
        if previous_start <= completion_date <= previous_end:
            key = completion_date.strftime("%Y-%m")
            if key in comparison_previous:
                comparison_previous[key] += net_revenue
        if (start and completion_date < start) or (end and completion_date > end):
            continue
        rows.append((row, "Freezing", completion_date))

    cur.execute("SELECT id, company_name, year, net_amount FROM csic_projects ORDER BY year DESC, company_name, id")
    csic_projects = []
    for project in cur.fetchall():
        project_year = int(project["year"])
        if start and project_year < start.year or end and project_year > end.year:
            continue
        csic_projects.append(project)

    totals = {
        "net": Decimal("0"), "gst": Decimal("0"), "gross": Decimal("0"),
        "billed": Decimal("0"), "received": Decimal("0"), "outstanding": Decimal("0"),
        "slots": Decimal("0"), "grids": Decimal("0"),
        "csic_net": Decimal("0"), "csic_slots": Decimal("0"),
    }
    by_category = {"Internal": Decimal("0"), "External/Academic": Decimal("0"), "Industrial": Decimal("0")}
    by_service = {"Data Collection": Decimal("0"), "Screening": Decimal("0"),
                  "Freezing": Decimal("0"), "Clipping": Decimal("0"),
                  "Handling Charge": Decimal("0"), "Data Processing": Decimal("0"),
                  "CSIC Projects": Decimal("0")}
    monthly = {}
    for row, service, completion_date in rows:
        slot = _money(row["slot_charge"])
        freezing = _money(row["freezing_charge"])
        clipping = _money(row["clipping_charge"])
        handling = _money(row["handling_charge"])
        processing = _money(row["processing_charge"])
        net, gst, gross = _revenue_components(row)
        totals["net"] += net
        totals["gst"] += gst
        totals["gross"] += gross
        billed = _money(row["total_billed"] or gross)
        recorded_received = row["amount_received"]
        payment_status = (row["payment_status"] or "").strip().casefold()
        debit_head_status = (row["debit_head_status"] or "").strip().casefold()
        internal_verified = (
            (row["origin"] or "").strip().casefold() == "internal"
            and debit_head_status == "debit head verified"
        )
        received = _money(
            recorded_received
            if recorded_received is not None
            else billed
            if payment_status == "payment verified" or internal_verified
            else 0
        )
        totals["billed"] += billed
        totals["received"] += received
        totals["outstanding"] += max(billed - received, Decimal("0"))
        totals["slots"] += Decimal(str(row["actual_slots"] or 0))
        totals["grids"] += Decimal(str(row["number_of_grids"] or row["actual_grids"] or 0))
        origin = (row["origin"] or "").strip().casefold()
        category = "Internal" if origin == "internal" else "Industrial" if origin in {"industry", "industrial"} else "External/Academic"
        by_category[category] += net
        by_service[service] += slot
        by_service["Freezing"] += freezing
        by_service["Clipping"] += clipping
        by_service["Handling Charge"] += handling
        by_service["Data Processing"] += processing
        if period == "weekly":
            period_key = completion_date.strftime("%G-W%V")
        elif period == "annual":
            period_key = _fiscal_year_label(completion_date.year, completion_date.month)
        else:
            period_key = completion_date.strftime("%Y-%m")
        monthly.setdefault(period_key, {"net": Decimal("0"), "slots": Decimal("0")})
        monthly[period_key]["net"] += net
        monthly[period_key]["slots"] += Decimal(str(row["actual_slots"] or 0))

    for month, amount in HISTORICAL_REVENUE:
        historical_year = int(month[:4])
        historical_month = int(month[5:7])
        historical_month_date = datetime.date(historical_year, historical_month, 1)
        historical_key = historical_month_date.strftime("%Y-%m")
        if (
            comparison_start.replace(day=1) <= historical_month_date
            <= comparison_end.replace(day=1)
            and historical_key in comparison_current
        ):
            comparison_current[historical_key] += amount
        if (
            previous_start.replace(day=1) <= historical_month_date
            <= previous_end.replace(day=1)
            and historical_key in comparison_previous
        ):
            comparison_previous[historical_key] += amount
        if historical_start and month < historical_start or historical_end and month > historical_end:
            continue
        totals["net"] += amount
        totals["gross"] += amount
        totals["billed"] += amount
        if period == "annual":
            period_key = _fiscal_year_label(int(month[:4]), int(month[5:7]))
        elif period == "weekly":
            period_key = f"{month}-01"
        else:
            period_key = month
        monthly.setdefault(period_key, {"net": Decimal("0"), "slots": Decimal("0")})
        monthly[period_key]["net"] += amount
    if not start and not end:
        for category, amount in HISTORICAL_CATEGORY_TOTALS.items():
            by_category[category] += amount
    else:
        for month, category_amounts in HISTORICAL_CATEGORY_BY_MONTH.items():
            if historical_start and month < historical_start or historical_end and month > historical_end:
                continue
            for category, amount in category_amounts.items():
                by_category[category] += amount

    for project in csic_projects:
        net = _money(project["net_amount"])
        totals["net"] += net
        totals["gross"] += net
        totals["csic_net"] += net
        totals["slots"] += Decimal("1")
        totals["csic_slots"] += Decimal("1")
        by_category["Industrial"] += net
        by_service["CSIC Projects"] += net

    monthly_items = [
        {"label": key, "value": values["net"].quantize(Decimal("0.01")),
         "slots": values["slots"].quantize(Decimal("0.01"))}
        for key, values in sorted(monthly.items())
    ]
    comparison_items = [
        {
            "label": (
                current_month.strftime("%b")
                if len(comparison_months) <= 12
                else current_month.strftime("%b %Y")
            ),
            "current": comparison_current[current_month.strftime("%Y-%m")].quantize(Decimal("0.01")),
            "previous": comparison_previous[previous_month.strftime("%Y-%m")].quantize(Decimal("0.01")),
        }
        for current_month, previous_month in comparison_months
    ]
    comparison_current_total = sum(
        (item["current"] for item in comparison_items), Decimal("0")
    )
    comparison_previous_total = sum(
        (item["previous"] for item in comparison_items), Decimal("0")
    )
    comparison_change_percent = (
        ((comparison_current_total - comparison_previous_total) / comparison_previous_total * 100)
        .quantize(Decimal("0.1"))
        if comparison_previous_total
        else None
    )
    if comparison_change_percent is None:
        comparison_change_label = (
            "New revenue; no previous-year baseline"
            if comparison_current_total
            else "No revenue in either period"
        )
        comparison_change_direction = "neutral"
    else:
        comparison_change_direction = (
            "increase" if comparison_change_percent > 0
            else "decrease" if comparison_change_percent < 0
            else "unchanged"
        )
        comparison_change_label = (
            f"{abs(comparison_change_percent):.1f}% {comparison_change_direction}"
        )
    current_period_label = (
        comparison_start.strftime("%Y")
        if comparison_start.year == comparison_end.year
        else f"{comparison_start.year}–{comparison_end.year}"
    )
    previous_period_label = (
        previous_start.strftime("%Y")
        if previous_start.year == previous_end.year
        else f"{previous_start.year}–{previous_end.year}"
    )
    comparison_max = max(
        (max(item["current"], item["previous"]) for item in comparison_items),
        default=Decimal("0"),
    )
    for index, item in enumerate(comparison_items):
        x = 5 + (index * 90 / max(len(comparison_items) - 1, 1))
        item["x"] = x
        item["current_y"] = 140 - float(item["current"] / comparison_max * 120) if comparison_max else 140
        item["previous_y"] = 140 - float(item["previous"] / comparison_max * 120) if comparison_max else 140
    monthly_max = max((item["value"] for item in monthly_items), default=Decimal("0"))
    if monthly_max:
        for index, item in enumerate(monthly_items):
            item["x"] = 8 + (index * 84 / max(len(monthly_items) - 1, 1))
            item["y"] = 160 - float(item["value"] / monthly_max * 140)
    return {
        "preset": preset,
        "period": period,
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "totals": {key: value.quantize(Decimal("0.01")) for key, value in totals.items()},
        "completed_bookings": len(rows),
        "by_category": [{"label": key, "value": value.quantize(Decimal("0.01"))} for key, value in by_category.items()],
        "by_service": [{"label": key, "value": value.quantize(Decimal("0.01"))} for key, value in by_service.items()],
        "monthly": monthly_items,
        "csic_projects": csic_projects,
        "year_comparison": {
            "current_year": current_period_label,
            "previous_year": previous_period_label,
            "change_period": (
                comparison_start.strftime("%b")
                if comparison_start.strftime("%Y-%m") == comparison_end.strftime("%Y-%m")
                else f"{comparison_start.strftime('%b')}–{comparison_end.strftime('%b')}"
            ),
            "change_label": comparison_change_label,
            "change_direction": comparison_change_direction,
            "months": comparison_items,
            "current_path": _smooth_chart_path(
                [(item["x"], item["current_y"]) for item in comparison_items]
            ),
            "previous_path": _smooth_chart_path(
                [(item["x"], item["previous_y"]) for item in comparison_items]
            ),
        },
    }


def _monthly_report_range() -> tuple[datetime.date, datetime.date]:
    today = datetime.date.today()
    preset = request.args.get("range", "this_month")
    if preset == "this_month":
        return today.replace(day=1), (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    if preset == "last_month":
        end = today.replace(day=1) - datetime.timedelta(days=1)
        return end.replace(day=1), end
    if preset == "this_year":
        return datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)
    if preset != "custom":
        raise ValueError("Choose a valid report period.")

    try:
        start = datetime.date.fromisoformat(request.args.get("start_date", ""))
        end = datetime.date.fromisoformat(request.args.get("end_date", ""))
    except ValueError:
        raise ValueError("Choose both a valid start date and end date.") from None
    if start > end:
        raise ValueError("The start date must be before or equal to the end date.")
    return start, end


@admin_bp.route("/activity-report.docx")
def download_activity_report():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    try:
        start, end = _monthly_report_range()
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype="text/plain")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bookings WHERE status='completed'")
    data_collection = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT * FROM screening_bookings WHERE status='completed'")
    screening = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT * FROM completed_freezing")
    freezing = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()

    report = build_monthly_activity_report(
        start, end, data_collection, screening, freezing, HISTORICAL_REVENUE
    )
    filename = f"ACCEM-activity-report-{start:%Y-%m-%d}-to-{end:%Y-%m-%d}.docx"
    return send_file(
        report,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ── Data Collecting Section ──────────────────────────────────────────────────

@admin_bp.route("/datacollecting")
def datacollecting():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM bookings WHERE status='waiting'")
    waiting = cur.fetchall()
    cur.execute("SELECT * FROM bookings WHERE status='ongoing'")
    ongoing = cur.fetchall()
    cur.close()
    conn.close()

    return render_template(
        "admin_datacollecting.html",
        waiting_registrations=waiting,
        ongoing_registrations=ongoing,
    )


@admin_bp.route("/datacollecting/load/<int:booking_id>")
def load_dc(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE bookings SET status='ongoing' WHERE id=?", [booking_id])
    cur.execute("SELECT * FROM bookings WHERE id=?", [booking_id])
    reg = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if reg:
        send_email(
            reg["email"],
            "Cryo-EM Data Collecting Slot Loaded",
            f"Dear {reg['user_name']},\n\nYour grids are loaded today.\n\nCryo-EM Team",
        )
    return redirect(url_for("admin.datacollecting"))


@admin_bp.route("/datacollecting/complete/<int:booking_id>", methods=["POST"])
def complete_dc(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    actual_slots = request.form.get("actual_slots", "").strip()
    actual_grids = (request.form.get("actual_grids") or request.form.get("number_of_grids", "")).strip()
    grid_source = request.form.get("grid_source", "").strip()
    grid_type = request.form.get("grid_type", "").strip()
    normal_grids = request.form.get("normal_grids", "").strip() or None
    gold_grids = request.form.get("gold_grids", "").strip() or None
    processing_requested = request.form.get("processing_requested") == "1"
    clipped_grids = request.form.get("clipped_grids", "0").strip() or "0"
    if not grid_source:
        flash("Please select the grid source before generating the bill.")
        return redirect(url_for("admin.datacollecting"))
    if not grid_type and not (normal_grids or gold_grids) and grid_source.casefold() in {"facility", "facility provided"}:
        flash("Please select the grid type for facility-provided grids.")
        return redirect(url_for("admin.datacollecting"))
    if not actual_slots or not actual_grids:
        flash("Actual slots and grids are required.")
        return redirect(url_for("admin.datacollecting"))
    user_category = request.form.get("user_category", "").strip() or None
    service_stage = request.form.get("service_stage", "").strip() or "Data Collection"
    if service_stage != "Data Collection":
        flash("This booking can only be completed as Data Collection.")
        return redirect(url_for("admin.datacollecting"))
    try:
        actual_slots_value = Decimal(actual_slots)
        if (actual_slots_value <= 0 or actual_slots_value != actual_slots_value.to_integral_value()
                ):
            raise ValueError
    except (InvalidOperation, ValueError):
        flash("Actual slots must be positive and grids must be a positive integer.")
        return redirect(url_for("admin.datacollecting"))
    try:
        actual_grids_value = parse_number_of_grids(actual_grids)
    except ValueError:
        flash("Number of grids must be at least 1.")
        return redirect(url_for("admin.datacollecting"))
    try:
        clipped_grids_value = int(clipped_grids)
        if clipped_grids_value < 0 or clipped_grids_value > actual_grids_value:
            raise ValueError
    except ValueError:
        flash("Clipped grids must be zero or no more than the actual grids.")
        return redirect(url_for("admin.datacollecting"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bookings WHERE id=? AND status='ongoing'", [booking_id])
    booking = cur.fetchone()
    if not booking:
        cur.close()
        conn.close()
        flash("That data collection booking is no longer ongoing.")
        return redirect(url_for("admin.datacollecting"))
    try:
        charges = calculate_booking_revenue(
            booking, actual_slots_value, actual_grids_value, processing_requested,
            grid_source, grid_type, "Data Collection", user_category,
            clipped_grids_value,
        )
    except ValueError as exc:
        cur.close()
        conn.close()
        flash(str(exc))
        return redirect(url_for("admin.datacollecting"))
    cur.execute(
        """UPDATE bookings SET status='completed', completion_date=?,
           actual_slots=?, actual_grids=?, number_of_grids=?, service_stage=?,
           grid_source=?, grid_type=?, grid_charge=?, handling_charge=?,
           clip_base_charge=?, slot_charge=?, freezing_charge=?, clipping_charge=?,
           processing_charge=?, subtotal=?, gst_amount=?, grand_total=?,
           clipped_grids=?,
           total_billed=?, processing_requested=?, bill_generated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (datetime.date.today(), str(charges["actual_slots"]), charges["actual_grids"],
         charges["number_of_grids"], charges["service_stage"], charges["grid_source"],
         charges["grid_type"], str(charges["grid_charge"]), str(charges["handling_charge"]),
         str(charges["clip_base_charge"]), str(charges["slot_charge"]), str(charges["freezing_charge"]),
         str(charges["clipping_charge"]), str(charges["processing_charge"]), str(charges["subtotal"]),
         str(charges["gst_amount"]), str(charges["grand_total"]), charges["clipped_grids"],
         str(charges["total_billed"]),
         int(processing_requested), booking_id),
    )
    cur.execute("SELECT * FROM bookings WHERE id=?", [booking_id])
    reg = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if reg:
        send_email(
            reg["email"],
            "Cryo-EM Data Collecting Slot Completed",
            (
                f"Dear {reg['user_name']},\n\n"
                f"Your data collecting slot is completed. Kindly collect your data.\n\n"
                f"Cryo-EM Team"
            ),
        )
    return redirect(url_for("admin.datacollecting"))


@admin_bp.route("/datacollecting/delete/<int:booking_id>")
def delete_dc(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM bookings WHERE id=?", [booking_id])
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for("admin.datacollecting"))


# ── Freezing Section ─────────────────────────────────────────────────────────

@admin_bp.route("/freezing")
def freezing_admin():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    today = datetime.date.today()

    cur.execute("SELECT * FROM freezing_bookings WHERE status='active'")
    active = cur.fetchall()
    cur.execute("SELECT * FROM completed_freezing ORDER BY completed_at DESC")
    completed = cur.fetchall()

    conn.commit()
    cur.close()
    conn.close()

    return render_template(
        "admin_freezing.html", active_slots=active, completed_slots=completed
    )


@admin_bp.route("/freezing/complete/<int:booking_id>", methods=["POST"])
def complete_freezing(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    actual_grids = (request.form.get("actual_grids") or request.form.get("number_of_grids", "")).strip()
    grid_source = request.form.get("grid_source", "").strip()
    grid_type = request.form.get("grid_type", "").strip()
    normal_grids = request.form.get("normal_grids", "").strip() or None
    gold_grids = request.form.get("gold_grids", "").strip() or None
    user_category = request.form.get("user_category", "").strip() or None
    if not grid_source:
        flash("Please select the grid source before generating the bill.")
        return redirect(url_for("admin.freezing_admin"))
    has_grid_breakdown = bool(normal_grids or gold_grids)
    if (
        not grid_type
        and not has_grid_breakdown
        and grid_source.casefold() in {"facility", "facility provided"}
    ):
        flash("Please select the grid type for facility-provided grids.")
        return redirect(url_for("admin.freezing_admin"))
    try:
        if not actual_grids or "." in actual_grids:
            raise ValueError
        actual_grids_value = Decimal(actual_grids)
        if actual_grids_value <= 0 or actual_grids_value != actual_grids_value.to_integral_value():
            raise ValueError
    except (InvalidOperation, ValueError):
        flash("Number of grids must be at least 1.")
        return redirect(url_for("admin.freezing_admin"))

    conn = get_db()
    cur = conn.cursor()
    try:
        booking = complete_freezing_booking(
            cur, booking_id, actual_grids_value, grid_source, grid_type, user_category,
            normal_grids, gold_grids,
        )
    except ValueError as exc:
        cur.close()
        conn.close()
        flash(str(exc))
        return redirect(url_for("admin.freezing_admin"))
    if not booking:
        cur.close()
        conn.close()
        flash("That freezing booking is no longer active.")
        return redirect(url_for("admin.freezing_admin"))
    conn.commit()
    cur.close()
    conn.close()
    send_email(
        booking["email"],
        "Cryo-EM Freezing Completed",
        f"Dear {booking['user_name']},\n\nYour freezing on {booking['freezing_date']} is completed.\n\nCryo-EM Team",
    )
    return redirect(url_for("admin.freezing_admin"))


# ── Screening Section ────────────────────────────────────────────────────────

@admin_bp.route("/screening")
def screening_admin():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM screening_bookings WHERE status='waiting'")
    waiting = cur.fetchall()
    cur.execute("SELECT * FROM screening_bookings WHERE status='ongoing'")
    ongoing = cur.fetchall()
    cur.close()
    conn.close()

    return render_template(
        "admin_screening.html",
        waiting_registrations=waiting,
        ongoing_registrations=ongoing,
    )


@admin_bp.route("/screening/load/<int:booking_id>")
def load_sc(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE screening_bookings SET status='ongoing' WHERE id=?", [booking_id])
    cur.execute("SELECT * FROM screening_bookings WHERE id=?", [booking_id])
    reg = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if reg:
        send_email(
            reg["email"],
            "Cryo-EM Screening Slot Loaded",
            f"Dear {reg['user_name']},\n\nYour screening grids are loaded today.\n\nCryo-EM Team",
        )
    return redirect(url_for("admin.screening_admin"))


@admin_bp.route("/screening/complete/<int:booking_id>", methods=["POST"])
def complete_sc(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    actual_slots = request.form.get("actual_slots", "").strip()
    actual_grids = (request.form.get("actual_grids") or request.form.get("number_of_grids", "")).strip()
    grid_source = request.form.get("grid_source", "").strip()
    grid_type = request.form.get("grid_type", "").strip()
    user_category = request.form.get("user_category", "").strip() or None
    service_stage = request.form.get("service_stage", "").strip() or "Screening / Clipping"
    if service_stage != "Screening / Clipping":
        flash("This booking can only be completed as Screening / Clipping.")
        return redirect(url_for("admin.screening_admin"))
    processing_requested = request.form.get("processing_requested") == "1"
    clipped_grids = request.form.get("clipped_grids", "0").strip() or "0"
    if not grid_source:
        flash("Please select the grid source before generating the bill.")
        return redirect(url_for("admin.screening_admin"))
    if not grid_type and grid_source.casefold() in {"facility", "facility provided"}:
        flash("Please select the grid type for facility-provided grids.")
        return redirect(url_for("admin.screening_admin"))
    if not actual_slots or not actual_grids:
        flash("Actual slots and grids are required.")
        return redirect(url_for("admin.screening_admin"))
    try:
        actual_slots_value = Decimal(actual_slots)
        if (actual_slots_value <= 0 or actual_slots_value != actual_slots_value.to_integral_value()
                ):
            raise ValueError
    except (InvalidOperation, ValueError):
        flash("Actual slots must be positive and grids must be a positive integer.")
        return redirect(url_for("admin.screening_admin"))
    try:
        actual_grids_value = parse_number_of_grids(actual_grids)
    except ValueError:
        flash("Number of grids must be at least 1.")
        return redirect(url_for("admin.screening_admin"))
    try:
        clipped_grids_value = int(clipped_grids)
        if clipped_grids_value < 0 or clipped_grids_value > actual_grids_value:
            raise ValueError
    except ValueError:
        flash("Clipped grids must be zero or no more than the actual grids.")
        return redirect(url_for("admin.screening_admin"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM screening_bookings WHERE id=? AND status='ongoing'", [booking_id])
    booking = cur.fetchone()
    if not booking:
        cur.close()
        conn.close()
        flash("That screening booking is no longer ongoing.")
        return redirect(url_for("admin.screening_admin"))
    try:
        charges = calculate_booking_revenue(
            booking, actual_slots_value, actual_grids_value, processing_requested,
            grid_source, grid_type, "Screening / Clipping", user_category,
            clipped_grids_value,
        )
    except ValueError as exc:
        cur.close()
        conn.close()
        flash(str(exc))
        return redirect(url_for("admin.screening_admin"))
    cur.execute(
        """UPDATE screening_bookings SET status='completed', completion_date=?,
           actual_slots=?, actual_grids=?, number_of_grids=?, service_stage=?,
           grid_source=?, grid_type=?, grid_charge=?, handling_charge=?,
           clip_base_charge=?, slot_charge=?, freezing_charge=?, clipping_charge=?,
           processing_charge=?, subtotal=?, gst_amount=?, grand_total=?,
           clipped_grids=?,
           total_billed=?, processing_requested=?, bill_generated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (datetime.date.today(), str(charges["actual_slots"]), charges["actual_grids"],
         charges["number_of_grids"], charges["service_stage"], charges["grid_source"],
         charges["grid_type"], str(charges["grid_charge"]), str(charges["handling_charge"]),
         str(charges["clip_base_charge"]), str(charges["slot_charge"]), str(charges["freezing_charge"]),
         str(charges["clipping_charge"]), str(charges["processing_charge"]), str(charges["subtotal"]),
         str(charges["gst_amount"]), str(charges["grand_total"]), charges["clipped_grids"],
         str(charges["total_billed"]),
         int(processing_requested), booking_id),
    )
    cur.execute("SELECT * FROM screening_bookings WHERE id=?", [booking_id])
    reg = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if reg:
        send_email(
            reg["email"],
            "Cryo-EM Screening Slot Completed",
            (
                f"Dear {reg['user_name']},\n\n"
                f"Your screening slot is completed. Kindly collect your data.\n\n"
                f"Cryo-EM Team"
            ),
        )
    return redirect(url_for("admin.screening_admin"))


@admin_bp.route("/screening/delete/<int:booking_id>")
def delete_sc(booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM screening_bookings WHERE id=?", [booking_id])
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for("admin.screening_admin"))


# ── Registration backup ───────────────────────────────────────────────────────

@admin_bp.route("/registrations.csv")
def download_registrations_csv():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    registrations = []
    for table, registration_type in (
        ("bookings", "Data Collection"),
        ("screening_bookings", "Screening"),
        ("freezing_bookings", "Freezing"),
        ("completed_freezing", "Freezing (completed)"),
    ):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table}")
        for database_row in cur.fetchall():
            row = dict(database_row)
            row["registration_type"] = registration_type
            row["source_table"] = table
            registrations.append(row)
        cur.close()
        conn.close()

    fields = ["registration_type", "source_table"]
    for row in registrations:
        for field in row:
            if field not in fields and field != "password_hash":
                fields.append(field)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {field: row.get(field, "") for field in fields}
        for row in registrations
    )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cryo-registrations-backup.csv"},
    )


def _registration_rows(include_completed=True):
    registrations = []
    tables = (
        ("users", "Users"),
        ("bookings", "Data Collection"),
        ("screening_bookings", "Screening"),
        ("freezing_bookings", "Freezing"),
        ("completed_freezing", "Freezing (completed)"),
    )
    conn = get_db()
    cur = conn.cursor()
    for table, registration_type in tables:
        where = ""
        if not include_completed and table in {"bookings", "screening_bookings"}:
            where = " WHERE status <> 'completed'"
        cur.execute(f"SELECT * FROM {table}{where}")
        for database_row in cur.fetchall():
            row = dict(database_row)
            row["registration_type"] = registration_type
            row["source_table"] = table
            registrations.append(row)
    cur.close()
    conn.close()
    return registrations


def _rows_csv(rows):
    fields = ["registration_type", "source_table"]
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    return output.getvalue()


def _commit_archive_to_github(filename, contents):
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY", "cryo-accem/Cryo-2.0")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not configured")

    path = f"instance/registration_archives/{filename}"
    encoded_path = urllib.parse.quote(path, safe="/")
    api_url = f"https://api.github.com/repos/{repository}/contents/{encoded_path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    sha = None
    try:
        request = urllib.request.Request(
            f"{api_url}?ref={urllib.parse.quote(branch)}",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            sha = json.load(response).get("sha")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise RuntimeError(f"GitHub archive lookup failed with HTTP {error.code}") from error

    payload = {
        "message": f"Archive completed registrations: {filename}",
        "content": base64.b64encode(contents.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"GitHub archive commit failed with HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub archive commit failed with HTTP {error.code}") from error


def _database_backup_bytes():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as backup:
        if _is_sqlite_url():
            conn = get_db()
            try:
                with tempfile.NamedTemporaryFile(suffix=".sqlite3") as snapshot:
                    snapshot_conn = sqlite3.connect(snapshot.name)
                    try:
                        conn.backup(snapshot_conn)
                        snapshot_conn.commit()
                    finally:
                        snapshot_conn.close()
                    snapshot.seek(0)
                    backup.writestr("cryo-database.sqlite3", snapshot.read())
            finally:
                conn.close()
        else:
            backup.writestr("registrations.csv", _rows_csv(_registration_rows()))
    return archive.getvalue()


def _email_database_backup(subject, body, backup_bytes):
    send_email(
        BACKUP_EMAIL,
        subject,
        body,
        attachments=[("cryo-database-backup.zip", "application/zip", backup_bytes)],
    )


@admin_bp.route("/database-backup.zip")
def download_database_backup():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    backup_bytes = _database_backup_bytes()
    _email_database_backup(
        "ACCEM database backup downloaded",
        "A full ACCEM database backup was requested from the admin dashboard. "
        "The backup ZIP is attached.",
        backup_bytes,
    )
    archive = io.BytesIO(backup_bytes)
    archive.seek(0)
    return send_file(archive, as_attachment=True, download_name="cryo-database-backup.zip", mimetype="application/zip")


@admin_bp.route("/archive-completed", methods=["POST"])
def archive_completed_registrations():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    rows = _registration_rows()
    completed = [
        row for row in rows
        if row["source_table"] == "completed_freezing"
        or row.get("status") == "completed"
    ]
    if not completed:
        flash("There are no completed registrations to archive.")
        return redirect(url_for("admin.panel"))

    if not current_app.config.get("GOOGLE_APPS_SCRIPT_URL") or not current_app.config.get(
        "GOOGLE_APPS_SCRIPT_TOKEN"
    ):
        flash(
            "Completed registrations were not removed because the email backup service "
            "is not configured.",
            "error",
        )
        return redirect(url_for("admin.panel"))

    backup_bytes = _database_backup_bytes()
    _email_database_backup(
        "ACCEM database backup before completed-record removal",
        f"A full ACCEM database backup was created before archiving and deleting "
        f"{len(completed)} completed registrations. The backup ZIP containing all "
        "available registration data is attached.",
        backup_bytes,
    )

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings WHERE status='completed'")
    cur.execute("DELETE FROM screening_bookings WHERE status='completed'")
    cur.execute("DELETE FROM freezing_bookings WHERE status='completed'")
    cur.execute("DELETE FROM completed_freezing")
    conn.commit()
    cur.close()
    conn.close()
    flash(
        f"Emailed a full backup to {BACKUP_EMAIL}, then removed "
        f"{len(completed)} completed registrations from the database."
    )
    return redirect(url_for("admin.panel"))


# ── History ──────────────────────────────────────────────────────────────────

_CHARGE_SHEET_TABLES = {
    "imaging": ("bookings", "Data Collection", "completion_date"),
    "screening": ("screening_bookings", "Screening", "completion_date"),
    "freezing": ("completed_freezing", "Freezing", "completed_at"),
}
_PAYMENT_PROOF_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
_PAYMENT_PROOF_MIME_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


def _history_rows(rows):
    """Normalize legacy rows so older production records render safely."""
    defaults = {
        "actual_slots": 0,
        "actual_grids": 0,
        "charge_sheet_sent_at": None,
        "grid_source": "",
        "grid_type": "",
        "payment_status": "Payment Pending",
        "debit_head_status": "Debit Head Pending",
        "payment_proof_path": None,
        "payment_proof_original_name": None,
        "transaction_reference": None,
        "transaction_date": None,
        "amount_received": None,
        "payment_mode": None,
        "proof_received_date": None,
        "admin_remarks": None,
        "debit_head_details": None,
        "pi_email": None,
    }
    normalized = []
    for row in rows:
        item = dict(row)
        for key, default in defaults.items():
            item.setdefault(key, default)
        breakdown = item.get("grid_breakdown")
        if isinstance(breakdown, str):
            try:
                item["grid_breakdown"] = json.loads(breakdown)
            except json.JSONDecodeError:
                item["grid_breakdown"] = {}
        normalized.append(item)
    return normalized


def _grid_breakdown(row):
    breakdown = row.get("grid_breakdown") or {}
    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(breakdown, dict):
        return None, None
    normal = breakdown.get("normal_holey_carbon")
    gold = breakdown.get("gold_carbon_graphene")
    return (normal or None), (gold or None)


def _charge_sheet_record(service_key, booking_id):
    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        return None, None
    table, service, _ = table_info
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE id=?", [booking_id])
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row, service


@admin_bp.route("/history/<service_key>/<int:booking_id>/delete", methods=["POST"])
def delete_completed_booking(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        flash("Unknown booking type.")
        return redirect(url_for("admin.history"))

    table = table_info[0]
    conn = get_db()
    cur = conn.cursor()
    selected_columns = "payment_proof_path" if service_key == "freezing" else "status, payment_proof_path"
    cur.execute(f"SELECT {selected_columns} FROM {table} WHERE id=?", [booking_id])
    row = cur.fetchone()
    if not row or (service_key != "freezing" and row["status"] != "completed"):
        cur.close()
        conn.close()
        flash("That completed booking could not be found.")
        return redirect(url_for("admin.history"))

    proof_path = row["payment_proof_path"]
    cur.execute(f"DELETE FROM {table} WHERE id=?", [booking_id])
    if cur.rowcount != 1:
        conn.rollback()
        cur.close()
        conn.close()
        flash("The booking could not be deleted.")
        return redirect(url_for("admin.history"))
    conn.commit()
    cur.close()
    conn.close()

    if proof_path:
        try:
            os.remove(proof_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            current_app.logger.warning("Could not remove payment proof for deleted booking %s: %s", booking_id, exc)
    flash("Completed booking deleted. Dashboard revenue and history totals have been updated.", "success")
    return redirect(url_for("admin.history"))


@admin_bp.route("/history/<service_key>/<int:booking_id>/edit", methods=["POST"])
def edit_completed_booking(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        flash("Unknown booking type.")
        return redirect(url_for("admin.history"))

    table, service, date_column = table_info
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE id=?", [booking_id])
    row = cur.fetchone()
    if not row or (service_key != "freezing" and row["status"] != "completed"):
        cur.close()
        conn.close()
        flash("That completed booking could not be found.")
        return redirect(url_for("admin.history"))
    if row["charge_sheet_sent_at"]:
        cur.close()
        conn.close()
        flash("Booking details cannot be edited after its charge sheet has been sent.")
        return redirect(url_for("admin.history"))

    updated = dict(row)
    for field, limit in (
        ("user_name", 100), ("pi_name", 100), ("email", 150),
        ("esm", 150), ("sample_name", 150),
    ):
        value = request.form.get(field, "").strip()
        if (field != "esm" and not value) or len(value) > limit:
            cur.close()
            conn.close()
            flash(f"Enter a valid {field.replace('_', ' ')} (maximum {limit} characters).")
            return redirect(url_for("admin.history"))
        updated[field] = value
    if not _valid_email(updated["email"]):
        cur.close()
        conn.close()
        flash("Enter a valid email address.")
        return redirect(url_for("admin.history"))
    pi_email = request.form.get("pi_email", "").strip()
    if pi_email and not _valid_email(pi_email):
        cur.close()
        conn.close()
        flash("Enter a valid PI email address.")
        return redirect(url_for("admin.history"))

    origin = request.form.get("origin", "").strip()
    if origin.casefold() not in {"internal", "external", "academic", "industry", "industrial"}:
        cur.close()
        conn.close()
        flash("Select a valid user origin.")
        return redirect(url_for("admin.history"))
    updated["origin"] = origin

    try:
        requested_grids = parse_number_of_grids(request.form.get("grids"))
        updated["grids"] = requested_grids
        if service_key != "freezing":
            days = parse_number_of_grids(request.form.get("days"))
            if days > 4 or requested_grids > 4:
                raise ValueError("Requested days and grids must not exceed 4.")
            try:
                actual_slots = Decimal(request.form.get("actual_slots", "").strip())
                if (
                    not actual_slots.is_finite()
                    or actual_slots <= 0
                    or actual_slots != actual_slots.to_integral_value()
                ):
                    raise ValueError("Actual slots must be a positive whole number.")
            except InvalidOperation:
                raise ValueError("Actual slots must be a positive whole number.") from None
        else:
            if requested_grids > 8:
                raise ValueError("Requested freezing grids must not exceed 8.")
            days = None
            actual_slots = Decimal("1")
        actual_grids = parse_number_of_grids(request.form.get("actual_grids"))
        completion_date = _parse_date(request.form.get("completion_date", "").strip())
        if not completion_date:
            raise ValueError("Enter a valid completion date.")
        grid_source = request.form.get("grid_source", "").strip()
        grid_type = request.form.get("grid_type", "").strip()
        normal_grids = request.form.get("normal_grids", "").strip() or None
        gold_grids = request.form.get("gold_grids", "").strip() or None
        clipped_grids = request.form.get("clipped_grids", "0").strip() or "0"
        if not clipped_grids.isdigit() or int(clipped_grids) > actual_grids:
            raise ValueError("Clipped grids must be zero or no more than the actual grids.")
        processing_requested = request.form.get("processing_requested") == "1"
        charges = calculate_charge_sheet(
            origin,
            service,
            actual_grids,
            grid_source,
            grid_type,
            actual_slots,
            processing_requested,
            clipped_grids if service_key != "freezing" else 0,
            normal_grids,
            gold_grids,
        )
    except ValueError as exc:
        cur.close()
        conn.close()
        flash(str(exc))
        return redirect(url_for("admin.history"))

    updated.update(charges)
    if is_non_billable_booking(updated):
        for field in (
            "grid_charge", "handling_charge", "clip_base_charge", "slot_charge",
            "processing_charge", "subtotal", "gst", "gst_amount", "grand_total",
            "total_billed", "freezing_charge", "clipping_charge",
        ):
            updated[field] = Decimal("0.00")

    values = {
        "user_name": updated["user_name"],
        "pi_name": updated["pi_name"],
        "email": updated["email"],
        "origin": origin,
        "sample_name": updated["sample_name"],
        "grids": requested_grids,
        "actual_grids": charges["actual_grids"],
        "number_of_grids": charges["number_of_grids"],
        "grid_source": charges["grid_source"],
        "grid_type": charges["grid_type"],
        "grid_charge": str(updated["grid_charge"]),
        "handling_charge": str(updated["handling_charge"]),
        "clip_base_charge": str(updated["clip_base_charge"]),
        "slot_charge": str(updated["slot_charge"]),
        "freezing_charge": str(updated["freezing_charge"]),
        "clipping_charge": str(updated["clipping_charge"]),
        "processing_charge": str(updated["processing_charge"]),
        "subtotal": str(updated["subtotal"]),
        "gst_amount": str(updated["gst_amount"]),
        "grand_total": str(updated["grand_total"]),
        "total_billed": str(updated["total_billed"]),
        "processing_requested": int(processing_requested),
        "clipped_grids": int(clipped_grids) if service_key != "freezing" else 0,
        "bill_generated_at": datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    if service_key != "freezing":
        values.update({
            "esm": updated["esm"],
            "days": days,
            date_column: completion_date.isoformat(),
            "actual_slots": str(actual_slots),
            "service_stage": charges["service_stage"],
        })
    else:
        values.update({
            "freezing_date": completion_date.isoformat(),
            "service_stage": charges["service_stage"],
        })
    if "pi_email" in request.form:
        values["pi_email"] = pi_email or None
    values["grid_breakdown"] = json.dumps({
        "normal_holey_carbon": charges["normal_grids"],
        "gold_carbon_graphene": charges["gold_grids"],
    })

    assignments = ", ".join(f"{column}=?" for column in values)
    cur.execute(
        f"UPDATE {table} SET {assignments} "
        "WHERE id=? AND charge_sheet_sent_at IS NULL",
        [*values.values(), booking_id],
    )
    if cur.rowcount != 1:
        conn.rollback()
        cur.close()
        conn.close()
        flash("The booking could not be edited. Its charge sheet may have been sent.")
        return redirect(url_for("admin.history"))
    conn.commit()
    cur.close()
    conn.close()
    flash("Booking details updated and charges recalculated.", "success")
    return redirect(url_for("admin.history"))


def _valid_email(value):
    return bool(value and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _academic_year(value):
    if hasattr(value, "year") and hasattr(value, "month"):
        year, month = value.year, value.month
    else:
        parsed = str(value or "").strip()[:10]
        try:
            year, month = (int(part) for part in parsed.split("-")[:2])
        except (TypeError, ValueError):
            today = datetime.date.today()
            year, month = today.year, today.month
    return year if month >= 4 else year - 1


def _charge_sheet_category(origin):
    normalized = str(origin or "").strip().casefold()
    if normalized == "internal":
        return "internal"
    if normalized in {"industry", "industrial", "external industry"}:
        return "industrial"
    return "academic"


def _next_charge_sheet_id(cur, origin, completed_date):
    category = _charge_sheet_category(origin)
    academic_year = _academic_year(completed_date)
    code = _CHARGE_SHEET_CATEGORY_CODES[category]
    if _is_sqlite_url():
        cur.execute(
            "INSERT OR IGNORE INTO charge_sheet_sequences "
            "(academic_year, category, next_number) VALUES (?, ?, 0)",
            [0, "all"],
        )
    else:
        cur.execute(
            "INSERT IGNORE INTO charge_sheet_sequences "
            "(academic_year, category, next_number) VALUES (?, ?, 0)",
            [0, "all"],
        )
    cur.execute(
        "UPDATE charge_sheet_sequences SET next_number=next_number + 1 "
        "WHERE academic_year=? AND category=?",
        [0, "all"],
    )
    cur.execute(
        "SELECT next_number FROM charge_sheet_sequences "
        "WHERE academic_year=? AND category=?",
        [0, "all"],
    )
    sequence = cur.fetchone()["next_number"]
    return f"{academic_year}_{code}_{sequence:04d}"


@admin_bp.route("/charge-sheet/<service_key>/<int:booking_id>", methods=["POST"])
def send_charge_sheet(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    row, service = _charge_sheet_record(service_key, booking_id)
    if not row or (service_key != "freezing" and row["status"] != "completed"):
        flash("That completed booking could not be found.")
        return redirect(url_for("admin.history"))


    row = dict(row)
    if is_non_billable_booking(row):
        flash("Charge sheets are not applicable for this PI.")
        return redirect(url_for("admin.history"))
    # Recalculate immediately before rendering/sending.  Stored totals and
    # browser values are never treated as the financial source of truth.
    grid_source = row.get("grid_source") or request.form.get("grid_source", "").strip()
    grid_type = row.get("grid_type") or request.form.get("grid_type", "").strip()
    normal_grids, gold_grids = _grid_breakdown(row)
    if not grid_source:
        flash("Please select the grid source before generating the bill.")
        return redirect(url_for("admin.history"))
    try:
        charges = calculate_charge_sheet(
            row.get("origin", ""), service,
            row.get("number_of_grids") or row.get("actual_grids") or row.get("grids"),
            grid_source, grid_type,
            row.get("actual_slots") or 1,
            bool(row.get("processing_requested")),
            clipped_grids=row.get("clipped_grids") or 0,
            normal_grids=normal_grids,
            gold_grids=gold_grids,
        )
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("admin.history"))
    row.update(charges)
    pi_email = request.form.get("pi_email", "").strip()
    if pi_email and not _valid_email(pi_email):
        flash("Enter a valid PI email address.")
        return redirect(url_for("admin.history"))
    if pi_email:
        row["pi_email"] = pi_email
    conn = get_db()
    cur = conn.cursor()
    charge_sheet_id = row.get("charge_sheet_id")
    if not charge_sheet_id:
        charge_sheet_id = _next_charge_sheet_id(
            cur, row.get("origin", ""), row.get("completion_date")
        )
        row["charge_sheet_id"] = charge_sheet_id

    cc = []
    facility_cc = current_app.config.get("CHARGE_SHEET_CC_EMAIL", "")
    cc.extend(address.strip() for address in facility_cc.split(",") if _valid_email(address.strip()))
    if str(row.get("origin", "")).casefold() == "internal" and _valid_email(row.get("pi_email")):
        cc.append(row["pi_email"])
    cc = list(dict.fromkeys(cc))

    pdf = generate_charge_sheet(row, service)
    filename = f"charge-sheet-{service_key}-{booking_id}.pdf"
    body = (
        f"Dear {row['user_name']},\n\n"
        "Please find attached the Charge Sheet for your Cryo-EM booking.\n\n"
    )
    if str(row.get("origin", "")).casefold() == "internal":
        body += (
            "As this is an Internal booking, kindly provide the appropriate Debit Head "
            "for processing the charges and copy your PI while submitting the Debit Head details.\n\n"
        )
    else:
        body += (
            "Kindly complete the payment using the bank details provided in the attached Charge Sheet. "
            "After completing the transaction, please email the transaction details along with valid proof "
            "of transaction/payment to the Cryo-EM Facility.\n\n"
        )
    body += "Regards,\nCryo-EM Facility"

    send_email(
        row["email"],
        f"Charge Sheet – Cryo-EM Booking #{booking_id}",
        body,
        cc=cc,
        attachments=[(filename, "application/pdf", pdf)],
    )

    cur.execute(
        "UPDATE {} SET number_of_grids=?, service_stage=?, grid_source=?, grid_type=?, "
        "grid_charge=?, handling_charge=?, clip_base_charge=?, slot_charge=?, "
        "freezing_charge=?, clipping_charge=?, processing_charge=?, subtotal=?, "
        "gst_amount=?, grand_total=?, total_billed=?, charge_sheet_id=?, "
        "bill_generated_at=CURRENT_TIMESTAMP "
        "WHERE id=?".format(_CHARGE_SHEET_TABLES[service_key][0]),
        [charges["number_of_grids"], charges["service_stage"], charges["grid_source"],
         charges["grid_type"], str(charges["grid_charge"]), str(charges["handling_charge"]),
         str(charges["clip_base_charge"]), str(charges["slot_charge"]),
         str(charges["freezing_charge"]), str(charges["clipping_charge"]),
         str(charges["processing_charge"]), str(charges["subtotal"]), str(charges["gst_amount"]),
         str(charges["grand_total"]), str(charges["total_billed"]), charge_sheet_id, booking_id],
    )
    if pi_email:
        cur.execute(
            "UPDATE {} SET pi_email=?, charge_sheet_sent_at=CURRENT_TIMESTAMP WHERE id=?".format(
                _CHARGE_SHEET_TABLES[service_key][0]
            ),
            [pi_email, booking_id],
        )
    else:
        cur.execute(
            "UPDATE {} SET charge_sheet_sent_at=CURRENT_TIMESTAMP WHERE id=?".format(
                _CHARGE_SHEET_TABLES[service_key][0]
            ),
            [booking_id],
        )
    conn.commit()
    cur.close()
    conn.close()
    flash("Charge Sheet queued for email delivery.")
    return redirect(url_for("admin.history"))


@admin_bp.route("/charge-sheet/<service_key>/<int:booking_id>/preview", methods=["POST"])
def preview_charge_sheet(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    row, service = _charge_sheet_record(service_key, booking_id)
    if not row or (service_key != "freezing" and row["status"] != "completed"):
        flash("That completed booking could not be found.")
        return redirect(url_for("admin.history"))
    row = dict(row)
    if is_non_billable_booking(row):
        flash("Charge sheets are not applicable for this PI.")
        return redirect(url_for("admin.history"))
    grid_source = row.get("grid_source") or request.form.get("grid_source", "").strip()
    grid_type = row.get("grid_type") or request.form.get("grid_type", "").strip()
    normal_grids, gold_grids = _grid_breakdown(row)
    if not grid_source:
        flash("Please select the grid source before previewing the bill.")
        return redirect(url_for("admin.history"))
    try:
        charges = calculate_charge_sheet(
            row.get("origin", ""), service,
            row.get("number_of_grids") or row.get("actual_grids") or row.get("grids"),
            grid_source, grid_type,
            row.get("actual_slots") or 1,
            bool(row.get("processing_requested")),
            clipped_grids=row.get("clipped_grids") or 0,
            normal_grids=normal_grids,
            gold_grids=gold_grids,
        )
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("admin.history"))
    row.update(charges)
    row["grid_source"] = grid_source
    row["grid_type"] = grid_type
    row["charge_sheet_id"] = row.get("charge_sheet_id") or f"PREVIEW-{service_key.upper()}-{booking_id}"
    pdf = generate_charge_sheet(row, service)
    return send_file(
        io.BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"charge-sheet-preview-{service_key}-{booking_id}.pdf",
    )


@admin_bp.route("/charge-sheet/combined", methods=["POST"])
def send_combined_charge_sheet():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    try:
        selections = json.loads(request.form.get("selected_slots", "[]"))
    except (TypeError, ValueError):
        selections = []
    if not isinstance(selections, list) or not selections:
        flash("Select at least one billable slot to create or resend a combined charge sheet.")
        return redirect(url_for("admin.history"))
    conn = get_db()
    cur = conn.cursor()
    items = []
    try:
        for selection in selections:
            service_key = str(selection.get("service_key", ""))
            table_info = _CHARGE_SHEET_TABLES.get(service_key)
            booking_id = int(selection["booking_id"])
            if not table_info:
                raise ValueError("One of the selected slots is invalid.")
            cur.execute(f"SELECT * FROM {table_info[0]} WHERE id=?", [booking_id])
            row = cur.fetchone()
            if not row or (service_key != "freezing" and row["status"] != "completed"):
                raise ValueError("One of the selected slots is no longer completed.")
            row = dict(row)
            if is_non_billable_booking(row):
                raise ValueError("Only billable slots can be combined.")
            grid_source = str(selection.get("grid_source") or row.get("grid_source") or "").strip()
            grid_type = str(selection.get("grid_type") or row.get("grid_type") or "").strip()
            normal_grids, gold_grids = _grid_breakdown(row)
            if not grid_source:
                raise ValueError("Select a grid source for every selected slot.")
            row.update(calculate_charge_sheet(
                row.get("origin", ""), table_info[1],
                row.get("number_of_grids") or row.get("actual_grids") or row.get("grids"),
                grid_source, grid_type, row.get("actual_slots") or 1,
                bool(row.get("processing_requested")),
                clipped_grids=row.get("clipped_grids") or 0,
                normal_grids=normal_grids,
                gold_grids=gold_grids,
            ))
            row["combined_service"] = table_info[1]
            items.append((service_key, booking_id, row))
    except (KeyError, TypeError, ValueError) as exc:
        cur.close()
        conn.close()
        flash(str(exc))
        return redirect(url_for("admin.history"))
    recipients = {(str(row.get("user_name") or "").strip().casefold(),
                   str(row.get("email") or "").strip().casefold()) for _, _, row in items}
    if len(recipients) != 1 or not _valid_email(next(iter(recipients))[1]):
        cur.close()
        conn.close()
        flash("Select slots belonging to the same user and email address.")
        return redirect(url_for("admin.history"))
    first = items[0][2]
    charge_sheet_id = _next_charge_sheet_id(
        cur, first.get("origin", ""), first.get("completion_date") or first.get("completed_at")
    )
    combined = dict(first)
    combined["charge_sheet_id"] = charge_sheet_id
    combined["combined_items"] = [row for _, _, row in items]
    for field in ("actual_slots", "number_of_grids", "subtotal", "gst_amount", "grand_total", "total_billed"):
        combined[field] = sum((Decimal(str(row.get(field) or 0)) for _, _, row in items), Decimal("0"))
    combined["actual_slots"] = str(combined["actual_slots"])
    combined["number_of_grids"] = int(combined["number_of_grids"])
    send_email(
        first["email"], "Charge Sheet - Cryo-EM Completed Services",
        f"Dear {first['user_name']},\n\nPlease find attached the combined Charge Sheet for your completed Cryo-EM bookings.\n\nRegards,\nCryo-EM Facility",
        attachments=[("charge-sheet-combined.pdf", "application/pdf", generate_charge_sheet(combined, "Combined Services"))],
    )
    for service_key, booking_id, row in items:
        cur.execute(
            f"UPDATE {_CHARGE_SHEET_TABLES[service_key][0]} SET number_of_grids=?, service_stage=?, grid_source=?, grid_type=?, "
            "grid_charge=?, handling_charge=?, clip_base_charge=?, slot_charge=?, freezing_charge=?, clipping_charge=?, "
            "processing_charge=?, subtotal=?, gst_amount=?, grand_total=?, total_billed=?, charge_sheet_id=?, "
            "bill_generated_at=CURRENT_TIMESTAMP, charge_sheet_sent_at=CURRENT_TIMESTAMP WHERE id=?",
            [row["number_of_grids"], row["service_stage"], row["grid_source"], row["grid_type"],
             str(row["grid_charge"]), str(row["handling_charge"]), str(row["clip_base_charge"]), str(row["slot_charge"]),
             str(row["freezing_charge"]), str(row["clipping_charge"]), str(row["processing_charge"]), str(row["subtotal"]),
             str(row["gst_amount"]), str(row["grand_total"]), str(row["total_billed"]), charge_sheet_id, booking_id],
        )
    conn.commit()
    cur.close()
    conn.close()
    flash(f"Combined charge sheet {charge_sheet_id} queued for email delivery.")
    return redirect(url_for("admin.history"))


@admin_bp.route("/payment/<service_key>/<int:booking_id>", methods=["POST"])
def update_payment(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        flash("Unknown booking type.")
        return redirect(url_for("admin.history"))
    table = table_info[0]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT pi_name FROM {table} WHERE id=?", [booking_id])
    booking = cur.fetchone()
    if booking and is_non_billable_booking(booking):
        cur.close()
        conn.close()
        flash("Non-billable bookings are automatically marked completed.")
        return redirect(url_for("admin.history"))
    cur.close()
    conn.close()

    origin = request.form.get("origin", "").strip().casefold()
    internal = origin == "internal"
    status = request.form.get("status", "").strip()
    allowed = (
        {"Debit Head Pending", "Debit Head Received", "Debit Head Verified"}
        if internal else
        {"Payment Pending", "Payment Proof Received", "Payment Verified", "Payment Rejected"}
    )
    if status not in allowed:
        flash("Invalid payment status.")
        return redirect(url_for("admin.history"))
    proof = request.files.get("payment_proof")
    proof_path = None
    proof_name = None
    if proof and proof.filename:
        safe_name = secure_filename(proof.filename)
        extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        if not safe_name or extension not in _PAYMENT_PROOF_EXTENSIONS:
            flash("Payment proof must be a PDF, PNG, JPG, or JPEG file.")
            return redirect(url_for("admin.history"))
        if proof.mimetype != _PAYMENT_PROOF_MIME_TYPES[extension]:
            flash("The payment proof file type does not match its extension.")
            return redirect(url_for("admin.history"))
        proof_directory = current_app.config["PAYMENT_PROOF_DIR"]
        os.makedirs(proof_directory, mode=0o700, exist_ok=True)
        proof_name = safe_name
        proof_path = f"{uuid.uuid4().hex}.{extension}"
        proof.save(os.path.join(proof_directory, proof_path))
    conn = get_db()
    cur = conn.cursor()
    if internal:
        cur.execute(
            f"UPDATE {table} SET debit_head_status=?, debit_head_details=?, admin_remarks=? WHERE id=?",
            [status, request.form.get("debit_head_details", "").strip(),
             request.form.get("admin_remarks", "").strip(), booking_id],
        )
    else:
        cur.execute(
            f"""UPDATE {table} SET payment_status=?, transaction_reference=?, transaction_date=?,
               amount_received=?, payment_mode=?, proof_received_date=?, payment_proof_path=?,
               payment_proof_original_name=?, admin_remarks=? WHERE id=?""",
            [status, request.form.get("transaction_reference", "").strip(),
             request.form.get("transaction_date") or None,
             request.form.get("amount_received") or None,
             request.form.get("payment_mode", "").strip(),
             request.form.get("proof_received_date") or None,
             proof_path, proof_name, request.form.get("admin_remarks", "").strip(), booking_id],
        )
    conn.commit()
    cur.close()
    conn.close()
    flash("Payment tracking details updated.")
    return redirect(url_for("admin.history"))


@admin_bp.route("/payment-proof/<service_key>/<int:booking_id>")
def download_payment_proof(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        flash("Unknown booking type.")
        return redirect(url_for("admin.history"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT payment_proof_path, payment_proof_original_name FROM {table_info[0]} WHERE id=?", [booking_id])
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row["payment_proof_path"]:
        flash("No payment proof is stored for this booking.")
        return redirect(url_for("admin.history"))
    proof_directory = os.path.realpath(current_app.config["PAYMENT_PROOF_DIR"])
    proof_path = os.path.realpath(os.path.join(proof_directory, row["payment_proof_path"]))
    if os.path.dirname(proof_path) != proof_directory or not os.path.isfile(proof_path):
        flash("The stored payment proof is unavailable.")
        return redirect(url_for("admin.history"))
    return send_file(proof_path, as_attachment=True, download_name=secure_filename(row["payment_proof_original_name"] or "payment-proof"))

@admin_bp.route("/history")
def history():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))

    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM bookings WHERE status='completed' ORDER BY completion_date DESC"
    )
    completed_imaging = _history_rows(cur.fetchall())
    cur.execute("SELECT * FROM completed_freezing ORDER BY completed_at DESC")
    completed_freezing = _history_rows(cur.fetchall())
    cur.execute(
        "SELECT * FROM screening_bookings WHERE status='completed' ORDER BY completion_date DESC"
    )
    completed_screening = _history_rows(cur.fetchall())
    cur.close()
    conn.close()

    return render_template(
        "history.html",
        completed_imaging=completed_imaging,
        completed_freezing=completed_freezing,
        completed_screening=completed_screening,
        charge_sheet_tables=_CHARGE_SHEET_TABLES,
        is_non_billable_booking=is_non_billable_booking,
    )
