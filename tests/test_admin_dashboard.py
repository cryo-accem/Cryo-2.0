"""Admin dashboard (/admin/panel): "needs attention" counts, chart fixes and
revenue range handling."""
import datetime
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from main import create_app

TODAY = datetime.date(2026, 11, 10)


def _day(offset):
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


class AdminDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "dashboard.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        # Pin "today" (IST) so date-relative assertions are stable.
        self.today = patch("blueprints.admin.today_ist", return_value=TODAY)
        self.today.start()
        self.addCleanup(self.today.stop)

        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.app.app_context():
            conn = database.get_db()
            self._seed(conn.cursor())
            conn.commit()
            conn.close()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            admin_conn = database.get_db()
            session["admin_user_id"] = admin_conn.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            admin_conn.execute("UPDATE users SET must_change_password=0")
            admin_conn.commit()
            admin_conn.close()

    def _seed(self, cur):
        def booking(table, name, status, pi="Dr. Meera Iyer", completion=None,
                    sent_at=None, payment="Payment Pending", total="1180.00"):
            cur.execute(
                f"""INSERT INTO {table}
                    (user_name, pi_name, email, origin, sample_name, status,
                     completion_date, total_billed, charge_sheet_sent_at, payment_status)
                    VALUES (?, ?, ?, 'External', 'Sample', ?, ?, ?, ?, ?)""",
                (name, pi, f"{name.replace(' ', '.').lower()}@example.com", status,
                 completion, total if completion else None, sent_at, payment),
            )

        for table in ("bookings", "screening_bookings"):
            booking(table, f"{table} waiting a", "waiting")
            booking(table, f"{table} waiting b", "waiting")
            booking(table, f"{table} ongoing", "ongoing")
            booking(table, f"{table} unsent", "completed", completion=_day(-2))
            booking(table, f"{table} nonbillable", "completed", pi="Prof. Somnath Dutta",
                    completion=_day(-2))
            booking(table, f"{table} sent", "completed", completion=_day(-9),
                    sent_at=f"{_day(-8)} 10:00:00")
            booking(table, f"{table} paid", "completed", completion=_day(-30),
                    sent_at=f"{_day(-29)} 10:00:00", payment="Payment Verified")
        # Only data collection gets a second ongoing session.
        booking("bookings", "bookings ongoing b", "ongoing")

        for name, date, status in (
            ("Freeze Today", _day(0), "active"),
            ("Freeze Overdue", _day(-3), "active"),
            ("Freeze Later A", _day(2), "active"),
            ("Freeze Later B", _day(5), "active"),
            ("Freeze Done", _day(-6), "completed"),
        ):
            cur.execute(
                """INSERT INTO freezing_bookings
                   (user_name, pi_name, email, origin, sample_name, grids, freezing_date, status)
                   VALUES (?, 'Dr. Meera Iyer', ?, 'Internal', 'Apoferritin', 4, ?, ?)""",
                (name, f"{name.replace(' ', '.').lower()}@example.com", date, status),
            )
        for name, sent_at in (("Frz Sent", f"{_day(-4)} 16:00:00"), ("Frz Unsent", None)):
            cur.execute(
                """INSERT INTO completed_freezing
                   (user_name, pi_name, email, origin, sample_name, grids, freezing_date,
                    completed_at, total_billed, charge_sheet_sent_at, payment_status)
                   VALUES (?, 'Prof. Anand Rao', ?, 'External', 'Spike', 4, ?, ?, '590.00', ?,
                           'Payment Pending')""",
                (name, f"{name.replace(' ', '.').lower()}@example.com", _day(-4),
                 f"{_day(-4)} 15:30:00", sent_at),
            )

        grid_type = cur.execute(
            "SELECT grid_type_id FROM grid_types ORDER BY grid_type_id LIMIT 1"
        ).fetchone()[0]
        created = f"{_day(-4)} 11:00:00"
        cur.execute(
            """INSERT INTO inventory_samples
               (sample_code, sample_name, researcher_name, lab_name, created_at)
               VALUES ('S-1', 'Apoferritin', 'Freeze Done', 'Iyer lab', ?)""",
            (created,),
        )
        sample_id = cur.lastrowid
        statuses = ["Stored", "Frozen", "Frozen", "Clipped", "Discarded", "In Data Collection",
                    "Frozen"]
        for index, status in enumerate(statuses, 1):
            cur.execute(
                """INSERT INTO inventory_grids
                   (grid_code, sample_id, grid_type_id, current_status, clipped, created_at)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (f"G-{index}", sample_id, grid_type, status, created),
            )
            grid_id = cur.lastrowid
            if status == "Stored" or index == len(statuses):
                # The last "Frozen" grid already has a box position.
                cur.execute(
                    """INSERT INTO storage_locations
                       (grid_id, falcon_container_id, grid_box_name, grid_position, stored_at)
                       VALUES (?, 'F1', 'Box A', ?, ?)""",
                    (grid_id, str(index), created),
                )
        # A grid whose storage row was removed counts as unstored again.
        cur.execute(
            """INSERT INTO inventory_grids
               (grid_code, sample_id, grid_type_id, current_status, clipped, created_at)
               VALUES ('G-removed', ?, ?, 'Clipped', 1, ?)""",
            (sample_id, grid_type, created),
        )
        cur.execute(
            """INSERT INTO storage_locations
               (grid_id, falcon_container_id, grid_box_name, grid_position, stored_at, removed_at)
               VALUES (?, 'F1', 'Box A', '99', ?, ?)""",
            (cur.lastrowid, created, created),
        )

    def _panel(self, query=""):
        response = self.client.get(f"/admin/panel{query}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def _attention(self, html):
        tiles = re.findall(
            r'data-attention="([a-z_]+)">.*?data-attention-value>(\d+)</strong>', html, re.S
        )
        return {key: int(value) for key, value in tiles}

    # -- Needs attention --------------------------------------------------

    def test_attention_counts_match_the_database(self):
        html = self._panel()
        self.assertIn("Needs attention", html)
        self.assertEqual(
            self._attention(html),
            {
                "datacollecting": 2,   # waiting
                "screening": 2,        # waiting
                "freezing": 1,         # due today
                "unsent_bills": 3,     # DC + screening + freezing; non-billable PI excluded
                "unpaid": 3,           # sent and not verified
                # 2 frozen + 1 clipped without a box, plus 1 clipped whose storage
                # row was removed; stored, discarded and in-microscope grids excluded.
                "grids_unstored": 4,
            },
        )
        self.assertIn("2 ongoing on the microscope", html)
        self.assertIn("1 ongoing", html)
        self.assertIn("1 past date, not completed · 2 upcoming", html)
        self.assertIn("Updated", html)
        self.assertIn("IST", html)

    def test_attention_links_point_to_the_right_pages(self):
        html = self._panel()
        for key, url in (
            ("datacollecting", "/admin/datacollecting"),
            ("screening", "/admin/screening"),
            ("freezing", "/admin/freezing"),
            ("unsent_bills", "/admin/history?sheet=unsent"),  # opens filtered to Not sent
            ("unpaid", "#pending-payments-title"),
            ("grids_unstored", "/admin/inventory/"),
        ):
            self.assertRegex(html, rf'data-attention="{key}">\s*<a href="{re.escape(url)}"')
        for url in ("/admin/datacollecting", "/admin/screening", "/admin/freezing",
                    "/admin/history", "/admin/inventory/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_attention_on_an_empty_database_shows_zero_without_errors(self):
        with self.app.app_context():
            conn = database.get_db()
            for table in ("bookings", "screening_bookings", "freezing_bookings",
                          "completed_freezing", "storage_locations", "inventory_grids"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
            conn.close()
        html = self._panel()
        self.assertEqual(set(self._attention(html).values()), {0})
        self.assertIn("No sent bills are currently awaiting receipt.", html)
        self.assertIn("dash-tile--clear", html)

    def test_dashboard_survives_missing_inventory_tables(self):
        with self.app.app_context():
            conn = database.get_db()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE storage_locations")
            conn.commit()
            conn.close()
        html = self._panel()
        self.assertNotIn('data-attention="grids_unstored"', html)
        self.assertIn('data-attention="unsent_bills"', html)

    def test_overdue_freezing_is_flagged(self):
        html = self._panel()
        self.assertIn("dash-tile--freezing dash-tile--alert", html)

    # -- Pending payments --------------------------------------------------

    def test_pending_payments_show_dates_without_utc_timestamps(self):
        html = self._panel()
        self.assertIn("Frz Sent", html)
        self.assertNotIn(f"{_day(-4)} 15:30:00", html)
        self.assertIn(f'data-label="Completed">{_day(-4)}</td>', html)
        self.assertNotIn("Frz Unsent", html.split('id="pending-payments-title"')[1].split("</table>")[0])
        self.assertIn('aria-label="Send reminder mail to Frz Sent', html)

    # -- Charts ------------------------------------------------------------

    def test_pi_chart_has_a_legend_and_distinct_other_colour(self):
        html = self._panel()
        legend = html.split('aria-label="Users by PI">', 1)[1].split("</ul>", 1)[0]
        self.assertIn("Other PIs", legend)
        self.assertIn("Dr. Meera Iyer", self._panel())
        self.assertIn("#9aa9b5", legend)
        colours = re.findall(r"background: (#[0-9a-f]{6})", legend)
        self.assertEqual(len(colours), len(set(colours)))
        self.assertIn('class="donut-track"', html)

    def test_queue_chart_legend_lists_each_service(self):
        html = self._panel()
        legend = html.split('aria-label="Queue by service">', 1)[1].split("</ul>", 1)[0]
        self.assertIn("Data collecting (waiting)</span><strong>2</strong>", legend)
        self.assertIn("Freezing (active)</span><strong>4</strong>", legend)
        self.assertIn("Screening (waiting)</span><strong>2</strong>", legend)

    def test_weekly_chart_puts_historical_months_in_iso_weeks(self):
        html = self._panel("?period=weekly&range=custom&start=2026-02-01&end=2026-02-28")
        # 1 Feb 2026 is a Sunday, ISO week 2026-W05.
        self.assertIn('title="2026-W05: ₹167,000.00"', html)
        self.assertNotIn('title="2026-02-01:', html)
        labels = re.findall(r'class="revenue-bar-item" title="([^:]+):', html)
        self.assertTrue(labels)
        self.assertTrue(all(re.fullmatch(r"\d{4}-W\d{2}", label) for label in labels), labels)
        self.assertEqual(labels, sorted(labels))

    def test_future_historical_months_are_not_plotted(self):
        html = self._panel("?range=all")
        self.assertIn('title="2026-11:', html)
        self.assertNotIn('title="2026-12:', html)

    def test_monthly_bars_use_short_axis_labels(self):
        html = self._panel("?range=custom&start=2026-02-01&end=2026-02-28")
        self.assertIn('title="2026-02: ₹167,000.00"', html)
        self.assertIn("<span>Feb 26</span>", html)

    def test_comparison_chart_offers_the_figures_as_a_table(self):
        html = self._panel()
        self.assertIn("Show figures", html)
        self.assertIn('<th scope="row">Feb</th><td>₹167,000</td>', html)

    # -- Revenue range handling ------------------------------------------

    def test_applied_range_states_the_ist_dates(self):
        html = self._panel()
        self.assertIn("Showing this year revenue.", html)
        self.assertIn("01 Jan 2026 – 10 Nov 2026", html)

    def test_last_year_is_selectable(self):
        html = self._panel("?range=last_year")
        self.assertIn('<option value="last_year" selected>Last year</option>', html)
        self.assertIn("01 Jan 2025 – 31 Dec 2025", html)

    def test_unknown_range_falls_back_to_all_time(self):
        html = self._panel("?range=bogus")
        self.assertIn("Showing all available revenue.", html)
        self.assertNotIn("bogus", html)
        self.assertIn('<option value="all" selected>All time</option>', html)

    def test_out_of_bounds_custom_dates_do_not_crash(self):
        # Year 1 used to raise ValueError while shifting the comparison back a year.
        for query in (
            "?range=custom&start=0001-01-01&end=0001-12-31",
            "?range=custom&start=2026-01-01&end=9999-12-31",
            "?range=custom&start=2026-05-01&end=2026-01-01",
            "?range=custom&start=nonsense&end=2026-01-01",
        ):
            html = self._panel(query)
            self.assertIn("The custom dates were not valid", html, query)
            self.assertIn("Showing all available revenue.", html, query)

    def test_completed_booking_count_is_labelled_without_csic(self):
        with self.app.app_context():
            conn = database.get_db()
            conn.execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, status, completion_date,
                    total_billed, is_csic, csic_final_amount)
                   VALUES ('Csic User', 'Dr. K', 'csic@example.com', 'Industry', 'S',
                           'completed', ?, '2360.00', 1, '2360.00')""",
                (_day(-1),),
            )
            conn.commit()
            conn.close()
        html = self._panel()
        self.assertIn("Completed bookings (excl. CSIC)</span><strong>", html)
        self.assertIn("CSIC bookings</span><strong>1</strong>", html)


