import os
import csv
import io
import json
import datetime
import threading
import re
from markupsafe import Markup, escape
from flask import Blueprint, render_template, current_app, redirect, url_for, Response, jsonify, request, session
from database import _is_sqlite_url, ensure_site_stats_cache_table, get_db
from normalization import clean_display_name
from inventory_service import (
    PUBLIC_LOOKUP_MIN_LENGTH, InventoryError, group_by_freezing_session, public_inventory_grids,
    similar_researcher_names,
)
from curated_publications import CURATED_PUBLICATIONS
from publication_finder import normalize_doi, normalize_title
import emdb_structures
import publication_finder

public_bp = Blueprint("public", __name__)

PI_USERS = [
    ("Prof. Dipshikha Chakravortty", 1), ("Prof. Dipankar Chatterji", 1), ("Prof. K. Suguna", 1),
    ("Prof. B. Gopal", 1), ("Dr. Aravind Pentamasa", 5), ("Dr. Somnath Dutta", 8),
    ("Dr. Ashok Sekhar", 1), ("Dr. Jayanta Chatterjee", 1), ("Dr. Mahavir Singh", 2),
    ("Dr. Tanweer Hussain", 7), ("Dr. Raghavan Varadarajan", 5), ("Dr. Vidhya Mangala Prasad", 8),
    ("Dr. Amit Baidya", 3), ("Dr. Mahipal Ganji", 2), ("Dr. Saibal Chatterjee", 1),
    ("Dr. Saravanan Palani", 1), ("Dr. Srimonta Gayen", 3), ("Prof. Siddhartha P. Sarma", 2),
    ("Dr. Amit Singh", 2), ("Dr. Deepak K. Saini", 1), ("Dr. Kartik Sunagar", 1),
    ("Prof. P. K. Das", 1), ("Prof. Aninda J. Bhattacharyya", 1), ("Dr. Subinoy Rana", 1),
    ("Dr. Debasis Das", 2), ("Prof. Uday Maitra", 5), ("Prof. Joydeep Basu", 1),
    ("Dr. Sivaprakasam Ramamoorthy", 2),
    ("Dr. Mrinmoy De", 4),
]


def get_combined_ongoing_slots(cur):
    """Return ongoing data-collection and screening slots in one schedule."""
    cur.execute(
        """SELECT id AS booking_ref, user_name, pi_name, origin, esm,
                  registration_date, 'Data collection' AS service, id
           FROM bookings WHERE status='ongoing'
           UNION ALL
           SELECT id AS booking_ref, user_name, pi_name, origin, esm,
                  registration_date, 'Screening' AS service, id
           FROM screening_bookings WHERE status='ongoing'
           ORDER BY registration_date, id"""
    )
    return cur.fetchall()


@public_bp.route("/inventory")
def grid_inventory():
    username = request.args.get("username", request.args.get("researcher_name", "")).strip()
    sample_name = request.args.get("sample_name", "").strip()
    slot_type = request.args.get("slot_type", "")
    if slot_type not in {"screening", "datacollecting"}:
        slot_type = ""
    selection_mode = request.args.get("select") == "1" and bool(slot_type)
    admin_selection = selection_mode and request.args.get("admin_selection") == "1"
    # The name typed on the booking form, kept while the user confirms which
    # recorded name is theirs, so the selection is sent back to that form.
    booking_name = request.args.get("booking_name", "").strip()[:150] or username
    grids = []
    name_suggestions = []
    search_error = ""
    searched = bool(username)
    if len(username) > 150 or len(sample_name) > 180:
        search_error = "Enter a shorter username or sample name."
    elif searched and len(username) < PUBLIC_LOOKUP_MIN_LENGTH:
        search_error = (
            f"Enter your full username (at least {PUBLIC_LOOKUP_MIN_LENGTH} characters)."
        )
    elif searched:
        try:
            grids = public_inventory_grids(username, sample_name or None)
            name_suggestions = [
                item for item in similar_researcher_names(username) if not item["exact"]
            ]
        except InventoryError as exc:
            search_error = str(exc)
    return render_template(
        "public_inventory.html",
        name_suggestions=name_suggestions,
        booking_name=booking_name,
        grids=grids,
        grid_sessions=group_by_freezing_session(grids),
        username=username,
        sample_name=sample_name,
        searched=searched,
        search_error=search_error,
        selection_mode=selection_mode,
        selection_limit=None if admin_selection else 4,
        admin_selection=admin_selection,
        slot_type=slot_type,
    )


