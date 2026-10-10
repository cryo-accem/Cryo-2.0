"""Transactional operations for the cryo-EM grid inventory."""

import datetime as dt
import re
import uuid
from zoneinfo import ZoneInfo

from database import get_db, _is_sqlite_url


FACILITY_TIMEZONE = ZoneInfo("Asia/Kolkata")
ALLOWED_STATUSES = {"Frozen", "Clipped", "Stored", "In Data Collection", "Discarded"}
MAX_GRIDS_PER_BATCH = 200
MAX_SESSION_GRIDS = 200


class InventoryError(ValueError):
    """A validated inventory operation cannot be completed."""


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def parse_datetime(value, label):
    value = (value or "").strip()
    if not value or len(value) > 32:
        raise InventoryError(f"Enter a valid {label}.")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise InventoryError(f"Enter a valid {label}.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=FACILITY_TIMEZONE)
    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def facility_time(value):
    if not value:
        return ""
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(FACILITY_TIMEZONE).strftime("%d %b %Y, %I:%M %p")


def _text(value, label, limit, required=False):
    cleaned = (value or "").strip()
    if required and not cleaned:
        raise InventoryError(f"{label} is required.")
    if len(cleaned) > limit:
        raise InventoryError(f"{label} must be {limit} characters or fewer.")
    return cleaned or None


def _positive_int(value, label, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InventoryError(f"Choose a valid {label}.") from exc
    if parsed < 1 or (maximum is not None and parsed > maximum):
        suffix = f" and no more than {maximum}" if maximum is not None else ""
        raise InventoryError(f"{label} must be at least 1{suffix}.")
    return parsed


def _begin(connection):
    if _is_sqlite_url():
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.begin()


def _next_code(cursor, sequence_name, prefix, width):
    cursor.execute(
        "UPDATE inventory_id_sequences SET next_value=next_value+1 "
        "WHERE sequence_name=?",
        [sequence_name],
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"The {sequence_name} inventory sequence is not installed.")
    lock_clause = "" if _is_sqlite_url() else " FOR UPDATE"
    cursor.execute(
        "SELECT next_value FROM inventory_id_sequences WHERE sequence_name=?" + lock_clause,
        [sequence_name],
    )
    return f"{prefix}{int(cursor.fetchone()['next_value']):0{width}d}"


def _lock_clause():
    return "" if _is_sqlite_url() else " FOR UPDATE"


def _location_label(location):
    if not location:
        return None
    return (
        f"{location['falcon_container_id']} / {location['grid_box_name']} / "
        f"{location['grid_position']}"
    )


def _event(cursor, grid_id, event_type, actor_id, old_value=None, new_value=None,
           notes=None, session_id=None, operation_reference=None, event_time=None):
    cursor.execute(
        """INSERT INTO grid_events
           (grid_id, event_type, old_value, new_value, operator_id, event_time,
            notes, related_session_id, operation_reference)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            grid_id, event_type, old_value, new_value, actor_id,
            event_time or now_utc(), notes, session_id, operation_reference,
        ),
    )


def _create_sample(cursor, fields, actor_id, created_at):
    sample_name = _text(fields.get("sample_name"), "Sample name", 180, True)
    researcher = _text(fields.get("researcher_name"), "Researcher name", 150, True)
    lab = _text(fields.get("lab_name"), "Laboratory or department", 180, True)
    description = _text(fields.get("description"), "Sample description", 4000)
    external_reference = _text(fields.get("external_reference"), "External reference", 120)
    linked_user = _text(fields.get("linked_user_id"), "Existing user", 12)
    linked_user_id = None
    if linked_user:
        if not linked_user.isdigit():
            raise InventoryError("Choose a valid existing user account.")
        linked_user_id = int(linked_user)
        cursor.execute("SELECT id FROM users WHERE id=?", [linked_user_id])
        if not cursor.fetchone():
            raise InventoryError("The selected user account no longer exists.")
    cursor.execute(
        """INSERT INTO inventory_samples
           (sample_code, sample_name, researcher_name, lab_name, description,
            external_reference, linked_user_id, created_at, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f"TMP-{uuid.uuid4().hex[:20]}", sample_name, researcher, lab, description, external_reference,
         linked_user_id, created_at, actor_id),
    )
    sample_id = cursor.lastrowid
    sample_code = f"ACCEM-S-{int(sample_id):05d}"
    cursor.execute(
        "UPDATE inventory_samples SET sample_code=? WHERE sample_id=?",
        [sample_code, sample_id],
    )
    return int(sample_id)


def register_freezing(fields, actor_id):
    count = _positive_int(fields.get("grid_count"), "Grid count", MAX_GRIDS_PER_BATCH)
    frozen_at = parse_datetime(fields.get("frozen_at"), "freezing date and time")
    created_at = now_utc()
    comments = _text(fields.get("comments"), "Comments", 2000)
    notes = _text(fields.get("preparation_notes"), "Preparation notes", 4000)
    new_sample = fields.get("create_sample") == "1"
    store_immediately = fields.get("store_immediately") == "1"
    if fields.get("store_immediately") not in {None, "", "0", "1"}:
        raise InventoryError("Choose whether to store these grids immediately.")
    sample_id = None if new_sample else _positive_int(fields.get("sample_id"), "sample")
    grid_type_id = _positive_int(fields.get("grid_type_id"), "grid type")

    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        if new_sample:
            sample_id = _create_sample(cursor, fields, actor_id, created_at)
        cursor.execute(
            "SELECT sample_id FROM inventory_samples WHERE sample_id=?" + _lock_clause(),
            [sample_id],
        )
        if not cursor.fetchone():
            raise InventoryError("The selected sample no longer exists.")
        cursor.execute(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_id=? AND active=1",
            [grid_type_id],
        )
        if not cursor.fetchone():
            raise InventoryError("Choose an active grid type.")

        batch_code = _next_code(cursor, "batch", "ACCEM-B-", 5)
        cursor.execute(
            """INSERT INTO freezing_batches
               (batch_code, sample_id, frozen_at, grid_type_id, notes, comments,
                created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_code, sample_id, frozen_at, grid_type_id, notes, comments,
             created_at, actor_id),
        )
        batch_id = cursor.lastrowid
        grid_codes = []
        for _ in range(count):
            grid_code = _next_code(cursor, "grid", "ACCEM-G-", 4)
            cursor.execute(
                """INSERT INTO inventory_grids
                   (grid_code, batch_id, sample_id, grid_type_id, current_status,
                    created_at, created_by)
                   VALUES (?, ?, ?, ?, 'Frozen', ?, ?)""",
                (grid_code, batch_id, sample_id, grid_type_id, created_at, actor_id),
            )
            grid_id = cursor.lastrowid
            grid_codes.append(grid_code)
            _event(cursor, grid_id, "grid_registered", actor_id,
                   new_value=grid_code, notes=f"Freezing batch {batch_code}.",
                   event_time=created_at)
            _event(cursor, grid_id, "freezing", actor_id,
                   new_value=frozen_at, notes=notes, event_time=created_at)
            if store_immediately:
                destination = {
                    "falcon_container_id": fields.get(f"storage_{_}_container"),
                    "grid_box_name": fields.get(f"storage_{_}_box"),
                    "grid_position": fields.get(f"storage_{_}_position"),
                }
                _store_grid(
                    cursor,
                    {"grid_id": grid_id, "grid_code": grid_code, "current_status": "Frozen"},
                    destination,
                    actor_id,
                    fields.get(f"storage_{_}_notes"),
                    created_at,
                    operation_reference=batch_code,
                )
        connection.commit()
        return {"batch_code": batch_code, "sample_id": sample_id, "grid_codes": grid_codes}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def update_clipping(grid_code, clipped, notes, actor_id):
    clipping_notes = _text(notes, "Clipping notes", 2000)
    occurred_at = now_utc() if clipped else None
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            "SELECT grid_id, clipped, current_status FROM inventory_grids "
            "WHERE grid_code=?" + _lock_clause(),
            [grid_code],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError("Grid not found.")
        if grid["current_status"] == "Discarded":
            raise InventoryError("A discarded grid cannot be changed.")
        old_clipped = bool(grid["clipped"])
        if old_clipped == bool(clipped):
            raise InventoryError("The grid already has that clipping status.")

        next_status = grid["current_status"]
        if next_status == "Frozen":
            next_status = "Clipped" if clipped else "Frozen"
        elif next_status == "Clipped" and not clipped:
            next_status = "Frozen"
        cursor.execute(
            """UPDATE inventory_grids SET clipped=?, clipped_at=?, clipped_by=?,
               clipping_notes=?, current_status=? WHERE grid_id=?""",
            (1 if clipped else 0, occurred_at, actor_id if clipped else None,
             clipping_notes, next_status, grid["grid_id"]),
        )
        _event(
            cursor, grid["grid_id"], "clipping_status_change", actor_id,
            old_value="Clipped" if old_clipped else "Not clipped",
            new_value="Clipped" if clipped else "Not clipped",
            notes=clipping_notes, event_time=occurred_at or now_utc(),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def update_grid_type(grid_code, grid_type_id, actor_id):
    grid_type_id = _positive_int(grid_type_id, "grid type")
    event_time = now_utc()
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            """SELECT g.grid_id, g.grid_type_id, g.current_status, old_type.grid_type_name
               FROM inventory_grids g
               JOIN grid_types old_type ON old_type.grid_type_id=g.grid_type_id
               WHERE g.grid_code=?""" + _lock_clause(),
            [grid_code],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError("Grid not found.")
        if grid["current_status"] == "Discarded":
            raise InventoryError("A discarded grid cannot be changed.")
        if int(grid["grid_type_id"]) == grid_type_id:
            raise InventoryError("The grid already has that type.")
        cursor.execute(
            "SELECT grid_type_name FROM grid_types WHERE grid_type_id=? AND active=1",
            [grid_type_id],
        )
        new_type = cursor.fetchone()
        if not new_type:
            raise InventoryError("Choose an active grid type.")
        cursor.execute(
            "UPDATE inventory_grids SET grid_type_id=? WHERE grid_id=?",
            (grid_type_id, grid["grid_id"]),
        )
        _event(
            cursor, grid["grid_id"], "grid_type_change", actor_id,
            old_value=grid["grid_type_name"], new_value=new_type["grid_type_name"],
            event_time=event_time,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _current_location(cursor, grid_id, lock=False):
    cursor.execute(
        """SELECT storage_location_id, falcon_container_id, grid_box_name,
                  grid_position, stored_at
           FROM storage_locations
           WHERE grid_id=? AND removed_at IS NULL""" + (_lock_clause() if lock else ""),
        [grid_id],
    )
    return cursor.fetchone()


def _store_grid(cursor, grid, destination, actor_id, notes, event_time,
                session_id=None, operation_reference=None):
    if grid["current_status"] == "Discarded":
        raise InventoryError(f"{grid['grid_code']} is discarded and cannot be stored.")
    if session_id and grid["current_status"] != "In Data Collection":
        raise InventoryError(f"{grid['grid_code']} is not currently in data collection.")
    if not session_id and grid["current_status"] == "In Data Collection":
        raise InventoryError(
            "Complete the active data collection session before returning this grid to storage."
        )
    container = _text(destination.get("falcon_container_id"), "Falcon container ID", 100, True)
    box = _text(destination.get("grid_box_name"), "Grid box", 100, True)
    position = _text(destination.get("grid_position"), "Grid position", 40, True)
    notes = _text(notes, "Storage notes", 2000)
    old_location = _current_location(cursor, grid["grid_id"], lock=True)
    old_label = _location_label(old_location)
    if old_location:
        cursor.execute(
            "UPDATE storage_locations SET removed_at=?, removed_by=? "
            "WHERE storage_location_id=? AND removed_at IS NULL",
            (event_time, actor_id, old_location["storage_location_id"]),
        )
        _event(cursor, grid["grid_id"], "storage_removal", actor_id,
               old_value=old_label, new_value=None, notes="Removed for relocation.",
               session_id=session_id, operation_reference=operation_reference,
               event_time=event_time)
    new_location = f"{container} / {box} / {position}"
    cursor.execute(
        """INSERT INTO storage_locations
           (grid_id, falcon_container_id, grid_box_name, grid_position, stored_at,
            stored_by, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (grid["grid_id"], container, box, position, event_time, actor_id, notes),
    )
    cursor.execute(
        "UPDATE inventory_grids SET current_status='Stored' WHERE grid_id=?",
        [grid["grid_id"]],
    )
    if session_id:
        event_type = "returned_to_storage"
    elif old_location:
        event_type = "storage_relocation"
    elif grid["current_status"] == "Stored":
        event_type = "storage_relocation"
    else:
        event_type = "initial_storage"
    _event(cursor, grid["grid_id"], event_type, actor_id,
           old_value=old_label, new_value=new_location, notes=notes,
           session_id=session_id, operation_reference=operation_reference,
           event_time=event_time)


def store_grid(grid_code, destination, actor_id, notes=None):
    storage_notes = _text(notes, "Storage notes", 2000)
    event_time = now_utc()
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            "SELECT grid_id, grid_code, current_status FROM inventory_grids "
            "WHERE grid_code=?" + _lock_clause(),
            [grid_code],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError("Grid not found.")
        _store_grid(cursor, grid, destination, actor_id, storage_notes, event_time)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def create_data_session(fields, actor_id):
    selected = fields.getlist("grid_codes") if hasattr(fields, "getlist") else fields.get("grid_codes", [])
    if isinstance(selected, str):
        selected = [selected]
    grid_codes = list(dict.fromkeys(value.strip() for value in selected if value.strip()))
    if not grid_codes:
        raise InventoryError("Select at least one grid for this session.")
    if len(grid_codes) > MAX_SESSION_GRIDS:
        raise InventoryError(f"A session can include no more than {MAX_SESSION_GRIDS} grids.")
    if any(not re.fullmatch(r"ACCEM-G-\d{4,}", code) for code in grid_codes):
        raise InventoryError("One or more selected grid IDs are invalid.")
    start_time = parse_datetime(fields.get("start_time"), "session date and time")
    booking_reference = _text(fields.get("booking_reference"), "Booking ID", 12)
    booking_id = None
    if booking_reference:
        if not booking_reference.isdigit():
            raise InventoryError("Booking ID must be a number from the existing booking list.")
        booking_id = int(booking_reference)
    instrument = _text(fields.get("instrument"), "Microscope or instrument", 120)
    notes = _text(fields.get("notes"), "Session notes", 4000)
    event_time = now_utc()
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        if booking_id is not None:
            cursor.execute("SELECT id FROM bookings WHERE id=?", [booking_id])
            if not cursor.fetchone():
                raise InventoryError("The selected booking does not exist.")
        session_code = _next_code(cursor, "session", "ACCEM-DC-", 5)
        cursor.execute(
            """INSERT INTO data_sessions
               (session_code, booking_id, start_time, operator_id, instrument, notes,
                session_status)
               VALUES (?, ?, ?, ?, ?, ?, 'in_progress')""",
            (session_code, booking_id, start_time, actor_id, instrument, notes),
        )
        session_id = cursor.lastrowid
        for grid_code in grid_codes:
            cursor.execute(
                "SELECT grid_id, grid_code, current_status FROM inventory_grids "
                "WHERE grid_code=?" + _lock_clause(),
                [grid_code],
            )
            grid = cursor.fetchone()
            if not grid:
                raise InventoryError(f"Grid {grid_code} was not found.")
            if grid["current_status"] == "Discarded":
                raise InventoryError(f"Discarded grid {grid_code} cannot enter data collection.")
            if grid["current_status"] == "In Data Collection":
                raise InventoryError(f"Grid {grid_code} is already in a data collection session.")

            previous = _current_location(cursor, grid["grid_id"], lock=True)
            previous_label = _location_label(previous)
            if previous:
                cursor.execute(
                    "UPDATE storage_locations SET removed_at=?, removed_by=? "
                    "WHERE storage_location_id=? AND removed_at IS NULL",
                    (event_time, actor_id, previous["storage_location_id"]),
                )
                _event(cursor, grid["grid_id"], "storage_removal", actor_id,
                       old_value=previous_label, notes="Retrieved for data collection.",
                       session_id=session_id, event_time=event_time)
            cursor.execute(
                """INSERT INTO grid_session_links
                   (grid_id, session_id, outcome, notes, recorded_at, recorded_by)
                   VALUES (?, ?, 'in_progress', NULL, ?, ?)""",
                (grid["grid_id"], session_id, event_time, actor_id),
            )
            cursor.execute(
                "UPDATE inventory_grids SET current_status='In Data Collection' WHERE grid_id=?",
                [grid["grid_id"]],
            )
            _event(cursor, grid["grid_id"], "data_collection_started", actor_id,
                   old_value=grid["current_status"], new_value="In Data Collection",
                   notes=f"Session {session_code}; previous location: {previous_label or 'not stored'}.",
                   session_id=session_id, event_time=event_time)
        return_value = {"session_id": int(session_id), "session_code": session_code}
        connection.commit()
        return return_value
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def complete_session_grid(session_id, grid_code, outcome, destination, actor_id, notes=None):
    if outcome not in {"store", "discard"}:
        raise InventoryError("Choose Store or Discard for each grid.")
    outcome_notes = _text(notes, "Outcome notes", 2000)
    event_time = now_utc()
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            "SELECT session_id, session_code, session_status FROM data_sessions "
            "WHERE session_id=?" + _lock_clause(),
            [session_id],
        )
        session_row = cursor.fetchone()
        if not session_row or session_row["session_status"] != "in_progress":
            raise InventoryError("The data collection session is not active.")
        cursor.execute(
            """SELECT g.grid_id, g.grid_code, g.current_status, l.outcome
               FROM inventory_grids g
               JOIN grid_session_links l ON l.grid_id=g.grid_id
               WHERE g.grid_code=? AND l.session_id=?""" + _lock_clause(),
            [grid_code, session_id],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError(f"{grid_code} is not part of this session.")
        if (
            grid["current_status"] != "In Data Collection"
            or grid["outcome"] != "in_progress"
        ):
            raise InventoryError(f"{grid_code} already has a final outcome.")
        if outcome == "store":
            _store_grid(cursor, grid, destination, actor_id, outcome_notes, event_time,
                        session_id=session_id, operation_reference=session_row["session_code"])
            final_text = "Stored"
        else:
            old_location = _current_location(cursor, grid["grid_id"], lock=True)
            old_label = _location_label(old_location)
            if old_location:
                cursor.execute(
                    "UPDATE storage_locations SET removed_at=?, removed_by=? "
                    "WHERE storage_location_id=? AND removed_at IS NULL",
                    (event_time, actor_id, old_location["storage_location_id"]),
                )
                _event(cursor, grid["grid_id"], "storage_removal", actor_id,
                       old_value=old_label, notes="Position released when grid was discarded.",
                       session_id=session_id,
                       operation_reference=session_row["session_code"],
                       event_time=event_time)
            cursor.execute(
                "UPDATE inventory_grids SET current_status='Discarded' WHERE grid_id=?",
                [grid["grid_id"]],
            )
            _event(cursor, grid["grid_id"], "discard", actor_id,
                   old_value=grid["current_status"], new_value="Discarded",
                   notes=outcome_notes, session_id=session_id,
                   operation_reference=session_row["session_code"],
                   event_time=event_time)
            final_text = "Discarded"
        cursor.execute(
            """UPDATE grid_session_links SET outcome=?, notes=?, recorded_at=?, recorded_by=?
               WHERE grid_id=? AND session_id=? AND outcome='in_progress'""",
            (outcome, outcome_notes, event_time, actor_id, grid["grid_id"], session_id),
        )
        _event(cursor, grid["grid_id"], "data_collection_completed", actor_id,
               old_value="In Data Collection", new_value=final_text,
               notes=outcome_notes, session_id=session_id,
               operation_reference=session_row["session_code"], event_time=event_time)
        cursor.execute(
            "SELECT COUNT(*) AS remaining FROM grid_session_links "
            "WHERE session_id=? AND outcome='in_progress'",
            [session_id],
        )
        if int(cursor.fetchone()["remaining"]) == 0:
            cursor.execute(
                "UPDATE data_sessions SET session_status='completed', end_time=? WHERE session_id=?",
                [event_time, session_id],
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def list_grid_types():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT grid_type_id, grid_type_name FROM grid_types "
            "WHERE active=1 ORDER BY grid_type_name"
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def all_grid_types():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT grid_type_id, grid_type_name, active FROM grid_types "
            "ORDER BY grid_type_name"
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def create_grid_type(name):
    grid_type_name = _text(name, "Grid type name", 100, True)
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO grid_types (grid_type_name, active) VALUES (?, 1)",
            [grid_type_name],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def set_grid_type_active(grid_type_id, active):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE grid_types SET active=? WHERE grid_type_id=?",
            (1 if active else 0, grid_type_id),
        )
        if cursor.rowcount != 1:
            raise InventoryError("Grid type not found.")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def list_accounts(limit=200):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, username FROM users ORDER BY username LIMIT ?",
            [limit],
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def list_samples(query="", limit=100):
    connection = get_db()
    cursor = connection.cursor()
    try:
        query = (query or "").strip()
        if query:
            pattern = f"%{query}%"
            cursor.execute(
                """SELECT sample_id, sample_code, sample_name, researcher_name, lab_name
                   FROM inventory_samples
                   WHERE sample_code LIKE ? OR sample_name LIKE ?
                      OR researcher_name LIKE ? OR lab_name LIKE ?
                   ORDER BY sample_name LIMIT ?""",
                (pattern, pattern, pattern, pattern, limit),
            )
        else:
            cursor.execute(
                """SELECT sample_id, sample_code, sample_name, researcher_name, lab_name
                   FROM inventory_samples ORDER BY sample_id DESC LIMIT ?""",
                [limit],
            )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def _grid_filters(args):
    where = []
    values = []
    query = (args.get("q") or "").strip()
    if len(query) > 180:
        raise InventoryError("Search text must be 180 characters or fewer.")
    if query:
        pattern = f"%{query}%"
        where.append(
            "(g.grid_code LIKE ? OR s.sample_code LIKE ? OR s.sample_name LIKE ? "
            "OR s.researcher_name LIKE ? OR gt.grid_type_name LIKE ? "
            "OR sl.falcon_container_id LIKE ? OR sl.grid_box_name LIKE ? "
            "OR sl.grid_position LIKE ?)"
        )
        values.extend([pattern] * 8)
    for key, expression in (
        ("status", "g.current_status=?"),
        ("clipped", "g.clipped=?"),
        ("grid_type_id", "g.grid_type_id=?"),
        ("sample_id", "g.sample_id=?"),
        ("container", "sl.falcon_container_id LIKE ?"),
        ("box", "sl.grid_box_name LIKE ?"),
        ("position", "sl.grid_position LIKE ?"),
    ):
        value = (args.get(key) or "").strip()
        if value:
            if key == "status" and value not in ALLOWED_STATUSES:
                raise InventoryError("Choose a valid lifecycle status filter.")
            if key == "clipped" and value not in {"0", "1"}:
                raise InventoryError("Choose a valid clipping filter.")
            if key == "sample_id":
                if value.isdigit() and len(value) <= 10 and int(value) > 0:
                    where.append("g.sample_id=?")
                    values.append(int(value))
                elif re.fullmatch(r"ACCEM-S-\d{5,}", value):
                    where.append("s.sample_code=?")
                    values.append(value)
                else:
                    raise InventoryError("Enter a valid sample ID or sample code.")
                continue
            if key == "grid_type_id" and (
                len(value) > 10 or not value.isdigit() or int(value) < 1
            ):
                raise InventoryError(f"Choose a valid {key.replace('_id', '')} filter.")
            if key == "grid_type_id":
                _positive_int(value, "grid type filter")
            if key == "container" and len(value) > 100:
                raise InventoryError("Falcon container search must be 100 characters or fewer.")
            if key == "box" and len(value) > 100:
                raise InventoryError("Grid box search must be 100 characters or fewer.")
            if key == "position" and len(value) > 40:
                raise InventoryError("Grid position search must be 40 characters or fewer.")
            where.append(expression)
            values.append(f"%{value}%" if key in {"container", "box", "position"} else value)
    start = (args.get("frozen_from") or "").strip()
    end = (args.get("frozen_to") or "").strip()
    for label, date_value in (("start", start), ("end", end)):
        if date_value:
            try:
                dt.date.fromisoformat(date_value)
            except ValueError as exc:
                raise InventoryError(f"Enter a valid freezing date {label}.") from exc
    if start and end and start > end:
        raise InventoryError("Freezing start date must not be later than the end date.")
    if start:
        where.append("fb.frozen_at >= ?")
        local_start = dt.datetime.combine(
            dt.date.fromisoformat(start), dt.time.min, tzinfo=FACILITY_TIMEZONE
        )
        values.append(
            local_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        )
    if end:
        where.append("fb.frozen_at <= ?")
        local_end = dt.datetime.combine(
            dt.date.fromisoformat(end), dt.time.max.replace(microsecond=0),
            tzinfo=FACILITY_TIMEZONE,
        )
        values.append(
            local_end.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        )
    return (" WHERE " + " AND ".join(where) if where else ""), values


def search_grids(args, page=1, page_size=25):
    where_sql, filter_values = _grid_filters(args)
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    joins = """ FROM inventory_grids g
        JOIN inventory_samples s ON s.sample_id=g.sample_id
        JOIN freezing_batches fb ON fb.batch_id=g.batch_id
        JOIN grid_types gt ON gt.grid_type_id=g.grid_type_id
        LEFT JOIN storage_locations sl ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL """
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) AS total" + joins + where_sql,
            filter_values,
        )
        total = int(cursor.fetchone()["total"])
        page = min(page, max(1, (total + page_size - 1) // page_size))
        cursor.execute(
            """SELECT g.grid_id, g.grid_code, g.current_status, g.clipped,
                      g.clipped_at, s.sample_code, s.sample_name, s.researcher_name,
                      gt.grid_type_name, fb.frozen_at, sl.falcon_container_id,
                      sl.grid_box_name, sl.grid_position
               """ + joins + where_sql +
            " ORDER BY g.grid_id DESC LIMIT ? OFFSET ?",
            [*filter_values, page_size, (page - 1) * page_size],
        )
        return cursor.fetchall(), total, page_size, page
    finally:
        cursor.close()
        connection.close()


def dashboard_data():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT COUNT(*) AS total,
                 SUM(CASE WHEN current_status IN ('Frozen','Clipped') THEN 1 ELSE 0 END) AS frozen,
                 SUM(CASE WHEN clipped=1 THEN 1 ELSE 0 END) AS clipped,
                 SUM(CASE WHEN current_status='Stored' THEN 1 ELSE 0 END) AS stored_count,
                 SUM(CASE WHEN current_status='In Data Collection' THEN 1 ELSE 0 END) AS collecting,
                 SUM(CASE WHEN current_status='Discarded' THEN 1 ELSE 0 END) AS discarded
               FROM inventory_grids"""
        )
        counts = dict(cursor.fetchone())
        counts["stored"] = counts.pop("stored_count")
        cursor.execute(
            """SELECT gt.grid_type_name, COUNT(*) AS count
               FROM inventory_grids g JOIN grid_types gt ON gt.grid_type_id=g.grid_type_id
               GROUP BY gt.grid_type_id, gt.grid_type_name ORDER BY gt.grid_type_name"""
        )
        by_type = cursor.fetchall()
        cursor.execute(
            """SELECT current_status, COUNT(*) AS count FROM inventory_grids
               GROUP BY current_status ORDER BY current_status"""
        )
        by_status = cursor.fetchall()
        cursor.execute(
            """SELECT e.event_type, e.event_time, e.notes, g.grid_code,
                      u.username AS operator_name
               FROM grid_events e JOIN inventory_grids g ON g.grid_id=e.grid_id
               LEFT JOIN users u ON u.id=e.operator_id
               ORDER BY e.event_time DESC, e.event_id DESC LIMIT 12"""
        )
        recent = cursor.fetchall()
        return counts, by_type, by_status, recent
    finally:
        cursor.close()
        connection.close()


def grid_details(grid_code):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT g.*, s.sample_code, s.sample_name, s.researcher_name, s.lab_name,
                      s.description AS sample_description, s.external_reference,
                      linked_user.username AS linked_account_name,
                      fb.batch_code, fb.frozen_at, fb.notes AS preparation_notes,
                      fb.comments AS batch_comments, gt.grid_type_name,
                      creator.username AS created_by_name, clipper.username AS clipped_by_name
               FROM inventory_grids g
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               JOIN freezing_batches fb ON fb.batch_id=g.batch_id
               JOIN grid_types gt ON gt.grid_type_id=g.grid_type_id
               LEFT JOIN users creator ON creator.id=g.created_by
               LEFT JOIN users clipper ON clipper.id=g.clipped_by
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               WHERE g.grid_code=?""",
            [grid_code],
        )
        grid = cursor.fetchone()
        if not grid:
            return None
        cursor.execute(
            """SELECT sl.*, stored.username AS stored_by_name, removed.username AS removed_by_name
               FROM storage_locations sl
               LEFT JOIN users stored ON stored.id=sl.stored_by
               LEFT JOIN users removed ON removed.id=sl.removed_by
               WHERE sl.grid_id=? ORDER BY sl.stored_at DESC, sl.storage_location_id DESC""",
            [grid["grid_id"]],
        )
        locations = cursor.fetchall()
        cursor.execute(
            """SELECT ds.session_id, ds.session_code, ds.start_time, ds.end_time, ds.session_status,
                      ds.instrument, l.outcome, l.notes
               FROM grid_session_links l JOIN data_sessions ds ON ds.session_id=l.session_id
               WHERE l.grid_id=? ORDER BY ds.start_time DESC""",
            [grid["grid_id"]],
        )
        sessions = cursor.fetchall()
        cursor.execute(
            """SELECT e.*, u.username AS operator_name, ds.session_code
               FROM grid_events e LEFT JOIN users u ON u.id=e.operator_id
               LEFT JOIN data_sessions ds ON ds.session_id=e.related_session_id
               WHERE e.grid_id=? ORDER BY e.event_time DESC, e.event_id DESC""",
            [grid["grid_id"]],
        )
        events = cursor.fetchall()
        return grid, locations, sessions, events
    finally:
        cursor.close()
        connection.close()


def search_storage(args):
    where = ["sl.removed_at IS NULL"]
    values = []
    for key, column in (
        ("container", "sl.falcon_container_id"),
        ("box", "sl.grid_box_name"),
        ("position", "sl.grid_position"),
        ("q", "g.grid_code"),
    ):
        value = (args.get(key) or "").strip()
        if value:
            limits = {"container": 100, "box": 100, "position": 40, "q": 24}
            if len(value) > limits[key]:
                raise InventoryError(f"{key.title()} search is too long.")
            where.append(f"{column} LIKE ?")
            values.append(f"%{value}%")
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT sl.storage_location_id, sl.falcon_container_id,
                      sl.grid_box_name, sl.grid_position, sl.stored_at, g.grid_code,
                      g.current_status, s.sample_name, s.researcher_name
               FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               WHERE """ + " AND ".join(where) +
            " ORDER BY sl.falcon_container_id, sl.grid_box_name, sl.grid_position LIMIT 500",
            values,
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def list_sessions():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT ds.session_id, ds.session_code, ds.start_time, ds.end_time,
                      ds.instrument, ds.session_status, u.username AS operator_name,
                      COUNT(l.grid_id) AS grid_count,
                      SUM(CASE WHEN l.outcome='in_progress' THEN 1 ELSE 0 END) AS pending_count
               FROM data_sessions ds
               LEFT JOIN users u ON u.id=ds.operator_id
               LEFT JOIN grid_session_links l ON l.session_id=ds.session_id
               GROUP BY ds.session_id, ds.session_code, ds.start_time, ds.end_time,
                        ds.instrument, ds.session_status, u.username
               ORDER BY ds.start_time DESC LIMIT 100"""
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def available_session_grids(limit=200):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT g.grid_code, g.current_status, s.sample_name
               FROM inventory_grids g JOIN inventory_samples s ON s.sample_id=g.sample_id
               WHERE g.current_status NOT IN ('Discarded','In Data Collection')
               ORDER BY g.grid_id DESC LIMIT ?""",
            [limit],
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def recent_bookings(limit=100):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT id, user_name, sample_name, registration_date, status
               FROM bookings WHERE status IN ('waiting','ongoing')
               ORDER BY registered_at DESC LIMIT ?""",
            [limit],
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def session_details(session_id):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT ds.*, u.username AS operator_name FROM data_sessions ds
               LEFT JOIN users u ON u.id=ds.operator_id WHERE ds.session_id=?""",
            [session_id],
        )
        session_row = cursor.fetchone()
        if not session_row:
            return None, []
        cursor.execute(
            """SELECT g.grid_code, g.current_status, l.outcome, l.notes,
                      l.recorded_at, s.sample_name
               FROM grid_session_links l JOIN inventory_grids g ON g.grid_id=l.grid_id
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               WHERE l.session_id=? ORDER BY g.grid_code""",
            [session_id],
        )
        return session_row, cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def activity_page(page=1, page_size=50):
    page = max(1, int(page))
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) AS total FROM grid_events")
        total = int(cursor.fetchone()["total"])
        page = min(page, max(1, (total + page_size - 1) // page_size))
        cursor.execute(
            """SELECT e.*, g.grid_code, u.username AS operator_name, ds.session_code
               FROM grid_events e JOIN inventory_grids g ON g.grid_id=e.grid_id
               LEFT JOIN users u ON u.id=e.operator_id
               LEFT JOIN data_sessions ds ON ds.session_id=e.related_session_id
               ORDER BY e.event_time DESC, e.event_id DESC LIMIT ? OFFSET ?""",
            [page_size, (page - 1) * page_size],
        )
        return cursor.fetchall(), total, page
    finally:
        cursor.close()
        connection.close()


def iter_export_grid_rows(args, batch_size=500):
    where_sql, values = _grid_filters(args)
    base_query = (
        """SELECT g.grid_code, s.sample_code, s.sample_name, gt.grid_type_name,
                  fb.frozen_at, g.clipped, g.current_status,
                  sl.falcon_container_id, sl.grid_box_name, sl.grid_position,
                  (SELECT MAX(ds.start_time) FROM grid_session_links l
                   JOIN data_sessions ds ON ds.session_id=l.session_id
                   WHERE l.grid_id=g.grid_id) AS latest_session
           FROM inventory_grids g JOIN inventory_samples s ON s.sample_id=g.sample_id
           JOIN freezing_batches fb ON fb.batch_id=g.batch_id
           JOIN grid_types gt ON gt.grid_type_id=g.grid_type_id
           LEFT JOIN storage_locations sl ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
           """ + where_sql + " ORDER BY g.grid_id DESC"
    )

    def generate():
        connection = get_db()
        cursor = connection.cursor()
        offset = 0
        try:
            while True:
                cursor.execute(
                    base_query + " LIMIT ? OFFSET ?",
                    [*values, batch_size, offset],
                )
                batch = cursor.fetchall()
                if not batch:
                    break
                yield from batch
                if len(batch) < batch_size:
                    break
                offset += batch_size
        finally:
            cursor.close()
            connection.close()

    return generate()


def iter_export_events(batch_size=500):
    query = (
        """SELECT g.grid_code, e.event_type, e.old_value, e.new_value, e.event_time,
                  e.notes, e.operation_reference, ds.session_code, u.username
           FROM grid_events e JOIN inventory_grids g ON g.grid_id=e.grid_id
           LEFT JOIN data_sessions ds ON ds.session_id=e.related_session_id
           LEFT JOIN users u ON u.id=e.operator_id
           ORDER BY e.event_time DESC, e.event_id DESC"""
    )

    def generate():
        connection = get_db()
        cursor = connection.cursor()
        offset = 0
        try:
            while True:
                cursor.execute(query + " LIMIT ? OFFSET ?", [batch_size, offset])
                batch = cursor.fetchall()
                if not batch:
                    break
                yield from batch
                if len(batch) < batch_size:
                    break
                offset += batch_size
        finally:
            cursor.close()
            connection.close()

    return generate()
