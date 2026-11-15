"""SQLite 持久层。

所有写操作通过 :class:`Store.transaction` 进入显式事务；服务层在同一个事务内
完成“修订原始记录 + 落盘新版汇总”，任何一步失败整体回滚，数据库中永远不会
出现半份汇总。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .buckets import BucketScheme, validate_edges

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bucket_schemes (
    scheme_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    edges_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS device_groups (
    group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL REFERENCES device_groups(group_id),
    value         REAL NOT NULL,
    measured_at   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active','superseded','revoked')),
    created_at    TEXT NOT NULL,
    corrected_at  TEXT,
    revoked_at    TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_group_time ON records(group_id, measured_at);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id     INTEGER REFERENCES records(record_id),
    event_type    TEXT NOT NULL CHECK (event_type IN
                    ('insert','correct','revoke')),
    old_value     REAL,
    new_value     REAL,
    old_measured_at TEXT,
    new_measured_at TEXT,
    reason        TEXT NOT NULL,
    event_at      TEXT NOT NULL,
    summary_version INTEGER REFERENCES summary_versions(version)
);

CREATE TABLE IF NOT EXISTS summary_versions (
    version        INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme_id      INTEGER NOT NULL REFERENCES bucket_schemes(scheme_id),
    triggered_by   INTEGER REFERENCES revisions(revision_id),
    event_type     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    active_count   INTEGER NOT NULL,
    details_json   TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 桶方案 ----------
    def create_scheme(
        self,
        conn: sqlite3.Connection,
        name: str,
        edges: list[float],
        note: str | None,
    ) -> BucketScheme:
        cleaned = validate_edges(edges)
        row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM bucket_schemes").fetchone()
        version = row["v"] + 1
        now = utc_now_iso()
        cur = conn.execute(
            "INSERT INTO bucket_schemes(version,name,edges_json,created_at,note)"
            " VALUES (?,?,?,?,?)",
            (version, name, json.dumps(cleaned), now, note),
        )
        return BucketScheme(cur.lastrowid, version, name, tuple(cleaned), now, note)

    def latest_scheme(self, conn: sqlite3.Connection) -> BucketScheme:
        row = conn.execute(
            "SELECT * FROM bucket_schemes ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise LookupError("尚未初始化任何桶方案")
        return _row_to_scheme(row)

    def get_scheme(self, conn: sqlite3.Connection, scheme_id: int) -> BucketScheme:
        row = conn.execute(
            "SELECT * FROM bucket_schemes WHERE scheme_id=?", (scheme_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"桶方案 {scheme_id} 不存在")
        return _row_to_scheme(row)

    def list_schemes(self, conn: sqlite3.Connection) -> list[BucketScheme]:
        rows = conn.execute("SELECT * FROM bucket_schemes ORDER BY version").fetchall()
        return [_row_to_scheme(r) for r in rows]

    # ---------- 设备组 ----------
    def ensure_group(self, conn: sqlite3.Connection, name: str) -> int:
        row = conn.execute(
            "SELECT group_id FROM device_groups WHERE name=?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["group_id"])
        cur = conn.execute(
            "INSERT INTO device_groups(name,created_at) VALUES (?,?)",
            (name, utc_now_iso()),
        )
        return int(cur.lastrowid)

    def list_groups(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT group_id, name, created_at FROM device_groups ORDER BY name"
            ).fetchall()
        ]

    # ---------- 记录 ----------
    def insert_record(
        self,
        conn: sqlite3.Connection,
        group_id: int,
        value: float,
        measured_at: str,
        note: str | None,
    ) -> int:
        now = utc_now_iso()
        cur = conn.execute(
            "INSERT INTO records(group_id,value,measured_at,status,created_at,note)"
            " VALUES (?,?,?,'active',?,?)",
            (group_id, value, measured_at, now, note),
        )
        return int(cur.lastrowid)

    def get_record(self, conn: sqlite3.Connection, record_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise LookupError(f"记录 {record_id} 不存在")
        return dict(row)

    def list_records(
        self,
        conn: sqlite3.Connection,
        group_id: int | None = None,
        include_inactive: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records WHERE 1=1"
        params: list[Any] = []
        if group_id is not None:
            sql += " AND group_id=?"
            params.append(group_id)
        if not include_inactive:
            sql += " AND status='active'"
        sql += " ORDER BY measured_at, record_id"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---------- 修订事件 ----------
    def insert_revision(
        self,
        conn: sqlite3.Connection,
        record_id: int | None,
        event_type: str,
        reason: str,
        old_value: float | None,
        new_value: float | None,
        old_measured_at: str | None,
        new_measured_at: str | None,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO revisions(record_id,event_type,old_value,new_value,"
            "old_measured_at,new_measured_at,reason,event_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                record_id,
                event_type,
                old_value,
                new_value,
                old_measured_at,
                new_measured_at,
                reason,
                utc_now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def list_revisions(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM revisions ORDER BY revision_id"
            ).fetchall()
        ]

    # ---------- 汇总版本（只增不改） ----------
    def insert_summary_version(
        self,
        conn: sqlite3.Connection,
        scheme_id: int,
        triggered_by: int,
        event_type: str,
        active_count: int,
        details: dict[str, Any],
    ) -> int:
        cur = conn.execute(
            "INSERT INTO summary_versions(scheme_id,triggered_by,event_type,"
            "created_at,active_count,details_json) VALUES (?,?,?,?,?,?)",
            (
                scheme_id,
                triggered_by,
                event_type,
                utc_now_iso(),
                active_count,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)

    def list_summary_versions(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT version,scheme_id,triggered_by,event_type,created_at,"
            "active_count FROM summary_versions ORDER BY version"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_summary_version(self, conn: sqlite3.Connection, version: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM summary_versions WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise LookupError(f"汇总版本 {version} 不存在")
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def latest_summary_version(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute("SELECT MAX(version) AS v FROM summary_versions").fetchone()
        return row["v"]

    def link_revision_to_summary(
        self, conn: sqlite3.Connection, revision_id: int, version: int
    ) -> None:
        conn.execute(
            "UPDATE revisions SET summary_version=? WHERE revision_id=?",
            (version, revision_id),
        )


def _row_to_scheme(row: sqlite3.Row) -> BucketScheme:
    return BucketScheme(
        scheme_id=int(row["scheme_id"]),
        version=int(row["version"]),
        name=row["name"],
        edges=tuple(json.loads(row["edges_json"])),
        created_at=row["created_at"],
        note=row["note"],
    )
