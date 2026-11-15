"""汇总引擎：贡献台账 + 周期切分 + 汇总构建/全量重算。

贡献可撤回的实现
================
:class:`ContributionLedger` 保存一个分组×周期内全部**有效**测量值（允许重复值，
用多重集合建模）。它同时支持：

* ``add`` / ``remove`` 单条贡献——更正/撤销时先移除旧值、再加入新值；
* :meth:`snapshot` 产出桶计数、中位数与高分位数。

两条等价路径
------------
* 全量重算 :func:`build_summary`：从当前全部有效记录直接建台账；
* 增量路径 :func:`build_summary_incremental`：从修订前的有效记录建台账，再对
  每条修订显式执行“移除旧贡献 / 加入新贡献”。

两者使用同一个 :meth:`ContributionLedger.snapshot`，输出结构完全一致；服务层
在每次修订的同一事务内同时跑两条路径并断言相等，任何不一致都会让事务回滚，
杜绝在不可撤回的摘要上假装删除。旧汇总版本永远不会被改写：每次修订都生成一个
新的、不可变的版本。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .buckets import BucketScheme
from .quantile import QUANTILE_DEFINITION, quantile_report

# 高分位：P50 中位数 + P90/P95/P99
DEFAULT_PROBS = (0.50, 0.90, 0.95, 0.99)
# 有效样本量不超过该阈值时精确计算，否则走桶插值近似并给出误差界
DEFAULT_APPROX_THRESHOLD = 1000

PERIOD_GRANULARITIES = ("all", "day", "week", "month")


def period_key(measured_at: str, granularity: str) -> str:
    """把 ISO8601 时间归入周期键。

    * ``all``   -> ``"all"``
    * ``day``   -> ``"YYYY-MM-DD"``
    * ``week``  -> ``"YYYY-Www"``（ISO 周，周一为起点）
    * ``month`` -> ``"YYYY-MM"``
    """
    dt = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
    if granularity == "all":
        return "all"
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "month":
        return dt.strftime("%Y-%m")
    if granularity == "week":
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    raise ValueError(f"未知周期粒度：{granularity}")


@dataclass
class ContributionLedger:
    """单分组×单周期的多重集合台账。"""

    values: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.values.append(float(value))

    def remove(self, value: float) -> None:
        # 多重集合语义：重复值只移除其中一个；找不到说明调用方状态有误
        try:
            self.values.remove(float(value))
        except ValueError as exc:
            raise ValueError(f"无法移除不存在的贡献 {value}") from exc

    def __len__(self) -> int:
        return len(self.values)

    def snapshot(
        self,
        scheme: BucketScheme,
        probs: Iterable[float] = DEFAULT_PROBS,
        approx_threshold: int = DEFAULT_APPROX_THRESHOLD,
    ) -> dict[str, Any]:
        """产出该台账当前状态的完整汇总（空台账有明确表现，见下）。"""
        n = len(self.values)
        if n == 0:
            return {
                "count": 0,
                "mean": None,
                "min": None,
                "max": None,
                "below_count": 0,
                "above_count": 0,
                "bins": [
                    {
                        "index": j,
                        "label": scheme.bin_label(j),
                        "edge_lo": float(scheme.edges[j]),
                        "edge_hi": float(scheme.edges[j + 1]),
                        "count": 0,
                    }
                    for j in range(scheme.bin_count)
                ],
                "out_of_range": {
                    "below": {"label": scheme.bin_label(-1), "count": 0},
                    "above": {
                        "label": scheme.bin_label(len(scheme.edges)),
                        "count": 0,
                    },
                },
                "quantiles": [
                    {
                        "p": p,
                        "method": "empty",
                        "value": None,
                        "max_abs_error": None,
                        "definition": QUANTILE_DEFINITION,
                        "side": None,
                    }
                    for p in probs
                ],
                "quantile_note": "空分组/空周期：无有效记录，分位数未定义",
            }

        ordered = sorted(self.values)
        counts: Counter[int] = Counter()
        below = 0
        above = 0
        for value in self.values:
            idx = scheme.assign(value)
            if idx == -1:
                below += 1
            elif idx == len(scheme.edges):
                above += 1
            else:
                counts[idx] += 1

        bin_counts = [counts.get(j, 0) for j in range(scheme.bin_count)]
        quantiles = [
            quantile_report(
                ordered, p, below, bin_counts, scheme.edges, approx_threshold
            )
            for p in probs
        ]
        mean = sum(ordered) / n
        return {
            "count": n,
            "mean": mean,
            "min": ordered[0],
            "max": ordered[-1],
            "below_count": below,
            "above_count": above,
            "bins": [
                {
                    "index": j,
                    "label": scheme.bin_label(j),
                    "edge_lo": float(scheme.edges[j]),
                    "edge_hi": float(scheme.edges[j + 1]),
                    "count": bin_counts[j],
                }
                for j in range(scheme.bin_count)
            ],
            "out_of_range": {
                "below": {"label": scheme.bin_label(-1), "count": below},
                "above": {
                    "label": scheme.bin_label(len(scheme.edges)),
                    "count": above,
                },
            },
            "quantiles": quantiles,
            "quantile_note": (
                f"n={n} <= 阈值 {approx_threshold}，分位数为 type-7 精确值"
                if n <= approx_threshold
                else f"n={n} > 阈值 {approx_threshold}，分位数为桶内线性插值近似，"
                "每项给出确定性最坏绝对误差界（=所在桶宽度）；桶外分位无有限界"
            ),
        }


def _wrap_detail(
    scheme: BucketScheme,
    granularities: dict[str, Any],
    probs: Iterable[float],
    approx_threshold: int,
    active_count: int,
) -> dict[str, Any]:
    return {
        "scheme_version": scheme.version,
        "scheme_id": scheme.scheme_id,
        "edges": list(scheme.edges),
        "quantile_definition": QUANTILE_DEFINITION,
        "quantile_probs": list(probs),
        "approx_threshold": approx_threshold,
        "granularities": granularities,
        "active_count": active_count,
    }


def _empty_granularities(
    groups: list[dict[str, Any]],
    scheme: BucketScheme,
    probs: Iterable[float],
    approx_threshold: int,
) -> dict[str, Any]:
    """零记录时每个设备组在每个粒度下都显式给出空表现。"""
    granularities: dict[str, Any] = {}
    for granularity in PERIOD_GRANULARITIES:
        granularities[granularity] = [
            {
                "group_id": group["group_id"],
                "group_name": group["name"],
                "period": "all",
                "stats": ContributionLedger().snapshot(
                    scheme, probs, approx_threshold
                ),
            }
            for group in groups
        ]
    return granularities


def _render_granularities(
    ledgers: dict[tuple[str, int, str], ContributionLedger],
    groups: list[dict[str, Any]],
    scheme: BucketScheme,
    probs: Iterable[float],
    approx_threshold: int,
) -> tuple[dict[str, Any], int]:
    group_names = {g["group_id"]: g["name"] for g in groups}
    granularities: dict[str, Any] = {}
    active_count = 0
    for granularity in PERIOD_GRANULARITIES:
        rows: list[dict[str, Any]] = []
        for group in groups:
            group_id = group["group_id"]
            periods = sorted(
                {
                    key[2]
                    for key, ledger in ledgers.items()
                    if key[0] == granularity
                    and key[1] == group_id
                    and len(ledger) > 0
                }
            )
            if not periods:
                # 空分组（含全部记录被撤销）显式输出一行空表现
                rows.append(
                    {
                        "group_id": group_id,
                        "group_name": group_names[group_id],
                        "period": "all",
                        "stats": ContributionLedger().snapshot(
                            scheme, probs, approx_threshold
                        ),
                    }
                )
                continue
            for period in periods:
                ledger = ledgers[(granularity, group_id, period)]
                if granularity == "all":
                    active_count += len(ledger)
                rows.append(
                    {
                        "group_id": group_id,
                        "group_name": group_names[group_id],
                        "period": period,
                        "stats": ledger.snapshot(scheme, probs, approx_threshold),
                    }
                )
        granularities[granularity] = rows
    return granularities, active_count


def _seed_ledgers(records: Iterable[dict[str, Any]]) -> dict[tuple[str, int, str], ContributionLedger]:
    ledgers: dict[tuple[str, int, str], ContributionLedger] = {}
    for record in records:
        if record["status"] != "active":
            continue
        for granularity in PERIOD_GRANULARITIES:
            key = (
                granularity,
                record["group_id"],
                period_key(record["measured_at"], granularity),
            )
            ledgers.setdefault(key, ContributionLedger()).add(record["value"])
    return ledgers


def build_summary(
    active_records: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    scheme: BucketScheme,
    probs: Iterable[float] = DEFAULT_PROBS,
    approx_threshold: int = DEFAULT_APPROX_THRESHOLD,
) -> dict[str, Any]:
    """全量重算：从有效记录集合直接构建完整汇总细节。"""
    if not groups:
        return _wrap_detail(scheme, {}, probs, approx_threshold, 0)
    if not active_records:
        granularities = _empty_granularities(groups, scheme, probs, approx_threshold)
        return _wrap_detail(scheme, granularities, probs, approx_threshold, 0)

    ledgers = _seed_ledgers(active_records)
    granularities, active_count = _render_granularities(
        ledgers, groups, scheme, probs, approx_threshold
    )
    return _wrap_detail(
        scheme, granularities, probs, approx_threshold, active_count
    )


def build_summary_incremental(
    active_records_before: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    scheme: BucketScheme,
    changes: Iterable[tuple[str, dict[str, Any] | None, dict[str, Any] | None]],
    probs: Iterable[float] = DEFAULT_PROBS,
    approx_threshold: int = DEFAULT_APPROX_THRESHOLD,
) -> dict[str, Any]:
    """增量路径：从修订前的有效记录建台账，再显式移除旧贡献/加入新贡献。

    ``changes`` 元素为 ``(动作, 旧记录, 新记录)``：

    * ``("insert", None, record)``
    * ``("correct", old_record, new_record)``——旧记录为修订前 active 快照
    * ``("revoke", old_record, None)``

    与 :func:`build_summary` 的区别只在于贡献的进入方式：这里每条修订的
    remove/add 都是一等操作，可被测试直接断言（重复值只移除一个、撤销后桶
    计数回落等）。两条路径的输出由服务层在同一事务内断言相等。
    """
    ledgers = _seed_ledgers(active_records_before)

    def _ledger_for(granularity: str, record: dict[str, Any]) -> ContributionLedger:
        key = (
            granularity,
            record["group_id"],
            period_key(record["measured_at"], granularity),
        )
        return ledgers.setdefault(key, ContributionLedger())

    for action, old, new in changes:
        if action == "insert":
            if new is None:
                raise ValueError("insert 事件缺少新记录")
            for granularity in PERIOD_GRANULARITIES:
                _ledger_for(granularity, new).add(new["value"])
        elif action == "correct":
            if old is None or new is None:
                raise ValueError("correct 事件需要旧记录与新记录")
            for granularity in PERIOD_GRANULARITIES:
                _ledger_for(granularity, old).remove(old["value"])
                _ledger_for(granularity, new).add(new["value"])
        elif action == "revoke":
            if old is None:
                raise ValueError("revoke 事件缺少旧记录")
            for granularity in PERIOD_GRANULARITIES:
                _ledger_for(granularity, old).remove(old["value"])
        else:
            raise ValueError(f"未知变更动作：{action}")

    granularities, active_count = _render_granularities(
        ledgers, groups, scheme, probs, approx_threshold
    )
    return _wrap_detail(
        scheme, granularities, probs, approx_threshold, active_count
    )
