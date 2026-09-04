import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from database import (
    get_db, service_enabled, service_message, validate_registration_fields,
    is_maintenance,
    make_booking_ref,
)
from revenue import pricing_category
from extensions import send_email

imaging_bp = Blueprint("imaging", __name__)


@imaging_bp.route("/list")
def list_view():
    """Public list of ongoing, waiting, and completed imaging slots."""
    conn = get_db()
    cur = conn.cursor()
    public_columns = "booking_ref, registration_date"
    cur.execute(f"SELECT {public_columns} FROM bookings WHERE status='ongoing' ORDER BY registration_date, id")
    ongoing = cur.fetchall()
    cur.execute(f"SELECT {public_columns} FROM bookings WHERE status='waiting' ORDER BY registration_date, id")
    waiting = cur.fetchall()
    cur.execute(f"SELECT {public_columns} FROM bookings WHERE status='completed' ORDER BY completion_date DESC, id DESC")
    completed = cur.fetchall()
    cur.close()
    conn.close()
    return render_template(
        "list.html",
        ongoing_slots=ongoing,
        waiting_slots=waiting,
        completed_slots=completed,
    )


def register_imaging(user_name, pi_name, email, origin, esm, sample_name, grids, days, phone=None):
    """
    Insert a new imaging booking.
    Returns (success: bool, message: str).
    """
    if not service_enabled("datacollecting"):
        return False, service_message("datacollecting")
    if is_maintenance("datacollecting", datetime.date.today()):
        return False, "Data Collection is under maintenance for today."
    try:
        fields = validate_registration_fields(user_name, pi_name, email, sample_name, phone)
        category = pricing_category(origin)
        grids = int(grids)
        days = int(days)
        if grids < 1 or days < 1:
            raise ValueError
    except (TypeError, ValueError):
        return False, "Please provide valid registration details, grids, and days."
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM bookings WHERE lower(email)=? AND lower(sample_name)=? "
        "AND status IN ('waiting','ongoing')",
        [fields["email"], fields["sample_name"].casefold()],
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        return False, "This email is already registered for an Imaging slot."

    cur.execute(
        """INSERT INTO bookings
           (user_name, pi_name, email, origin, esm, sample_name,
            grids, days, registration_date, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')""",
        (fields["user_name"], fields["pi_name"], fields["email"], str(origin).strip(),
         str(esm or "").strip()[:150], fields["sample_name"],
         grids, days, datetime.date.today()),
    )
    booking_id = cur.lastrowid
    cur.execute(
        "UPDATE bookings SET billing_category=?, booking_ref=? WHERE id=?",
        [category, make_booking_ref("DC", booking_id), booking_id],
    )
    conn.commit()
    cur.close()
    conn.close()

    send_email(
        fields["email"],
        "Cryo-EM Imaging Slot Registered",
        (
            f"Dear {fields['user_name']},\n\n"
            f"Your Imaging slot has been registered.\n"
            f"Booking reference: {make_booking_ref('DC', booking_id)}\n"
            f"PI: {pi_name}\nSample: {sample_name}\n"
            f"Grids: {grids}\nDays: {days}\n\n"
            f"Cryo-EM Team"
        ),
    )
    return True, "Imaging slot registered successfully."