if __name__ == "__main__":
    unittest.main()


class DashboardResilienceTests(unittest.TestCase):
    """Production runs MySQL; these guard the dashboard against SQL that only SQLite accepts."""

    def setUp(self):
        import tempfile, os
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        url = patch.object(database, "DATABASE_URL", f"sqlite:///{os.path.join(self.temp_dir.name, 'r.sqlite3')}")
        url.start()
        self.addCleanup(url.stop)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["admin_logged_in"] = True
            conn = database.get_db()
            sess["admin_user_id"] = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()["id"]
            conn.close()

    def test_attention_sql_never_compares_timestamps_with_empty_string(self):
        import inspect, re
        from blueprints import admin as admin_module
        source = inspect.getsource(admin_module._dashboard_attention)
        self.assertIsNone(re.search(r"_at\s*=\s*''", source))

    def test_dashboard_still_loads_if_attention_summary_fails(self):
        with patch("blueprints.admin._dashboard_attention", side_effect=RuntimeError("db")):
            response = self.client.get("/admin/panel")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Needs attention", response.data)


class RevenueComparisonAxisTests(unittest.TestCase):
    def test_axis_max_rounds_up_to_even_steps(self):
        from blueprints.admin import _nice_axis_max
        self.assertEqual(_nice_axis_max(167000), 200000)
        self.assertEqual(_nice_axis_max(345000), 400000)
        self.assertEqual(_nice_axis_max(0), 0)

    def test_ticks_share_one_unit(self):
        from blueprints.admin import _inr_compact
        labels = [_inr_compact(200000 * step / 4, 200000) for step in range(5)]
        self.assertEqual(labels, ["₹0", "₹0.5L", "₹1L", "₹1.5L", "₹2L"])

    def test_line_path_is_straight_segments(self):
        from blueprints.admin import _line_path
        self.assertEqual(_line_path([(5, 100), (15, 20)]), "M5.00 100.00 L15.00 20.00")
