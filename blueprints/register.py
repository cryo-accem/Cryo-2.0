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


def _booking_grid_selection():
    grid_codes = request.form.getlist("inventory_grid_codes")
    if len(grid_codes) > 4:
        raise InventoryError("Select no more than four inventory grids.")
    if grid_codes:
        return grid_codes, len(grid_codes)
    try:
        grids = int(request.form.get("grids") or 0)
    except ValueError as exc:
        raise InventoryError("Enter a valid number of grids.") from exc
    if not 1 <= grids <= 4:
        raise InventoryError("Enter a grid count from 1 to 4, or select inventory grids.")
    return [], grids


@register_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        reg_type    = request.form["reg_type"]
        user_name   = request.form["user_name"]
        pi_name     = request.form["pi_name"]
        email       = request.form["email"]
        origin      = _ORIGIN_CATEGORIES.get(
            request.form.get("origin", "").strip().casefold()
        )
        if not origin:
            flash("Choose a valid user origin.", "registration")
            return redirect(url_for("register.register"))
        sample_name = request.form["sample_name"]

        # ── Data Collecting (was Imaging) ────────────────────────────────────
        if reg_type == "datacollecting":
            esm   = request.form.get("esm", "")
            try:
                grid_codes, grids = _booking_grid_selection()
                days = int(request.form.get("days") or 0)
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
            esm   = request.form.get("esm", "")
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
            grids         = int(request.form.get("grids_freezing") or 0)
            freezing_date = request.form.get("freezing_date")

            success, message = register_freezing(
                user_name, pi_name, email, origin, sample_name, grids, freezing_date
            )
            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("freezing.freezing_schedule", success="freezing"))

    return render_template("register.html")
