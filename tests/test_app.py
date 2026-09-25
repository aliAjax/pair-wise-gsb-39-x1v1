import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class TransitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_route_version_review_publish_and_snapshot_isolation(self):
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"])["minutes"], 23)
        disruption = self.db.create_disruption("planner-01", {"code": "D-001", "name": "会展站跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.add_change(v1, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        with self.assertRaises(DomainError):
            self.db.transition(v1, "planner-01", "planner", "publish")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)

        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)
        # Publishing v2 as a draft snapshot does not alter the old published v1.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)

    def test_cross_midnight_times_and_bad_data_isolation(self):
        times = self.db.trip_times(self.db.list_trips()[0]["id"])
        self.assertEqual(times[0]["clock"], "23:50")
        self.assertEqual(times[-1]["service_minute"], 1461)
        self.assertEqual(times[-1]["clock"], "00:21")
        self.assertEqual(times[-1]["day_offset"], 1)

        fresh = Database(Path(self.tmp.name) / "bad.db")
        result = fresh.import_base("planner-01", {
            "lines": [{"code": "B1", "name": "错误线路"}],
            "stops": [{"code": "B-S1", "name": "站点一", "latitude": 31, "longitude": 121}],
            "line_stops": [
                {"line_code": "B1", "stop_code": "B-S1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "B1", "stop_code": "NO-SUCH", "sequence": 1, "travel_minutes_from_previous": 5},
            ],
            "trips": [],
        }, "planner")
        self.assertFalse(result["accepted"])
        self.assertTrue(fresh.list_import_errors())
        self.assertEqual(fresh.list_lines(), [])

    def test_accessibility_and_conflict_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-002", "name": "站点无障碍设施故障", "starts_at": "2026-09-24T00:00:00+08:00", "ends_at": "2026-09-25T00:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        normal = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=False)
        accessible = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=True)
        self.assertEqual(normal["minutes"], 23)
        self.assertEqual(accessible["minutes"], 31)
        with self.assertRaises(DomainError):
            self.db.add_change(version, "viewer", {"kind": "stop_closure", "stop_id": self.stops["S2"]}, "viewer")


class AffectedTripsLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}
        self.trip_id = self.db.list_trips()[0]["id"]  # departs 1430, reaches S4 at 1455

    def tearDown(self):
        self.tmp.cleanup()

    def _approve_and_publish(self, vid):
        self.db.transition(vid, "planner-01", "planner", "submit")
        self.db.transition(vid, "reviewer-01", "reviewer", "approve")
        self.db.transition(vid, "reviewer-01", "reviewer", "publish")

    def test_draft_preview_matches_dedup_and_window(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-100", "name": "码头封停", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        vid = disruption["draft_version_id"]
        # All-day closure at S4 (arrival 1455) hits the trip.
        self.db.add_change(vid, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        preview = self.db.affected_trips_for_version(vid)
        self.assertFalse(preview["frozen"])
        self.assertEqual(preview["count"], 1)
        entry = preview["items"][0]
        self.assertEqual(entry["trip_id"], self.trip_id)
        self.assertEqual(entry["matches"][0]["arrival_minute"], 1455)
        self.assertEqual(entry["matches"][0]["arrival_clock"], "00:15")
        self.assertEqual(entry["matches"][0]["arrival_day_offset"], 1)

        # A second change (skip) on the same stop must not duplicate the row.
        self.db.add_change(vid, "planner-01", {"kind": "skip_stop", "stop_id": self.stops["S4"], "effective_start_minute": 600, "effective_end_minute": 2000}, "planner")
        preview = self.db.affected_trips_for_version(vid)
        self.assertEqual(preview["count"], 1)
        self.assertEqual(len(preview["items"][0]["matches"]), 2)

        # A window that misses 1455 removes only that change's match.
        other = self.db.create_disruption("planner-01", {"code": "D-101", "name": "窗口外跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        ovid = other["draft_version_id"]
        self.db.add_change(ovid, "planner-01", {"kind": "skip_stop", "stop_id": self.stops["S4"], "effective_start_minute": 600, "effective_end_minute": 1454}, "planner")
        self.assertEqual(self.db.affected_trips_for_version(ovid)["count"], 0)
        # End minute is inclusive: 1455 still counts.
        edge = self.db.create_disruption("planner-01", {"code": "D-102", "name": "窗口边界", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        evid = edge["draft_version_id"]
        self.db.add_change(evid, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"], "effective_start_minute": 1455, "effective_end_minute": 1500}, "planner")
        self.assertEqual(self.db.affected_trips_for_version(evid)["count"], 1)

        # Detours and accessibility changes never create ledger entries.
        neutral = self.db.create_disruption("planner-01", {"code": "D-103", "name": "绕行无障碍", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        nvid = neutral["draft_version_id"]
        self.db.add_change(nvid, "planner-01", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self.db.add_change(nvid, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        self.assertEqual(self.db.affected_trips_for_version(nvid)["items"], [])

    def test_published_ledger_freezes_and_new_version_recomputes(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-200", "name": "窗口封停", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        vid = disruption["draft_version_id"]
        self.db.add_change(vid, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"], "effective_start_minute": 1400, "effective_end_minute": 1500}, "planner")
        self._approve_and_publish(vid)

        frozen = self.db.affected_trips_for_version(vid)
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["count"], 1)
        snapshot = self.db.get_version(vid)["snapshot"]
        self.assertEqual(len(snapshot["affected_trips"]), 1)
        self.assertEqual(snapshot["affected_trips"][0]["matches"][0]["stop_id"], self.stops["S4"])

        # Shift the trip far outside the effective window after publication.
        with self.db.connect() as conn:
            conn.execute("UPDATE trips SET departure_minute=600 WHERE id=?", (self.trip_id,))
        still_frozen = self.db.affected_trips_for_version(vid)
        self.assertTrue(still_frozen["frozen"])
        self.assertEqual(still_frozen["count"], 1)
        self.assertEqual(still_frozen["items"][0]["matches"][0]["arrival_minute"], 1455)
        self.assertEqual(self.db.get_version(vid)["snapshot"]["affected_trips"][0]["matches"][0]["arrival_minute"], 1455)

        # The new version recomputes against current base data: arrival 625 misses the window.
        v2 = self.db.create_version_copy(disruption["id"], vid, "planner-02", "planner")["id"]
        preview = self.db.affected_trips_for_version(v2)
        self.assertFalse(preview["frozen"])
        self.assertEqual(preview["items"], [])

        # Trip-centric lookup shows the frozen v1 entry but not the empty v2 preview.
        per_trip = self.db.affected_trips_for_trip(self.trip_id)
        self.assertEqual([item["version_id"] for item in per_trip["items"]], [vid])
        self.assertTrue(per_trip["items"][0]["frozen"])

    def test_missing_version_and_trip_404(self):
        with self.assertRaises(DomainError):
            self.db.affected_trips_for_version(9999)
        with self.assertRaises(DomainError):
            self.db.affected_trips_for_trip(9999)


if __name__ == "__main__":
    unittest.main()
