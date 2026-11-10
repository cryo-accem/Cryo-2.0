"""Transactional operations for the cryo-EM grid inventory."""

import datetime as dt
import re
import uuid
from decimal import Decimal, InvalidOperation
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


def facility_today():
    """Return today's date in the facility's (India) time zone."""
    return dt.datetime.now(FACILITY_TIMEZONE).date()


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


def _storage_position_sort_key(item):
    position = re.split(r"(\d+)", str(item["grid_position"]).casefold())
    natural_position = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in position
        if part
    )
    keys = item.keys()
    falcon_key = (
        "storage_falcon_group" if "storage_falcon_group" in keys
        else "falcon_container_id"
    )
    box_key = "storage_box_group" if "storage_box_group" in keys else "grid_box_name"
    return (
        str(item[falcon_key] or "").casefold(),
        str(item[box_key] or "").casefold(),
        natural_position,
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


def billing_category_for_grid_type_name(name):
    """Map an inventory grid type to its billing category, or None if unknown.

    Billing has two grid categories: normal holey carbon, and gold / carbon
    coated / graphene oxide.  Admin-defined types that match neither are
    left unvalidated.
    """
    value = re.sub(r"[^a-z]+", " ", str(name or "").casefold()).strip()
    if not value:
        return None
    if "gold" in value or "graphene" in value or "carbon coated" in value:
        return "gold_carbon_graphene"
    if "holey" in value or "quantifoil" in value:
        return "normal_holey_carbon"
    return None


def register_completed_freezing(cursor, booking_id, booking, grid_count, fields, actor_id,
                                billed_split=None, billed_at_freezing=False):
    """Create linked inventory records inside the caller's freezing-completion transaction.

    ``billed_split`` is the billed (normal, gold) facility grid counts; when
    given, the per-grid inventory types must agree with it.  When
    ``billed_at_freezing`` is true each grid records that its grid charge was
    billed here, so screening and data collection do not charge it again.
    """
    count = _positive_int(grid_count, "Grid count", MAX_GRIDS_PER_BATCH)
    sample_names = fields.getlist("inventory_grid_sample_name")
    if not sample_names:
        legacy_sample_name = fields.get("inventory_sample_name")
        sample_names = [legacy_sample_name] * count
    if len(sample_names) != count:
        raise InventoryError("Enter a sample name for every grid.")
    sample_names = [
        _text(name, f"Grid {index + 1} sample name", 180, True)
        for index, name in enumerate(sample_names)
    ]
    sample_id_values = fields.getlist("inventory_sample_id")
    if not sample_id_values:
        sample_id_values = [""] * count
    if len(sample_id_values) != count:
        raise InventoryError("Choose an inventory sample or create one for every grid.")
    grid_type_ids = [
        _positive_int(value, "grid type")
        for value in fields.getlist("inventory_grid_type_id")
    ]
    blot_seconds_values = fields.getlist("blot_seconds")
    blot_force_values = fields.getlist("blot_force")
    if not blot_seconds_values:
        blot_seconds_values = [fields.get("blot_seconds", "")] * count
    elif len(blot_seconds_values) == 1 and count > 1:
        blot_seconds_values *= count
    if not blot_force_values:
        blot_force_values = [fields.get("blot_force", "")] * count
    elif len(blot_force_values) == 1 and count > 1:
        blot_force_values *= count
    if len(blot_seconds_values) != count or len(blot_force_values) != count:
        raise InventoryError("Enter blot seconds and force for every grid.")
    blot_conditions = []
    for index, (seconds_value, force_value) in enumerate(
        zip(blot_seconds_values, blot_force_values)
    ):
        try:
            seconds = Decimal((seconds_value or "").strip())
            force = Decimal((force_value or "").strip())
        except (InvalidOperation, AttributeError) as exc:
            raise InventoryError(
                f"Enter valid blot seconds and force for grid {index + 1}."
            ) from exc
        if (
            not seconds.is_finite() or not force.is_finite()
            or seconds <= 0 or force < 0
            or seconds > 999999 or force > 999999
        ):
            raise InventoryError(
                f"Grid {index + 1}: blot seconds must be positive and force cannot be negative."
            )
        if (
            seconds != seconds.quantize(Decimal("0.01"))
            or force != force.quantize(Decimal("0.01"))
        ):
            raise InventoryError(
                f"Grid {index + 1}: blot seconds and force can have at most two decimal places."
            )
        blot_conditions.append((seconds, force))
    containers = fields.getlist("inventory_falcon")
    boxes = fields.getlist("inventory_box")
    positions = fields.getlist("inventory_position")
    if not (
        len(grid_type_ids) == len(containers) == len(boxes) == len(positions) == count
    ):
        raise InventoryError(
            "Enter a grid type, Falcon/container, grid box, and position for every grid."
        )

    cursor.execute(
        "SELECT booking_id FROM freezing_booking_inventory WHERE booking_id=?",
        [booking_id],
    )
    if cursor.fetchone():
        raise InventoryError("Inventory has already been registered for this freezing booking.")
    for grid_type_id in grid_type_ids:
        cursor.execute(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_id=? AND active=1",
            [grid_type_id],
        )
        if not cursor.fetchone():
            raise InventoryError("Choose an active inventory grid type for every grid.")
    if billed_split is not None:
        categories = []
        for grid_type_id in grid_type_ids:
            cursor.execute(
                "SELECT grid_type_name FROM grid_types WHERE grid_type_id=?",
                [grid_type_id],
            )
            categories.append(
                billing_category_for_grid_type_name(cursor.fetchone()["grid_type_name"])
            )
        if all(categories):
            entered_normal = categories.count("normal_holey_carbon")
            entered_gold = categories.count("gold_carbon_graphene")
            billed_normal, billed_gold = billed_split
            if (entered_normal, entered_gold) != (int(billed_normal), int(billed_gold)):
                raise InventoryError(
                    f"The billed grid types ({billed_normal} normal holey carbon, "
                    f"{billed_gold} gold / carbon coated / graphene oxide) do not match "
                    f"the per-grid inventory types ({entered_normal} normal holey carbon, "
                    f"{entered_gold} gold / carbon coated / graphene oxide). "
                    "Correct the billing split or the grid types."
                )

    created_at = now_utc()
    frozen_at = parse_datetime(str(booking["freezing_date"]), "freezing date")
    batch_grid_type_id = grid_type_ids[0]
    sample_ids = {}
    for index, sample_name in enumerate(sample_names):
        sample_key = sample_name.casefold()
        selected_sample_id = sample_id_values[index].strip()
        if selected_sample_id:
            selected_id = _positive_int(selected_sample_id, "sample")
            cursor.execute(
                """SELECT s.sample_id FROM inventory_samples s
                   LEFT JOIN users u ON u.id=s.linked_user_id
                   WHERE s.sample_id=?
                     AND LOWER(TRIM(s.sample_name))=LOWER(TRIM(?))
                     AND (LOWER(TRIM(s.researcher_name))=LOWER(TRIM(?))
                          OR LOWER(TRIM(u.username))=LOWER(TRIM(?)))""",
                [selected_id, sample_name, booking["user_name"], booking["user_name"]],
            )
            selected_sample = cursor.fetchone()
            if not selected_sample:
                raise InventoryError(
                    f"Grid {index + 1}: the selected inventory sample does not match "
                    "this freezing user and sample name."
                )
            sample_ids[f"{sample_key}:{index}"] = int(selected_sample["sample_id"])
            continue
        if sample_key not in sample_ids:
            sample_ids[sample_key] = _create_sample(
                cursor,
                {
                    "sample_name": sample_name,
                    "researcher_name": booking["user_name"] or "Freezing booking",
                    "lab_name": booking["origin"] or "ACCEM",
                    "external_reference": f"FREEZING-{booking_id}",
                },
                actor_id,
                created_at,
            )
    first_sample_key = sample_names[0].casefold()
    batch_sample_id = sample_ids.get(
        f"{first_sample_key}:0", sample_ids.get(first_sample_key)
    )
    batch_code = _next_code(cursor, "batch", "ACCEM-B-", 5)
    cursor.execute(
        """INSERT INTO freezing_batches
           (batch_code, sample_id, frozen_at, grid_type_id, notes, comments,
            created_at, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_code, batch_sample_id, frozen_at, batch_grid_type_id,
            "Per-grid blot seconds and force are recorded on each inventory grid.",
            f"Linked to freezing booking {booking_id}.", created_at, actor_id,
        ),
    )
    batch_id = cursor.lastrowid
    cursor.execute(
        """INSERT INTO freezing_booking_inventory
           (booking_id, batch_id, blot_seconds, blot_force)
           VALUES (?, ?, ?, ?)""",
        (
            booking_id, batch_id,
            str(blot_conditions[0][0]), str(blot_conditions[0][1]),
        ),
    )

    grid_codes = []
    for index in range(count):
        grid_type_id = grid_type_ids[index]
        sample_key = sample_names[index].casefold()
        sample_id = sample_ids.get(
            f"{sample_key}:{index}", sample_ids.get(sample_key)
        )
        blot_seconds, blot_force = blot_conditions[index]
        destination = {
            "falcon_container_id": containers[index],
            "grid_box_name": boxes[index],
            "grid_position": positions[index],
        }
        container = _text(destination["falcon_container_id"], "Falcon/container", 100, True)
        box = _text(destination["grid_box_name"], "Grid box", 100, True)
        position = _text(destination["grid_position"], "Grid position", 40, True)
        grid_code = _next_code(cursor, "grid", "ACCEM-G-", 4)
        cursor.execute(
            """INSERT INTO inventory_grids
               (grid_code, batch_id, sample_id, grid_type_id, blot_seconds, blot_force,
                current_status, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, 'Frozen', ?, ?)""",
            (
                grid_code, batch_id, sample_id, grid_type_id,
                str(blot_seconds), str(blot_force), created_at, actor_id,
            ),
        )
        grid_id = cursor.lastrowid
        grid_codes.append(grid_code)
        _event(cursor, grid_id, "grid_registered", actor_id,
               new_value=grid_code, notes=f"Freezing batch {batch_code}.",
               event_time=created_at)
        _event(cursor, grid_id, "freezing", actor_id,
               new_value=frozen_at,
               notes=f"Blot seconds: {blot_seconds}; blot force: {blot_force}.",
               event_time=created_at)
        if billed_at_freezing:
            _event(cursor, grid_id, "freezing_grid_billed", actor_id,
                   new_value=f"Freezing booking #{booking_id}",
                   notes="Facility grid charge billed at freezing completion.",
                   operation_reference=batch_code, event_time=created_at)
        _store_grid(
            cursor,
            {"grid_id": grid_id, "grid_code": grid_code, "current_status": "Frozen"},
            {
                "falcon_container_id": container,
                "grid_box_name": box,
                "grid_position": position,
            },
            actor_id,
            "Stored during freezing booking completion.",
            created_at,
            operation_reference=batch_code,
        )
    return {"batch_code": batch_code, "sample_id": sample_id, "grid_codes": grid_codes}


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


def discard_grids(grid_codes, notes, actor_id):
    codes = list(dict.fromkeys(str(code).strip() for code in grid_codes if str(code).strip()))
    if not codes or len(codes) > 200:
        raise InventoryError("Select between 1 and 200 grids to discard.")
    if any(not re.fullmatch(r"ACCEM-G-\d{4,}", code) for code in codes):
        raise InventoryError("One or more selected grid IDs are invalid.")
    discard_notes = _text(notes, "Discard notes", 2000)
    event_time = now_utc()
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        for code in codes:
            cursor.execute(
                "SELECT grid_id, grid_code, current_status FROM inventory_grids "
                "WHERE grid_code=?" + _lock_clause(),
                [code],
            )
            grid = cursor.fetchone()
            if not grid:
                raise InventoryError("One of the selected grids was not found.")
            _ensure_grid_not_reserved(cursor, grid["grid_id"], code)
            _discard_grid(
                cursor,
                grid,
                actor_id,
                discard_notes,
                event_time,
                operation_reference="INVENTORY-BULK-DISCARD",
            )
        connection.commit()
        return codes
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _ensure_grid_not_reserved(cursor, grid_id, grid_code):
    cursor.execute(
        """SELECT 1 FROM screening_booking_grids l
           JOIN screening_bookings b ON b.id=l.screening_booking_id
           WHERE l.grid_id=? AND b.status IN ('waiting', 'ongoing')
           UNION ALL
           SELECT 1 FROM data_collection_booking_grids l
           JOIN bookings b ON b.id=l.booking_id
           WHERE l.grid_id=? AND b.status IN ('waiting', 'ongoing')
           LIMIT 1""",
        [grid_id, grid_id],
    )
    if cursor.fetchone():
        raise InventoryError(f"{grid_code} is reserved by an active booking.")


def discard_grid_from_storage(grid_code, notes, actor_id):
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        raise InventoryError("Choose a valid inventory grid.")
    discard_notes = _text(notes, "Discard notes", 2000)
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
        if not _current_location(cursor, grid["grid_id"], lock=True):
            raise InventoryError("This grid has no active storage position.")
        _ensure_grid_not_reserved(cursor, grid["grid_id"], grid_code)
        _discard_grid(
            cursor,
            grid,
            actor_id,
            discard_notes,
            event_time,
            operation_reference="GRID-DETAIL-DISCARD",
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
                session_id=None, operation_reference=None, mark_stored=False,
                allow_microscope_return=False,
                return_event_type="screening_returned_to_storage"):
    if grid["current_status"] == "Discarded":
        raise InventoryError(f"{grid['grid_code']} is discarded and cannot be stored.")
    if session_id and grid["current_status"] != "In Data Collection":
        raise InventoryError(f"{grid['grid_code']} is not currently in data collection.")
    if (
        not session_id
        and not allow_microscope_return
        and grid["current_status"] == "In Data Collection"
    ):
        raise InventoryError(
            "Complete the active data collection session before returning this grid to storage."
        )
    container = _text(destination.get("falcon_container_id"), "Falcon container ID", 100, True)
    box = _text(destination.get("grid_box_name"), "Grid box", 100, True)
    position = _text(destination.get("grid_position"), "Grid position", 40, True)
    notes = _text(notes, "Storage notes", 2000)
    cursor.execute(
        """SELECT g.grid_code FROM storage_locations sl
           JOIN inventory_grids g ON g.grid_id=sl.grid_id
           WHERE LOWER(sl.falcon_container_id)=LOWER(?)
             AND LOWER(sl.grid_box_name)=LOWER(?)
             AND LOWER(sl.grid_position)=LOWER(?) AND sl.removed_at IS NULL
             AND sl.grid_id<>? LIMIT 1""",
        [container, box, position, grid["grid_id"]],
    )
    occupied = cursor.fetchone()
    if occupied:
        raise InventoryError(
            f"That position is already occupied by {occupied['grid_code']}."
        )
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
    if session_id or mark_stored:
        cursor.execute(
            "UPDATE inventory_grids SET current_status='Stored' WHERE grid_id=?",
            [grid["grid_id"]],
        )
    if session_id:
        event_type = "returned_to_storage"
    elif allow_microscope_return:
        event_type = return_event_type
    elif (
        not old_location
        and "batch_id" in grid.keys()
        and grid["batch_id"] is None
    ):
        event_type = "initial_storage"
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


def _discard_grid(
    cursor, grid, actor_id, notes, event_time, operation_reference=None,
    allow_microscope_return=False,
):
    if grid["current_status"] == "Discarded":
        raise InventoryError(f"{grid['grid_code']} is already discarded.")
    if grid["current_status"] == "In Data Collection" and not allow_microscope_return:
        raise InventoryError(
            f"Complete the active microscope session before discarding {grid['grid_code']}."
        )
    cursor.execute(
        """SELECT 1 FROM grid_session_links
           WHERE grid_id=? AND outcome='in_progress' LIMIT 1""",
        [grid["grid_id"]],
    )
    if cursor.fetchone():
        raise InventoryError(
            f"Complete the active data-collection session before discarding {grid['grid_code']}."
        )
    old_location = _current_location(cursor, grid["grid_id"], lock=True)
    old_label = _location_label(old_location)
    if old_location:
        cursor.execute(
            "UPDATE storage_locations SET removed_at=?, removed_by=? "
            "WHERE storage_location_id=? AND removed_at IS NULL",
            (event_time, actor_id, old_location["storage_location_id"]),
        )
        _event(
            cursor, grid["grid_id"], "storage_removal", actor_id,
            old_value=old_label, notes="Removed from storage when grid was discarded.",
            operation_reference=operation_reference, event_time=event_time,
        )
    cursor.execute(
        "UPDATE inventory_grids SET current_status='Discarded' WHERE grid_id=?",
        [grid["grid_id"]],
    )
    _event(
        cursor, grid["grid_id"], "grid_discarded", actor_id,
        old_value=grid["current_status"], new_value="Discarded",
        notes=notes or "Grid marked discarded.",
        operation_reference=operation_reference, event_time=event_time,
    )


def link_grid_to_booking(fields, actor_id):
    service_type = (fields.get("service_type") or "").strip()
    link_targets = {
        "screening": ("screening_booking_grids", "screening_booking_id", "screening_bookings", "Screening"),
        "data_collection": ("data_collection_booking_grids", "booking_id", "bookings", "Data collection"),
    }
    if service_type not in link_targets:
        raise InventoryError("Choose a valid booking type.")
    link_table, booking_column, booking_table, service_label = link_targets[service_type]
    booking_id = _positive_int(fields.get("booking_id"), "booking")
    grid_code = _text(fields.get("grid_code"), "Grid ID", 24, True)
    if not re.fullmatch(r"ACCEM-G-\d{4,}", grid_code):
        raise InventoryError("Choose a valid inventory grid.")

    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            f"SELECT id FROM {booking_table} WHERE id=? AND status IN ('waiting','ongoing')"
            + _lock_clause(),
            [booking_id],
        )
        if not cursor.fetchone():
            raise InventoryError("Choose a waiting or ongoing booking.")
        cursor.execute(
            "SELECT grid_id, grid_code, current_status FROM inventory_grids "
            "WHERE grid_code=?" + _lock_clause(),
            [grid_code],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError("Grid not found.")
        if grid["current_status"] not in {"Frozen", "Clipped", "Stored"}:
            raise InventoryError(
                f"{grid_code} is not available to link; its status is {grid['current_status']}."
            )
        cursor.execute(
            """SELECT 1 FROM grid_session_links
               WHERE grid_id=? AND outcome='in_progress' LIMIT 1""",
            [grid["grid_id"]],
        )
        if cursor.fetchone():
            raise InventoryError(f"{grid_code} is already in data collection.")
        cursor.execute(
            """SELECT 1 FROM screening_booking_grids l
               JOIN screening_bookings b ON b.id=l.screening_booking_id
               WHERE l.grid_id=? AND b.status IN ('waiting','ongoing')
               UNION ALL
               SELECT 1 FROM data_collection_booking_grids l
               JOIN bookings b ON b.id=l.booking_id
               WHERE l.grid_id=? AND b.status IN ('waiting','ongoing')
               LIMIT 1""",
            [grid["grid_id"], grid["grid_id"]],
        )
        if cursor.fetchone():
            raise InventoryError(f"{grid_code} is already linked to another active booking.")

        linked_at = now_utc()
        cursor.execute(
            f"INSERT INTO {link_table} ({booking_column}, grid_id, linked_at, linked_by) "
            "VALUES (?, ?, ?, ?)",
            [booking_id, grid["grid_id"], linked_at, actor_id],
        )
        _event(
            cursor,
            grid["grid_id"],
            f"{service_type}_booking_linked",
            actor_id,
            new_value=f"{service_label} booking #{booking_id}",
            notes="Linked from the grid inventory record.",
            operation_reference=f"{service_type.upper()}-{booking_id}",
            event_time=linked_at,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def available_grid_bookings():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT 'data_collection' AS service_type, id AS booking_id,
                      user_name, sample_name, grids, status
               FROM bookings WHERE status IN ('waiting','ongoing')
               UNION ALL
               SELECT 'screening' AS service_type, id AS booking_id,
                      user_name, sample_name, grids, status
               FROM screening_bookings WHERE status IN ('waiting','ongoing')
               ORDER BY service_type, booking_id DESC"""
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def unlink_grid_from_booking(fields, actor_id):
    service_type = (fields.get("service_type") or "").strip()
    link_targets = {
        "screening": ("screening_booking_grids", "screening_booking_id", "screening_bookings", "Screening"),
        "data_collection": ("data_collection_booking_grids", "booking_id", "bookings", "Data collection"),
    }
    if service_type not in link_targets:
        raise InventoryError("Choose a valid booking type.")
    link_table, booking_column, booking_table, service_label = link_targets[service_type]
    booking_id = _positive_int(fields.get("booking_id"), "booking")
    grid_code = _text(fields.get("grid_code"), "Grid ID", 24, True)
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            f"SELECT id FROM {booking_table} WHERE id=? AND status IN ('waiting','ongoing')"
            + _lock_clause(),
            [booking_id],
        )
        if not cursor.fetchone():
            raise InventoryError("Only a waiting or ongoing booking can be changed.")
        cursor.execute(
            "SELECT grid_id FROM inventory_grids WHERE grid_code=?" + _lock_clause(),
            [grid_code],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError("Grid not found.")
        cursor.execute(
            f"DELETE FROM {link_table} WHERE {booking_column}=? AND grid_id=?",
            [booking_id, grid["grid_id"]],
        )
        if cursor.rowcount != 1:
            raise InventoryError("This grid is not linked to that booking.")
        _event(
            cursor,
            grid["grid_id"],
            f"{service_type}_booking_unlinked",
            actor_id,
            old_value=f"{service_label} booking #{booking_id}",
            notes="Booking link removed from the grid inventory record.",
            operation_reference=f"{service_type.upper()}-{booking_id}",
            event_time=now_utc(),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def link_selected_grids_to_booking(
    cursor, service_type, booking_id, grid_codes, researcher_name
):
    link_targets = {
        "screening": ("screening_booking_grids", "screening_booking_id", "SCREENING"),
        "data_collection": ("data_collection_booking_grids", "booking_id", "DATA-COLLECTION"),
    }
    if service_type not in link_targets:
        raise InventoryError("Choose a valid booking type.")
    link_table, booking_column, operation_prefix = link_targets[service_type]
    codes = [str(code).strip() for code in grid_codes if str(code).strip()]
    owner_name = (researcher_name or "").strip()
    if codes and not owner_name:
        raise InventoryError("Enter the username associated with the selected grids.")
    if len(codes) != len(set(codes)):
        raise InventoryError("A grid can only be selected once.")
    for code in codes:
        if not re.fullmatch(r"ACCEM-G-\d{4,}", code):
            raise InventoryError("Choose a valid inventory grid.")
        cursor.execute(
            """SELECT g.grid_id, g.grid_code, g.current_status
               FROM inventory_grids g
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users u ON u.id=s.linked_user_id
               WHERE g.grid_code=?
                 AND (LOWER(TRIM(s.researcher_name))=LOWER(TRIM(?))
                      OR LOWER(TRIM(u.username))=LOWER(TRIM(?)))"""
            + _lock_clause(),
            [code, owner_name, owner_name],
        )
        grid = cursor.fetchone()
        if not grid:
            raise InventoryError(
                f"{code} is not associated with the entered username."
            )
        if grid["current_status"] not in {"Frozen", "Clipped", "Stored"}:
            raise InventoryError(
                f"{code} is not available to book; its status is {grid['current_status']}."
            )
        cursor.execute(
            """SELECT 1 FROM grid_session_links
               WHERE grid_id=? AND outcome='in_progress' LIMIT 1""",
            [grid["grid_id"]],
        )
        if cursor.fetchone():
            raise InventoryError(f"{code} is already in data collection.")
        cursor.execute(
            """SELECT 1 FROM screening_booking_grids l
               JOIN screening_bookings b ON b.id=l.screening_booking_id
               WHERE l.grid_id=? AND b.status IN ('waiting','ongoing')
               UNION ALL
               SELECT 1 FROM data_collection_booking_grids l
               JOIN bookings b ON b.id=l.booking_id
               WHERE l.grid_id=? AND b.status IN ('waiting','ongoing')
               LIMIT 1""",
            [grid["grid_id"], grid["grid_id"]],
        )
        if cursor.fetchone():
            raise InventoryError(f"{code} is already linked to another active booking.")
        linked_at = now_utc()
        cursor.execute(
            f"INSERT INTO {link_table} ({booking_column}, grid_id, linked_at, linked_by) "
            "VALUES (?, ?, ?, NULL)",
            [booking_id, grid["grid_id"], linked_at],
        )
        _event(
            cursor,
            grid["grid_id"],
            f"{service_type}_booking_linked",
            None,
            new_value=f"{service_type.replace('_', ' ').title()} booking #{booking_id}",
            notes="Grid selected from the public booking form.",
            operation_reference=f"{operation_prefix}-{booking_id}",
            event_time=linked_at,
        )
    return len(codes)


def booking_grid_links_for_bookings(service_type, booking_ids):
    link_targets = {
        "screening": ("screening_booking_grids", "screening_booking_id"),
        "data_collection": ("data_collection_booking_grids", "booking_id"),
    }
    if service_type not in link_targets:
        raise ValueError("Unsupported booking type.")
    ids = list(dict.fromkeys(int(booking_id) for booking_id in booking_ids))
    if not ids:
        return {}
    link_table, booking_column = link_targets[service_type]
    placeholders = ", ".join("?" for _ in ids)
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""SELECT l.{booking_column} AS booking_id, g.grid_code, g.current_status,
                       s.sample_name, sl.falcon_container_id, sl.grid_box_name,
                       sl.grid_position, g.clipped,
                       (SELECT p.falcon_container_id FROM storage_locations p WHERE p.grid_id=g.grid_id
                        ORDER BY p.stored_at DESC, p.storage_location_id DESC LIMIT 1) AS last_falcon,
                       (SELECT p.grid_box_name FROM storage_locations p WHERE p.grid_id=g.grid_id
                        ORDER BY p.stored_at DESC, p.storage_location_id DESC LIMIT 1) AS last_box,
                       (SELECT p.grid_position FROM storage_locations p WHERE p.grid_id=g.grid_id
                        ORDER BY p.stored_at DESC, p.storage_location_id DESC LIMIT 1) AS last_position,
                       CASE WHEN {_BILLED_AT_FREEZING_SQL} THEN 1 ELSE 0 END
                           AS billed_at_freezing
                FROM {link_table} l
                JOIN inventory_grids g ON g.grid_id=l.grid_id
                JOIN inventory_samples s ON s.sample_id=g.sample_id
                LEFT JOIN storage_locations sl
                  ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
                WHERE l.{booking_column} IN ({placeholders})
                ORDER BY l.{booking_column}, g.grid_id""",
            ids,
        )
        linked = {}
        for row in with_grid_labels(cursor.fetchall()):
            linked.setdefault(row["booking_id"], []).append(row)
        return linked
    finally:
        cursor.close()
        connection.close()


def grid_booking_links(grid_code):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT 'Screening' AS service_name, 'screening' AS service_type,
                      b.id AS booking_id, b.sample_name,
                      b.status, l.linked_at
               FROM screening_booking_grids l
               JOIN screening_bookings b ON b.id=l.screening_booking_id
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE g.grid_code=?
               UNION ALL
               SELECT 'Data collection' AS service_name, 'data_collection' AS service_type,
                      b.id AS booking_id, b.sample_name,
                      b.status, l.linked_at
               FROM data_collection_booking_grids l
               JOIN bookings b ON b.id=l.booking_id
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE g.grid_code=?
               ORDER BY linked_at DESC""",
            [grid_code, grid_code],
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


_LOAD_TARGETS = {
    "screening": {
        "booking_table": "screening_bookings",
        "link_table": "screening_booking_grids",
        "booking_column": "screening_booking_id",
        "operation_prefix": "SCREENING",
        "label": "screening",
        "started_event": "screening_started",
    },
    "data_collection": {
        "booking_table": "bookings",
        "link_table": "data_collection_booking_grids",
        "booking_column": "booking_id",
        "operation_prefix": "DATA-COLLECTION",
        "label": "data collection",
        "started_event": "data_collection_booking_started",
    },
}


def _load_booking(service_type, booking_id, actor_id):
    """Mark a waiting booking ongoing and take its linked grids out of storage."""
    target = _LOAD_TARGETS[service_type]
    label = target["label"]
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            f"SELECT id, status FROM {target['booking_table']} WHERE id=?" + _lock_clause(),
            [booking_id],
        )
        booking = cursor.fetchone()
        if not booking or booking["status"] != "waiting":
            raise InventoryError(f"That {label} booking is no longer waiting.")

        cursor.execute(
            f"""SELECT g.grid_id, g.grid_code, g.current_status
               FROM {target['link_table']} l
               JOIN inventory_grids g ON g.grid_id=l.grid_id
               WHERE l.{target['booking_column']}=? ORDER BY g.grid_id""" + _lock_clause(),
            [booking_id],
        )
        grids = cursor.fetchall()
        event_time = now_utc()
        operation_reference = f"{target['operation_prefix']}-{booking_id}"
        for grid in grids:
            if grid["current_status"] not in {"Frozen", "Clipped", "Stored"}:
                raise InventoryError(
                    f"{grid['grid_code']} cannot be loaded for {label} from "
                    f"status {grid['current_status']}."
                )
            cursor.execute(
                """SELECT 1 FROM grid_session_links
                   WHERE grid_id=? AND outcome='in_progress' LIMIT 1""",
                [grid["grid_id"]],
            )
            if cursor.fetchone():
                raise InventoryError(
                    f"{grid['grid_code']} is already in an active microscope session."
                )
            previous = _current_location(cursor, grid["grid_id"], lock=True)
            previous_label = _location_label(previous)
            if previous:
                cursor.execute(
                    "UPDATE storage_locations SET removed_at=?, removed_by=? "
                    "WHERE storage_location_id=? AND removed_at IS NULL",
                    (event_time, actor_id, previous["storage_location_id"]),
                )
                _event(
                    cursor, grid["grid_id"], "storage_removal", actor_id,
                    old_value=previous_label,
                    notes=f"Retrieved from storage for {label}.",
                    operation_reference=operation_reference,
                    event_time=event_time,
                )
            cursor.execute(
                "UPDATE inventory_grids SET current_status='In Data Collection' "
                "WHERE grid_id=?",
                [grid["grid_id"]],
            )
            _event(
                cursor, grid["grid_id"], target["started_event"], actor_id,
                old_value=grid["current_status"], new_value="In Data Collection",
                notes=f"Loaded for {label} booking #{booking_id}; "
                      f"previous location: {previous_label or 'not stored'}.",
                operation_reference=operation_reference,
                event_time=event_time,
            )

        cursor.execute(
            f"UPDATE {target['booking_table']} SET status='ongoing' WHERE id=?",
            [booking_id],
        )
        cursor.execute(
            f"SELECT * FROM {target['booking_table']} WHERE id=?",
            [booking_id],
        )
        loaded_booking = cursor.fetchone()
        connection.commit()
        return loaded_booking
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def load_screening_booking(booking_id, actor_id):
    return _load_booking("screening", booking_id, actor_id)


def load_data_collection_booking(booking_id, actor_id):
    return _load_booking("data_collection", booking_id, actor_id)


# A linked inventory grid was already billed a grid charge at freezing when
# freezing completion recorded it (event below), or, for grids registered
# before that event existed, when its freezing booking's completed bill used
# facility-provided grids.
_BILLED_AT_FREEZING_SQL = """(
    EXISTS (SELECT 1 FROM grid_events be
            WHERE be.grid_id=g.grid_id AND be.event_type='freezing_grid_billed')
    OR EXISTS (
        SELECT 1 FROM freezing_booking_inventory fbi
        JOIN freezing_bookings fb ON fb.id=fbi.booking_id
        JOIN completed_freezing cf
          ON cf.email=fb.email AND cf.freezing_date=fb.freezing_date
         AND cf.sample_name=fb.sample_name AND cf.user_name=fb.user_name
        WHERE fbi.batch_id=g.batch_id
          AND LOWER(COALESCE(cf.grid_source, '')) IN ('facility', 'facility provided')
    )
)"""


def screened_grid_codes(fields):
    """Codes of the linked grids ticked as screened/used, or None if not stated.

    The completion form sends one ``inventory_screened`` value ("1"/"0") per
    ``inventory_grid_code``; older forms send none, meaning all were used.
    """
    codes = fields.getlist("inventory_grid_code")
    flags = fields.getlist("inventory_screened")
    if not flags:
        return None
    if len(flags) != len(codes):
        raise InventoryError("Reload the booking and tick which linked grids were screened.")
    return [code for code, flag in zip(codes, flags) if flag == "1"]


def booking_billing_context(cursor, service_type, booking_id, grid_codes=None):
    """Return linked, already-billed and already-clipped grid counts for a booking.

    ``grid_codes`` limits the counts to the linked grids actually used, so a
    grid that went back to storage unused does not affect the bill.
    """
    target = _LOAD_TARGETS.get(service_type)
    if not target:
        raise ValueError("Unsupported booking type.")
    code_filter = ""
    values = [booking_id]
    if grid_codes is not None:
        if not grid_codes:
            return {"linked": 0, "prebilled": 0, "clipped": 0}
        code_filter = " AND g.grid_code IN (" + ", ".join("?" for _ in grid_codes) + ")"
        values.extend(grid_codes)
    cursor.execute(
        f"""SELECT COUNT(*) AS linked,
                   COALESCE(SUM(CASE WHEN {_BILLED_AT_FREEZING_SQL} THEN 1 ELSE 0 END), 0)
                       AS prebilled,
                   COALESCE(SUM(CASE WHEN g.clipped=1 THEN 1 ELSE 0 END), 0) AS clipped
            FROM {target['link_table']} l
            JOIN inventory_grids g ON g.grid_id=l.grid_id
            WHERE l.{target['booking_column']}=?""" + code_filter,
        values,
    )
    row = cursor.fetchone()
    return {
        "linked": int(row["linked"] or 0),
        "prebilled": int(row["prebilled"] or 0),
        "clipped": int(row["clipped"] or 0),
    }


def booking_billing_context_for(service_type, booking_id, grid_codes=None):
    connection = get_db()
    cursor = connection.cursor()
    try:
        return booking_billing_context(cursor, service_type, booking_id, grid_codes)
    finally:
        cursor.close()
        connection.close()


def delete_booking_grid_links(cursor, service_type, booking_id):
    """Remove a deleted booking's grid links (SQLite does not cascade by default)."""
    target = _LOAD_TARGETS[service_type]
    cursor.execute(
        f"DELETE FROM {target['link_table']} WHERE {target['booking_column']}=?",
        [booking_id],
    )
    return cursor.rowcount


def _mark_grids_clipped(cursor, grid_ids, actor_id, event_time, notes):
    """Record clipping on grids that are not discarded or already clipped."""
    for grid_id in grid_ids:
        cursor.execute(
            "SELECT clipped, current_status FROM inventory_grids WHERE grid_id=?",
            [grid_id],
        )
        grid = cursor.fetchone()
        if not grid or grid["clipped"] or grid["current_status"] == "Discarded":
            continue
        next_status = "Clipped" if grid["current_status"] == "Frozen" else grid["current_status"]
        cursor.execute(
            """UPDATE inventory_grids SET clipped=1, clipped_at=?, clipped_by=?,
               current_status=? WHERE grid_id=?""",
            (event_time, actor_id, next_status, grid_id),
        )
        _event(
            cursor, grid_id, "clipping_status_change", actor_id,
            old_value="Not clipped", new_value="Clipped", notes=notes,
            event_time=event_time,
        )


def complete_booking_grid_moves(
    cursor, service_type, booking_id, fields, actor_id, booking=None
):
    link_targets = {
        "screening": ("screening_booking_grids", "screening_booking_id", "SCREENING"),
        "data_collection": ("data_collection_booking_grids", "booking_id", "DATA-COLLECTION"),
    }
    if service_type not in link_targets:
        raise ValueError("Unsupported booking type.")
    link_table, booking_column, operation_prefix = link_targets[service_type]
    cursor.execute(
        f"""SELECT g.grid_id, g.grid_code, g.current_status
            FROM {link_table} l
            JOIN inventory_grids g ON g.grid_id=l.grid_id
            WHERE l.{booking_column}=? ORDER BY g.grid_id""" + _lock_clause(),
        [booking_id],
    )
    grids = cursor.fetchall()

    grid_codes = fields.getlist("inventory_grid_code")
    containers = fields.getlist("inventory_falcon")
    boxes = fields.getlist("inventory_box")
    positions = fields.getlist("inventory_position")
    outcomes = fields.getlist("inventory_outcome")
    discard_notes = fields.getlist("inventory_discard_notes")
    count = len(grids)
    expected_codes = [grid["grid_code"] for grid in grids]
    if (
        grid_codes != expected_codes
        or len(outcomes) != count
        or len(discard_notes) != count
        or len(containers) != count
        or len(boxes) != count
        or len(positions) != count
    ):
        raise InventoryError(
            "The mapped grids changed. Reload the booking and choose Store or Mark discarded for each linked grid."
        )
    screened = screened_grid_codes(fields)
    screened = set(expected_codes if screened is None else screened)
    if count and fields.get("actual_grids"):
        actual = _positive_int(fields.get("actual_grids"), "Actual grids")
        needed = min(actual, count)
        if len(screened) != needed:
            raise InventoryError(
                f"Actual grids is {actual}: tick the {needed} linked grid"
                f"{'' if needed == 1 else 's'} that were used ({len(screened)} ticked). "
                "Unticked grids go back to storage unused."
            )
    event_time = now_utc()
    return_event_type = (
        "screening_returned_to_storage" if service_type == "screening"
        else "data_collection_returned_to_storage"
    )
    for index, grid in enumerate(grids):
        # Loading a screening or data-collection booking takes its linked
        # grids out of storage ("In Data Collection"); completing the booking
        # returns or discards them.
        allow_microscope_return = grid["current_status"] == "In Data Collection"
        if allow_microscope_return:
            cursor.execute(
                """SELECT 1 FROM grid_session_links
                   WHERE grid_id=? AND outcome='in_progress' LIMIT 1""",
                [grid["grid_id"]],
            )
            if cursor.fetchone():
                raise InventoryError(
                    f"Complete the active data-collection session before returning "
                    f"{grid['grid_code']} through this booking."
                )
        used = grid["grid_code"] in screened
        if not used and outcomes[index] != "discard":
            # Reserved but not screened: back to storage, not clipped or billed.
            _store_grid(
                cursor,
                grid,
                {
                    "falcon_container_id": containers[index],
                    "grid_box_name": boxes[index],
                    "grid_position": positions[index],
                },
                actor_id,
                f"Not used in booking #{booking_id}; returned to storage unchanged.",
                event_time,
                operation_reference=f"{operation_prefix}-{booking_id}",
                mark_stored=True,
                allow_microscope_return=allow_microscope_return,
                return_event_type=return_event_type,
            )
            continue
        if outcomes[index] == "discard":
            _discard_grid(
                cursor,
                grid,
                actor_id,
                discard_notes[index],
                event_time,
                operation_reference=f"{operation_prefix}-{booking_id}",
                allow_microscope_return=allow_microscope_return,
            )
            continue
        if outcomes[index] != "store":
            raise InventoryError(f"Choose Store or Mark discarded for {grid['grid_code']}.")
        if (
            grid["current_status"] == "Discarded"
            or (
                grid["current_status"] == "In Data Collection"
                and not allow_microscope_return
            )
        ):
            raise InventoryError(
                f"{grid['grid_code']} is currently {grid['current_status']} and cannot be returned through this booking."
            )
        _store_grid(
            cursor,
            grid,
            {
                "falcon_container_id": containers[index],
                "grid_box_name": boxes[index],
                "grid_position": positions[index],
            },
            actor_id,
            "Storage confirmed when the linked booking was completed.",
            event_time,
            operation_reference=f"{operation_prefix}-{booking_id}",
            mark_stored=True,
            allow_microscope_return=allow_microscope_return,
            return_event_type=return_event_type,
        )
    if service_type == "screening":
        # Screening clips every screened grid, so remember it on the linked
        # grids; later data collection must not bill the C-clip/base again.
        _mark_grids_clipped(
            cursor, [grid["grid_id"] for grid in grids if grid["grid_code"] in screened],
            actor_id, event_time,
            f"Clipped during screening booking #{booking_id}.",
        )
    add_inventory_grids = fields.get("add_inventory_grids")
    if add_inventory_grids not in {None, "", "1"}:
        raise InventoryError("Choose whether to add additional grids to inventory.")
    if add_inventory_grids == "1":
        if booking is None:
            raise RuntimeError("The booking details are required to register new inventory grids.")
        actual_grid_count = _positive_int(fields.get("actual_grids"), "Actual grids")
        additional_count = max(0, actual_grid_count - len(screened))
        if additional_count > MAX_GRIDS_PER_BATCH:
            raise InventoryError(
                f"No more than {MAX_GRIDS_PER_BATCH} additional grids can be added to inventory at once."
            )
        new_type_ids = fields.getlist("inventory_new_grid_type_id")
        new_sample_names = fields.getlist("inventory_new_sample_name")
        new_containers = fields.getlist("inventory_new_falcon")
        new_boxes = fields.getlist("inventory_new_box")
        new_positions = fields.getlist("inventory_new_position")
        if not new_sample_names:
            new_sample_names = [booking["sample_name"]] * additional_count
        if not (
            len(new_type_ids) == len(new_containers)
            == len(new_sample_names) == len(new_boxes) == len(new_positions) == additional_count
        ):
            raise InventoryError(
                "Enter a sample name, grid type, and storage location for each additional grid."
            )
        if additional_count:
            sample_ids = {}
            for index in range(additional_count):
                sample_name = _text(
                    new_sample_names[index], "Sample name", 180, True
                )
                if sample_name not in sample_ids:
                    sample_ids[sample_name] = _create_sample(
                        cursor,
                        {
                            "sample_name": sample_name,
                            "researcher_name": booking["user_name"] or "Booking user",
                            "lab_name": booking["esm"] or booking["origin"] or "ACCEM",
                            "external_reference": f"{operation_prefix}-{booking_id}",
                        },
                        actor_id,
                        event_time,
                    )
                grid_type_id = _positive_int(new_type_ids[index], "grid type")
                cursor.execute(
                    "SELECT grid_type_id FROM grid_types WHERE grid_type_id=? AND active=1",
                    [grid_type_id],
                )
                if not cursor.fetchone():
                    raise InventoryError("Choose an active grid type for each additional grid.")
                container = _text(
                    new_containers[index], "Falcon container ID", 100, True
                )
                box = _text(new_boxes[index], "Grid box", 100, True)
                position = _text(new_positions[index], "Grid position", 40, True)
                grid_code = _next_code(cursor, "grid", "ACCEM-G-", 4)
                cursor.execute(
                    """INSERT INTO inventory_grids
                       (grid_code, batch_id, sample_id, grid_type_id, current_status,
                        created_at, created_by)
                       VALUES (?, NULL, ?, ?, 'Stored', ?, ?)""",
                    (grid_code, sample_ids[sample_name], grid_type_id, event_time, actor_id),
                )
                grid_id = cursor.lastrowid
                cursor.execute(
                    f"""INSERT INTO {link_table}
                        ({booking_column}, grid_id, linked_at, linked_by)
                        VALUES (?, ?, ?, ?)""",
                    (booking_id, grid_id, event_time, actor_id),
                )
                _event(
                    cursor, grid_id, "grid_registered", actor_id,
                    new_value=grid_code,
                    notes=(
                        f"Added during {service_type.replace('_', ' ')} booking "
                        f"{booking_id}; no freezing record exists."
                    ),
                    operation_reference=f"{operation_prefix}-{booking_id}",
                    event_time=event_time,
                )
                _store_grid(
                    cursor,
                    {
                        "grid_id": grid_id,
                        "grid_code": grid_code,
                        "batch_id": None,
                        "current_status": "Stored",
                    },
                    {
                        "falcon_container_id": container,
                        "grid_box_name": box,
                        "grid_position": position,
                    },
                    actor_id,
                    "Storage recorded when the booking was completed.",
                    event_time,
                    operation_reference=f"{operation_prefix}-{booking_id}",
                    mark_stored=True,
                )
                if service_type == "screening":
                    _mark_grids_clipped(
                        cursor, [grid_id], actor_id, event_time,
                        f"Clipped during screening booking #{booking_id}.",
                    )
        return count + additional_count
    return count


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


def rename_falcon_container(current_name, new_name, actor_id):
    current_name = _text(current_name, "Current Falcon container name", 100, True)
    new_name = _text(new_name, "New Falcon container name", 100, True)
    if current_name == new_name:
        raise InventoryError("Enter a different Falcon container name.")

    event_time = now_utc()
    notes = f"Falcon container renamed from {current_name} to {new_name}."
    connection = get_db()
    cursor = connection.cursor()
    try:
        _begin(connection)
        cursor.execute(
            """SELECT sl.storage_location_id, sl.grid_id, sl.falcon_container_id,
                      sl.grid_box_name, sl.grid_position, sl.stored_at, g.grid_code
               FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               WHERE sl.falcon_container_id=? AND sl.removed_at IS NULL
               ORDER BY sl.storage_location_id""" + _lock_clause(),
            [current_name],
        )
        locations = cursor.fetchall()
        if not locations:
            raise InventoryError(
                f"No active grids were found in Falcon container {current_name}."
            )

        for location in locations:
            cursor.execute(
                """SELECT g.grid_code FROM storage_locations sl
                   JOIN inventory_grids g ON g.grid_id=sl.grid_id
                   WHERE LOWER(sl.falcon_container_id)=LOWER(?)
                     AND LOWER(sl.grid_box_name)=LOWER(?)
                     AND LOWER(sl.grid_position)=LOWER(?) AND sl.removed_at IS NULL
                     AND sl.grid_id<>? LIMIT 1""" + _lock_clause(),
                [
                    new_name,
                    location["grid_box_name"],
                    location["grid_position"],
                    location["grid_id"],
                ],
            )
            occupied = cursor.fetchone()
            if occupied:
                raise InventoryError(
                    f"Cannot rename the Falcon container: position "
                    f"{location['grid_box_name']} / {location['grid_position']} "
                    f"is occupied by {occupied['grid_code']} in {new_name}."
                )

            old_label = (
                f"{location['falcon_container_id']} / "
                f"{location['grid_box_name']} / {location['grid_position']}"
            )
            new_label = (
                f"{new_name} / {location['grid_box_name']} / "
                f"{location['grid_position']}"
            )
            cursor.execute(
                """UPDATE storage_locations SET removed_at=?, removed_by=?
                   WHERE storage_location_id=? AND removed_at IS NULL""",
                [event_time, actor_id, location["storage_location_id"]],
            )
            cursor.execute(
                """INSERT INTO storage_locations
                   (grid_id, falcon_container_id, grid_box_name, grid_position,
                    stored_at, stored_by, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    location["grid_id"],
                    new_name,
                    location["grid_box_name"],
                    location["grid_position"],
                    event_time,
                    actor_id,
                    notes,
                ],
            )
            _event(
                cursor,
                location["grid_id"],
                "storage_relocation",
                actor_id,
                old_value=old_label,
                new_value=new_label,
                notes=notes,
                operation_reference="FALCON-CONTAINER-RENAME",
                event_time=event_time,
            )
        connection.commit()
        return len(locations)
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
            cursor.execute(
                """SELECT g.grid_code
                   FROM data_collection_booking_grids l
                   JOIN bookings b ON b.id=l.booking_id
                   JOIN inventory_grids g ON g.grid_id=l.grid_id
                   WHERE l.booking_id=? AND b.status IN ('waiting', 'ongoing')
                   ORDER BY g.grid_code""",
                [booking_id],
            )
            booked_grid_codes = [row["grid_code"] for row in cursor.fetchall()]
            if booked_grid_codes and set(grid_codes) != set(booked_grid_codes):
                raise InventoryError(
                    "This booking has selected inventory grids. Add exactly those grids to its data-collection session."
                )
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
                # A loaded data-collection booking has already taken its
                # linked grids out of storage; its session may use them.
                loaded_for_booking = False
                if booking_id is not None:
                    cursor.execute(
                        """SELECT 1 FROM data_collection_booking_grids l
                           JOIN bookings b ON b.id=l.booking_id
                           WHERE l.grid_id=? AND l.booking_id=? AND b.status='ongoing'
                             AND NOT EXISTS (
                                 SELECT 1 FROM grid_session_links gsl
                                 WHERE gsl.grid_id=l.grid_id AND gsl.outcome='in_progress'
                             )""",
                        [grid["grid_id"], booking_id],
                    )
                    loaded_for_booking = cursor.fetchone() is not None
                if not loaded_for_booking:
                    raise InventoryError(f"Grid {grid_code} is already in a data collection session.")
            cursor.execute(
                """SELECT 1
                   FROM screening_booking_grids l
                   JOIN screening_bookings b ON b.id=l.screening_booking_id
                   WHERE l.grid_id=? AND b.status IN ('waiting', 'ongoing')
                   UNION ALL
                   SELECT 1
                   FROM data_collection_booking_grids l
                   JOIN bookings b ON b.id=l.booking_id
                   WHERE l.grid_id=? AND b.status IN ('waiting', 'ongoing')
                     AND (? IS NULL OR l.booking_id<>?)
                   LIMIT 1""",
                [grid["grid_id"], grid["grid_id"], booking_id, booking_id],
            )
            if cursor.fetchone():
                raise InventoryError(f"{grid_code} is reserved by another active booking.")

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
            "WHERE grid_type_name <> 'Other' ORDER BY grid_type_name"
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
        # pymysql reports affected (changed) rows, so rowcount is 0 when the
        # type already has the requested state; check existence explicitly.
        cursor.execute(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_id=?",
            [grid_type_id],
        )
        if not cursor.fetchone():
            raise InventoryError("Grid type not found.")
        cursor.execute(
            "UPDATE grid_types SET active=? WHERE grid_type_id=?",
            (1 if active else 0, grid_type_id),
        )
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


def matching_inventory_samples(researcher_name, sample_name, limit=30):
    researcher_name = _text(researcher_name, "Researcher name", 150, True)
    sample_name = _text(sample_name, "Sample name", 180, True)
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT s.sample_id, s.sample_code, s.sample_name,
                      s.researcher_name, s.lab_name
               FROM inventory_samples s
               LEFT JOIN users u ON u.id=s.linked_user_id
               WHERE LOWER(TRIM(s.sample_name))=LOWER(TRIM(?))
                 AND (LOWER(TRIM(s.researcher_name))=LOWER(TRIM(?))
                      OR LOWER(TRIM(u.username))=LOWER(TRIM(?)))
               ORDER BY s.created_at DESC, s.sample_id DESC LIMIT ?""",
            [sample_name, researcher_name, researcher_name, limit],
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
            "(LOWER(g.grid_code) LIKE LOWER(?) OR LOWER(s.sample_code) LIKE LOWER(?) "
            "OR LOWER(s.sample_name) LIKE LOWER(?) "
            "OR LOWER(s.researcher_name) LIKE LOWER(?) "
            "OR EXISTS (SELECT 1 FROM users linked_user "
            "WHERE linked_user.id=s.linked_user_id "
            "AND LOWER(linked_user.username) LIKE LOWER(?)) "
            "OR LOWER(gt.grid_type_name) LIKE LOWER(?) "
            "OR LOWER(sl.falcon_container_id) LIKE LOWER(?) "
            "OR LOWER(sl.grid_box_name) LIKE LOWER(?) "
            "OR LOWER(sl.grid_position) LIKE LOWER(?) "
            "OR LOWER(fb.batch_code) LIKE LOWER(?))"
        )
        values.extend([pattern] * 10)
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
        LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
        LEFT JOIN freezing_batches fb ON fb.batch_id=g.batch_id
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
                      gt.grid_type_name, fb.batch_code, fb.frozen_at,
                      (SELECT COUNT(*) FROM inventory_grids batch_grid
                       WHERE batch_grid.batch_id=g.batch_id) AS batch_grid_count,
                      sl.falcon_container_id,
                      sl.grid_box_name, sl.grid_position
               """ + joins + where_sql +
            " ORDER BY g.grid_id DESC LIMIT ? OFFSET ?",
            [*filter_values, page_size, (page - 1) * page_size],
        )
        return cursor.fetchall(), total, page_size, page
    finally:
        cursor.close()
        connection.close()


def search_grid_box_groups(args, page=1, page_size=40):
    where_sql, filter_values = _grid_filters(args)
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    joins = """ FROM inventory_grids g
        JOIN inventory_samples s ON s.sample_id=g.sample_id
        LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
        LEFT JOIN freezing_batches fb ON fb.batch_id=g.batch_id
        JOIN grid_types gt ON gt.grid_type_id=g.grid_type_id
        LEFT JOIN storage_locations sl ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL """
    falcon_group = (
        "COALESCE(NULLIF(TRIM(sl.falcon_container_id), ''), 'Unassigned Falcon')"
    )
    box_group = (
        "COALESCE(NULLIF(TRIM(sl.grid_box_name), ''), 'Unassigned box')"
    )
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) AS total" + joins + where_sql,
            filter_values,
        )
        total_grids = int(cursor.fetchone()["total"])
        cursor.execute(
            f"""SELECT COUNT(*) AS total FROM (
                    SELECT {falcon_group}, {box_group}
                    {joins}{where_sql}
                    GROUP BY {falcon_group}, {box_group}
                ) grouped_boxes""",
            filter_values,
        )
        total_boxes = int(cursor.fetchone()["total"])
        page = min(page, max(1, (total_boxes + page_size - 1) // page_size))
        cursor.execute(
            f"""SELECT {falcon_group} AS storage_falcon_group,
                       {box_group} AS storage_box_group
                {joins}{where_sql}
                GROUP BY {falcon_group}, {box_group}
                ORDER BY storage_falcon_group, storage_box_group
                LIMIT ? OFFSET ?""",
            [*filter_values, page_size, (page - 1) * page_size],
        )
        page_groups = cursor.fetchall()
        if not page_groups:
            return [], total_grids, total_boxes, page_size, page

        group_filters = []
        group_values = []
        for group in page_groups:
            group_filters.append(
                f"({falcon_group}=? AND {box_group}=?)"
            )
            group_values.extend(
                [group["storage_falcon_group"], group["storage_box_group"]]
            )
        detail_where = where_sql or " WHERE 1=1"
        cursor.execute(
            f"""SELECT g.grid_id, g.grid_code, g.current_status, g.clipped,
                       g.clipped_at, s.sample_code, s.sample_name,
                       s.researcher_name, linked_user.username AS linked_username,
                       gt.grid_type_name, fb.batch_code,
                       fb.frozen_at,
                       (SELECT COUNT(*) FROM inventory_grids batch_grid
                        WHERE batch_grid.batch_id=g.batch_id) AS batch_grid_count,
                       sl.falcon_container_id, sl.grid_box_name, sl.grid_position,
                       {falcon_group} AS storage_falcon_group,
                       {box_group} AS storage_box_group
                {joins}{detail_where}
                  AND ({" OR ".join(group_filters)})
                ORDER BY storage_falcon_group, storage_box_group, g.grid_id DESC""",
            [*filter_values, *group_values],
        )
        rows = [dict(row) for row in cursor.fetchall()]
        rows.sort(
            key=lambda item: (
                *_storage_position_sort_key(item),
                str(item["grid_code"]),
            )
        )
        return with_grid_labels(rows), total_grids, total_boxes, page_size, page
    finally:
        cursor.close()
        connection.close()


def dashboard_data():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT COUNT(*) AS total,
                 SUM(CASE WHEN current_status='Frozen' THEN 1 ELSE 0 END) AS frozen,
                 SUM(CASE WHEN current_status='Clipped' THEN 1 ELSE 0 END) AS clipped,
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
                      s.sample_name, s.researcher_name,
                      linked_user.username AS linked_username,
                      u.username AS operator_name
               FROM grid_events e JOIN inventory_grids g ON g.grid_id=e.grid_id
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               LEFT JOIN users u ON u.id=e.operator_id
               ORDER BY e.event_time DESC, e.event_id DESC LIMIT 12"""
        )
        recent = with_grid_labels(cursor.fetchall())
        return counts, by_type, by_status, recent
    finally:
        cursor.close()
        connection.close()


def dashboard_storage():
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT g.grid_code, g.current_status, g.clipped,
                      s.sample_code, s.sample_name, s.researcher_name,
                      linked_user.username AS linked_username,
                      fb.batch_code, fb.frozen_at,
                      sl.falcon_container_id, sl.grid_box_name, sl.grid_position
               FROM inventory_grids g
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               LEFT JOIN freezing_batches fb ON fb.batch_id=g.batch_id
               LEFT JOIN storage_locations sl
                 ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
               ORDER BY sl.falcon_container_id, sl.grid_box_name,
                        sl.grid_position, g.grid_code"""
        )
        stored = []
        not_stored = []
        for row in cursor.fetchall():
            item = dict(row)
            if item["current_status"] == "Discarded":
                item["status_code"] = "D"
                item["status_label"] = "Discarded"
            elif item["current_status"] == "Clipped":
                item["status_code"] = "C"
                item["status_label"] = "Clipped"
            elif item["current_status"] == "Stored":
                item["status_code"] = "S"
                item["status_label"] = "Stored"
            elif item["current_status"] == "In Data Collection":
                item["status_code"] = "U"
                item["status_label"] = "In microscope"
            else:
                item["status_code"] = "F"
                item["status_label"] = "Frozen"

            if item["falcon_container_id"] is None:
                not_stored.append(item)
            else:
                stored.append(item)
        stored.sort(key=_storage_position_sort_key)
        return with_grid_labels(stored), with_grid_labels(not_stored)
    finally:
        cursor.close()
        connection.close()


PUBLIC_LOOKUP_MIN_LENGTH = 2


def public_inventory_grids(researcher_name, sample_name=None, available_only=False):
    researcher_name = _text(researcher_name, "Researcher name", 150, True)
    if len(researcher_name) < PUBLIC_LOOKUP_MIN_LENGTH:
        raise InventoryError(
            f"Enter at least {PUBLIC_LOOKUP_MIN_LENGTH} characters of your username."
        )
    if sample_name:
        sample_name = _text(sample_name, "Sample name", 180, True)
    connection = get_db()
    cursor = connection.cursor()
    try:
        query = """SELECT g.grid_code, g.current_status, g.clipped, s.sample_code,
                      s.sample_name, sl.falcon_container_id, sl.grid_box_name,
                      sl.grid_position,
                      CASE WHEN g.current_status IN ('Frozen', 'Clipped', 'Stored')
                        AND NOT EXISTS (
                          SELECT 1 FROM screening_booking_grids l
                          JOIN screening_bookings b ON b.id=l.screening_booking_id
                          WHERE l.grid_id=g.grid_id AND b.status IN ('waiting', 'ongoing')
                        )
                        AND NOT EXISTS (
                          SELECT 1 FROM data_collection_booking_grids l
                          JOIN bookings b ON b.id=l.booking_id
                          WHERE l.grid_id=g.grid_id AND b.status IN ('waiting', 'ongoing')
                        )
                        AND NOT EXISTS (
                          SELECT 1 FROM grid_session_links l
                          WHERE l.grid_id=g.grid_id AND l.outcome='in_progress'
                        )
                        THEN 1 ELSE 0 END AS available
               FROM inventory_grids g
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users u ON u.id=s.linked_user_id
               LEFT JOIN storage_locations sl
                 ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
               WHERE (LOWER(TRIM(s.researcher_name))=LOWER(TRIM(?))
                      OR LOWER(TRIM(u.username))=LOWER(TRIM(?)))
               """
        # Public lookups are unauthenticated, so only an exact (case-insensitive,
        # trimmed) username/researcher-name match may reveal grid records.
        values = [researcher_name, researcher_name]
        if sample_name:
            query += " AND LOWER(TRIM(s.sample_name))=LOWER(TRIM(?))"
            values.append(sample_name)
        if available_only:
            query += """AND g.current_status IN ('Frozen', 'Clipped', 'Stored')
                         AND NOT EXISTS (
                           SELECT 1 FROM screening_booking_grids l
                           JOIN screening_bookings b ON b.id=l.screening_booking_id
                           WHERE l.grid_id=g.grid_id AND b.status IN ('waiting', 'ongoing')
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM data_collection_booking_grids l
                           JOIN bookings b ON b.id=l.booking_id
                           WHERE l.grid_id=g.grid_id AND b.status IN ('waiting', 'ongoing')
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM grid_session_links l
                           WHERE l.grid_id=g.grid_id AND l.outcome='in_progress'
                         )
                      """
        query += " ORDER BY s.sample_name, g.grid_code"
        cursor.execute(query, values)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            if item["current_status"] == "Discarded":
                item["status_code"] = "D"
            elif item["current_status"] == "Clipped":
                item["status_code"] = "C"
            elif item["current_status"] == "Stored":
                item["status_code"] = "S"
            elif item["current_status"] == "In Data Collection":
                item["status_code"] = "U"
            else:
                item["status_code"] = "F"
            if not available_only or item["available"]:
                rows.append(item)
        rows = with_grid_labels(rows)
        # Newest freezing session first; grids in session order within it.
        rows.sort(key=lambda item: item.get("gl_grid_number") or 0)
        rows.sort(  # stable: keeps grid order inside each session
            key=lambda item: (str(item.get("gl_frozen_at") or ""), item.get("gl_session_key") or ""),
            reverse=True,
        )
        return rows
    finally:
        cursor.close()
        connection.close()


def grid_details(grid_code):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT g.*, s.sample_code,             s.sample_code, s.sample_name, s.researcher_name, s.lab_name,
                      s.description AS sample_description, s.external_reference,
                      linked_user.username AS linked_account_name,
                      fb.batch_code, fb.frozen_at, fb.notes AS preparation_notes,
                      fb.comments AS batch_comments,
                      COALESCE(g.blot_seconds, fbi.blot_seconds) AS recorded_blot_seconds,
                      COALESCE(g.blot_force, fbi.blot_force) AS recorded_blot_force,
                      gt.grid_type_name,
                      creator.username AS created_by_name, clipper.username AS clipped_by_name
               FROM inventory_grids g
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN freezing_batches fb ON fb.batch_id=g.batch_id
               LEFT JOIN freezing_booking_inventory fbi ON fbi.batch_id=fb.batch_id
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
        grid = with_grid_labels([grid])[0]
        cursor.execute(
            """SELECT sl.*, storage_user.username AS stored_by_name,
                      removed_user.username AS removed_by_name
               FROM storage_locations sl
               LEFT JOIN users storage_user ON storage_user.id=sl.stored_by
               LEFT JOIN users removed_user ON removed_user.id=sl.removed_by
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
    query = (args.get("q") or "").strip()
    if query:
        if len(query) > 180:
            raise InventoryError("Search text must be 180 characters or fewer.")
        pattern = f"%{query}%"
        where.append(
            "(LOWER(g.grid_code) LIKE LOWER(?) OR LOWER(s.sample_code) LIKE LOWER(?) "
            "OR LOWER(s.sample_name) LIKE LOWER(?) "
            "OR LOWER(s.researcher_name) LIKE LOWER(?) "
            "OR LOWER(linked_user.username) LIKE LOWER(?))"
        )
        values.extend([pattern] * 5)
    for key, column in (
        ("container", "sl.falcon_container_id"),
        ("box", "sl.grid_box_name"),
        ("position", "sl.grid_position"),
    ):
        value = (args.get(key) or "").strip()
        if value:
            limits = {"container": 100, "box": 100, "position": 40}
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
                      g.current_status, s.sample_code, s.sample_name,
                      s.researcher_name, linked_user.username AS linked_username
               FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               WHERE """ + " AND ".join(where) +
            " ORDER BY sl.falcon_container_id, sl.grid_box_name, sl.grid_position",
            values,
        )
        return with_grid_labels(sorted(cursor.fetchall(), key=_storage_position_sort_key))
    finally:
        cursor.close()
        connection.close()


def storage_name_suggestions(limit=20):
    connection = get_db()
    cursor = connection.cursor()
    try:
        suggestions = {}
        for key, column in (
            ("falcons", "falcon_container_id"),
            ("boxes", "grid_box_name"),
        ):
            cursor.execute(
                f"""SELECT {column} AS name
                    FROM storage_locations
                    WHERE TRIM({column})<>''
                    GROUP BY {column}
                    ORDER BY COUNT(*) DESC, MAX(storage_location_id) DESC
                    LIMIT ?""",
                [limit],
            )
            suggestions[key] = [row["name"] for row in cursor.fetchall()]
        return suggestions
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
            """SELECT g.grid_code, g.current_status, s.sample_name,
                      s.researcher_name, linked_user.username AS linked_username
               FROM inventory_grids g JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               WHERE g.current_status NOT IN ('Discarded','In Data Collection')
               ORDER BY s.researcher_name, s.sample_name, g.grid_id DESC LIMIT ?""",
            [limit],
        )
        return with_grid_labels(cursor.fetchall())
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
                      l.recorded_at, s.sample_name, s.researcher_name,
                      linked_user.username AS linked_username
               FROM grid_session_links l JOIN inventory_grids g ON g.grid_id=l.grid_id
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               WHERE l.session_id=? ORDER BY g.grid_code""",
            [session_id],
        )
        return session_row, with_grid_labels(cursor.fetchall())
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
            """SELECT e.*, g.grid_code, s.sample_name, s.researcher_name,
                      linked_user.username AS linked_username,
                      u.username AS operator_name, ds.session_code
               FROM grid_events e JOIN inventory_grids g ON g.grid_id=e.grid_id
               JOIN inventory_samples s ON s.sample_id=g.sample_id
               LEFT JOIN users linked_user ON linked_user.id=s.linked_user_id
               LEFT JOIN users u ON u.id=e.operator_id
               LEFT JOIN data_sessions ds ON ds.session_id=e.related_session_id
               ORDER BY e.event_time DESC, e.event_id DESC LIMIT ? OFFSET ?""",
            [page_size, (page - 1) * page_size],
        )
        return with_grid_labels(cursor.fetchall()), total, page
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
           LEFT JOIN freezing_batches fb ON fb.batch_id=g.batch_id
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


