"""Transit disruption planning and atomic publication service."""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "transit_disruption.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def format_service_time(minutes: int) -> dict[str, Any]:
    """Format minutes measured from the start of a service day, including >24h."""
    if minutes < 0:
        raise DomainError("服务时间不能为负")
    day_offset, minute_of_day = divmod(minutes, 1440)
    return {"minutes": minutes, "clock": f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}", "day_offset": day_offset}


def _within_window(value: int, start: int | None, end: int | None) -> bool:
    if start is None and end is None:
        return True
    if start is None or end is None:
        return False
    return start <= value <= end


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'bus'
                );
                CREATE TABLE IF NOT EXISTS stops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    accessible INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS line_stops (
                    line_id INTEGER NOT NULL REFERENCES lines(id),
                    stop_id INTEGER NOT NULL REFERENCES stops(id),
                    sequence INTEGER NOT NULL,
                    travel_minutes_from_previous INTEGER NOT NULL CHECK(travel_minutes_from_previous >= 0),
                    PRIMARY KEY(line_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id INTEGER NOT NULL REFERENCES lines(id),
                    service_code TEXT NOT NULL,
                    direction INTEGER NOT NULL CHECK(direction IN (0,1)),
                    departure_minute INTEGER NOT NULL CHECK(departure_minute >= 0 AND departure_minute < 2880)
                );
                CREATE TABLE IF NOT EXISTS disruptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    disruption_id INTEGER NOT NULL REFERENCES disruptions(id),
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_by TEXT NOT NULL,
                    submitted_by TEXT,
                    approved_by TEXT,
                    snapshot_hash TEXT,
                    snapshot TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT,
                    UNIQUE(disruption_id,version_no)
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('stop_closure','skip_stop','detour','accessibility_change')),
                    line_id INTEGER REFERENCES lines(id),
                    stop_id INTEGER REFERENCES stops(id),
                    from_stop_id INTEGER REFERENCES stops(id),
                    to_stop_id INTEGER REFERENCES stops(id),
                    travel_minutes INTEGER,
                    effective_start_minute INTEGER,
                    effective_end_minute INTEGER,
                    accessible INTEGER,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    row_key TEXT NOT NULL,
                    error TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def import_base(self, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "admin"}:
            raise DomainError("只有线路规划人员可以导入基础数据", 403)
        lines = payload.get("lines", [])
        stops = payload.get("stops", [])
        memberships = payload.get("line_stops", [])
        trips = payload.get("trips", [])
        if not isinstance(lines, list) or not isinstance(stops, list) or not isinstance(memberships, list) or not isinstance(trips, list):
            raise DomainError("基础数据必须是列表结构")
        errors: list[dict[str, Any]] = []
        line_codes = [str(x.get("code", "")).strip() for x in lines if isinstance(x, dict)]
        stop_codes = [str(x.get("code", "")).strip() for x in stops if isinstance(x, dict)]
        if len(line_codes) != len(set(line_codes)) or any(not x for x in line_codes):
            errors.append({"row_key": "lines", "error": "线路编号为空或重复", "payload": lines})
        if len(stop_codes) != len(set(stop_codes)) or any(not x for x in stop_codes):
            errors.append({"row_key": "stops", "error": "站点编号为空或重复", "payload": stops})
        for stop in stops:
            if not isinstance(stop, dict):
                errors.append({"row_key": "stops", "error": "站点必须为对象", "payload": stop})
                continue
            try:
                lat, lon = float(stop.get("latitude")), float(stop.get("longitude"))
                if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append({"row_key": str(stop.get("code", "?")), "error": "站点经纬度不合法", "payload": stop})
        by_line: dict[str, list[dict[str, Any]]] = {}
        for item in memberships:
            if not isinstance(item, dict):
                errors.append({"row_key": "line_stops", "error": "线路站序必须为对象", "payload": item})
                continue
            by_line.setdefault(str(item.get("line_code", "")), []).append(item)
        for line_code, items in by_line.items():
            if line_code not in line_codes:
                errors.append({"row_key": line_code, "error": "线路不存在", "payload": items})
                continue
            items.sort(key=lambda x: int(x.get("sequence", -1)) if str(x.get("sequence", "")).lstrip("-").isdigit() else -1)
            expected = list(range(len(items)))
            sequences = [int(x.get("sequence", -1)) for x in items if str(x.get("sequence", "")).lstrip("-").isdigit()]
            seen: set[str] = set()
            for item in items:
                stop_code = str(item.get("stop_code", ""))
                if stop_code not in stop_codes or stop_code in seen:
                    errors.append({"row_key": line_code, "error": "站序引用未知站点或重复站点", "payload": item})
                seen.add(stop_code)
                try:
                    if float(item.get("travel_minutes_from_previous", 0)) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append({"row_key": line_code, "error": "站间行驶时间必须为非负数", "payload": item})
            if sequences != expected:
                errors.append({"row_key": line_code, "error": "站序必须从 0 连续递增", "payload": items})
        for trip in trips:
            if not isinstance(trip, dict) or str(trip.get("line_code", "")) not in line_codes:
                errors.append({"row_key": "trips", "error": "班次引用未知线路", "payload": trip})
                continue
            try:
                departure = int(trip.get("departure_minute"))
                if not 0 <= departure < 2880:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append({"row_key": "trips", "error": "发车时间必须在服务日 0 到 2879 分钟", "payload": trip})
        with self.connect() as conn:
            existing_lines = {row["code"] for row in conn.execute("SELECT code FROM lines")}
            existing_stops = {row["code"] for row in conn.execute("SELECT code FROM stops")}
        duplicate_lines = sorted(set(line_codes) & existing_lines)
        duplicate_stops = sorted(set(stop_codes) & existing_stops)
        if duplicate_lines:
            errors.append({"row_key": "lines", "error": "线路编号已存在: " + ", ".join(duplicate_lines), "payload": duplicate_lines})
        if duplicate_stops:
            errors.append({"row_key": "stops", "error": "站点编号已存在: " + ", ".join(duplicate_stops), "payload": duplicate_stops})
        if errors:
            with self.connect() as conn:
                for error in errors:
                    conn.execute("INSERT INTO import_errors(actor,row_key,error,payload,created_at) VALUES(?,?,?,?,?)", (actor, error["row_key"], error["error"], canonical(error["payload"]), utcnow()))
                self._audit(conn, actor, "base.import.rejected", "import", None, {"errors": len(errors)})
            return {"accepted": False, "errors": errors, "created": 0}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            line_ids: dict[str, int] = {}
            stop_ids: dict[str, int] = {}
            for line in lines:
                cur = conn.execute("INSERT INTO lines(code,name,mode) VALUES(?,?,?)", (str(line["code"]).strip(), str(line.get("name", line["code"])).strip(), str(line.get("mode", "bus"))))
                line_ids[str(line["code"]).strip()] = int(cur.lastrowid)
            for stop in stops:
                cur = conn.execute("INSERT INTO stops(code,name,latitude,longitude,accessible) VALUES(?,?,?,?,?)", (str(stop["code"]).strip(), str(stop.get("name", stop["code"])).strip(), float(stop["latitude"]), float(stop["longitude"]), int(bool(stop.get("accessible", True)))))
                stop_ids[str(stop["code"]).strip()] = int(cur.lastrowid)
            for line_code, items in by_line.items():
                for item in sorted(items, key=lambda x: int(x["sequence"])):
                    conn.execute("INSERT INTO line_stops(line_id,stop_id,sequence,travel_minutes_from_previous) VALUES(?,?,?,?)", (line_ids[line_code], stop_ids[str(item["stop_code"])], int(item["sequence"]), int(float(item.get("travel_minutes_from_previous", 0)))))
            for trip in trips:
                conn.execute("INSERT INTO trips(line_id,service_code,direction,departure_minute) VALUES(?,?,?,?)", (line_ids[str(trip["line_code"])], str(trip.get("service_code", "daily")), int(trip.get("direction", 0)), int(trip["departure_minute"])))
            self._audit(conn, actor, "base.imported", "import", None, {"lines": len(lines), "stops": len(stops), "trips": len(trips)})
        return {"accepted": True, "errors": [], "created": len(lines) + len(stops) + len(memberships) + len(trips)}

    def create_disruption(self, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("只有调度编辑可以创建中断事件", 403)
        code, name = str(payload.get("code", "")).strip(), str(payload.get("name", "")).strip()
        starts_at, ends_at = str(payload.get("starts_at", "")).strip(), str(payload.get("ends_at", "")).strip()
        if not code or not name or not starts_at or not ends_at or starts_at >= ends_at:
            raise DomainError("中断编号、名称和时间范围不合法")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                disruption = conn.execute("INSERT INTO disruptions(code,name,starts_at,ends_at,created_by,created_at) VALUES(?,?,?,?,?,?)", (code, name, starts_at, ends_at, actor, utcnow()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("中断编号已存在", 409) from exc
            version = conn.execute("INSERT INTO versions(disruption_id,version_no,status,created_by,created_at,updated_at) VALUES(?,1,'draft',?,?,?)", (disruption.lastrowid, actor, utcnow(), utcnow()))
            self._audit(conn, actor, "disruption.created", "disruption", disruption.lastrowid, {"version_id": version.lastrowid})
            return {"id": int(disruption.lastrowid), "code": code, "name": name, "draft_version_id": int(version.lastrowid), "status": "draft"}

    def create_version_copy(self, disruption_id: int, parent_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("没有创建版本的权限", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            disruption = conn.execute("SELECT * FROM disruptions WHERE id=?", (disruption_id,)).fetchone()
            if not disruption:
                raise DomainError("中断事件不存在", 404)
            parent = conn.execute("SELECT * FROM versions WHERE id=? AND disruption_id=?", (parent_id, disruption_id)).fetchone()
            if not parent:
                raise DomainError("父版本不存在", 404)
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE disruption_id=?", (disruption_id,)).fetchone()["value"])
            cur = conn.execute("INSERT INTO versions(disruption_id,version_no,parent_id,status,created_by,created_at,updated_at) VALUES(?,?,?, 'draft',?,?,?)", (disruption_id, next_no, parent_id, actor, utcnow(), utcnow()))
            new_id = int(cur.lastrowid)
            for change in conn.execute("SELECT * FROM changes WHERE version_id=?", (parent_id,)):
                conn.execute(
                    """INSERT INTO changes(version_id,kind,line_id,stop_id,from_stop_id,to_stop_id,travel_minutes,effective_start_minute,effective_end_minute,accessible,payload,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id, change["kind"], change["line_id"], change["stop_id"], change["from_stop_id"], change["to_stop_id"], change["travel_minutes"], change["effective_start_minute"], change["effective_end_minute"], change["accessible"], change["payload"], utcnow()),
                )
            self._audit(conn, actor, "version.copied", "version", new_id, {"parent_id": parent_id})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (new_id,)).fetchone())

    def add_change(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("没有修改方案的权限", 403)
        kind = str(payload.get("kind", "")).strip()
        if kind not in {"stop_closure", "skip_stop", "detour", "accessibility_change"}:
            raise DomainError("变更类型不合法")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改", 409)
            line_id = payload.get("line_id")
            stop_id = payload.get("stop_id")
            from_stop_id = payload.get("from_stop_id")
            to_stop_id = payload.get("to_stop_id")
            travel = payload.get("travel_minutes")
            start = payload.get("effective_start_minute")
            end = payload.get("effective_end_minute")
            if line_id is not None and not conn.execute("SELECT 1 FROM lines WHERE id=?", (int(line_id),)).fetchone():
                raise DomainError("线路不存在", 404)
            for candidate in (stop_id, from_stop_id, to_stop_id):
                if candidate is not None and not conn.execute("SELECT 1 FROM stops WHERE id=?", (int(candidate),)).fetchone():
                    raise DomainError("站点不存在", 404)
            if kind in {"stop_closure", "skip_stop", "accessibility_change"} and stop_id is None:
                raise DomainError("该变更需要 stop_id")
            if kind == "detour" and (from_stop_id is None or to_stop_id is None or travel is None):
                raise DomainError("绕行需要起止站点和行驶分钟数")
            if travel is not None and int(travel) < 0:
                raise DomainError("行驶时间不能为负")
            if (start is None) != (end is None):
                raise DomainError("生效开始和结束时间必须同时提供")
            if start is not None:
                start, end = int(start), int(end)
                if start < 0 or end <= start or end >= 2880:
                    raise DomainError("生效时间范围不合法")
            accessible = payload.get("accessible")
            if kind == "accessibility_change" and not isinstance(accessible, bool):
                raise DomainError("无障碍变更需要布尔值 accessible")
            cur = conn.execute(
                """INSERT INTO changes(version_id,kind,line_id,stop_id,from_stop_id,to_stop_id,travel_minutes,effective_start_minute,effective_end_minute,accessible,payload,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, kind, int(line_id) if line_id is not None else None, int(stop_id) if stop_id is not None else None,
                 int(from_stop_id) if from_stop_id is not None else None, int(to_stop_id) if to_stop_id is not None else None,
                 int(travel) if travel is not None else None, int(start) if start is not None else None, int(end) if end is not None else None,
                 int(accessible) if isinstance(accessible, bool) else None, canonical(payload), utcnow()),
            )
            conn.execute("UPDATE versions SET updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "change.added", "version", version_id, {"kind": kind, "change_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM changes WHERE id=?", (cur.lastrowid,)).fetchone())

    def transition(self, version_id: int, actor: str, role: str, action: str) -> dict[str, Any]:
        if role not in {"planner", "editor", "reviewer", "admin"}:
            raise DomainError("没有状态流转权限", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            status = version["status"]
            if action == "submit":
                if status != "draft" or role not in {"planner", "editor", "admin"}:
                    raise DomainError("只有草稿版本可以提交复核", 409)
                conn.execute("UPDATE versions SET status='review',submitted_by=?,updated_at=? WHERE id=?", (actor, utcnow(), version_id))
            elif action == "reject":
                if status != "review" or role not in {"reviewer", "admin"}:
                    raise DomainError("只有复核版本可以退回", 409)
                if actor == version["created_by"]:
                    raise DomainError("创建人不能自行复核", 403)
                conn.execute("UPDATE versions SET status='draft',submitted_by=NULL,updated_at=? WHERE id=?", (utcnow(), version_id))
            elif action == "approve":
                if status != "review" or role not in {"reviewer", "admin"}:
                    raise DomainError("只有复核版本可以批准", 409)
                if actor in {version["created_by"], version["submitted_by"]}:
                    raise DomainError("创建人或提交人不能自行批准", 403)
                conn.execute("UPDATE versions SET status='approved',approved_by=?,updated_at=? WHERE id=?", (actor, utcnow(), version_id))
            elif action == "publish":
                if status != "approved" or role not in {"reviewer", "admin"}:
                    raise DomainError("只有已批准版本可以发布", 409)
                changes = [dict(r) for r in conn.execute("SELECT kind,line_id,stop_id,from_stop_id,to_stop_id,travel_minutes,effective_start_minute,effective_end_minute,accessible,payload FROM changes WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]
                snapshot = {"version_id": version_id, "disruption_id": version["disruption_id"], "version_no": version["version_no"], "changes": changes, "base_hash": self._base_hash(conn)}
                snapshot_text = canonical(snapshot)
                digest = hashlib.sha256(snapshot_text.encode()).hexdigest()
                conn.execute("UPDATE versions SET status='published',snapshot_hash=?,snapshot=?,published_at=?,updated_at=? WHERE id=?", (digest, snapshot_text, utcnow(), utcnow(), version_id))
            else:
                raise DomainError("未知状态操作")
            self._audit(conn, actor, f"version.{action}", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def _base_hash(self, conn: sqlite3.Connection) -> str:
        lines = [dict(r) for r in conn.execute("SELECT * FROM lines ORDER BY id")]
        stops = [dict(r) for r in conn.execute("SELECT * FROM stops ORDER BY id")]
        memberships = [dict(r) for r in conn.execute("SELECT * FROM line_stops ORDER BY line_id,sequence")]
        trips = [dict(r) for r in conn.execute("SELECT * FROM trips ORDER BY id")]
        return hashlib.sha256(canonical({"lines": lines, "stops": stops, "line_stops": memberships, "trips": trips}).encode()).hexdigest()

    def _graph(self, version_id: int | None, at_minute: int, require_accessible: bool) -> tuple[dict[int, list[tuple[int, int, dict[str, Any]]]], dict[int, sqlite3.Row]]:
        with self.connect() as conn:
            stops = {int(r["id"]): r for r in conn.execute("SELECT * FROM stops")}
            memberships: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute("SELECT * FROM line_stops ORDER BY line_id,sequence"):
                memberships.setdefault(int(row["line_id"]), []).append(row)
            active: list[sqlite3.Row] = []
            if version_id is not None:
                version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
                if not version:
                    raise DomainError("方案版本不存在", 404)
                active = conn.execute("SELECT * FROM changes WHERE version_id=?", (version_id,)).fetchall()
            closed: set[int] = set()
            inaccessible: set[int] = set()
            detours: list[sqlite3.Row] = []
            for change in active:
                if not _within_window(at_minute, change["effective_start_minute"], change["effective_end_minute"]):
                    continue
                if change["kind"] in {"stop_closure", "skip_stop"}:
                    closed.add(int(change["stop_id"]))
                elif change["kind"] == "accessibility_change" and not bool(change["accessible"]):
                    inaccessible.add(int(change["stop_id"]))
                elif change["kind"] == "detour":
                    detours.append(change)
            graph: dict[int, list[tuple[int, int, dict[str, Any]]]] = {sid: [] for sid in stops}
            for line_id, rows in memberships.items():
                if not rows:
                    continue
                # Vehicles may pass a skipped or closed stop but passengers
                # cannot board or alight there. Build one edge between each
                # pair of usable stops and accumulate all omitted segment time.
                usable = [
                    row for row in rows
                    if int(row["stop_id"]) not in closed
                    and not (require_accessible and (not stops[int(row["stop_id"])]["accessible"] or int(row["stop_id"]) in inaccessible))
                ]
                for current, nxt in zip(usable, usable[1:]):
                    between = [r for r in rows if int(current["sequence"]) < int(r["sequence"]) <= int(nxt["sequence"])]
                    minutes = sum(int(r["travel_minutes_from_previous"]) for r in between)
                    edge = {"line_id": line_id, "kind": "route", "from_sequence": current["sequence"], "to_sequence": nxt["sequence"]}
                    graph[int(current["stop_id"])].append((int(nxt["stop_id"]), minutes, edge))
                    reverse_edge = {"line_id": line_id, "kind": "route", "from_sequence": nxt["sequence"], "to_sequence": current["sequence"]}
                    graph[int(nxt["stop_id"])].append((int(current["stop_id"]), minutes, reverse_edge))
            for change in detours:
                src, dst, minutes = int(change["from_stop_id"]), int(change["to_stop_id"]), int(change["travel_minutes"])
                if src in closed or dst in closed:
                    continue
                if require_accessible and (not stops[src]["accessible"] or not stops[dst]["accessible"] or src in inaccessible or dst in inaccessible):
                    continue
                graph[src].append((dst, minutes, {"line_id": change["line_id"], "kind": "detour", "change_id": change["id"]}))
                graph[dst].append((src, minutes, {"line_id": change["line_id"], "kind": "detour", "change_id": change["id"]}))
            return graph, stops

    def route(self, from_stop_id: int, to_stop_id: int, version_id: int | None = None,
              at_minute: int = 0, require_accessible: bool = False) -> dict[str, Any]:
        if from_stop_id == to_stop_id:
            return {"from_stop_id": from_stop_id, "to_stop_id": to_stop_id, "minutes": 0, "path": [from_stop_id], "legs": []}
        graph, stops = self._graph(version_id, at_minute, require_accessible)
        if from_stop_id not in stops or to_stop_id not in stops:
            raise DomainError("起讫站点不存在", 404)
        if require_accessible and not stops[from_stop_id]["accessible"]:
            raise DomainError("起点不具备无障碍通行条件", 409)
        queue: list[tuple[int, int]] = [(0, from_stop_id)]
        distance = {from_stop_id: 0}
        predecessor: dict[int, tuple[int, dict[str, Any]]] = {}
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != distance.get(node):
                continue
            if node == to_stop_id:
                break
            for nxt, weight, edge in graph.get(node, []):
                new_cost = cost + weight
                if new_cost < distance.get(nxt, math.inf):
                    distance[nxt] = new_cost
                    predecessor[nxt] = (node, edge)
                    heapq.heappush(queue, (new_cost, nxt))
        if to_stop_id not in distance:
            return {"from_stop_id": from_stop_id, "to_stop_id": to_stop_id, "minutes": None, "path": [], "legs": [], "status": "unreachable"}
        path = [to_stop_id]
        legs: list[dict[str, Any]] = []
        cursor = to_stop_id
        while cursor != from_stop_id:
            previous, edge = predecessor[cursor]
            legs.append({"from_stop_id": previous, "to_stop_id": cursor, "minutes": distance[cursor] - distance[previous], **edge})
            path.append(previous)
            cursor = previous
        path.reverse()
        legs.reverse()
        return {"from_stop_id": from_stop_id, "to_stop_id": to_stop_id, "minutes": distance[to_stop_id], "path": path,
                "legs": legs, "status": "ok", "arrival": format_service_time(at_minute + distance[to_stop_id])}

    def trip_times(self, trip_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip:
                raise DomainError("班次不存在", 404)
            rows = conn.execute("SELECT s.id stop_id,s.code station_code,ls.sequence,ls.travel_minutes_from_previous FROM line_stops ls JOIN stops s ON s.id=ls.stop_id WHERE ls.line_id=? ORDER BY ls.sequence", (trip["line_id"],)).fetchall()
            if int(trip["direction"]) == 1:
                rows = list(reversed(rows))
            current = int(trip["departure_minute"])
            result = []
            for index, row in enumerate(rows):
                if index:
                    # For reversed direction, the stored segment time belongs
                    # to the forward direction; use the adjacent forward edge.
                    current += int(row["travel_minutes_from_previous"]) if int(trip["direction"]) == 0 else int(rows[index]["travel_minutes_from_previous"])
                result.append({"stop_id": row["stop_id"], "station_code": row["station_code"], "service_minute": current, **format_service_time(current)})
        return result

    def list_lines(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM lines ORDER BY id").fetchall()]

    def list_stops(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM stops ORDER BY id").fetchall()]

    def list_trips(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM trips ORDER BY id").fetchall()]

    def list_disruptions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM disruptions ORDER BY id DESC").fetchall()]

    def list_versions(self, disruption_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if disruption_id:
                rows = conn.execute("SELECT * FROM versions WHERE disruption_id=? ORDER BY version_no DESC", (disruption_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def get_version(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            changes = [dict(r) for r in conn.execute("SELECT * FROM changes WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]
            result = dict(version)
            result["changes"] = changes
            if result["snapshot"]:
                result["snapshot"] = json.loads(result["snapshot"])
            return result

    def list_import_errors(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM import_errors ORDER BY id DESC").fetchall()]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_lines():
        return {"line": int(db.list_lines()[0]["id"])}
    data = {
        "lines": [{"code": "L1", "name": "滨江线"}, {"code": "L2", "name": "环路快线"}],
        "stops": [
            {"code": "S1", "name": "北站", "latitude": 31.0, "longitude": 121.0, "accessible": True},
            {"code": "S2", "name": "人民广场", "latitude": 31.01, "longitude": 121.01, "accessible": True},
            {"code": "S3", "name": "南门", "latitude": 31.02, "longitude": 121.02, "accessible": True},
            {"code": "S4", "name": "码头", "latitude": 31.03, "longitude": 121.03, "accessible": True},
            {"code": "S5", "name": "机场", "latitude": 31.04, "longitude": 121.04, "accessible": True},
            {"code": "X1", "name": "会展中心", "latitude": 31.015, "longitude": 121.025, "accessible": True},
        ],
        "line_stops": [
            {"line_code": "L1", "stop_code": "S1", "sequence": 0, "travel_minutes_from_previous": 0},
            {"line_code": "L1", "stop_code": "S2", "sequence": 1, "travel_minutes_from_previous": 10},
            {"line_code": "L1", "stop_code": "S3", "sequence": 2, "travel_minutes_from_previous": 5},
            {"line_code": "L1", "stop_code": "S4", "sequence": 3, "travel_minutes_from_previous": 10},
            {"line_code": "L1", "stop_code": "S5", "sequence": 4, "travel_minutes_from_previous": 6},
            {"line_code": "L2", "stop_code": "S1", "sequence": 0, "travel_minutes_from_previous": 0},
            {"line_code": "L2", "stop_code": "X1", "sequence": 1, "travel_minutes_from_previous": 8},
            {"line_code": "L2", "stop_code": "S4", "sequence": 2, "travel_minutes_from_previous": 9},
        ],
        "trips": [{"line_code": "L1", "service_code": "daily", "direction": 0, "departure_minute": 1430}],
    }
    result = db.import_base("planner-01", data, "planner")
    if not result["accepted"]:
        raise RuntimeError(result["errors"])
    return {"line": int(db.list_lines()[0]["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "TransitDisruption/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            endpoints = {
                "/api/lines": self.db.list_lines,
                "/api/stops": self.db.list_stops,
                "/api/trips": self.db.list_trips,
                "/api/disruptions": self.db.list_disruptions,
                "/api/versions": self.db.list_versions,
                "/api/import-errors": self.db.list_import_errors,
                "/api/audit": self.db.audit,
            }
            if parsed.path in endpoints:
                return self._send({"items": endpoints[parsed.path]()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "versions"]:
                return self._send(self.db.get_version(int(parts[2])))
            if len(parts) == 3 and parts[:2] == ["api", "trips"]:
                return self._send({"times": self.db.trip_times(int(parts[2]))})
            if parsed.path == "/api/route":
                q = parse_qs(parsed.query)
                version = q.get("version_id", [None])[0]
                return self._send(self.db.route(int(q["from"][0]), int(q["to"][0]), int(version) if version else None,
                                                int(q.get("at_minute", ["0"])[0]), q.get("accessible", ["false"])[0].lower() == "true"))
            raise DomainError("接口不存在", 404)
        except (KeyError, ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "import"]:
                return self._send(self.db.import_base(actor, body, role))
            if parts == ["api", "disruptions"]:
                return self._send(self.db.create_disruption(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "disruptions"] and parts[3] == "versions":
                return self._send(self.db.create_version_copy(int(parts[2]), int(body.get("parent_id")), actor, role), 201)
            if len(parts) == 3 and parts[:2] == ["api", "versions"] and parts[2] == "changes":
                return self._send(self.db.add_change(int(body.get("version_id")), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "changes":
                return self._send(self.db.add_change(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "approve", "reject", "publish"}:
                return self._send(self.db.transition(int(parts[2]), actor, role, parts[3]))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[transit] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="公交中断改道发布服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8010")))
    parser.add_argument("--db", default=os.getenv("TRANSIT_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并导入示例线路")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; line={seed['line']}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"transit-disruption listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
