import os
import datetime
import secrets
from hmac import compare_digest
from flask import Flask, abort, request, session

from database import init_db
from extensions import init_mail

from blueprints.public    import public_bp
from blueprints.imaging   import imaging_bp
from blueprints.freezing  import freezing_bp
from blueprints.screening import screening_bp
from blueprints.register  import register_bp
from blueprints.admin     import admin_bp


def create_app() -> Flask:
    app = Flask(__name__)

    app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(minutes=30)
    secure_cookies = os.environ.get("SECURE_COOKIES", "").lower() in {"1", "true", "yes"}
    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if not secret_key:
        if secure_cookies:
            raise RuntimeError("SECRET_KEY must be configured when SECURE_COOKIES is enabled")
        secret_key = "change-me-in-production"
        app.logger.warning("SECRET_KEY is using the development fallback; set SECRET_KEY in production")
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

    @app.context_processor
    def security_context():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return {"csrf_token": token}

    @app.before_request
    def protect_state_changing_requests():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        expected = session.get("_csrf_token")
        supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not compare_digest(expected, supplied):
            abort(400, description="Invalid or missing CSRF token.")

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        if request.path.startswith("/admin"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    init_mail(app)

    app.register_blueprint(public_bp)
    app.register_blueprint(imaging_bp)
    app.register_blueprint(freezing_bp)
    app.register_blueprint(screening_bp)
    app.register_blueprint(register_bp)
    app.register_blueprint(admin_bp)

    with app.app_context():
        init_db()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