@public_bp.route("/inventory/search")
def search_public_inventory():
    researcher_name = request.args.get("user_name", "").strip()
    if not researcher_name:
        return jsonify({"grids": []})
    if len(researcher_name) > 150:
        return jsonify({"error": "Enter a shorter username."}), 400
    if len(researcher_name) < PUBLIC_LOOKUP_MIN_LENGTH:
        # Too short to identify a researcher; never fall back to a broad listing.
        return jsonify({"grids": []})
    try:
        grids = public_inventory_grids(researcher_name, available_only=True)
        suggestions = [
            {"name": item["name"], "grid_count": item["grid_count"]}
            for item in similar_researcher_names(researcher_name) if not item["exact"]
        ]
    except InventoryError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        # Loosely matching recorded names, offered as "Is this you?" choices.
        "suggestions": suggestions,
        "grids": [
            {
                "grid_code": grid["grid_code"],
                "sample_code": grid["sample_code"],
                "sample_name": grid["sample_name"],
                "status_code": grid["status_code"],
                "falcon_container_id": grid["falcon_container_id"],
                "grid_box_name": grid["grid_box_name"],
                "grid_position": grid["grid_position"],
                "grid_type_name": grid.get("gl_grid_type_name", ""),
                "label": grid.get("gl_label", grid["sample_name"]),
                "session_key": grid.get("gl_session_key", ""),
                "session_label": grid.get("gl_session_label", grid["sample_name"]),
                "frozen_label": grid.get("gl_frozen_label", ""),
                "grid_number": grid.get("gl_grid_number"),
                "session_total": grid.get("gl_session_total"),
            }
            for grid in grids
        ]
    })


ACADEMIC_USERS = [
    ("NCCS, Pune", 2), ("IIT-Bombay", 3), ("InStem", 1), ("Bose Institute", 4),
    ("Siddaganga Institute of Technology", 2), ("IISER-Mohali", 4), ("IISER-Pune", 1),
    ("NICED-ICMR, Kolkata", 3), ("CDRI, Lucknow", 10), ("Manipal College of Dental Sciences", 1),
    ("AIIMS, Delhi", 2), ("THSTI, New Delhi", 4), ("IICB, Kolkata", 1),
    ("Raman Research Institute", 1), ("IIT-Kanpur", 10), ("IIT-Delhi", 3),
    ("University of Kalyani", 1), ("JSS College of Pharmacy", 2), ("IISER-TVM", 4),
    ("ICAR-IVRI", 3), ("CSIR-NIO", 1),
]

INDUSTRY_USERS = [
    "INTAS Pharmaceuticals Ltd.", "Aragen Life Sciences Pvt. Ltd.", "Biovet Pvt. Ltd.",
    "Venture Incubation Center, Pune", "Aurea Biolabs", "PS Therapy, Coimbatore",
    "DifGen Pharmaceuticals Pvt. Ltd.", "Pidilite Industries Ltd.", "Enzene Biosciences Ltd.",
    "Hindustan Unilever Ltd.", "Bekaert Industries, Pune", "Vastu Vihar Biotech Pvt. Ltd.",
    "Go Green Bioenergy India Pvt. Ltd.", "Cedal Nano, IR", "SPARC, Mumbai",
    "Embright Infotech Pvt. Ltd.", "ITC, Bangalore", "Jodas Expoim Pvt. Ltd.",
    "FTF Pharma Pvt. Ltd.", "Alkem Laboratories Ltd., Mumbai", "Akay Natural Ingredients Pvt. Ltd.",
    "Almora Botanica UK Ltd.", "Zydus Lifesciences", "Adex Pharma Consultancy Services",
    "Global Calcium Pharma", "Expert Vision Labs", "Hitech Formulations Pvt. Ltd.", "Pfizer",
    "Alembic Pharmaceuticals", "Shilpa Medicare", "Pulse Pharma Ltd.",
    "Serum Institute of India (SII)", "Lupin Biotech, Delhi", "Slayback Pharma",
    "Gulfic Bioscience Ltd.", "Micro Labs Ltd., Bangalore", "Natco Pharma Ltd.",
    "Sun Pharmaceutical Industries Ltd.", "Mylan Laboratories", "Aurigene Pharmaceutical Services Ltd.",
    "Botanic Healthcare", "Bharat Serums and Vaccines Ltd.", "Hetero Labs Ltd.", "Cipla India Ltd.",
]


