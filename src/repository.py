from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evacuation_zones (
                    zone TEXT PRIMARY KEY,
                    capacity INTEGER NOT NULL CHECK(capacity >= 0),
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reinforcement_projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    building_name TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    owner_consent INTEGER NOT NULL DEFAULT 0 CHECK(owner_consent IN (0,1)),
                    estimate REAL NOT NULL CHECK(estimate >= 0),
                    funds_available REAL NOT NULL CHECK(funds_available >= 0),
                    zone TEXT NOT NULL,
                    construction_start TEXT NOT NULL,
                    construction_end TEXT NOT NULL,
                    resettlement_count INTEGER NOT NULL CHECK(resettlement_count >= 0),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved')),
                    version INTEGER NOT NULL DEFAULT 1,
                    approval_snapshot TEXT,
                    approved_at TEXT,
                    approved_by TEXT,
                    voided_at TEXT,
                    voided_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(construction_end >= construction_start)
                );
                CREATE INDEX IF NOT EXISTS ix_reinforcement_zone_status
                    ON reinforcement_projects(zone, status);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 加固立项 ----

    @staticmethod
    def _reinforcement(row: sqlite3.Row) -> Dict[str, Any]:
        project = dict(row)
        project["owner_consent"] = bool(project["owner_consent"])
        project["approval_snapshot"] = (
            json.loads(project["approval_snapshot"]) if project["approval_snapshot"] else None
        )
        return project

    def upsert_zone(self, zone: str, capacity: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO evacuation_zones(zone, capacity, updated_by, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(zone) DO UPDATE SET capacity=excluded.capacity,
                       updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (zone, capacity, actor, now),
            )
        return self.get_zone(zone)

    def get_zone(self, zone: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM evacuation_zones WHERE zone=?", (zone,)
            ).fetchone()
        return dict(row) if row else None

    def list_zones(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM evacuation_zones ORDER BY zone"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_reinforcement(self, data: Dict[str, Any], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO reinforcement_projects(building_name, owner_name, owner_consent,
                       estimate, funds_available, zone, construction_start, construction_end,
                       resettlement_count, status, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (data["building_name"], data["owner_name"], int(data["owner_consent"]),
                 data["estimate"], data["funds_available"], data["zone"],
                 data["construction_start"], data["construction_end"],
                 data["resettlement_count"], "pending", actor, now, now),
            )
            project_id = int(cur.lastrowid)
        return self.get_reinforcement(project_id)

    def get_reinforcement(self, project_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reinforcement_projects WHERE id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("加固立项不存在")
        return self._reinforcement(row)

    def list_reinforcement(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM reinforcement_projects"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._reinforcement(row) for row in rows]

    def list_approved_in_zone(self, zone: str,
                              exclude_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT * FROM reinforcement_projects WHERE zone=? AND status='approved'")
        params: tuple = (zone,)
        if exclude_id is not None:
            sql += " AND id<>?"
            params = (zone, exclude_id)
        sql += " ORDER BY construction_start"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._reinforcement(row) for row in rows]

    def update_reinforcement(self, project_id: int, data: Dict[str, Any],
                             expected_version: int, actor: str,
                             void_reason: Optional[str]) -> Dict[str, Any]:
        """乐观锁更新；若项目原已批准，则同时把批复置为失效（回到待立项）。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE reinforcement_projects SET
                       building_name=?, owner_name=?, owner_consent=?, estimate=?,
                       funds_available=?, zone=?, construction_start=?, construction_end=?,
                       resettlement_count=?,
                       status=CASE WHEN ? IS NOT NULL THEN 'pending' ELSE status END,
                       approval_snapshot=CASE WHEN ? IS NOT NULL THEN NULL
                                             ELSE approval_snapshot END,
                       approved_at=CASE WHEN ? IS NOT NULL THEN NULL ELSE approved_at END,
                       approved_by=CASE WHEN ? IS NOT NULL THEN NULL ELSE approved_by END,
                       voided_at=CASE WHEN ? IS NOT NULL THEN ? ELSE voided_at END,
                       voided_reason=CASE WHEN ? IS NOT NULL THEN ? ELSE voided_reason END,
                       version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (data["building_name"], data["owner_name"], int(data["owner_consent"]),
                 data["estimate"], data["funds_available"], data["zone"],
                 data["construction_start"], data["construction_end"],
                 data["resettlement_count"],
                 void_reason, void_reason, void_reason, void_reason,
                 void_reason, now, void_reason, void_reason, now,
                 project_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM reinforcement_projects WHERE id=?", (project_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("加固立项不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_reinforcement(project_id)

    def approve_reinforcement(self, project_id: int, snapshot: dict,
                              expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE reinforcement_projects
                   SET status='approved', version=version+1, updated_at=?,
                       approval_snapshot=?, approved_at=?, approved_by=?,
                       voided_at=NULL, voided_reason=NULL
                   WHERE id=? AND version=? AND status='pending'""",
                (now, json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                 now, actor, project_id, expected_version),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT status, version FROM reinforcement_projects WHERE id=?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError("加固立项不存在")
                raise ConflictError("状态或版本已变化，请刷新后重新判断立项卡点")
        return self.get_reinforcement(project_id)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
