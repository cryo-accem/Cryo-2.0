import os
import threading
import base64
import datetime
import http.client
import json
import socket
import urllib.error
import urllib.request
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

try:
    from zoneinfo import ZoneInfo

    INDIA_TZ = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - tzdata missing; India has no DST
    INDIA_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime.datetime:
    """Current wall-clock time in India as a naive datetime (facility local time)."""
    return datetime.datetime.now(INDIA_TZ).replace(tzinfo=None)


def today_ist() -> datetime.date:
    """Today's date in India, independent of the server's timezone."""
    return datetime.datetime.now(INDIA_TZ).date()

def init_mail(app):
    """Configure email delivery through a Google Apps Script HTTPS relay."""
    app.config["GOOGLE_APPS_SCRIPT_URL"] = os.environ.get(
        "GOOGLE_APPS_SCRIPT_URL", ""
    ).strip()
    app.config["GOOGLE_APPS_SCRIPT_TOKEN"] = os.environ.get(
        "GOOGLE_APPS_SCRIPT_TOKEN", ""
    ).strip()
    if not app.config["GOOGLE_APPS_SCRIPT_URL"] or not app.config["GOOGLE_APPS_SCRIPT_TOKEN"]:
        app.logger.warning(
            "Email is not configured; Google Apps Script URL or token is missing"
        )


def _deliver_email(app, recipient, subject, body, cc, attachments, priority):
    with app.app_context():
        relay_url = app.config.get("GOOGLE_APPS_SCRIPT_URL")
        relay_token = app.config.get("GOOGLE_APPS_SCRIPT_TOKEN")
        if not relay_url or not relay_token:
            app.logger.error(
                "Email to %s was not sent because Google Apps Script email relay is not configured",
                recipient,
            )
            return False

        payload = {
            "token": relay_token,
            "to": recipient,
            "subject": subject,
            "body": body,
        }
        if priority:
            payload.update(
                priority=priority,
                importance=priority,
                headers={
                    "Importance": priority,
                    "X-Priority": "1" if priority == "high" else "3",
                },
            )
        if cc:
            payload["cc"] = ",".join(cc)
        if attachments:
            payload["attachments"] = [
                {
                    "filename": filename,
                    "content": base64.b64encode(data).decode("ascii"),
                }
                for filename, _content_type, data in attachments
            ]

        relay_request = urllib.request.Request(
            relay_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(relay_request, timeout=15) as response:
                response_body = response.read().decode("utf-8", errors="replace")
            app.logger.info(
                "Email to %s sent through Google Apps Script: %s",
                recipient,
                response_body,
            )
            return True
        except urllib.error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8", errors="replace")
            except Exception:
                details = ""
            app.logger.error(
                "Email to %s failed through Google Apps Script (HTTP %s): %s",
                recipient,
                exc.code,
                details,
            )
        except urllib.error.URLError as exc:
            app.logger.error(
                "Email to %s failed through Google Apps Script: %s",
                recipient,
                exc.reason,
            )
        except (socket.timeout, TimeoutError, http.client.HTTPException, OSError, ValueError) as exc:
            app.logger.error(
                "Email to %s failed through Google Apps Script: %s: %s",
                recipient,
                type(exc).__name__,
                exc,
            )
        return False


def send_email_sync(
    recipient: str, subject: str, body: str, cc=None, attachments=None, priority=None
):
    """Send through the configured email relay and report its HTTP result."""
    from flask import current_app

    return _deliver_email(
        current_app._get_current_object(),
        recipient,
        subject,
        body,
        cc,
        attachments,
        priority,
    )


def send_email(recipient: str, subject: str, body: str, cc=None, attachments=None,
               priority=None):
    """Send email asynchronously through a Google Apps Script HTTPS relay."""
    from flask import current_app

    app = current_app._get_current_object()
    threading.Thread(
        target=_deliver_email,
        args=(app, recipient, subject, body, cc, attachments, priority),
        daemon=True,
    ).start()
