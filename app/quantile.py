"""分位数计算：明确的定义、精确/近似两条路径与误差口径。

分位定义
========
采用 Hyndman & Fan (1996) 的 **type 7**（numpy ``quantile`` 默认、R  ``quantile``
默认）。对已排序样本 ``x[0..n-1]`` 与概率 ``p``：

    h = (n - 1) * p
    q = x[floor(h)] + (h - floor(h)) * (x[ceil(h)] - x[floor(h)])

两条路径
========
* 数据量不超过阈值时 :func:`quantile_exact` —— 精确值，最坏绝对误差为 0。
* 数据量较大时 :func:`quantile_from_bins` —— 只用固定直方图桶计数，在目标分位
  落入的桶内按均匀分布做线性插值。误差口径是**确定性的最坏界**，不是抽样误差：
  桶内真实值可以是桶区间内任意位置，因此
      |估计值 - 真实 type-7 分位| <= 该桶宽度
  目标分位落入“桶外”（低于首边/高于末边）时，桶外没有有限宽度的区间，
  无法给出有限误差界，函数显式返回 ``unbounded`` 而不是编造一个数字。
"""

from __future__ import annotations

import math
from typing import Sequence

QUANTILE_DEFINITION = "Hyndman-Fan type 7, h=(n-1)p 线性插值"


def quantile_exact(sorted_values: Sequence[float], p: float) -> float:
    """返回 type-7 精确分位数。调用方须保证序列已升序排序且非空。"""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("空序列无法计算分位数")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"分位概率必须落在 [0,1]，收到 {p}")
    if n == 1:
        return float(sorted_values[0])
    h = (n - 1) * p
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return float(sorted_values[lo])
    return float(sorted_values[lo]) + (h - lo) * (
        float(sorted_values[hi]) - float(sorted_values[lo])
    )


def quantile_from_bins(
    p: float,
    total_count: int,
    below_count: int,
    bin_counts: Sequence[int],
    edges: Sequence[float],
) -> dict:
    """仅依据直方图桶计数估计分位数。

    ``total_count`` 是包含桶外上界值在内的完整样本量；桶外上界的个数 =
    ``total_count - below_count - sum(bin_counts)``。

    返回字典字段：

    ``method``
        ``"interpolated"``：目标分位落在某个有限宽度桶内；
        ``"unbounded"``：目标分位落在桶外区间，无法给出有限误差界。
    ``value``
        插值估计值；``unbounded`` 时为 ``None``。
    ``max_abs_error``
        最坏绝对误差界（= 所在桶宽度）；``unbounded`` 时为 ``None``。
    ``side``
        ``unbounded`` 时为 ``"below"`` / ``"above"``，否则为 ``None``。
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"分位概率必须落在 [0,1]，收到 {p}")
    in_range = below_count + sum(bin_counts)
    n = total_count
    if n == 0:
        raise ValueError("空样本无法计算分位数")
    if in_range > n:
        raise ValueError("桶内与桶外下界计数之和不能超过总样本量")

    target = p * n  # 1-based 经验分布位置：寻找第 target 个值所在的桶
    # 桶外下界：前 below_count 个值都小于 edges[0]
    if target <= below_count:
        return {
            "method": "unbounded",
            "value": None,
            "max_abs_error": None,
            "side": "below",
        }

    cum = below_count
    for j, count in enumerate(bin_counts):
        cum_next = cum + count
        # 严格小于：target == cum_next 时第 target 个值属于下一个桶；
        # 若已是最后一个桶，则落入桶外上界
        if target < cum_next:
            edge_lo = float(edges[j])
            width = float(edges[j + 1]) - edge_lo
            if count <= 0 or width <= 0:
                # 理论上不可达：target 落入的桶必然非空、边严格递增
                return {
                    "method": "unbounded",
                    "value": None,
                    "max_abs_error": None,
                    "side": None,
                }
            fraction = (target - cum) / count
            return {
                "method": "interpolated",
                "value": edge_lo + fraction * width,
                "max_abs_error": width,
                "side": None,
            }
        cum = cum_next

    # 目标位置越过全部桶内值，落入桶外上界，无有限宽度可约束误差
    return {
        "method": "unbounded",
        "value": None,
        "max_abs_error": None,
        "side": "above",
    }


def quantile_report(
    sorted_values: Sequence[float],
    p: float,
    below_count: int,
    bin_counts: Sequence[int],
    edges: Sequence[float],
    approx_threshold: int,
) -> dict:
    """按样本量选择精确或近似路径，返回带口径说明的统一结构。"""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("空样本无法计算分位数")
    if n <= approx_threshold:
        return {
            "p": p,
            "method": "exact",
            "value": quantile_exact(sorted_values, p),
            "max_abs_error": 0.0,
            "definition": QUANTILE_DEFINITION,
            "side": None,
        }
    result = quantile_from_bins(p, n, below_count, bin_counts, edges)
    return {
        "p": p,
        "method": result["method"],
        "value": result["value"],
        "max_abs_error": result["max_abs_error"],
        "definition": f"{QUANTILE_DEFINITION}；大样本近似：目标桶内均匀线性插值",
        "side": result["side"],
    }
