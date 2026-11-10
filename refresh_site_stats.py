"""Recompute the public home-page statistics and store them in site_stats_cache.

Run daily (Render cron) so /api/user-statistics and the home page can serve the
stored payload instantly instead of scanning the bookings tables. The
publication count is the number of entries on the Publications page; the
EMDB structure count is refreshed from the EMDB API (see emdb_structures.py).
"""

import logging

from flask import Flask

from blueprints.public import refresh_emdb_structures, refresh_site_stats
from database import ensure_site_stats_cache_table, get_db


def run(app):
    with app.app_context():
        conn = get_db()
        cur = conn.cursor()
        ensure_site_stats_cache_table(cur)
        conn.commit()
        cur.close()
        conn.close()
        # EMDB first so the stored statistics carry the new structure count.
        # An EMDB outage keeps the previous count and must not block the rest.
        try:
            structures = refresh_emdb_structures()
            app.logger.info("Stored EMDB structures: %s.", structures["count"])
        except Exception:
            app.logger.exception("EMDB structure refresh failed; keeping the previous count")
        payload = refresh_site_stats()
        app.logger.info(
            "Stored site statistics: %s users, %s publications.",
            payload["total"],
            payload["metrics"]["publications"],
        )
        return payload


def main():
    logging.basicConfig(level=logging.INFO)
    app = Flask(__name__)
    run(app)


if __name__ == "__main__":
    main()