# ── Human-readable grid labels ────────────────────────────────────────────────
# Grids are identified to people by sample, grid type and their place in the
# freezing session ("Spike RBD · Gold Quantifoil · grid 2 of 4"), never by the
# internal ACCEM-G-… code, which stays only in URLs and form values.

def facility_date(value):
    """'12 Oct 2026' in facility time, or '' when unknown."""
    if not value:
        return ""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(FACILITY_TIMEZONE).strftime("%d %b %Y")


def _session_key(batch_id, sample_id):
    return f"b{batch_id}" if batch_id is not None else f"s{sample_id}"


def grid_label_info(codes):
    """Map grid codes to their sample, type, freezing session and position in it."""
    codes = [code for code in dict.fromkeys(codes) if code]
    if not codes:
        return {}
    connection = get_db()
    cursor = connection.cursor()
    try:
        rows = []
        for offset in range(0, len(codes), 400):
            chunk = codes[offset:offset + 400]
            cursor.execute(
                """SELECT g.grid_code, g.grid_id, g.batch_id, g.sample_id,
                          s.sample_name, s.researcher_name, gt.grid_type_name,
                          fb.frozen_at
                   FROM inventory_grids g
                   JOIN inventory_samples s ON s.sample_id=g.sample_id
                   JOIN grid_types gt ON gt.grid_type_id=g.grid_type_id
                   LEFT JOIN freezing_batches fb ON fb.batch_id=g.batch_id
                   WHERE g.grid_code IN (""" + ", ".join("?" for _ in chunk) + ")",
                chunk,
            )
            rows.extend(dict(row) for row in cursor.fetchall())
        batch_ids = sorted({row["batch_id"] for row in rows if row["batch_id"] is not None})
        loose_samples = sorted({row["sample_id"] for row in rows if row["batch_id"] is None})
        siblings = {}
        for column, ids in (("batch_id", batch_ids), ("sample_id", loose_samples)):
            for offset in range(0, len(ids), 400):
                chunk = ids[offset:offset + 400]
                extra = " AND batch_id IS NULL" if column == "sample_id" else ""
                cursor.execute(
                    f"SELECT grid_id, batch_id, sample_id FROM inventory_grids "
                    f"WHERE {column} IN (" + ", ".join("?" for _ in chunk) + ")" + extra,
                    chunk,
                )
                for sibling in cursor.fetchall():
                    key = _session_key(sibling["batch_id"], sibling["sample_id"])
                    siblings.setdefault(key, []).append(sibling["grid_id"])
        info = {}
        for row in rows:
            key = _session_key(row["batch_id"], row["sample_id"])
            members = sorted(siblings.get(key, [row["grid_id"]]))
            number = members.index(row["grid_id"]) + 1 if row["grid_id"] in members else 1
            frozen_label = facility_date(row["frozen_at"])
            info[row["grid_code"]] = {
                "sample_name": row["sample_name"],
                "researcher_name": row["researcher_name"],
                "grid_type_name": row["grid_type_name"],
                "frozen_at": row["frozen_at"],
                "frozen_label": frozen_label,
                "session_key": key,
                "grid_number": number,
                "session_total": len(members),
                "label": f"{row['sample_name']} · {row['grid_type_name']} · grid {number} of {len(members)}",
                "session_label": (
                    f"Frozen {frozen_label} · {row['sample_name']}" if frozen_label
                    else f"Not frozen yet · {row['sample_name']}"
                ),
            }
        return info
    finally:
        cursor.close()
        connection.close()


