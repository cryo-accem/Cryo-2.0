import datetime
from flask import Blueprint, render_template
from database import (
    get_db, service_enabled, service_message, validate_registration_fields,
    is_maintenance,
    make_booking_ref,
)
from revenue import pricing_category
from extensions import send_email

screening_bp = Blueprint("screening", __name__)


@screening_bp.route("/screening")
def screening_list():
    """Public list of ongoing, waiting, and completed screening slots."""
    conn = get_db()
    cur = conn.cursor()
    public_columns = "booking_ref, registration_date"
    cur.execute(f"SELECT {public_columns} FROM screening_bookings WHERE status='ongoing' ORDER BY registration_date, id")
    ongoing = cur.fetchall()
    cur.execute(f"SELECT {public_columns} FROM screening_bookings WHERE status='waiting' ORDER BY registration_date, id")
    waiting = cur.fetchall()
    cur.execute(f"SELECT {public_columns} FROM screening_bookings WHERE status='completed' ORDER BY completion_date DESC, id DESC")
    completed = cur.fetchall()
    cur.close()
    conn.close()
    return render_template(
        "screening_list.html",
        ongoing_slots=ongoing,
        waiting_slots=waiting,
        completed_slots=completed,
    )


def register_screening(user_name, pi_name, email, origin, esm, sample_name, grids, days, phone=None):
    """
    Insert a new screening booking.
    Returns (success: bool, message: str).
    """
    if not service_enabled("screening"):
        return False, service_message("screening")
    if is_maintenance("screening", datetime.date.today()):
        return False, "Screening is under maintenance for today."
    try:
        fields = validate_registration_fields(user_name, pi_name, email, sample_name, phone)
        category = pricing_category(origin)
        grids = int(grids)
        if grids < 1:
            raise ValueError
    except (TypeError, ValueError):
        return False, "Please provide valid registration details and a positive number of grids."
    days = 1
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM screening_bookings WHERE lower(email)=? AND lower(sample_name)=? "
        "AND status IN ('waiting','ongoing')",
        [fields["email"], fields["sample_name"].casefold()],
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        return False, "This email is already registered for a Screening slot."

    cur.execute(
        """INSERT INTO screening_bookings
           (user_name, pi_name, email, origin, esm, sample_name,
            grids, days, registration_date, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')""",
        (fields["user_name"], fields["pi_name"], fields["email"], str(origin).strip(),
         str(esm or "").strip()[:150], fields["sample_name"],
         grids, days, datetime.date.today()),
    )
    booking_id = cur.lastrowid
    cur.execute(
        "UPDATE screening_bookings SET billing_category=?, booking_ref=? WHERE id=?",
        [category, make_booking_ref("SC", booking_id), booking_id],
    )
    conn.commit()
    cur.close()
    conn.close()

    send_email(
        fields["email"],
        "Cryo-EM Screening Slot Registered",
        (
            f"Dear {fields['user_name']},\n\n"
            f"Your Screening slot has been registered.\n"
            f"Booking reference: {make_booking_ref('SC', booking_id)}\n"
            f"PI: {pi_name}\nSample: {sample_name}\n"
            f"Grids: {grids}\nDays: {days}\n\n"
            f"Cryo-EM Team"
        ),
    )
    return True, "Screening slot registered successfully."
