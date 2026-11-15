"""固定边界、带版本的直方图桶方案。

桶边界一旦发布即冻结：``bucket_schemes`` 只允许新增版本，不允许修改旧版本，
因此任何历史汇总的桶计数都可以按当时的边界复现。

归属规则（边界值有明确、可测试的行为）：

* ``v < edges[0]``              -> 桶外下界，索引 ``-1``
* ``edges[j] <= v < edges[j+1]`` -> 第 ``j`` 号桶
* ``v >= edges[-1]``            -> 桶外上界，索引 ``len(edges)``

即左闭右开；恰好等于上边界的值进入右侧相邻桶，恰好等于末边的值计入桶外上界。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class BucketScheme:
    scheme_id: int
    version: int
    name: str
    edges: tuple[float, ...]
    created_at: str
    note: str | None

    @property
    def bin_count(self) -> int:
        return len(self.edges) - 1

    def assign(self, value: float) -> int:
        """返回桶索引；``-1`` 为桶外下界，``len(edges)`` 为桶外上界。"""
        edges = self.edges
        if value < edges[0]:
            return -1
        if value >= edges[-1]:
            return len(edges)
        # 边界数量很少，线性扫描即可，且避免 bisect 在浮点边界上的歧义
        for j in range(len(edges) - 1):
            if edges[j] <= value < edges[j + 1]:
                return j
        raise RuntimeError(f"无法为值 {value} 分配桶")  # pragma: no cover

    def bin_label(self, index: int) -> str:
        if index == -1:
            return f"桶外：< {format_edge(self.edges[0])}"
        if index == len(self.edges):
            return f"桶外：>= {format_edge(self.edges[-1])}"
        return (
            f"[{format_edge(self.edges[index])}, "
            f"{format_edge(self.edges[index + 1])})"
        )


def format_edge(value: float) -> str:
    """边界展示：整数不带小数点。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def validate_edges(edges: Sequence[float]) -> list[float]:
    """校验边界：全部有限、严格递增、至少两条边（一个桶）。"""
    if len(edges) < 2:
        raise ValueError("桶方案至少需要 2 条边界（1 个桶）")
    cleaned: list[float] = []
    for raw in edges:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("桶边界必须是有限实数，拒绝 NaN/Inf")
        cleaned.append(value)
    for a, b in zip(cleaned, cleaned[1:]):
        if b <= a:
            raise ValueError(f"桶边界必须严格递增：{a} 之后出现 {b}")
    return cleaned
