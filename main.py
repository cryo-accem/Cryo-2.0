import os
import datetime
import hashlib
import secrets
from hmac import compare_digest
from flask import Flask, abort, flash, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.security import safe_join
from flask_compress import Compress

from database import init_db
from extensions import init_mail, today_ist

from blueprints.public    import public_bp, get_active_facility_notices
from blueprints.imaging   import imaging_bp
from blueprints.freezing  import freezing_bp
from blueprints.screening import screening_bp
from blueprints.register  import register_bp
from blueprints.admin     import admin_bp
from blueprints.inventory import inventory_bp


compress = Compress()

# CSS and JS links get an automatic ?v=<content hash>, so every edit produces a new
# URL and the one-year browser cache never serves a stale file. Nobody has to bump
# version strings by hand. Fonts keep their fixed ?v= because the font preload link
# and the CSS @font-face must use the identical URL.
_FINGERPRINTED_STATIC_EXTENSIONS = (".css", ".js")
_static_fingerprints = {}


def _static_fingerprint(static_folder, filename):
    path = safe_join(static_folder, filename)
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _static_fingerprints.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:12]
    _static_fingerprints[path] = (mtime, digest)
    return digest



COMPRESS_MIMETYPES = [
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "application/json",
    "image/svg+xml",
]
_LONG_CACHE_STATIC_EXTENSIONS = (
    ".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff2", ".woff", ".ttf",
)
_COMPRESSED_ETAG_SUFFIXES = (':br"', ':gzip"', ':zstd"', ':deflate"')


def _strip_compressed_etag_suffixes(header):
    """Flask-Compress appends ':<algorithm>' to ETags; undo that for matching."""
    parts = []
    for tag in header.split(","):
        tag = tag.strip()
        for suffix in _COMPRESSED_ETAG_SUFFIXES:
            if tag.endswith(suffix):
                tag = tag[: -len(suffix)] + '"'
                break
        parts.append(tag)
    return ", ".join(parts)