def _user_csv(filename, category, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Category", "PI / Institution / Organisation", "Users"])
    writer.writerows((category, label, count) for label, count in rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def get_slideshow_images():
    """Return list of image filenames from static/slideshow/."""
    try:
        slideshow_dir = os.path.join(current_app.static_folder, "slideshow")
        if not os.path.exists(slideshow_dir):
            return []
        valid = (".jpg", ".jpeg", ".png", ".gif")
        filenames = os.listdir(slideshow_dir)
        webp_stems = {
            os.path.splitext(filename)[0].casefold()
            for filename in filenames
            if filename.lower().endswith(".webp")
        }
        return [
            filename
            for filename in filenames
            if filename.lower().endswith(".webp")
            or (
                filename.lower().endswith(valid)
                and os.path.splitext(filename)[0].casefold() not in webp_stems
            )
        ]
    except Exception as exc:
        current_app.logger.error(f"Slideshow error: {exc}")
        return []


def get_user_statistics():
    """Combine the published baseline with newly logged registrations."""
    baseline = {"Internal": 74, "External": 64, "Industry": 25}
    try:
        conn = get_db()
        cur = conn.cursor()
        registrations = []
        for table in ("bookings", "screening_bookings", "freezing_bookings"):
            cur.execute(
                f"SELECT origin, user_name, email FROM {table} "
                "WHERE origin IS NOT NULL AND origin != ''"
            )
            registrations.extend(cur.fetchall())
        conn.close()

        seen_users = set()
        for row in registrations:
            origin = row["origin"] if isinstance(row, dict) else row[0]
            user_name = row["user_name"] if isinstance(row, dict) else row[1]
            email = row["email"] if isinstance(row, dict) else row[2]
            identity = clean_display_name(email or user_name).casefold()
            if not identity or identity in seen_users:
                continue
            seen_users.add(identity)
            normalized = origin.strip().lower()
            category = (
                "Industry" if "industry" in normalized
                else "Internal" if "internal" in normalized
                else "External" if "external" in normalized or "academic" in normalized
                else origin.strip()
            )
            baseline[category] = baseline.get(category, 0) + 1
        return sorted(baseline.items(), key=lambda item: item[1], reverse=True)
    except Exception as exc:
        current_app.logger.error(f"User statistics error: {exc}")
        return []


# Color palette for pie charts (vibrant and visually appealing)
CHART_COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
    "#F7DC6F", "#BB8FCE", "#85C1E2", "#F8B88B", "#52C9B7",
    "#ED7E4D", "#6C96C2", "#FF8A5B", "#4A9B7F", "#D4A574",
    "#7B68EE", "#FF69B4", "#20B2AA", "#FFB347", "#87CEEB",
    "#FF7F50", "#32CD32", "#FFD700", "#FF4500", "#00CED1",
]


def get_color_for_index(index):
    """Get a color from the palette for a given index."""
    return CHART_COLORS[index % len(CHART_COLORS)]


STRUCTURES_FALLBACK = "85+"  # shown only until the first EMDB check is stored
PUBLICATION_FINDER_CACHE_KEY = "publication_finder"
EMDB_STRUCTURES_CACHE_KEY = "emdb_structures"
EMDB_REVIEW_CACHE_KEY = "emdb_structures_review"
# Finder results an admin marked "Not ours" (e.g. papers that used the NCBS
# National Cryo-EM Facility). Kept across searches so they are not suggested
# again: {"items": {key: {"title", "year", "doi_url", "dismissed_at"}}}.
PUBLICATION_FINDER_DISMISSED_KEY = "publication_finder_dismissed"


def get_facility_metrics():
    """Return the publication count shown on the Publications page and the
    stored EMDB structure count."""
    return {
        "publications": count_site_publications(),
        "structures": get_structures_metric(),
    }


def get_structures_metric():
    """Stored EMDB structure count, or the "85+" fallback before the first check."""
    try:
        payload = load_cache_payload(EMDB_STRUCTURES_CACHE_KEY)
    except Exception as exc:
        current_app.logger.warning("EMDB structure count read failed: %s", exc)
        return STRUCTURES_FALLBACK
    if isinstance(payload, dict) and isinstance(payload.get("count"), int):
        return payload["count"]
    return STRUCTURES_FALLBACK


def load_finder_dismissed():
    """{candidate key: details} of finder results marked "Not ours"."""
    try:
        state = load_cache_payload(PUBLICATION_FINDER_DISMISSED_KEY) or {}
    except Exception as exc:
        current_app.logger.warning("Dismissed publications read failed: %s", exc)
        return {}
    items = state.get("items") if isinstance(state, dict) else None
    return dict(items) if isinstance(items, dict) else {}


def save_finder_dismissed(items):
    save_cache_payload(PUBLICATION_FINDER_DISMISSED_KEY, {"items": items})


def load_emdb_review_state():
    state = load_cache_payload(EMDB_REVIEW_CACHE_KEY) or {}
    return {
        "approved": list(state.get("approved") or []),
        "dismissed": list(state.get("dismissed") or []),
    }


def refresh_emdb_structures():
    """Query EMDB, store the result (with the previous ids for "new since last
    check") and return the stored payload. Raises EmdbError on failure, in
    which case the previously stored payload is kept."""
    previous = load_cache_payload(EMDB_STRUCTURES_CACHE_KEY)
    finder_run = load_cache_payload(PUBLICATION_FINDER_CACHE_KEY) or {}
    review = load_emdb_review_state()
    report = {}
    result = emdb_structures.compute_structures(
        get_site_publications(),
        # Papers marked "Not ours" never contribute structures.
        finder_candidates=publication_finder.without_dismissed(
            finder_run.get("candidates"), load_finder_dismissed()
        ),
        approved_ids=review["approved"],
        dismissed_ids=review["dismissed"],
        report=report,
    )
    payload = emdb_structures.build_payload(
        result, previous=previous if isinstance(previous, dict) else None,
        checked_at=_utc_now().strftime("%Y-%m-%d %H:%M:%S"), report=report,
    )
    save_cache_payload(EMDB_STRUCTURES_CACHE_KEY, payload)
    return payload


def get_managed_publications():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, citation, doi_url, published_year FROM managed_publications "
        "WHERE is_visible=1 ORDER BY COALESCE(published_year, 0) DESC, id DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _publication_dedupe_keys(item):
    keys = []
    doi = normalize_doi(item.get("doi"))
    if doi:
        keys.append(("doi", doi))
    text = normalize_title(item.get("text") or item.get("html") or "")
    if text:
        keys.append(("text", text))
    return keys


def get_site_publications(managed_rows=None):
    """The website's Publications list: visible managed entries first, then
    the curated (hard-coded) list, without duplicates (same DOI or same
    citation text).

    Items are dicts with "year" (0 = not stated), "doi" (URL or None),
    "origin" ("managed" or "curated"), and "text" (managed, plain text) or
    "html" (curated, trusted markup). Managed items also carry "id".
    """
    if managed_rows is None:
        managed_rows = get_managed_publications()
    items = [
        {
            "id": row["id"], "year": row["published_year"] or 0,
            "text": row["citation"], "doi": row["doi_url"], "origin": "managed",
        }
        for row in managed_rows
    ]
    items += [dict(entry, origin="curated") for entry in CURATED_PUBLICATIONS]
    seen = set()
    unique = []
    for item in items:
        keys = _publication_dedupe_keys(item)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        unique.append(item)
    return unique


def count_site_publications():
    return len(get_site_publications())


def get_managed_instruments():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT name, category, description, specifications, image_url "
        "FROM managed_instruments WHERE is_visible=1 ORDER BY id DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_managed_page(page_key):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM managed_pages WHERE page_key=?", [page_key])
    page = cur.fetchone()
    cur.close()
    conn.close()
    return page


