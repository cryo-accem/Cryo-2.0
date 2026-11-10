import re

from flask import Blueprint, render_template, request, redirect, url_for, flash
from blueprints.imaging import register_imaging
from blueprints.freezing import register_freezing
from blueprints.screening import register_screening
from inventory_service import InventoryError

register_bp = Blueprint("register", __name__)
_ORIGIN_CATEGORIES = {
    "internal": "Internal",
    "external": "External",
    "industry": "Industry",
}


# Column sizes from the bookings / screening_bookings / freezing_bookings tables
# (database.py); MySQL strict mode rejects longer values with a server error.
_FIELD_LIMITS = (
    ("user_name", "Name of the user", 100, True),
    ("pi_name", "Name of PI", 100, True),
    ("email", "Email address", 150, True),
    ("sample_name", "Sample name", 150, True),
    ("esm", "Institute name", 150, False),
)
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REG_TYPES = {"datacollecting", "screening", "freezing"}


def _registration_fields():
    """Return trimmed, validated text fields or raise InventoryError."""
    fields = {}
    for name, label, limit, required in _FIELD_LIMITS:
        value = (request.form.get(name) or "").strip()
        if required and not value:
            raise InventoryError(f"{label} is required.")
        if len(value) > limit:
            raise InventoryError(f"{label} must be {limit} characters or fewer.")
        fields[name] = value
    if not _EMAIL_PATTERN.fullmatch(fields["email"]):
        raise InventoryError("Enter a valid email address.")
    return fields


def _booking_grid_selection():
    grid_codes = [code.strip() for code in request.form.getlist("inventory_grid_codes") if code.strip()]
    if len(grid_codes) > 4:
        raise InventoryError("Select no more than four inventory grids.")
    if grid_codes:
        return grid_codes, len(grid_codes)
    try:
        grids = int((request.form.get("grids") or "0").strip())
    except ValueError as exc:
        raise InventoryError("Enter a valid number of grids.") from exc
    if not 1 <= grids <= 4:
        raise InventoryError("Enter a grid count from 1 to 4, or select inventory grids.")
    return [], grids


@register_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        reg_type = request.form.get("reg_type", "")
        if reg_type not in _REG_TYPES:
            flash("Choose a valid booking type.", "registration")
            return redirect(url_for("register.register"))
        try:
            fields = _registration_fields()
        except InventoryError as exc:
            flash(str(exc), "registration")
            return redirect(url_for("register.register"))
        user_name = fields["user_name"]
        pi_name = fields["pi_name"]
        email = fields["email"]
        sample_name = fields["sample_name"]
        origin      = _ORIGIN_CATEGORIES.get(
            request.form.get("origin", "").strip().casefold()
        )
        if not origin:
            flash("Choose a valid user origin.", "registration")
            return redirect(url_for("register.register"))

        # ── Data Collecting (was Imaging) ────────────────────────────────────
        if reg_type == "datacollecting":
            esm   = fields["esm"]
            try:
                grid_codes, grids = _booking_grid_selection()
                try:
                    days = int((request.form.get("days") or "0").strip())
                except ValueError as exc:
                    raise InventoryError("Enter a valid number of days.") from exc
                if not 1 <= days <= 4:
                    raise InventoryError("Enter a number of data-collection days from 1 to 4.")
                success, message = register_imaging(
                    user_name, pi_name, email, origin, esm, sample_name, grids, days,
                    grid_codes=grid_codes,
                )
            except (InventoryError, ValueError) as exc:
                flash(str(exc), "registration")
                return redirect(url_for("register.register"))

            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("imaging.list_view", success="datacollecting"))

        # ── Screening ────────────────────────────────────────────────────────
        elif reg_type == "screening":
            esm   = fields["esm"]
            days  = 1

            try:
                grid_codes, grids = _booking_grid_selection()
                success, message = register_screening(
                    user_name, pi_name, email, origin, esm, sample_name, grids, days,
                    grid_codes=grid_codes,
                )
            except (InventoryError, ValueError) as exc:
                flash(str(exc), "registration")
                return redirect(url_for("register.register"))
            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("screening.screening_list", success="screening"))

        # ── Freezing ─────────────────────────────────────────────────────────
        elif reg_type == "freezing":
            try:
                grids = int((request.form.get("grids_freezing") or "0").strip())
            except ValueError:
                flash("Enter a valid number of grids.", "registration")
                return redirect(url_for("register.register"))
            freezing_date = (request.form.get("freezing_date") or "").strip()

            success, message = register_freezing(
                user_name, pi_name, email, origin, sample_name, grids, freezing_date
            )
            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("freezing.freezing_schedule", success="freezing"))

    return render_template("register.html")
