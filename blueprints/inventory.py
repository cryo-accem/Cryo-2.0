import csv
import io
import re
import sqlite3

import pymysql
from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template,
    request, session, stream_with_context, url_for,
)

from inventory_service import (
    InventoryError, activity_page, all_grid_types, available_session_grids,
    complete_session_grid, create_data_session, create_grid_type, dashboard_data,
    facility_time, grid_details, iter_export_events, iter_export_grid_rows, list_accounts,
    list_grid_types, list_samples, list_sessions, recent_bookings, register_freezing,
    search_grids, search_storage, session_details, set_grid_type_active, store_grid,
    update_clipping, update_grid_type,
)


inventory_bp = Blueprint("inventory", __name__, url_prefix="/admin/inventory")
inventory_bp.add_app_template_filter(facility_time, "facility_time")


@inventory_bp.before_request
def require_inventory_admin():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin.login"))
    if session.get("must_change_password"):
        return redirect(url_for("admin.change_password"))
    return None


def _actor_id():
    return session.get("admin_user_id")


def _safe_page(value):
    try:
        if value is None or len(str(value)) > 9:
            return 1
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _form_bool(value, label):
    if value not in {"0", "1"}:
        raise InventoryError(f"Choose a valid {label}.")
    return value == "1"


def _integrity_message(exc):
    current_app.logger.warning("Grid inventory storage constraint rejected a write: %s", exc)
    return (
        "The storage position may already be occupied, or this grid already has an "
        "active location. No changes were saved."
    )