def get_active_facility_notices():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, title, message, event_date, link_url
           FROM facility_notices WHERE is_active=1
           ORDER BY COALESCE(event_date, '1900-01-01') DESC, updated_at DESC, id DESC
           LIMIT 5"""
    )
    rows = []
    for row in cur.fetchall():
        notice = dict(row)
        if isinstance(notice["event_date"], str) and notice["event_date"]:
            notice["event_date"] = datetime.date.fromisoformat(notice["event_date"])
        rows.append(notice)
    cur.close()
    conn.close()
    return rows


# ── Precomputed site statistics ─────────────────────────────────────────────
# The home page statistics scan the bookings tables. The full payload is
# computed by refresh_site_stats.py (daily cron) or a background thread and
# stored in site_stats_cache, so requests only read one row. The publication
# count is cheap (managed_publications + the curated list) and is re-read on
# every request so it always matches the Publications page.
SITE_STATS_CACHE_KEY = "user_statistics"
SITE_STATS_MAX_AGE_SECONDS = 24 * 3600
_site_stats_refresh_lock = threading.Lock()


def build_user_statistics_payload(stats, metrics):
    """Build the /api/user-statistics JSON body (shape relied on by home JS)."""
    total_users = sum(count for _, count in stats)
    chart_data = []
    if total_users > 0:
        running_percentage = 0
        for index, (label, count) in enumerate(stats):
            percentage = (count / total_users) * 100
            chart_data.append({
                'label': label,
                'value': count,
                'percentage': round(percentage, 1),
                'color': get_color_for_index(index),
                'start': round(running_percentage, 1),
                'end': round(running_percentage + percentage, 1)
            })
            running_percentage += percentage
    return {
        'success': True,
        'total': total_users,
        'data': chart_data,
        'metrics': metrics,
    }


def compute_site_stats():
    """Compute the statistics payload (database only)."""
    return build_user_statistics_payload(get_user_statistics(), get_facility_metrics())


def with_current_publication_count(payload):
    """Return ``payload`` with metrics.publications set to the number of
    entries on the Publications page (a stored payload may predate an edit)
    and metrics.structures set to the stored EMDB count, once one exists."""
    if not payload or not isinstance(payload.get("metrics"), dict):
        return payload
    metrics = dict(payload["metrics"])
    try:
        metrics["publications"] = count_site_publications()
        structures = get_structures_metric()
        if structures != STRUCTURES_FALLBACK:
            metrics["structures"] = structures
    except Exception as exc:
        current_app.logger.warning("Publication count failed: %s", exc)
        return payload
    return dict(payload, metrics=metrics)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def load_site_stats():
    """Return (payload, age_seconds) from site_stats_cache, or (None, None)."""
    try:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT payload, updated_at FROM site_stats_cache WHERE cache_key=?",
                [SITE_STATS_CACHE_KEY],
            )
            row = cur.fetchone()
        finally:
            cur.close()
            conn.close()
    except Exception as exc:
        current_app.logger.warning("Site statistics cache read failed: %s", exc)
        return None, None
    if not row:
        return None, None
    try:
        payload = json.loads(row["payload"])
        updated_at = row["updated_at"]
        if not isinstance(updated_at, datetime.datetime):
            updated_at = datetime.datetime.fromisoformat(str(updated_at))
        age = (_utc_now() - updated_at.replace(tzinfo=None)).total_seconds()
    except (TypeError, ValueError, KeyError) as exc:
        current_app.logger.warning("Site statistics cache row is invalid: %s", exc)
        return None, None
    return payload, age


def save_cache_payload(cache_key, payload):
    """Upsert a JSON payload into site_stats_cache under ``cache_key``."""
    conn = get_db()
    cur = conn.cursor()
    try:
        ensure_site_stats_cache_table(cur)
        values = [
            cache_key,
            json.dumps(payload),
            _utc_now().strftime("%Y-%m-%d %H:%M:%S"),
        ]
        if _is_sqlite_url():
            cur.execute(
                """INSERT INTO site_stats_cache (cache_key, payload, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       payload=excluded.payload, updated_at=excluded.updated_at""",
                values,
            )
        else:
            cur.execute(
                """INSERT INTO site_stats_cache (cache_key, payload, updated_at)
                   VALUES (?, ?, ?)
                   ON DUPLICATE KEY UPDATE
                       payload=VALUES(payload), updated_at=VALUES(updated_at)""",
                values,
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def load_cache_payload(cache_key):
    """Return the decoded payload stored under ``cache_key``, or None."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT payload FROM site_stats_cache WHERE cache_key=?", [cache_key])
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def save_site_stats(payload):
    save_cache_payload(SITE_STATS_CACHE_KEY, payload)


