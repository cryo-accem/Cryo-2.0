from flask import Blueprint, render_template, request, redirect, url_for, flash
from blueprints.imaging import register_imaging
from blueprints.freezing import register_freezing
from blueprints.screening import register_screening

register_bp = Blueprint("register", __name__)


@register_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        reg_type     = request.form.get("reg_type", "").strip().lower()
        user_name   = request.form.get("user_name", "")
        pi_name     = request.form.get("pi_name", "")
        email       = request.form.get("email", "")
        origin      = request.form.get("origin", "")
        sample_name = request.form.get("sample_name", "")
        phone      = request.form.get("phone", "")
        if reg_type not in {"datacollecting", "screening", "freezing"}:
            flash("Please select a valid service.", "registration")
            return redirect(url_for("register.register"))

        # ── Data Collecting (was Imaging) ────────────────────────────────────
        if reg_type == "datacollecting":
            esm   = request.form.get("esm", "")
            try:
                grids = int(request.form.get("grids") or 0)
                days = int(request.form.get("days") or 0)
            except (TypeError, ValueError):
                grids = days = 0

            success, message = register_imaging(
                user_name, pi_name, email, origin, esm, sample_name, grids, days, phone
            )
            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("imaging.list_view", success="datacollecting"))

        # ── Screening ────────────────────────────────────────────────────────
        elif reg_type == "screening":
            esm   = request.form.get("esm", "")
            try:
                grids = int(request.form.get("grids") or 0)
            except (TypeError, ValueError):
                grids = 0
            days  = 1

            success, message = register_screening(
                user_name, pi_name, email, origin, esm, sample_name, grids, days, phone
            )
            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("screening.screening_list", success="screening"))

        # ── Freezing ─────────────────────────────────────────────────────────
        elif reg_type == "freezing":
            try:
                grids = int(request.form.get("grids_freezing") or 0)
            except (TypeError, ValueError):
                grids = 0
            freezing_date = request.form.get("freezing_date")

            success, message = register_freezing(
                user_name, pi_name, email, origin, sample_name, grids, freezing_date
            )
            if not success:
                flash(message, "registration")
                return redirect(url_for("register.register"))
            return redirect(url_for("freezing.freezing_schedule", success="freezing"))

    return render_template("register.html")