def _csv_value(value):
    text = "" if value is None else str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip(" \t").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _csv_response(filename, headings, rows):
    def generate():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headings)
        yield output.getvalue()
        for row in rows:
            output.seek(0)
            output.truncate(0)
            writer.writerow([_csv_value(value) for value in row])
            yield output.getvalue()

    return Response(
        stream_with_context(generate()),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@inventory_bp.route("/")
def dashboard():
    counts, by_type, by_status, recent = dashboard_data()
    return render_template(
        "inventory_dashboard.html",
        counts=counts,
        by_type=by_type,
        by_status=by_status,
        recent=recent,
    )


@inventory_bp.route("/grids")
def grids():
    page = _safe_page(request.args.get("page"))
    try:
        rows, total, page_size, page = search_grids(request.args, page=page)
    except InventoryError as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.grids"))
    types = all_grid_types()
    query_args = request.args.to_dict()
    query_args.pop("page", None)
    export_url = url_for("inventory.export_csv", **query_args)
    previous_url = url_for("inventory.grids", **query_args, page=page - 1) if page > 1 else None
    next_url = (
        url_for("inventory.grids", **query_args, page=page + 1)
        if page < max(1, (total + page_size - 1) // page_size)
        else None
    )
    return render_template(
        "inventory_grids.html",
        rows=rows,
        total=total,
        page=page,
        pages=max(1, (total + page_size - 1) // page_size),
        filters=request.args,
        query_args=query_args,
        export_url=export_url,
        previous_url=previous_url,
        next_url=next_url,
        grid_types=types,
    )


@inventory_bp.route("/register", methods=["GET", "POST"])
def new_grid():
    samples = list_samples(limit=100)
    grid_types = list_grid_types()
    if request.method == "POST":
        try:
            result = register_freezing(request.form, _actor_id())
        except InventoryError as exc:
            flash(str(exc), "error")
        except (sqlite3.IntegrityError, pymysql.err.IntegrityError) as exc:
            current_app.logger.warning("Grid registration rejected by a database constraint: %s", exc)
            if request.form.get("store_immediately") == "1":
                flash(
                    "Initial storage could not be saved; a requested position may already be occupied. "
                    "No sample, batch, or grids were registered.",
                    "error",
                )
            else:
                flash("The sample or grid type could not be validated. No grids were registered.", "error")
        else:
            flash(
                f"Registered {len(result['grid_codes'])} grids in batch {result['batch_code']}.",
                "success",
            )
            return redirect(url_for("inventory.grid_detail", grid_code=result["grid_codes"][0]))
    return render_template(
        "inventory_register.html",
        samples=samples,
        grid_types=grid_types,
        accounts=list_accounts(),
    )


@inventory_bp.route("/grid-types", methods=["GET", "POST"])
def grid_types():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "create":
                create_grid_type(request.form.get("grid_type_name"))
                flash("Grid type added.", "success")
            elif action == "toggle":
                type_id = request.form.get("grid_type_id")
                active = request.form.get("active")
                if not type_id or len(type_id) > 10 or not type_id.isdigit():
                    raise InventoryError("Choose a valid grid type.")
                if active not in {"0", "1"}:
                    raise InventoryError("Choose a valid grid type availability.")
                set_grid_type_active(int(type_id), active == "1")
                flash("Grid type availability updated.", "success")
            else:
                raise InventoryError("Choose a valid grid type action.")
        except InventoryError as exc:
            flash(str(exc), "error")
        except (sqlite3.IntegrityError, pymysql.err.IntegrityError) as exc:
            current_app.logger.warning("Grid type update rejected by a database constraint: %s", exc)
            flash("A grid type with that name already exists.", "error")
        return redirect(url_for("inventory.grid_types"))
    return render_template("inventory_types.html", grid_types=all_grid_types())


@inventory_bp.route("/api/samples")
def samples_json():
    query = (request.args.get("q") or "").strip()
    if len(query) > 180:
        return jsonify({"error": "Search text is too long."}), 400
    samples = list_samples(query=query, limit=30)
    return jsonify([
        {
            "id": sample["sample_id"],
            "code": sample["sample_code"],
            "name": sample["sample_name"],
            "researcher": sample["researcher_name"],
            "lab": sample["lab_name"],
        }
        for sample in samples
    ])


@inventory_bp.route("/grids/<grid_code>")
def grid_detail(grid_code):
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        abort(404)
    details = grid_details(grid_code)
    if not details:
        abort(404)
    grid, locations, sessions, events = details
    active_location = next((item for item in locations if item["removed_at"] is None), None)
    return render_template(
        "inventory_grid_detail.html",
        grid=grid,
        grid_types=all_grid_types(),
        locations=locations,
        active_location=active_location,
        sessions=sessions,
        events=events,
    )


@inventory_bp.route("/grids/<grid_code>/type", methods=["POST"])
def grid_type(grid_code):
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        abort(404)
    try:
        update_grid_type(grid_code, request.form.get("grid_type_id"), _actor_id())
    except InventoryError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Grid type updated for {grid_code}.", "success")
    return redirect(url_for("inventory.grid_detail", grid_code=grid_code))


@inventory_bp.route("/grids/<grid_code>/clipping", methods=["POST"])
def grid_clipping(grid_code):
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        abort(404)
    try:
        clipped = _form_bool(request.form.get("clipped"), "clipping status")
        update_clipping(grid_code, clipped, request.form.get("notes"), _actor_id())
    except InventoryError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Clipping status updated for {grid_code}.", "success")
    return redirect(url_for("inventory.grid_detail", grid_code=grid_code))


@inventory_bp.route("/grids/bulk-clipping", methods=["POST"])
def bulk_clipping():
    codes = list(dict.fromkeys(request.form.getlist("selected_grid_codes")))
    codes = [code for code in codes if re.fullmatch(r"ACCEM-G-\d{4,}", code)]
    if not codes or len(codes) > 200:
        flash("Select between 1 and 200 valid grids for a bulk clipping update.", "error")
        return redirect(url_for("inventory.grids"))
    try:
        clipped = _form_bool(request.form.get("clipped"), "clipping status")
    except InventoryError as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.grids"))
    succeeded = []
    failed = []
    for code in codes:
        try:
            update_clipping(code, clipped, request.form.get("notes"), _actor_id())
        except InventoryError as exc:
            failed.append(f"{code}: {exc}")
        else:
            succeeded.append(code)
    if succeeded:
        flash(
            f"Updated {len(succeeded)} grid(s): {', '.join(succeeded)}.",
            "success",
        )
    if failed:
        flash("Not updated: " + "; ".join(failed), "error")
    return redirect(url_for("inventory.grids"))


@inventory_bp.route("/grids/<grid_code>/storage", methods=["POST"])
def grid_storage(grid_code):
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        abort(404)
    try:
        store_grid(
            grid_code,
            request.form,
            _actor_id(),
            request.form.get("notes"),
        )
    except InventoryError as exc:
        flash(str(exc), "error")
    except (sqlite3.IntegrityError, pymysql.err.IntegrityError) as exc:
        flash(_integrity_message(exc), "error")
    else:
        flash(f"Storage location saved for {grid_code}.", "success")
    return redirect(url_for("inventory.grid_detail", grid_code=grid_code))


@inventory_bp.route("/storage")
def storage():
    try:
        locations = search_storage(request.args)
    except InventoryError as exc:
        flash(str(exc), "error")
        locations = []
    return render_template("inventory_storage.html", locations=locations, filters=request.args)


@inventory_bp.route("/sessions", methods=["GET", "POST"])
def sessions():
    if request.method == "POST":
        try:
            result = create_data_session(request.form, _actor_id())
        except InventoryError as exc:
            flash(str(exc), "error")
        except (sqlite3.IntegrityError, pymysql.err.IntegrityError) as exc:
            current_app.logger.warning("Grid session registration rejected by a constraint: %s", exc)
            flash("The session could not be saved because a referenced record was invalid.", "error")
        else:
            flash(f"Data collection session {result['session_code']} started.", "success")
            return redirect(url_for("inventory.session_detail", session_id=result["session_id"]))
    return render_template(
        "inventory_sessions.html",
        sessions=list_sessions(),
        available_grids=available_session_grids(),
        bookings=recent_bookings(),
    )


@inventory_bp.route("/sessions/<int:session_id>")
def session_detail(session_id):
    session_row, grid_rows = session_details(session_id)
    if not session_row:
        abort(404)
    return render_template(
        "inventory_session_detail.html",
        session=session_row,
        grids=grid_rows,
    )


@inventory_bp.route("/sessions/<int:session_id>/complete/<grid_code>", methods=["POST"])
def complete_grid_session(session_id, grid_code):
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        abort(404)
    outcome = request.form.get("outcome", "")
    try:
        complete_session_grid(
            session_id,
            grid_code,
            outcome,
            request.form,
            _actor_id(),
            request.form.get("notes"),
        )
    except InventoryError as exc:
        flash(str(exc), "error")
    except (sqlite3.IntegrityError, pymysql.err.IntegrityError) as exc:
        flash(_integrity_message(exc), "error")
    else:
        flash(f"{grid_code} outcome recorded as {outcome}.", "success")
    return redirect(url_for("inventory.session_detail", session_id=session_id))


@inventory_bp.route("/activity")
def activity():
    if request.args.get("format") == "csv":
        rows = iter_export_events()
        return _csv_response(
            "grid-inventory-activity.csv",
            ["Grid ID", "Event", "Previous value", "New value", "Event time (UTC)",
             "Notes", "Operation reference", "Session", "Operator"],
            [
                [
                    row["grid_code"], row["event_type"], row["old_value"], row["new_value"],
                    row["event_time"], row["notes"], row["operation_reference"],
                    row["session_code"], row["username"],
                ]
                for row in rows
            ],
        )
    page = _safe_page(request.args.get("page"))
    rows, total, page = activity_page(page)
    page_size = 50
    return render_template(
        "inventory_activity.html",
        events=rows,
        page=page,
        pages=max(1, (total + page_size - 1) // page_size),
        total=total,
    )


@inventory_bp.route("/export.csv")
def export_csv():
    try:
        rows = iter_export_grid_rows(request.args)
    except InventoryError as exc:
        abort(400, description=str(exc))
    return _csv_response(
        "grid-inventory.csv",
        ["Grid ID", "Sample ID", "Sample name", "Grid type", "Freezing date (UTC)",
         "Clipping status", "Lifecycle status", "Falcon container ID", "Grid box",
         "Position", "Latest data collection date (UTC)"],
        [
            [
                row["grid_code"], row["sample_code"], row["sample_name"],
                row["grid_type_name"], row["frozen_at"],
                "Clipped" if row["clipped"] else "Not clipped",
                row["current_status"], row["falcon_container_id"],
                row["grid_box_name"], row["grid_position"], row["latest_session"],
            ]
            for row in rows
        ],
    )
