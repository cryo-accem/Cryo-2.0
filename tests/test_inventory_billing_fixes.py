"""Regression tests for inventory-linked billing and grid movement fixes."""
import json
import os
import re
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import database
from inventory_service import (
    InventoryError, billing_category_for_grid_type_name, booking_grid_links_for_bookings,
    create_data_session, store_grid,
)
from main import create_app
from revenue import calculate_booking_revenue, calculate_charge_sheet, stored_prebilled_grids


class PrebilledGridChargeTests(unittest.TestCase):
    def test_prebilled_grids_are_not_charged_again_but_other_charges_are(self):
        charges = calculate_charge_sheet(
            "academic", "Screening / Clipping", 3, "facility", "normal_holey_carbon",
            prebilled_grids=2,
        )
        self.assertEqual(charges["grid_charge"], Decimal("3000.00"))
        self.assertEqual(charges["handling_charge"], Decimal("750.00"))
        self.assertEqual(charges["clip_base_charge"], Decimal("7500.00"))
        self.assertEqual(charges["prebilled_grids"], 2)

    def test_fully_prebilled_facility_booking_needs_no_grid_type(self):
        charges = calculate_charge_sheet(
            "academic", "Data Collection", 2, "facility", "", prebilled_grids=2,
        )
        self.assertEqual(charges["grid_charge"], Decimal("0.00"))

    def test_breakdown_must_cover_only_chargeable_grids(self):
        with self.assertRaisesRegex(ValueError, "already billed at freezing"):
            calculate_charge_sheet(
                "academic", "Screening / Clipping", 3, "facility", "",
                normal_grids="3", gold_grids="0", prebilled_grids=1,
            )
        with self.assertRaises(ValueError):
            calculate_charge_sheet(
                "academic", "Screening / Clipping", 1, "facility", "normal_holey_carbon",
                prebilled_grids=2,
            )

    def test_booking_revenue_uses_normal_gold_breakdown(self):
        booking = {"origin": "Industry", "pi_name": "Industry PI"}
        charges = calculate_booking_revenue(
            booking, 1, 2, False, "facility", "normal_holey_carbon", "Data Collection",
            "industry", 0, "facility", normal_grids=None, gold_grids="2",
        )
        self.assertEqual(charges["grid_charge"], Decimal("14000.00"))

    def test_inventory_grid_type_billing_categories(self):
        self.assertEqual(billing_category_for_grid_type_name("Quantifoil holey carbon"), "normal_holey_carbon")
        for name in ("Carbon-coated grids", "Gold Quantifoil Gold Grids", "Graphene-coated grids"):
            self.assertEqual(billing_category_for_grid_type_name(name), "gold_carbon_graphene")
        self.assertIsNone(billing_category_for_grid_type_name("Lacey custom"))


class InventoryBillingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.temp_dir.name, "inventory-billing.sqlite3")
        self.database_url = patch.object(database, "DATABASE_URL", f"sqlite:///{db_path}")
        self.database_url.start()
        self.addCleanup(self.database_url.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            session["must_change_password"] = False
            conn = database.get_db()
            session["admin_user_id"] = conn.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            conn.close()

    # ── helpers ────────────────────────────────────────────────────────────

    def _execute(self, sql, params=()):
        conn = database.get_db()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        last_id = cur.lastrowid
        cur.close()
        conn.close()
        return last_id

    def _one(self, sql, params=()):
        conn = database.get_db()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row

    def _csrf(self, path="/admin/screening"):
        page = self.client.get(path)
        return re.search(rb'name="_csrf_token" value="([^"]+)"', page.data).group(1).decode()

    def _preview(self, data):
        return self.client.post(
            "/admin/billing/preview", data=dict(data, _csrf_token=self._csrf())
        )

    def _grid_type_id(self, name):
        return self._one(
            "SELECT grid_type_id FROM grid_types WHERE grid_type_name=?", (name,)
        )["grid_type_id"]

    def _freeze(self, count, grid_source="facility", normal=None, gold=None,
                type_names=None, expect_ok=True, position_prefix="P"):
        email = f"freeze-{position_prefix}@example.test"
        booking_id = self._execute(
            """INSERT INTO freezing_bookings
               (user_name, pi_name, email, origin, sample_name, grids, freezing_date, status)
               VALUES ('Grid User', 'Grid PI', ?, 'External', 'Sample', ?, '2026-10-02', 'active')""",
            (email, count),
        )
        type_names = type_names or ["Quantifoil holey carbon"] * count
        data = {
            "_csrf_token": self._csrf("/admin/freezing"),
            "user_category": "academic",
            "actual_grids": str(count),
            "grid_source": grid_source,
            "grid_type": "",
            "grid_management": "1",
            "inventory_grid_sample_name": ["Sample"] * count,
            "inventory_grid_type_id": [str(self._grid_type_id(name)) for name in type_names],
            "blot_seconds": ["4"] * count,
            "blot_force": ["8"] * count,
            "inventory_falcon": ["FALCON"] * count,
            "inventory_box": [f"BOX-{position_prefix}"] * count,
            "inventory_position": [str(index + 1) for index in range(count)],
        }
        if normal is not None:
            data["normal_grids"] = str(normal)
        if gold is not None:
            data["gold_grids"] = str(gold)
        with patch("blueprints.admin.send_email"):
            response = self.client.post(
                f"/admin/freezing/complete/{booking_id}", data=data, follow_redirects=True
            )
        if expect_ok:
            self.assertEqual(
                self._one("SELECT status FROM freezing_bookings WHERE id=?", (booking_id,))["status"],
                "completed", response.get_data(as_text=True)[-2000:],
            )
        conn = database.get_db()
        codes = [
            row["grid_code"] for row in conn.execute(
                """SELECT g.grid_code FROM inventory_grids g
                   JOIN freezing_booking_inventory fbi ON fbi.batch_id=g.batch_id
                   WHERE fbi.booking_id=? ORDER BY g.grid_id""",
                (booking_id,),
            ).fetchall()
        ]
        conn.close()
        return booking_id, codes, response

    def _booking(self, service_type, status, codes, email):
        if service_type == "screening":
            booking_id = self._execute(
                """INSERT INTO screening_bookings
                   (user_name, pi_name, email, origin, sample_name, grids, days, status)
                   VALUES ('Grid User', 'Grid PI', ?, 'External', 'Sample', ?, 1, ?)""",
                (email, max(len(codes), 1), status),
            )
            link_sql = ("INSERT INTO screening_booking_grids (screening_booking_id, grid_id, linked_at) "
                        "VALUES (?, ?, '2026-11-10 10:00:00')")
        else:
            booking_id = self._execute(
                """INSERT INTO bookings
                   (user_name, pi_name, email, origin, sample_name, grids, days, status)
                   VALUES ('Grid User', 'Grid PI', ?, 'External', 'Sample', ?, 1, ?)""",
                (email, max(len(codes), 1), status),
            )
            link_sql = ("INSERT INTO data_collection_booking_grids (booking_id, grid_id, linked_at) "
                        "VALUES (?, ?, '2026-11-10 10:00:00')")
        for code in codes:
            grid_id = self._one(
                "SELECT grid_id FROM inventory_grids WHERE grid_code=?", (code,)
            )["grid_id"]
            self._execute(link_sql, (booking_id, grid_id))
        return booking_id

    def _completion(self, codes, actual_grids, box="RETURN", **extra):
        data = {
            "user_category": "academic",
            "actual_slots": "1",
            "actual_grids": str(actual_grids),
            "grid_source": "facility",
            "grid_type": "normal_holey_carbon",
            "clipped_grids": "0",
            "inventory_grid_code": list(codes),
            "inventory_outcome": ["store"] * len(codes),
            "inventory_discard_notes": [""] * len(codes),
            "inventory_falcon": ["FALCON-R"] * len(codes),
            "inventory_box": [box] * len(codes),
            "inventory_position": [str(index + 1) for index in range(len(codes))],
        }
        data.update(extra)
        return data

    def _complete(self, service_type, booking_id, data):
        path = "screening" if service_type == "screening" else "datacollecting"
        data = dict(data, _csrf_token=self._csrf(f"/admin/{path}"))
        with patch("blueprints.admin.send_email"):
            return self.client.post(
                f"/admin/{path}/complete/{booking_id}", data=data, follow_redirects=True
            )

    def _recalculate(self, table, booking_id, stage):
        row = dict(self._one(f"SELECT * FROM {table} WHERE id=?", (booking_id,)))
        breakdown = json.loads(row["grid_breakdown"] or "{}")
        return row, calculate_charge_sheet(
            row["origin"], stage, row["number_of_grids"], row["grid_source"],
            row["grid_type"], row["actual_slots"], bool(row["processing_requested"]),
            row["clipped_grids"],
            breakdown.get("normal_holey_carbon") or None,
            breakdown.get("gold_carbon_graphene") or None,
            row["clip_base_source"],
            prebilled_grids=stored_prebilled_grids(row),
        )

    # ── 1. No double grid charge ──────────────────────────────────────────

    def test_screening_does_not_charge_grids_already_billed_at_freezing(self):
        _, codes, _ = self._freeze(2, normal=2)
        booking_id = self._booking("screening", "ongoing", codes, "sc@example.test")
        response = self._complete("screening", booking_id, self._completion(codes, 3))
        row, recalculated = self._recalculate(
            "screening_bookings", booking_id, "Screening / Clipping"
        )
        self.assertEqual(row["status"], "completed", response.get_data(as_text=True)[-1500:])
        # One extra facility grid is charged; the two linked grids are not.
        self.assertEqual(Decimal(str(row["grid_charge"])), Decimal("3000"))
        # Handling stays pro-rated per 4 grids on every grid.
        self.assertEqual(Decimal(str(row["handling_charge"])), Decimal("750"))
        self.assertEqual(stored_prebilled_grids(row), 2)
        self.assertEqual(recalculated["grand_total"], Decimal(str(row["grand_total"])))

    def test_charge_sheet_preview_reproduces_the_completed_prebilled_total(self):
        _, codes, _ = self._freeze(2, normal=2)
        booking_id = self._booking("screening", "ongoing", codes, "sheet@example.test")
        self._complete("screening", booking_id, self._completion(codes, 3))
        stored = self._one(
            "SELECT grand_total FROM screening_bookings WHERE id=?", (booking_id,)
        )["grand_total"]
        with patch("blueprints.admin.generate_charge_sheet", return_value=b"%PDF-1.4") as generate:
            response = self.client.post(
                f"/admin/charge-sheet/screening/{booking_id}/preview",
                data={"_csrf_token": self._csrf("/admin/history")},
            )
        self.assertEqual(response.status_code, 200)
        sheet_row = generate.call_args[0][0]
        self.assertEqual(sheet_row["grand_total"], Decimal(str(stored)))
        self.assertEqual(sheet_row["grid_charge"], Decimal("3000.00"))

    def test_data_collection_with_only_prebilled_grids_has_no_grid_charge(self):
        _, codes, _ = self._freeze(2, normal=2)
        booking_id = self._booking("data_collection", "ongoing", codes, "dc@example.test")
        response = self._complete(
            "data_collection", booking_id, self._completion(codes, 2, grid_type="")
        )
        row, recalculated = self._recalculate("bookings", booking_id, "Data Collection")
        self.assertEqual(row["status"], "completed", response.get_data(as_text=True)[-1500:])
        self.assertEqual(Decimal(str(row["grid_charge"])), Decimal("0"))
        self.assertEqual(recalculated["grand_total"], Decimal(str(row["grand_total"])))

    def test_self_owned_freezing_grids_are_not_treated_as_billed(self):
        _, codes, _ = self._freeze(1, grid_source="self_owned")
        booking_id = self._booking("data_collection", "ongoing", codes, "own@example.test")
        self._complete("data_collection", booking_id, self._completion(codes, 1))
        row = self._one("SELECT grid_charge, grid_breakdown FROM bookings WHERE id=?", (booking_id,))
        self.assertEqual(Decimal(str(row["grid_charge"])), Decimal("3000"))
        self.assertEqual(stored_prebilled_grids(row), 0)

    def test_billing_preview_matches_completion_for_prebilled_grids(self):
        _, codes, _ = self._freeze(2, normal=2)
        booking_id = self._booking("screening", "ongoing", codes, "pv@example.test")
        preview = self._preview({
            "user_category": "academic", "service_stage": "Screening / Clipping",
            "actual_slots": "1", "actual_grids": "3", "grid_source": "facility",
            "grid_type": "normal_holey_carbon", "clip_base_source": "facility",
            "preview_service": "screening", "preview_booking_id": str(booking_id),
        })
        self.assertEqual(preview.status_code, 200, preview.get_json())
        self.assertEqual(Decimal(preview.get_json()["grid_charge"]), Decimal("3000.00"))
        self.assertEqual(preview.get_json()["prebilled_grids"], "2")
        self._complete("screening", booking_id, self._completion(codes, 3))
        row = self._one("SELECT grand_total FROM screening_bookings WHERE id=?", (booking_id,))
        self.assertEqual(Decimal(preview.get_json()["grand_total"]), Decimal(str(row["grand_total"])))

    def test_completion_pages_flag_linked_grids_billed_at_freezing(self):
        _, codes, _ = self._freeze(1, normal=1)
        booking_id = self._booking("screening", "ongoing", codes, "note@example.test")
        links = booking_grid_links_for_bookings("screening", [booking_id])[booking_id]
        self.assertEqual(links[0]["billed_at_freezing"], 1)
        for path in ("/admin/screening", "/admin/datacollecting"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("already billed at freezing", page)
            self.assertIn('name="preview_booking_id"', page)

    # ── 2. Clipping is remembered ─────────────────────────────────────────

    def test_screening_records_clipping_and_data_collection_does_not_rebill_it(self):
        _, codes, _ = self._freeze(2, normal=2)
        screening_id = self._booking("screening", "ongoing", codes, "clip-sc@example.test")
        self._complete("screening", screening_id, self._completion(codes, 2))
        clipped = self._one(
            "SELECT COUNT(*) AS n FROM inventory_grids WHERE clipped=1 AND grid_code IN (?, ?)",
            tuple(codes),
        )["n"]
        self.assertEqual(clipped, 2)

        dc_id = self._booking("data_collection", "ongoing", codes, "clip-dc@example.test")
        links = booking_grid_links_for_bookings("data_collection", [dc_id])[dc_id]
        self.assertEqual([link["clipped"] for link in links], [1, 1])
        self._complete("data_collection", dc_id, self._completion(codes, 2, box="DC-RETURN"))
        row = self._one(
            "SELECT status, clipped_grids, clip_base_charge FROM bookings WHERE id=?", (dc_id,)
        )
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["clipped_grids"], 2)
        self.assertEqual(Decimal(str(row["clip_base_charge"])), Decimal("0"))

    # ── 3. Data-collection load moves grids ───────────────────────────────

    def test_data_collection_load_takes_grids_out_and_completion_returns_them(self):
        _, codes, _ = self._freeze(1, normal=1)
        booking_id = self._booking("data_collection", "waiting", codes, "load@example.test")
        with patch("blueprints.admin.send_email"):
            self.client.post(
                f"/admin/datacollecting/load/{booking_id}",
                data={"_csrf_token": self._csrf("/admin/datacollecting")},
            )
        grid = self._one(
            """SELECT g.grid_id, g.current_status,
                      (SELECT COUNT(*) FROM storage_locations sl
                       WHERE sl.grid_id=g.grid_id AND sl.removed_at IS NULL) AS active
               FROM inventory_grids g WHERE grid_code=?""",
            (codes[0],),
        )
        self.assertEqual(
            self._one("SELECT status FROM bookings WHERE id=?", (booking_id,))["status"], "ongoing"
        )
        self.assertEqual(grid["current_status"], "In Data Collection")
        self.assertEqual(grid["active"], 0)

        # A data-collection session for the loaded booking can use its grids.
        operator = self._one("SELECT id FROM users WHERE role='admin' LIMIT 1")["id"]
        create_data_session(MultiDict([
            ("booking_reference", str(booking_id)), ("grid_codes", codes[0]),
            ("start_time", "2026-11-10T10:00"), ("instrument", "Krios"), ("notes", ""),
        ]), operator)
        self._execute("UPDATE grid_session_links SET outcome='store' WHERE grid_id=?", (grid["grid_id"],))

        response = self._complete(
            "data_collection", booking_id, self._completion(codes, 1, box="DC-BOX")
        )
        after = self._one(
            """SELECT g.current_status, sl.grid_box_name FROM inventory_grids g
               JOIN storage_locations sl ON sl.grid_id=g.grid_id AND sl.removed_at IS NULL
               WHERE g.grid_code=?""",
            (codes[0],),
        )
        self.assertIsNotNone(after, response.get_data(as_text=True)[-1500:])
        self.assertEqual(after["current_status"], "Stored")
        self.assertEqual(after["grid_box_name"], "DC-BOX")

    def test_loaded_data_collection_with_grids_cannot_be_deleted(self):
        _, codes, _ = self._freeze(1, normal=1)
        booking_id = self._booking("data_collection", "waiting", codes, "del@example.test")
        with patch("blueprints.admin.send_email"):
            self.client.post(
                f"/admin/datacollecting/load/{booking_id}",
                data={"_csrf_token": self._csrf("/admin/datacollecting")},
            )
        response = self.client.post(
            f"/admin/datacollecting/delete/{booking_id}",
            data={"_csrf_token": self._csrf("/admin/datacollecting")},
            follow_redirects=True,
        )
        self.assertIn(b"Complete it (store or discard each grid)", response.data)
        self.assertEqual(
            self._one("SELECT COUNT(*) AS n FROM bookings WHERE id=?", (booking_id,))["n"], 1
        )

    # ── 4. Screening mixed grids ──────────────────────────────────────────

    def test_screening_accepts_normal_gold_breakdown_like_the_preview(self):
        booking_id = self._booking("screening", "ongoing", [], "mixed@example.test")
        fields = {
            "user_category": "academic", "service_stage": "Screening / Clipping",
            "actual_slots": "1", "actual_grids": "2", "grid_source": "facility",
            "grid_type": "", "normal_grids": "1", "gold_grids": "1",
            "clip_base_source": "facility",
        }
        preview = self._preview(fields)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        response = self._complete("screening", booking_id, dict(fields, clipped_grids="0"))
        row = self._one(
            "SELECT status, grid_type, grid_charge, grand_total FROM screening_bookings WHERE id=?",
            (booking_id,),
        )
        self.assertEqual(row["status"], "completed", response.get_data(as_text=True)[-1500:])
        self.assertEqual(row["grid_type"], "mixed")
        self.assertEqual(Decimal(str(row["grid_charge"])), Decimal("7000"))
        self.assertEqual(Decimal(preview.get_json()["grand_total"]), Decimal(str(row["grand_total"])))
        page = self.client.get("/admin/screening").get_data(as_text=True)
        self.assertIn('name="normal_grids"', page)
        self.assertIn('name="gold_grids"', page)

    def test_data_collection_bills_gold_breakdown_not_selected_single_type(self):
        booking_id = self._booking("data_collection", "ongoing", [], "gold@example.test")
        self._complete("data_collection", booking_id, {
            "user_category": "industry", "actual_slots": "1", "actual_grids": "2",
            "grid_source": "facility", "grid_type": "normal_holey_carbon",
            "gold_grids": "2", "clipped_grids": "0",
        })
        row = self._one("SELECT grid_charge FROM bookings WHERE id=?", (booking_id,))
        self.assertEqual(Decimal(str(row["grid_charge"])), Decimal("14000"))

    # ── 5 & 6. Data-collection form stage and grid cap ────────────────────

    def test_data_collection_form_offers_only_data_collection_and_caps_grids(self):
        self._booking("data_collection", "ongoing", [], "form@example.test")
        page = self.client.get("/admin/datacollecting").get_data(as_text=True)
        stage_select = re.search(r'<select name="service_stage"[^>]*>(.*?)</select>', page).group(1)
        self.assertIn("Data Collection", stage_select)
        self.assertNotIn("Screening / Clipping", stage_select)
        self.assertNotIn("Freezing / Grid Registration", stage_select)
        self.assertRegex(page, r'name="actual_grids" type="number" min="1" max="12"')

    def test_data_collection_rejects_more_than_twelve_grids(self):
        booking_id = self._booking("data_collection", "ongoing", [], "cap@example.test")
        response = self._complete("data_collection", booking_id, {
            "user_category": "academic", "actual_slots": "1", "actual_grids": "13",
            "grid_source": "self_owned", "clipped_grids": "0",
        })
        self.assertIn(b"between 1 and 12", response.data)
        self.assertEqual(
            self._one("SELECT status FROM bookings WHERE id=?", (booking_id,))["status"], "ongoing"
        )

    # ── 7. Orphan link rows ───────────────────────────────────────────────

    def test_deleting_bookings_removes_grid_link_rows(self):
        _, codes, _ = self._freeze(2, normal=2)
        screening_id = self._booking("screening", "waiting", codes[:1], "orph-sc@example.test")
        dc_id = self._booking("data_collection", "waiting", codes[1:], "orph-dc@example.test")
        self.client.post(f"/admin/screening/delete/{screening_id}",
                         data={"_csrf_token": self._csrf("/admin/screening")})
        self.client.post(f"/admin/datacollecting/delete/{dc_id}",
                         data={"_csrf_token": self._csrf("/admin/datacollecting")})
        self.assertEqual(self._one("SELECT COUNT(*) AS n FROM screening_booking_grids")["n"], 0)
        self.assertEqual(self._one("SELECT COUNT(*) AS n FROM data_collection_booking_grids")["n"], 0)
        self.assertEqual(self._one("SELECT COUNT(*) AS n FROM bookings")["n"], 0)

    # ── 8. Case-insensitive storage positions ─────────────────────────────

    def test_storage_positions_are_compared_case_insensitively(self):
        _, codes, _ = self._freeze(2, normal=2)
        store_grid(codes[0], {"falcon_container_id": "f-qa", "grid_box_name": "box-b",
                              "grid_position": "1"}, None)
        with self.assertRaisesRegex(InventoryError, "already occupied"):
            store_grid(codes[1], {"falcon_container_id": "F-QA", "grid_box_name": "BOX-B",
                                  "grid_position": "1"}, None)
        stored = self._one(
            """SELECT sl.falcon_container_id FROM storage_locations sl
               JOIN inventory_grids g ON g.grid_id=sl.grid_id
               WHERE g.grid_code=? AND sl.removed_at IS NULL""",
            (codes[0],),
        )
        self.assertEqual(stored["falcon_container_id"], "f-qa")

    # ── 9. Freezing billed split must match per-grid types ────────────────

    def test_freezing_rejects_billed_split_that_disagrees_with_grid_types(self):
        booking_id, codes, response = self._freeze(
            2, normal=2, type_names=["Gold Quantifoil Gold Grids"] * 2, expect_ok=False,
        )
        self.assertIn(b"do not match", response.data)
        self.assertEqual(
            self._one("SELECT status FROM freezing_bookings WHERE id=?", (booking_id,))["status"],
            "active",
        )
        self.assertEqual(codes, [])
        self.assertEqual(self._one("SELECT COUNT(*) AS n FROM completed_freezing")["n"], 0)

    def test_freezing_accepts_billed_split_that_matches_grid_types(self):
        _, codes, _ = self._freeze(
            2, normal=1, gold=1,
            type_names=["Quantifoil holey carbon", "Graphene-coated grids"],
        )
        self.assertEqual(len(codes), 2)

    # ── Preview validates like completion ────────────────────────────────

    def test_preview_rejects_processing_on_freezing_and_fractional_slots(self):
        base = {
            "user_category": "industry", "actual_grids": "2", "grid_source": "self_owned",
            "clip_base_source": "facility",
        }
        freezing = self._preview(dict(
            base, service_stage="Freezing / Grid Registration", processing_requested="1",
        ))
        self.assertEqual(freezing.status_code, 400)
        for slots in ("1.5", "0.333", "0"):
            fractional = self._preview(dict(
                base, service_stage="Data Collection", actual_slots=slots,
            ))
            self.assertEqual(fractional.status_code, 400, slots)
        whole = self._preview(dict(
            base, service_stage="Data Collection", actual_slots="2", processing_requested="1",
        ))
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(Decimal(whole.get_json()["processing_charge"]), Decimal("20000.00"))


if __name__ == "__main__":
    unittest.main()
