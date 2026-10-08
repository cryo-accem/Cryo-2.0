"""Send scheduled high-priority reminders for active freezing bookings."""

import argparse
import datetime
import os
from zoneinfo import ZoneInfo

from flask import Flask

from database import get_db
from extensions import init_mail, send_email_sync


IST = ZoneInfo("Asia/Kolkata")
REMINDER_TYPES = {"day_before", "slot_day"}


def _ensure_delivery_table(cur):
    cur.execute(
        """CREATE TABLE IF NOT EXISTS freezing_reminder_deliveries (
               booking_id INT NOT NULL,
               reminder_type VARCHAR(20) NOT NULL,
               sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (booking_id, reminder_type)
           )"""
    )


def send_freezing_reminders(app, reminder_type, today=None):
    """Send one priority digest for due active slots and prevent duplicate sends."""
    if reminder_type not in REMINDER_TYPES:
        raise ValueError("Reminder type must be 'day_before' or 'slot_day'.")

    today = today or datetime.datetime.now(IST).date()
    target_date = today + datetime.timedelta(days=1) if reminder_type == "day_before" else today
    reminder_label = "tomorrow" if reminder_type == "day_before" else "today"

    with app.app_context():
        conn = get_db()
        cur = conn.cursor()
        _ensure_delivery_table(cur)
        conn.commit()
        cur.execute(
            """SELECT booking.id, booking.user_name, booking.pi_name, booking.email,
                      booking.origin, booking.sample_name, booking.grids,
                      booking.freezing_date
               FROM freezing_bookings AS booking
               LEFT JOIN freezing_reminder_deliveries AS delivery
                 ON delivery.booking_id = booking.id
                AND delivery.reminder_type = ?
               WHERE booking.status = 'active'
                 AND booking.freezing_date = ?
                 AND delivery.booking_id IS NULL
               ORDER BY booking.user_name, booking.id""",
            [reminder_type, target_date.isoformat()],
        )
        bookings = cur.fetchall()
        if not bookings:
            cur.close()
            conn.close()
            app.logger.info("No active freezing slots due for %s reminders.", reminder_label)
            return 0

        lines = [
            f"Active Cryo-EM freezing slots scheduled for {target_date:%A, %d %B %Y}:",
            "",
        ]
        for index, booking in enumerate(bookings, start=1):
            lines.extend(
                (
                    f"{index}. {booking['user_name'] or 'Name not provided'}",
                    f"   PI: {booking['pi_name'] or 'Not provided'}",
                    f"   Sample: {booking['sample_name'] or 'Not provided'}",
                    f"   Grids: {booking['grids'] or 0}",
                    f"   Origin: {booking['origin'] or 'Not provided'}",
                    "",
                )
            )
        lines.append("This is the automated high-priority freezing-slot reminder.")

        subject = f"[HIGH PRIORITY] Cryo-EM freezing slots {reminder_label}"
        sent = send_email_sync(
            app.config["FREEZING_REMINDER_EMAIL"],
            subject,
            "\n".join(lines),
            priority="high",
        )
        if not sent:
            cur.close()
            conn.close()
            raise RuntimeError("The freezing reminder email was not accepted by the email relay.")

        for booking in bookings:
            cur.execute(
                """INSERT INTO freezing_reminder_deliveries
                   (booking_id, reminder_type) VALUES (?, ?)""",
                [booking["id"], reminder_type],
            )
        conn.commit()
        cur.close()
        conn.close()
        app.logger.info(
            "Sent %s freezing-slot reminders to %s for %s.",
            len(bookings),
            app.config["FREEZING_REMINDER_EMAIL"],
            target_date,
        )
        return len(bookings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--when",
        required=True,
        choices=sorted(REMINDER_TYPES),
        help="Send the day-before or slot-day reminder.",
    )
    args = parser.parse_args()

    app = Flask(__name__)
    init_mail(app)
    app.config["FREEZING_REMINDER_EMAIL"] = os.environ.get(
        "FREEZING_REMINDER_EMAIL", "cryoem.iisc@gmail.com"
    ).strip()
    if not app.config["FREEZING_REMINDER_EMAIL"]:
        parser.error("FREEZING_REMINDER_EMAIL cannot be empty.")
    send_freezing_reminders(app, args.when)


if __name__ == "__main__":
    main()
