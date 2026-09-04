import datetime
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash
from database import (
    get_db, service_enabled, service_message, validate_registration_fields,
    is_maintenance,
    make_booking_ref, _is_sqlite_url,
)
from revenue import pricing_category
from extensions import send_email
from revenue import calculate_charge_sheet, is_non_billable_booking

freezing_bp = Blueprint("freezing", __name__)

GRID_LIMIT_PER_DAY = 8


def complete_freezing_booking(cur, booking_id, actual_grids, grid_source="facility",
                              grid_type="normal_holey_carbon", user_category=None):
    """Complete one freezing booking using the grids actually frozen."""
    cur.execute(
        "SELECT * FROM freezing_bookings WHERE id=? AND status='active'",
        [booking_id],
    )
    booking = cur.fetchone()
    if not booking:
        return None
    charges = calculate_charge_sheet(
        booking["billing_category"] or booking["origin"], "Freezing / Grid Registration", actual_grids,
        grid_source, grid_type,
    )
    if is_non_billable_booking(booking):
        for key in ("grid_charge", "handling_charge", "subtotal", "gst", "gst_amount",
                   "grand_total", "total_billed", "freezing_charge"):
           charges[key] = Decimal("0.00")
    cur.execute(
        """INSERT INTO completed_freezing
          (user_name, pi_name, email, origin, sample_name, grids, freezing_date,
           booking_ref, billing_category,
            actual_grids, number_of_grids, service_stage, grid_source, grid_type,
            grid_charge, handling_charge, clip_base_charge, slot_charge,
            freezing_charge, clipping_charge, processing_charge, subtotal,
            gst_amount, grand_total, total_billed, processing_requested,
            bill_generated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (booking["user_name"], booking["pi_name"], booking["email"],
         booking["origin"], booking["sample_name"], booking["grids"],
         booking["freezing_date"], booking["booking_ref"], booking["billing_category"],
         str(actual_grids), charges["number_of_grids"],
         charges["service_stage"], charges["grid_source"], charges["grid_type"],
         str(charges["grid_charge"]), str(charges["handling_charge"]), str(charges["clip_base_charge"]),
         str(charges["slot_charge"]), str(charges["freezing_charge"]), str(charges["clipping_charge"]),
         str(charges["processing_charge"]), str(charges["subtotal"]), str(charges["gst_amount"]),
         str(charges["grand_total"]), str(charges["total_billed"]), 0),
    )
    cur.execute(
        "UPDATE freezing_bookings SET status='completed' WHERE id=?",
        [booking_id],
    )
    return booking


@freezing_bp.route("/freezing_schedule")
def freezing_schedule():
    """
    Auto-expire past active freezing slots → completed_freezing,
    then render active + completed lists.
    """
    conn = get_db()
    cur = conn.cursor()
    today = datetime.date.today()

    cur.execute("SELECT booking_ref, registered_at, freezing_date FROM freezing_bookings WHERE status='active' ORDER BY freezing_date, registered_at")
    active = cur.fetchall()

    cur.execute("SELECT booking_ref, completed_at, freezing_date FROM completed_freezing ORDER BY completed_at DESC")
    completed = cur.fetchall()

    conn.commit()
    cur.close()
    conn.close()

    return render_template(
        "freezingschedule.html", active_slots=active, completed_slots=completed
    )


def register_freezing(user_name, pi_name, email, origin, sample_name, grids, freezing_date):
    """
    Insert a new freezing booking after checking the daily grid cap.
    Returns (success: bool, message: str).
    """
    if not service_enabled("freezing"):
        return False, service_message("freezing")
    try:
        fields = validate_registration_fields(user_name, pi_name, email, sample_name)
        category = pricing_category(origin)
        grids = int(grids)
        freezing_date = datetime.date.fromisoformat(str(freezing_date))
        if grids < 1 or freezing_date < datetime.date.today():
            raise ValueError
    except (TypeError, ValueError):
        return False, "Please provide valid registration details, a future date, and grids."
    if is_maintenance("freezing", freezing_date):
        return False, "Freezing is under maintenance for the selected date."
    conn = get_db()
    cur = conn.cursor()
    try:
        if _is_sqlite_url():
            cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "SELECT COALESCE(SUM(grids),0) AS total FROM freezing_bookings "
            "WHERE freezing_date=? AND status='active'",
            [freezing_date],
        )
        total = int(cur.fetchone()["total"] or 0)
        cur.execute(
            "SELECT id FROM freezing_bookings WHERE lower(email)=? AND freezing_date=? AND status='active'",
            [fields["email"], freezing_date],
        )
        if cur.fetchone():
            conn.rollback()
            return False, "This email is already registered for a Freezing slot on this date."
        if total + grids > GRID_LIMIT_PER_DAY:
            remaining = GRID_LIMIT_PER_DAY - total
            conn.rollback()
            return False, f"Grid limit exceeded. Only {remaining} grids left for this date."
        cur.execute(
            """INSERT INTO freezing_bookings
               (user_name, pi_name, email, origin, sample_name,
                grids, freezing_date, status, billing_category)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (fields["user_name"], fields["pi_name"], fields["email"], str(origin).strip(),
             fields["sample_name"], grids, freezing_date, category),
        )
        booking_id = cur.lastrowid
        cur.execute(
            "UPDATE freezing_bookings SET booking_ref=? WHERE id=?",
            [make_booking_ref("FZ", booking_id), booking_id],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    send_email(
        fields["email"],
        "Cryo-EM Freezing Slot Registered",
        (
            f"Dear {fields['user_name']},\n\n"
            f"Your Freezing slot has been registered.\n"
            f"Booking reference: {make_booking_ref('FZ', booking_id)}\n"
            f"PI: {pi_name}\nSample: {sample_name}\n"
            f"Grids: {grids}\nDate: {freezing_date}\n\n"
            f"Cryo-EM Team"
        ),
    )
    return True, "Freezing slot registered successfully."
