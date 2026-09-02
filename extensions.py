import os
import threading
import base64
import json
import urllib.error
import urllib.request
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

def init_mail(app):
    """Configure email delivery through Resend's HTTPS API."""
    app.config["RESEND_API_KEY"] = os.environ.get("RESEND_API_KEY", "").strip()
    app.config["RESEND_FROM_EMAIL"] = (
        os.environ.get("RESEND_FROM_EMAIL", "").strip()
        or "onboarding@resend.dev"
    )
    if not app.config["RESEND_API_KEY"]:
        app.logger.warning("Email is not configured; RESEND_API_KEY is missing")


def send_email(recipient: str, subject: str, body: str, cc=None, attachments=None):
    """Send email asynchronously through Resend's HTTPS API."""
    from flask import current_app

    app = current_app._get_current_object()

    def _send():
        with app.app_context():
            api_key = app.config.get("RESEND_API_KEY")
            if not api_key:
                app.logger.error(
                    "Email to %s was not sent because RESEND_API_KEY is not configured",
                    recipient,
                )
                return

            payload = {
                "from": app.config["RESEND_FROM_EMAIL"],
                "to": [recipient],
                "subject": subject,
                "text": body,
            }
            if cc:
                payload["cc"] = cc
            if attachments:
                payload["attachments"] = [
                    {
                        "filename": filename,
                        "content": base64.b64encode(data).decode("ascii"),
                    }
                    for filename, _content_type, data in attachments
                ]

            request = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    response.read()
                app.logger.info("Email to %s sent through Resend", recipient)
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                app.logger.error(
                    "Email to %s failed through Resend (HTTP %s): %s",
                    recipient,
                    exc.code,
                    details,
                )
            except urllib.error.URLError as exc:
                app.logger.error("Email to %s failed through Resend: %s", recipient, exc.reason)

    threading.Thread(target=_send, daemon=True).start()
