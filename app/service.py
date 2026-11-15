"""修订服务：把原始记录修订与汇总发布绑定在同一个事务里。

失败原子性
==========
:meth:`RevisionService._publish` 支持 ``fail_before_commit`` 故障注入：在提交前
抛错，整个事务回滚。由于修订事件、记录状态与新汇总版本在同一事务中写入，
回滚后数据库恢复到上一版汇总，绝不会出现“记录已改但汇总停在旧状态”或
只写了一半的汇总。
"""

from __future__ import annotations

import math
from typing import Any

from .storage import Store, utc_now_iso
from .summary import (
    DEFAULT_APPROX_THRESHOLD,
    DEFAULT_PROBS,
    build_summary,
    build_summary_incremental,
)


class RevisionError(ValueError):
    """修订请求不合法（记录不可改、数值非法等）。"""


class SummaryMismatchError(RuntimeError):
    """增量路径与全量重算结果不一致——数据完整性故障。"""


def validate_value(value: Any) -> float:
    if isinstance(value, bool):
        raise RevisionError("布尔值不是合法测量值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RevisionError(f"测量值必须是数值，收到 {value!r}") from exc
    if not math.isfinite(number):
        raise RevisionError("测量值必须是有限实数，拒绝 NaN/Inf")
    return number


def validate_measured_at(measured_at: Any) -> str:
    if not isinstance(measured_at, str) or not measured_at:
        raise RevisionError("measured_at 必须是非空 ISO8601 字符串")
    text = measured_at.strip()
    try:
        # 与 summary.period_key 的解析方式保持一致
        from datetime import datetime

        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RevisionError(f"measured_at 不是合法 ISO8601 时间：{text!r}") from exc
    return text


class RevisionService:
    def __init__(
        self,
        store: Store,
        probs=DEFAULT_PROBS,
        approx_threshold: int = DEFAULT_APPROX_THRESHOLD,
    ):
        self.store = store
        self.probs = tuple(probs)
        self.approx_threshold = approx_threshold

    # ---------- 内部：双路径构建 + 发布 ----------
    def _publish(
        self,
        conn,
        scheme,
        changes: list[tuple[str, dict | None, dict | None]],
        active_before: list[dict],
        revision_id: int,
        event_type: str,
        fail_before_commit: bool = False,
    ) -> int:
        groups = self.store.list_groups(conn)

        # 增量路径：修订前全部有效记录 + 显式 remove/add
        incremental = build_summary_incremental(
            active_before,
            groups,
            scheme,
            changes,
            self.probs,
            self.approx_threshold,
        )

        # 全量重算路径：从事务内当前记录状态直接重建（含本次修订）
        after_active = self.store.list_records(conn, include_inactive=False)
        full = build_summary(
            after_active, groups, scheme, self.probs, self.approx_threshold
        )

        if incremental != full:
            raise SummaryMismatchError(
                "增量贡献台账与全量重算结果不一致，事务回滚，未发布任何汇总"
            )

        version = self.store.insert_summary_version(
            conn,
            scheme_id=scheme.scheme_id,
            triggered_by=revision_id,
            event_type=event_type,
            active_count=full["active_count"],
            details=full,
        )
        self.store.link_revision_to_summary(conn, revision_id, version)

        if fail_before_commit:
            raise RuntimeError("故障注入：提交前主动失败，验证事务回滚")
        return version

    # ---------- 新增 ----------
    def insert(
        self,
        group_name: str,
        value: Any,
        measured_at: Any,
        reason: str,
        note: str | None = None,
        fail_before_commit: bool = False,
    ) -> dict[str, Any]:
        number = validate_value(value)
        when = validate_measured_at(measured_at)
        if not isinstance(reason, str) or not reason.strip():
            raise RevisionError("修订必须填写原因")

        with self.store.transaction() as conn:
            scheme = self.store.latest_scheme(conn)
            group_id = self.store.ensure_group(conn, group_name)
            active_before = self.store.list_records(conn, include_inactive=False)
            record_id = self.store.insert_record(conn, group_id, number, when, note)
            new_record = self.store.get_record(conn, record_id)
            revision_id = self.store.insert_revision(
                conn,
                record_id=record_id,
                event_type="insert",
                reason=reason.strip(),
                old_value=None,
                new_value=number,
                old_measured_at=None,
                new_measured_at=when,
            )
            version = self._publish(
                conn,
                scheme,
                [("insert", None, new_record)],
                active_before,
                revision_id,
                "insert",
                fail_before_commit,
            )
            return {"record_id": record_id, "revision_id": revision_id, "version": version}

    # ---------- 更正 ----------
    def correct(
        self,
        record_id: int,
        new_value: Any,
        new_measured_at: Any,
        reason: str,
        note: str | None = None,
        fail_before_commit: bool = False,
    ) -> dict[str, Any]:
        number = validate_value(new_value)
        when = validate_measured_at(new_measured_at)
        if not isinstance(reason, str) or not reason.strip():
            raise RevisionError("修订必须填写原因")

        with self.store.transaction() as conn:
            scheme = self.store.latest_scheme(conn)
            old = self.store.get_record(conn, record_id)
            if old["status"] != "active":
                raise RevisionError(
                    f"记录 {record_id} 当前状态为 {old['status']}，"
                    "只有 active 记录可以更正"
                )
            active_before = self.store.list_records(conn, include_inactive=False)
            revision_id = self.store.insert_revision(
                conn,
                record_id=record_id,
                event_type="correct",
                reason=reason.strip(),
                old_value=old["value"],
                new_value=number,
                old_measured_at=old["measured_at"],
                new_measured_at=when,
            )
            conn.execute(
                "UPDATE records SET value=?, measured_at=?, status='superseded',"
                " corrected_at=?, note=COALESCE(?, note) WHERE record_id=?",
                (number, when, utc_now_iso(), note, record_id),
            )
            # 更正 = 旧记录失效 + 一条新的有效记录（保留完整修订链）
            new_id = self.store.insert_record(
                conn, old["group_id"], number, when, note
            )
            new_record = self.store.get_record(conn, new_id)
            version = self._publish(
                conn,
                scheme,
                [("correct", old, new_record)],
                active_before,
                revision_id,
                "correct",
                fail_before_commit,
            )
            return {
                "record_id": new_id,
                "superseded_record_id": record_id,
                "revision_id": revision_id,
                "version": version,
            }

    # ---------- 撤销 ----------
    def revoke(
        self,
        record_id: int,
        reason: str,
        fail_before_commit: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise RevisionError("修订必须填写原因")

        with self.store.transaction() as conn:
            scheme = self.store.latest_scheme(conn)
            old = self.store.get_record(conn, record_id)
            if old["status"] != "active":
                raise RevisionError(
                    f"记录 {record_id} 当前状态为 {old['status']}，"
                    "只有 active 记录可以撤销"
                )
            active_before = self.store.list_records(conn, include_inactive=False)
            revision_id = self.store.insert_revision(
                conn,
                record_id=record_id,
                event_type="revoke",
                reason=reason.strip(),
                old_value=old["value"],
                new_value=None,
                old_measured_at=old["measured_at"],
                new_measured_at=None,
            )
            conn.execute(
                "UPDATE records SET status='revoked', revoked_at=? WHERE record_id=?",
                (utc_now_iso(), record_id),
            )
            version = self._publish(
                conn,
                scheme,
                [("revoke", old, None)],
                active_before,
                revision_id,
                "revoke",
                fail_before_commit,
            )
            return {"record_id": record_id, "revision_id": revision_id, "version": version}

    # ---------- 查询辅助 ----------
    def full_recompute(self, conn=None) -> dict[str, Any]:
        """从事务内或新连接做一次独立全量重算（不写库），供页面比对。"""

        def _work(c) -> dict[str, Any]:
            scheme = self.store.latest_scheme(c)
            groups = self.store.list_groups(c)
            active = self.store.list_records(c, include_inactive=False)
            return build_summary(
                active, groups, scheme, self.probs, self.approx_threshold
            )

        if conn is not None:
            return _work(conn)
        with self.store.transaction() as c:
            return _work(c)

    def compare_versions(self, conn, version_a: int, version_b: int) -> dict[str, Any]:
        a = self.store.get_summary_version(conn, version_a)
        b = self.store.get_summary_version(conn, version_b)
        rows: list[dict[str, Any]] = []
        for granularity in a["details"]["granularities"]:
            map_a = {
                (r["group_id"], r["period"]): r
                for r in a["details"]["granularities"][granularity]
            }
            map_b = {
                (r["group_id"], r["period"]): r
                for r in b["details"]["granularities"][granularity]
            }
            for key in sorted(set(map_a) | set(map_b)):
                ra = map_a.get(key)
                rb = map_b.get(key)
                rows.append(
                    {
                        "granularity": granularity,
                        "group_id": key[0],
                        "group_name": (rb or ra)["group_name"],
                        "period": key[1],
                        "count_a": ra["stats"]["count"] if ra else None,
                        "count_b": rb["stats"]["count"] if rb else None,
                        "count_delta": (
                            (rb["stats"]["count"] if rb else 0)
                            - (ra["stats"]["count"] if ra else 0)
                        ),
                        "quantiles_a": ra["stats"]["quantiles"] if ra else None,
                        "quantiles_b": rb["stats"]["quantiles"] if rb else None,
                        "bins_a": ra["stats"]["bins"] if ra else None,
                        "bins_b": rb["stats"]["bins"] if rb else None,
                    }
                )
        return {
            "version_a": version_a,
            "version_b": version_b,
            "created_at_a": a["created_at"],
            "created_at_b": b["created_at"],
            "event_a": a["event_type"],
            "event_b": b["event_type"],
            "rows": rows,
        }

    def verify_version(self, conn, version: int) -> dict[str, Any]:
        """把指定历史版本与当前数据的全量重算逐字段比对。"""
        stored = self.store.get_summary_version(conn, version)
        fresh = self.full_recompute(conn)
        match = stored["details"] == fresh
        mismatches: list[str] = []
        if not match:
            stored_d = stored["details"]
            for key in ("active_count", "scheme_id", "scheme_version"):
                if stored_d.get(key) != fresh.get(key):
                    mismatches.append(f"{key}: 存储={stored_d.get(key)} 重算={fresh.get(key)}")
            for granularity, rows in stored_d.get("granularities", {}).items():
                fresh_rows = {
                    (r["group_id"], r["period"]): r
                    for r in fresh["granularities"][granularity]
                }
                for row in rows:
                    key = (row["group_id"], row["period"])
                    if key not in fresh_rows:
                        mismatches.append(f"{granularity}/{key} 在重算结果中缺失")
                    elif row["stats"] != fresh_rows[key]["stats"]:
                        mismatches.append(f"{granularity}/{key} 统计不一致")
        return {
            "version": version,
            "matches_full_recompute": match,
            "mismatches": mismatches,
        }