def with_grid_labels(rows, code_key="grid_code"):
    """Copy rows to dicts carrying the grid_label_info fields (prefixed 'gl_')."""
    items = [dict(row) for row in rows]
    info = grid_label_info([item.get(code_key) for item in items])
    for item in items:
        for key, value in info.get(item.get(code_key), {}).items():
            item[f"gl_{key}"] = value
        item.setdefault("gl_label", item.get("sample_name") or "Grid")
    return items


def group_by_freezing_session(rows):
    """Group labelled rows (see with_grid_labels) by freezing session, keeping order."""
    sessions = {}
    for row in rows:
        key = row.get("gl_session_key") or row.get("sample_name") or ""
        session = sessions.get(key)
        if session is None:
            session = sessions[key] = {
                "key": key,
                "frozen_label": row.get("gl_frozen_label", ""),
                "sample_name": row.get("sample_name") or row.get("gl_sample_name", ""),
                "total": row.get("gl_session_total") or 0,
                "grids": [],
            }
        session["grids"].append(row)
    for session in sessions.values():
        counts = {}
        for grid in session["grids"]:
            name = grid.get("gl_grid_type_name") or "Grid"
            counts[name] = counts.get(name, 0) + 1
        session["type_summary"] = ", ".join(f"{name} × {count}" for name, count in counts.items())
        session["total"] = session["total"] or len(session["grids"])
    return list(sessions.values())


_GRID_CODE_PATTERN = re.compile(r"ACCEM-G-\d{4,}")


def humanize_grid_codes(text):
    """Replace internal grid codes in a message with the grid's readable label."""
    text = "" if text is None else str(text)
    codes = _GRID_CODE_PATTERN.findall(text)
    if not codes:
        return text
    try:
        info = grid_label_info(codes)
    except Exception:  # a message must still render if the lookup fails
        info = {}
    return _GRID_CODE_PATTERN.sub(
        lambda match: (
            f"“{info[match.group(0)]['label']}”" if match.group(0) in info else "an unknown grid"
        ),
        text,
    )
