import datetime
from flask import Blueprint, render_template
from database import get_db
from extensions import send_email
from blueprints.public import get_combined_ongoing_slots
from inventory_service import _begin, _lock_clause, link_selected_grids_to_booking

screening_bp = Blueprint("screening", __name__)


@screening_bp.route("/screening")
def screening_list():
    """Public list of ongoing, waiting, and completed screening slots."""
    conn = get_db()
    cur = conn.cursor()
    ongoing = get_combined_ongoing_slots(cur)
    cur.execute("SELECT * FROM screening_bookings WHERE status='waiting'")
    waiting = cur.fetchall()
    cur.execute("SELECT * FROM screening_bookings WHERE status='completed'")
    completed = cur.fetchall()
    cur.close()
    conn.close()
    return render_template(
        "screening_list.html",
        ongoing_slots=ongoing,
        waiting_slots=waiting,
        completed_slots=completed,
    )


def register_screening(user_name, pi_name, email, origin, esm, sample_name, grids, days,
                       grid_codes=None, allow_existing=False):
    """
    Insert a new screening booking.
    Returns (success: bool, message: str).
    """
    days = 1
    conn = get_db()
    cur = conn.cursor()

    try:
        _begin(conn)
        cur.execute(
            "SELECT id, grids FROM screening_bookings "
            "WHERE email=? AND status IN ('waiting','ongoing')"
            + _lock_clause(),
            [email],
        )
        existing_booking = cur.fetchone()
        if existing_booking:
            if not allow_existing or not grid_codes:
                conn.rollback()
                return False, "This email is already registered for a Screening slot."
            booking_id = existing_booking["id"]
            grids = int(existing_booking["grids"] or 0) + len(grid_codes)
            cur.execute(
                "UPDATE screening_bookings SET grids=? WHERE id=?",
                [grids, booking_id],
            )
            link_selected_grids_to_booking(
                cur, "screening", booking_id, grid_codes, user_name
            )
            success_message = (
                f"Added {len(grid_codes)} grids to the existing Screening slot. "
                f"It now has {grids} grids."
            )
            email_subject = "Cryo-EM Screening Slot Updated"
            email_body = (
                f"Dear {user_name},\n\n"
                f"Your existing Screening slot has been updated.\n"
                f"PI: {pi_name}\nSample: {sample_name}\n"
                f"Grids added: {len(grid_codes)}\nTotal grids: {grids}\n\n"
                f"Cryo-EM Team"
            )
        else:
            cur.execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, email, origin, esm, sample_name,
                    grids, days, registration_date, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')""",
                (user_name, pi_name, email, origin, esm, sample_name,
                 grids, days, datetime.date.today()),
            )
            booking_id = cur.lastrowid
            if grid_codes:
                link_selected_grids_to_booking(
                    cur, "screening", booking_id, grid_codes, user_name
                )
            success_message = "Screening slot registered successfully."
            email_subject = "Cryo-EM Screening Slot Registered"
            email_body = (
                f"Dear {user_name},\n\n"
                f"Your Screening slot has been registered.\n"
                f"PI: {pi_name}\nSample: {sample_name}\n"
                f"Grids: {grids}\nDays: {days}\n\n"
                f"Cryo-EM Team"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    send_email(
        email,
        email_subject,
        email_body,
    )
    return True, success_message