def refresh_site_stats():
    """Compute the full payload and store it."""
    payload = compute_site_stats()
    save_site_stats(payload)
    return payload


def start_site_stats_refresh():
    """Refresh in a daemon thread; at most one refresh runs per process.

    Returns the started thread, or None when a refresh is already running.
    """
    app = current_app._get_current_object()
    # Disabled by default under app.testing so unrelated tests never start
    # threads; tests of this behaviour enable it explicitly.
    if not app.config.get("SITE_STATS_BACKGROUND_REFRESH", not app.testing):
        return None
    if not _site_stats_refresh_lock.acquire(blocking=False):
        return None

    def run():
        try:
            with app.app_context():
                refresh_site_stats()
        except Exception:
            app.logger.exception("Background site statistics refresh failed")
        finally:
            _site_stats_refresh_lock.release()

    thread = threading.Thread(target=run, name="site-stats-refresh", daemon=True)
    try:
        thread.start()
    except Exception:
        _site_stats_refresh_lock.release()
        raise
    return thread


def get_cached_site_stats():
    """Return the stored payload (possibly stale) or None.

    A missing or stale payload schedules a background refresh. The
    publication count is always the current Publications-page count.
    """
    payload, age = load_site_stats()
    if payload is None or age is None or age > SITE_STATS_MAX_AGE_SECONDS:
        start_site_stats_refresh()
    return with_current_publication_count(payload)


