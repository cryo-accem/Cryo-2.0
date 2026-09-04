import datetime
import csv
import io
import base64
import json
import os
import re
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
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from database import get_db
from database import _is_sqlite_url
from extensions import send_email
from revenue import (
    calculate_booking_revenue, calculate_charge_sheet, is_non_billable_booking,
    parse_number_of_grids,
)
from blueprints.freezing import complete_freezing_booking
from charge_sheet import generate_charge_sheet

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_ALLOWED_ADMIN_ENDPOINTS = {
    "admin.panel",
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
    "admin.update_payment",
    "admin.download_payment_proof",
    "admin.download_registrations_csv",
    "admin.download_database_backup",
    "admin.archive_completed_registrations",
    "admin.billing_preview",
    "static",
}


# ── Session guard ────────────────────────────────────────────────────────────

@admin_bp.before_app_request
def check_admin_session():
    if "admin_logged_in" in session and request.endpoint:
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
            return redirect(url_for("admin.panel"))

        flash("Invalid username or password.", "login")
    return render_template("admin.html")


@admin_bp.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
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
    pi_users = {}
    for table in ("bookings", "screening_bookings", "freezing_bookings"):
        cur.execute(f"SELECT pi_name, user_name, email FROM {table} WHERE pi_name IS NOT NULL AND pi_name <> ''")
        for row in cur.fetchall():
            pi_key = " ".join(row["pi_name"].split()).casefold()
            entry = pi_users.setdefault(pi_key, {"label": " ".join(row["pi_name"].split()), "users": set()})
            entry["users"].add((row["email"] or row["user_name"]).strip().casefold())
    pi_counts = sorted(
        ({"label": entry["label"], "value": len(entry["users"])} for entry in pi_users.values()),
        key=lambda item: (-item["value"], item["label"]),
    )
    if len(pi_counts) > 6:
        other_count = sum(item["value"] for item in pi_counts[6:])
        pi_counts = pi_counts[:6] + [{"label": "Other PIs", "value": other_count}]
    cur.execute("SELECT COUNT(*) AS count FROM bookings WHERE status='waiting'")
    collecting_waiting = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) AS count FROM screening_bookings WHERE status='waiting'")
    screening_waiting = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) AS count FROM freezing_bookings WHERE status='active'")
    freezing_active = cur.fetchone()["count"]
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
        revenue=revenue,
        updated_at=datetime.datetime.now().strftime("%d %b %Y, %H:%M"),
    )


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _revenue_dashboard(cur):
    today = datetime.date.today()
    preset = request.args.get("range", "all")
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

    rows = []
    for table, service in (("bookings", "Data Collection"), ("screening_bookings", "Screening")):
        cur.execute(
            f"""            SELECT pi_name, origin, completion_date, actual_slots, actual_grids,
                       number_of_grids, slot_charge, freezing_charge, clipping_charge,
                       handling_charge, subtotal, processing_charge, gst_amount,
                       grand_total, total_billed
                FROM {table}
                WHERE status='completed'"""
        )
        for row in cur.fetchall():
            if is_non_billable_booking(row):
                continue
            completion_date = _parse_date(str(row["completion_date"])[:10])
            if not completion_date or (start and completion_date < start) or (end and completion_date > end):
                continue
            rows.append((row, service, completion_date))
    cur.execute(
        """SELECT pi_name, origin, completed_at AS completion_date, NULL AS actual_slots,
                  actual_grids, number_of_grids, slot_charge, freezing_charge, clipping_charge,
                  handling_charge, subtotal, processing_charge, gst_amount,
                  grand_total, total_billed
           FROM completed_freezing"""
    )
    for row in cur.fetchall():
        if is_non_billable_booking(row):
            continue
        completion_date = _parse_date(str(row["completion_date"])[:10])
        if not completion_date or (start and completion_date < start) or (end and completion_date > end):
            continue
        rows.append((row, "Freezing", completion_date))

    totals = {
        "net": Decimal("0"), "gst": Decimal("0"), "gross": Decimal("0"),
        "slots": Decimal("0"), "grids": Decimal("0"),
    }
    by_category = {"Internal": Decimal("0"), "External/Academic": Decimal("0"), "Industrial": Decimal("0")}
    by_service = {"Data Collection": Decimal("0"), "Screening": Decimal("0"),
                  "Freezing": Decimal("0"), "Clipping": Decimal("0"),
                  "Handling Charge": Decimal("0"), "Data Processing": Decimal("0")}
    monthly = {}
    for row, service, completion_date in rows:
        slot = _money(row["slot_charge"])
        freezing = _money(row["freezing_charge"])
        clipping = _money(row["clipping_charge"])
        handling = _money(row["handling_charge"])
        processing = _money(row["processing_charge"])
        gst = _money(row["gst_amount"])
        subtotal = _money(row["subtotal"])
        gross = _money(row["grand_total"] or row["total_billed"])
        if not subtotal:
            subtotal = slot + freezing + clipping + handling + processing
        if not gross:
            gross = subtotal + gst
        net = gross - gst
        totals["net"] += net
        totals["gst"] += gst
        totals["gross"] += gross
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
        month = completion_date.strftime("%Y-%m")
        monthly.setdefault(month, {"net": Decimal("0"), "slots": Decimal("0")})
        monthly[month]["net"] += net
        monthly[month]["slots"] += Decimal(str(row["actual_slots"] or 0))

    monthly_items = [
        {"label": key, "value": values["net"].quantize(Decimal("0.01")),
         "slots": values["slots"].quantize(Decimal("0.01"))}
        for key, values in sorted(monthly.items())
    ]
    monthly_max = max((item["value"] for item in monthly_items), default=Decimal("0"))
    if monthly_max:
        for index, item in enumerate(monthly_items):
            item["x"] = 8 + (index * 84 / max(len(monthly_items) - 1, 1))
            item["y"] = 160 - float(item["value"] / monthly_max * 140)
    return {
        "preset": preset,
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "totals": {key: value.quantize(Decimal("0.01")) for key, value in totals.items()},
        "completed_bookings": len(rows),
        "by_category": [{"label": key, "value": value.quantize(Decimal("0.01"))} for key, value in by_category.items()],
        "by_service": [{"label": key, "value": value.quantize(Decimal("0.01"))} for key, value in by_service.items()],
        "monthly": monthly_items,
    }


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
    processing_requested = request.form.get("processing_requested") == "1"
    if not grid_source:
        flash("Please select the grid source before generating the bill.")
        return redirect(url_for("admin.datacollecting"))
    if not grid_type and grid_source.casefold() in {"facility", "facility provided"}:
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
           total_billed=?, processing_requested=?, bill_generated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (datetime.date.today(), str(charges["actual_slots"]), charges["actual_grids"],
         charges["number_of_grids"], charges["service_stage"], charges["grid_source"],
         charges["grid_type"], str(charges["grid_charge"]), str(charges["handling_charge"]),
         str(charges["clip_base_charge"]), str(charges["slot_charge"]), str(charges["freezing_charge"]),
         str(charges["clipping_charge"]), str(charges["processing_charge"]), str(charges["subtotal"]),
         str(charges["gst_amount"]), str(charges["grand_total"]), str(charges["total_billed"]),
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
    user_category = request.form.get("user_category", "").strip() or None
    if not grid_source:
        flash("Please select the grid source before generating the bill.")
        return redirect(url_for("admin.freezing_admin"))
    if not grid_type and grid_source.casefold() in {"facility", "facility provided"}:
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
        booking = complete_freezing_booking(cur, booking_id, actual_grids_value, grid_source, grid_type, user_category)
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
           total_billed=?, processing_requested=?, bill_generated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (datetime.date.today(), str(charges["actual_slots"]), charges["actual_grids"],
         charges["number_of_grids"], charges["service_stage"], charges["grid_source"],
         charges["grid_type"], str(charges["grid_charge"]), str(charges["handling_charge"]),
         str(charges["clip_base_charge"]), str(charges["slot_charge"]), str(charges["freezing_charge"]),
         str(charges["clipping_charge"]), str(charges["processing_charge"]), str(charges["subtotal"]),
         str(charges["gst_amount"]), str(charges["grand_total"]), str(charges["total_billed"]),
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


@admin_bp.route("/database-backup.zip")
def download_database_backup():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as backup:
        if _is_sqlite_url():
            conn = get_db()
            backup.writestr("cryo-database.sqlite3", conn.serialize())
            conn.close()
        else:
            backup.writestr("registrations.csv", _rows_csv(_registration_rows()))
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

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(current_app.instance_path, "registration_archives")
    os.makedirs(backup_dir, exist_ok=True)
    archive_filename = f"completed-registrations-{timestamp}.csv"
    archive_path = os.path.join(backup_dir, archive_filename)
    archive_contents = _rows_csv(completed)
    with open(archive_path, "w", newline="", encoding="utf-8") as archive:
        archive.write(archive_contents)

    github_error = None
    if os.environ.get("GITHUB_TOKEN"):
        try:
            _commit_archive_to_github(archive_filename, archive_contents)
        except RuntimeError as error:
            github_error = str(error)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings WHERE status='completed'")
    cur.execute("DELETE FROM screening_bookings WHERE status='completed'")
    cur.execute("DELETE FROM freezing_bookings WHERE status='completed'")
    cur.execute("DELETE FROM completed_freezing")
    conn.commit()
    cur.close()
    conn.close()
    if github_error:
        flash(
            f"Archived {len(completed)} completed registrations locally and removed them from "
            f"the database, but GitHub synchronization failed: {github_error}",
            "warning",
        )
    elif not os.environ.get("GITHUB_TOKEN"):
        flash(
            f"Archived {len(completed)} completed registrations locally and removed them from "
            "the database. GitHub synchronization is unavailable because GITHUB_TOKEN is not configured.",
            "warning",
        )
    else:
        flash(f"Archived {len(completed)} completed registrations, then removed them from the database.")
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


def _valid_email(value):
    return bool(value and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


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

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE {} SET number_of_grids=?, service_stage=?, grid_source=?, grid_type=?, "
        "grid_charge=?, handling_charge=?, clip_base_charge=?, slot_charge=?, "
        "freezing_charge=?, clipping_charge=?, processing_charge=?, subtotal=?, "
        "gst_amount=?, grand_total=?, total_billed=?, bill_generated_at=CURRENT_TIMESTAMP "
        "WHERE id=?".format(_CHARGE_SHEET_TABLES[service_key][0]),
        [charges["number_of_grids"], charges["service_stage"], charges["grid_source"],
         charges["grid_type"], str(charges["grid_charge"]), str(charges["handling_charge"]),
         str(charges["clip_base_charge"]), str(charges["slot_charge"]),
         str(charges["freezing_charge"]), str(charges["clipping_charge"]),
         str(charges["processing_charge"]), str(charges["subtotal"]), str(charges["gst_amount"]),
         str(charges["grand_total"]), str(charges["total_billed"]), booking_id],
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


@admin_bp.route("/payment/<service_key>/<int:booking_id>", methods=["POST"])
def update_payment(service_key, booking_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    table_info = _CHARGE_SHEET_TABLES.get(service_key)
    if not table_info:
        flash("Unknown booking type.")
        return redirect(url_for("admin.history"))
    table = table_info[0]
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
    completed_imaging = cur.fetchall()
    cur.execute("SELECT * FROM completed_freezing ORDER BY completed_at DESC")
    completed_freezing = cur.fetchall()
    cur.execute(
        "SELECT * FROM screening_bookings WHERE status='completed' ORDER BY completion_date DESC"
    )
    completed_screening = cur.fetchall()
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