class _CSRFToken(str):
    def __call__(self):
        return str(self)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.url_defaults
    def add_static_fingerprint(endpoint, values):
        if endpoint != "static":
            return
        filename = values.get("filename") or ""
        if filename.endswith(_FINGERPRINTED_STATIC_EXTENSIONS):
            fingerprint = _static_fingerprint(app.static_folder, filename)
            if fingerprint:
                values["v"] = fingerprint

    app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(minutes=30)
    secure_cookies = os.environ.get("SECURE_COOKIES", "").lower() in {"1", "true", "yes"}
    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if not secret_key:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        is_production = secure_cookies or (
            database_url and not database_url.lower().startswith("sqlite:")
        )
        if is_production:
            raise RuntimeError(
                "SECRET_KEY must be configured in production "
                "(SECURE_COOKIES is enabled or a server DATABASE_URL is set)"
            )
        # Development only: a random per-process key. Sessions do not survive
        # restarts and are not shared between worker processes.
        secret_key = secrets.token_urlsafe(48)
        app.logger.warning(
            "SECRET_KEY is not set; using a random development key. "
            "Set SECRET_KEY in production."
        )
    app.secret_key = secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
    )
    app.config["CHARGE_SHEET_CC_EMAIL"] = os.environ.get("CHARGE_SHEET_CC_EMAIL", "")
    app.config["PAYMENT_PROOF_DIR"] = os.environ.get(
        "PAYMENT_PROOF_DIR",
        os.path.join(app.instance_path, "payment_proofs"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    # Static cache lifetimes are set per-request in add_security_headers;
    # leaving this as None keeps Flask's "no-cache" (revalidate) default for
    # anything not explicitly given a longer lifetime there.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = None
    # Only text-like types are compressed; audio, CSV, PDF and ZIP downloads
    # pass through untouched (streamed CSV is excluded by mimetype too).
    app.config.update(
        COMPRESS_MIMETYPES=COMPRESS_MIMETYPES,
        COMPRESS_ALGORITHM=["br", "gzip"],
        COMPRESS_LEVEL=6,
        COMPRESS_BR_LEVEL=5,
        COMPRESS_MIN_SIZE=500,
    )

    @app.context_processor
    def security_context():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return {
            "csrf_token": _CSRFToken(token),
            "current_year": today_ist().year,
            "facility_notices": (
                get_active_facility_notices()
                if not request.path.startswith("/admin")
                else []
            ),
            "admin_assistant_enabled": bool(
                session.get("admin_logged_in") and not session.get("must_change_password")
            ),
        }

    @app.before_request
    def normalize_compressed_etags():
        # Let conditional requests for compressed static files still get 304s.
        if_none_match = request.environ.get("HTTP_IF_NONE_MATCH")
        if if_none_match and ":" in if_none_match:
            request.environ["HTTP_IF_NONE_MATCH"] = _strip_compressed_etag_suffixes(
                if_none_match
            )

    @app.before_request
    def protect_state_changing_requests():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        expected = session.get("_csrf_token")
        supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not compare_digest(expected, supplied):
            return _expired_form_response()

    def _expired_form_response():
        """A form posted after its session expired: explain, don't show a bare 400.

        Pages left open past the session lifetime (30 minutes for staff) post
        a token the server no longer has.  Staff go back to the sign-in page,
        scripts get JSON, and public visitors return to the page they were on.
        """
        endpoint = request.endpoint or ""
        if session.get("_csrf_token") or session.get("admin_logged_in"):
            # The session is alive but the token is missing or wrong: this is
            # not an expired page, so refuse as before.
            abort(400, description="Invalid or missing CSRF token.")
        staff_area = endpoint.startswith(("admin.", "inventory."))
        wants_json = (
            request.is_json
            or request.accept_mimetypes.best == "application/json"
            or endpoint in {"admin.billing_preview"}
            or endpoint.startswith("admin.assistant")
        )
        if wants_json:
            message = (
                "Your session has expired. Please sign in again."
                if staff_area else "This page has expired. Please reload it and try again."
            )
            return jsonify({"error": message}), (401 if staff_area else 400)
        if staff_area:
            flash("Your session expired, so nothing was saved. Please sign in again.", "error")
            return redirect(url_for("admin.login"))
        flash("This page had been open for a long time, so the form expired and was not sent. Please fill it in again.", "error")
        referrer = request.referrer or ""
        if referrer.startswith(request.host_url):
            return redirect(referrer)
        return redirect(url_for("public.home"))

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        microphone_policy = (
            "microphone=(self)"
            if request.path.startswith("/admin/")
            and request.path != "/admin/"
            else "microphone=()"
        )
        response.headers.setdefault(
            "Permissions-Policy",
            f"camera=(), {microphone_policy}, geolocation=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        if request.path.startswith("/admin"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.endpoint == "static" and response.status_code in (200, 206, 304):
            path = request.path.lower()
            if request.args.get("v"):
                # Versioned URL: the content for this URL never changes.
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path.endswith(_LONG_CACHE_STATIC_EXTENSIONS):
                response.headers["Cache-Control"] = "public, max-age=604800"
            # Unversioned CSS/JS keep Flask's default "no-cache" revalidation.
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    init_mail(app)
    compress.init_app(app)

    app.register_blueprint(public_bp)
    app.register_blueprint(imaging_bp)
    app.register_blueprint(freezing_bp)
    app.register_blueprint(screening_bp)
    app.register_blueprint(register_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(inventory_bp)

    @app.route("/favicon.ico")
    def favicon():
        # Browsers ask for /favicon.ico directly: serve the IISc logo icon.
        return send_from_directory(
            os.path.join(app.static_folder, "images"), "favicon.ico",
            mimetype="image/vnd.microsoft.icon", max_age=86400,
        )

    with app.app_context():
        init_db()
        from migrations.apply_grid_inventory import apply_migration

        apply_migration()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