def get_site_stats_payload():
    """Stored payload if any; otherwise the database-only payload computed now."""
    payload = get_cached_site_stats()
    if payload is not None:
        return payload
    return compute_site_stats()


@public_bp.route("/")
def index():
    return redirect(url_for("public.home"))


@public_bp.route("/home")
def home():
    slideshow_images = get_slideshow_images()
    return render_template(
        "home.html",
        slideshow_images=slideshow_images,
        page_content=get_managed_page("home"),
        user_stats=get_cached_site_stats(),
        structures_count=get_structures_metric(),
    )


@public_bp.route("/api/user-statistics")
def api_user_statistics():
    """Serve the precomputed statistics payload."""
    try:
        return jsonify(get_site_stats_payload())
    except Exception:
        current_app.logger.exception("User statistics API error")
        return jsonify({
            'success': False,
            'error': 'Statistics are temporarily unavailable.',
        }), 500


PUBLIC_SITEMAP_ENDPOINTS = (
    "public.home", "public.about", "public.team", "public.facility",
    "public.workflow", "public.equipments", "public.publications",
    "public.events", "public.community_gallery", "public.grid_inventory",
    "public.rates", "public.faq", "public.contact",
    "register.register", "freezing.freezing_schedule",
    "screening.screening_list", "imaging.list_view",
)


@public_bp.route("/robots.txt")
def robots_txt():
    body = "\n".join((
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        f"Sitemap: {url_for('public.sitemap_xml', _external=True)}",
        "",
    ))
    return Response(body, mimetype="text/plain")


@public_bp.route("/sitemap.xml")
def sitemap_xml():
    from xml.sax.saxutils import escape

    urls = []
    for endpoint in PUBLIC_SITEMAP_ENDPOINTS:
        try:
            urls.append(url_for(endpoint, _external=True))
        except Exception:  # endpoint not registered (e.g. partial app)
            continue
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{escape(url)}</loc></url>\n" for url in urls)
        + "</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


def _admin_only_download():
    """The user lists name PIs and institutions, so only signed-in staff get them."""
    if session.get("admin_logged_in") and session.get("admin_user_id"):
        return None
    return redirect(url_for("admin.login"))


@public_bp.route("/downloads/iisc-users.csv")
def download_iisc_users():
    if (denied := _admin_only_download()):
        return denied
    return _user_csv("iisc-users.csv", "IISc internal", PI_USERS)


@public_bp.route("/downloads/academic-users.csv")
def download_academic_users():
    if (denied := _admin_only_download()):
        return denied
    return _user_csv("academic-external-users.csv", "Academic / external", ACADEMIC_USERS)


