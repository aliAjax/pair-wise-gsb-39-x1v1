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
        import tempfile as _tempfile
        from pathlib import Path as _Path
        self.tmp = _tempfile.TemporaryDirectory()
        self.db = Database(_Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}
        self.trip_id = self.db.list_trips()[0]["id"]
        # L1 trip departs 1430: S1 23:50, S2 1440 (00:00 next day), S3 1445,
        # S4 1455 (00:15), S5 1461 (00:21).
        disruption = self.db.create_disruption("planner-01", {"code": "D-A1", "name": "码头封停台账", "starts_at": "2026-09-25T00:00:00+08:00", "ends_at": "2026-09-25T04:00:00+08:00"}, "planner")
        self.disruption_id = disruption["id"]
        self.version_id = disruption["draft_version_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _publish(self):
        self.db.transition(self.version_id, "planner-01", "planner", "submit")
        self.db.transition(self.version_id, "reviewer-01", "reviewer", "approve")
        self.db.transition(self.version_id, "reviewer-01", "reviewer", "publish")

    def test_window_gate_and_nonstop_exclusion(self):
        # A 1440..1445 window on S5 (arrival 1461) misses the trip entirely.
        self.db.add_change(self.version_id, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S5"], "effective_start_minute": 1440, "effective_end_minute": 1445}, "planner")
        preview = self.db.list_affected_trips(version_id=self.version_id)
        self.assertFalse(preview["frozen"])
        self.assertEqual([it["trip_id"] for it in preview["items"]], [])
        # A wider skip window and an all-day closure both hit S4 at 1455 but
        # collapse into a single ledger entry for the trip.
        self.db.add_change(self.version_id, "planner-01", {"kind": "skip_stop", "stop_id": self.stops["S4"], "effective_start_minute": 1440, "effective_end_minute": 1460}, "planner")
        self.db.add_change(self.version_id, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        preview = self.db.list_affected_trips(version_id=self.version_id)
        self.assertEqual(len(preview["items"]), 1)
        entry = preview["items"][0]
        self.assertEqual(entry["trip_id"], self.trip_id)
        self.assertEqual(entry["stop_ids"], [self.stops["S4"]])
        self.assertEqual(entry["earliest_arrival_minute"], 1455)
        self.assertEqual({m["kind"] for m in entry["matches"]}, {"stop_closure", "skip_stop"})
        # A stop the trip never serves cannot create a ledger entry.
        other = self.db.create_disruption("planner-01", {"code": "D-A2", "name": "会展跳站", "starts_at": "2026-09-25T00:00:00+08:00", "ends_at": "2026-09-25T04:00:00+08:00"}, "planner")
        self.db.add_change(other["draft_version_id"], "planner-01", {"kind": "skip_stop", "stop_id": self.stops["X1"]}, "planner")
        self.assertEqual(self.db.list_affected_trips(version_id=other["draft_version_id"])["items"], [])

    def test_publish_freezes_ledger_against_later_base_changes(self):
        self.db.add_change(self.version_id, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self._publish()
        frozen = self.db.list_affected_trips(version_id=self.version_id)
        self.assertTrue(frozen["frozen"])
        self.assertEqual(len(frozen["items"]), 1)
        snapshot = self.db.get_version(self.version_id)["snapshot"]
        self.assertEqual(snapshot["affected_trips"], frozen["items"])
        # Drop the trip from current base data; the published ledger and
        # embedded snapshot stay untouched.
        with self.db.connect() as conn:
            conn.execute("DELETE FROM trips WHERE id=?", (self.trip_id,))
        still = self.db.list_affected_trips(version_id=self.version_id)
        self.assertEqual(still["items"], frozen["items"])
        self.assertEqual(self.db.get_version(self.version_id)["snapshot"]["affected_trips"], frozen["items"])

    def test_new_version_recomputes_while_old_list_stays_frozen(self):
        self.db.add_change(self.version_id, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self._publish()
        v2 = self.db.create_version_copy(self.disruption_id, self.version_id, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "skip_stop", "stop_id": self.stops["S3"]}, "planner")
        draft_items = self.db.list_affected_trips(version_id=v2)["items"]
        self.assertEqual(len(draft_items), 1)
        self.assertEqual(set(draft_items[0]["stop_ids"]), {self.stops["S3"], self.stops["S4"]})
        # Querying by trip only lists published ledgers until v2 is published.
        by_trip = self.db.list_affected_trips(trip_id=self.trip_id)
        self.assertEqual([it["trip_id"] for it in by_trip["items"]], [self.trip_id])
        self.assertEqual(len(by_trip["items"]), 1)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        by_trip = self.db.list_affected_trips(trip_id=self.trip_id)
        self.assertEqual(len(by_trip["items"]), 2)
        v1_items = self.db.list_affected_trips(version_id=self.version_id)["items"]
        self.assertEqual(v1_items[0]["stop_ids"], [self.stops["S4"]])
        self.assertEqual(self.db.list_affected_trips(version_id=v2, trip_id=self.trip_id)["items"][0]["stop_ids"],
                         [self.stops["S3"], self.stops["S4"]])


if __name__ == "__main__":
    unittest.main()