@public_bp.route("/downloads/industry-users.csv")
def download_industry_users():
    if (denied := _admin_only_download()):
        return denied
    return _user_csv(
        "industry-users.csv",
        "Industry",
        [(organisation, 1) for organisation in INDUSTRY_USERS],
    )


@public_bp.route("/about")
def about():
    return render_template("about.html", page_content=get_managed_page("about"))


@public_bp.route("/team")
def team():
    return render_template("team.html", page_content=get_managed_page("team"))


@public_bp.route("/facility")
def facility():
    return render_template("facility.html", page_content=get_managed_page("facility"))


@public_bp.route("/workflow")
def workflow():
    return render_template("workflow.html", page_content=get_managed_page("workflow"))


@public_bp.route("/equipments")
def equipments():
    return render_template("equipments.html", managed_instruments=get_managed_instruments(), page_content=get_managed_page("equipments"))


_EMDB_ID_RE = re.compile(r"\bEMD-(\d{4,6})\b")


def link_emdb_ids(text):
    """Escape a citation (unless it is already trusted Markup) and link any
    EMDB accession such as EMD-12345 to its EBI entry page."""
    safe = text if isinstance(text, Markup) else escape(text or "")
    return Markup(_EMDB_ID_RE.sub(
        lambda m: (
            f'<a href="https://www.ebi.ac.uk/emdb/EMD-{m.group(1)}" '
            f'target="_blank" rel="noopener noreferrer">EMD-{m.group(1)}</a>'
        ),
        str(safe),
    ))


@public_bp.route("/publication")
def publications():
    return render_template(
        "pub.html",
        publications=get_site_publications(),
        page_content=get_managed_page("publications"),
        link_emdb=link_emdb_ids,
    )


@public_bp.route("/events")
def events():
    return render_template("events.html", page_content=get_managed_page("events"))


@public_bp.route("/community-gallery")
def community_gallery():
    return render_template("community_gallery.html")


def format_inr(amount):
    """Format an amount as rupees with Indian digit grouping (e.g. Rs 1,23,456)."""
    from decimal import Decimal, ROUND_HALF_UP

    value = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, _, paise = f"{value:.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    text = f"\u20b9{whole}" if paise == "00" else f"\u20b9{whole}.{paise}"
    return sign + text


RATE_CATEGORIES = (
    ("internal", "Internal (IISc)"),
    ("academic", "Academic (other educational institutes)"),
    ("industry", "Industry"),
)

# Worked example on /rates, computed by revenue.calculate_charge_sheet (the
# function behind every issued charge sheet) so the page cannot drift.
RATES_WORKED_EXAMPLE = {
    "user_category": "academic",
    "service_stage": "Data Collection",
    "number_of_grids": 2,
    "grid_source": "facility",
    "grid_type": "normal_holey_carbon",
    "actual_slots": 1,
    "clip_base_source": "facility",
}

RATES_FREEZING_EXAMPLE = {
    "user_category": "academic",
    "service_stage": "Freezing / Grid Registration",
    "number_of_grids": 4,
    "grid_source": "self_owned",
}


def public_rate_table():
    """Single source for the public "Charge sheet" table on /rates.

    The billing code is authoritative: every amount is read from
    revenue.CHARGE_CONFIG / PROCESSING_RATE / GST_RATE, and the data-processing
    eligibility rule comes from revenue.calculate_charge_sheet.  Each value list
    is in RATE_CATEGORIES order; a Decimal is INR before GST, a str is shown as-is.
    """
    from revenue import CHARGE_CONFIG, GST_RATE, calculate_charge_sheet

    keys = [key for key, _ in RATE_CATEGORIES]

    def config(getter):
        return [getter(CHARGE_CONFIG[key]) for key in keys]

    def processing(key):
        amount = calculate_charge_sheet(
            key, "Data Collection", 1, "self_owned", actual_slots=1, processing_requested=True,
        )["processing_charge"]
        return amount if amount else "Not applicable"

    return {
        "categories": [label for _, label in RATE_CATEGORIES],
        "gst_rate": GST_RATE,
        "gst": {
            key: (f"plus {int(CHARGE_CONFIG[key]['gst_rate'] * 100)}% GST"
                  if CHARGE_CONFIG[key]["gst_rate"] else "No GST")
            for key in keys
        },
        "rows": [
            {"id": "slot", "label": "Cryo-Electron Microscope analysis",
             "unit": "per 24-hour slot, screening and data collection",
             "values": config(lambda c: c["slot"])},
            {"id": "grid_normal", "label": "Cryo-EM holey carbon grid",
             "unit": "per grid, facility-provided",
             "values": config(lambda c: c["grid"]["normal_holey_carbon"])},
            {"id": "grid_gold", "label": "Gold / carbon-coated / graphene-oxide grid",
             "unit": "per grid, facility-provided",
             "values": config(lambda c: c["grid"]["gold_carbon_graphene"])},
            {"id": "clip_base", "label": "C-clip and base ring",
             "unit": "per unclipped grid, screening and data collection",
             "values": config(lambda c: c["clip_base"])},
            {"id": "clip_base_note", "note": True,
             "label": ("Charged for each grid that is not already clipped. Not charged if "
                       "your lab provides the C-clip and base ring.")},
            {"id": "processing", "label": "Data processing / image analysis",
             "unit": "if requested", "values": [processing(key) for key in keys]},
            # Handling charges are grouped together at the bottom of the table.
            {"id": "handling_freezing",
             "label": "Handling charge \u2014 Grid freezing (Glow discharger & Vitrobot)",
             "unit": "per 4 grids, pro-rated per grid",
             "row_note": "Charged on the freezing booking.",
             "values": config(lambda c: c["handling_per_4"])},
            {"id": "handling_clipping", "label": "Handling charge \u2014 Grid clipping",
             "unit": "per 4 grids, pro-rated per grid",
             "row_note": ("Charged on the screening / clipping or data-collection booking, "
                          "in addition to the C-clip and base ring."),
             "values": config(lambda c: c["handling_per_4"])},
        ],
    }


def _rates_effective_date():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    except Exception:  # tzdata unavailable: IST is a fixed UTC+05:30 offset
        return (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=5, minutes=30)).date()


def build_rates_context():
    from revenue import CHARGE_CONFIG, calculate_charge_sheet
    from charge_sheet import _billing_lines

    table = public_rate_table()
    keys = [key for key, _ in RATE_CATEGORIES]
    rows = []
    for row in table["rows"]:
        if row.get("note"):
            rows.append(row)
            continue
        cells = []
        for key, value in zip(keys, row["values"]):
            if isinstance(value, str):
                cells.append({"text": value, "gst": ""})
            else:
                gst = "" if key == "internal" else table["gst"][key]
                cells.append({"text": format_inr(value), "gst": gst})
        rows.append({**row, "cells": cells})

    def worked_example(key, params, title):
        charges = calculate_charge_sheet(**params)
        return {
            "key": key,
            "title": title,
            "lines": [(name, qty, format_inr(amount))
                      for name, qty, amount in _billing_lines(charges, charges["service_stage"])],
            "subtotal": format_inr(charges["subtotal"]),
            "gst_percent": int(CHARGE_CONFIG[charges["user_category"]]["gst_rate"] * 100),
            "gst": format_inr(charges["gst"]),
            "total": format_inr(charges["grand_total"]),
        }

    labels = dict(RATE_CATEGORIES)
    collection = RATES_WORKED_EXAMPLE
    freezing = RATES_FREEZING_EXAMPLE
    examples = [
        worked_example("example", collection,
                       f"{labels[collection['user_category']]} user, data collection with "
                       f"{collection['number_of_grids']} facility-provided holey carbon grids "
                       f"(not pre-clipped) and {collection['actual_slots']} 24-hour slot"),
        worked_example("freezing-example", freezing,
                       f"{labels[freezing['user_category']]} user, freezing booking for "
                       f"{freezing['number_of_grids']} of their own grids"),
    ]
    return {
        "categories": table["categories"],
        "gst_labels": [table["gst"][key] for key in keys],
        "rows": rows,
        "examples": examples,
        "effective_date": _rates_effective_date(),
    }


@public_bp.route("/rates")
def rates():
    return render_template("rates.html", **build_rates_context())


@public_bp.route("/faq")
def faq():
    from blueprints.freezing import GRID_LIMIT_PER_DAY
    return render_template("faq.html", freezing_grid_limit=GRID_LIMIT_PER_DAY)


@public_bp.route("/contact")
def contact():
    return render_template("contact.html")
