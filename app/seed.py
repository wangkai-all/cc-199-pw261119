"""样例数据：重复修订、边界值、极端值，以及触发近似分位的大样本组。

运行：``python -I -m app.seed [数据库路径]``
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from .service import RevisionService
from .storage import Store

# 固定边界、带版本的桶方案 v1（0/10/20/.../100 共 10 个桶，桶外双侧计数）
DEFAULT_EDGES = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]

GROUPS = ["光谱仪-A", "温控箱-B", "压力台-C"]


def seed(db_path: str) -> dict:
    store = Store(db_path)
    with store.transaction() as conn:
        store.create_scheme(
            conn,
            name="默认测量桶 v1",
            edges=DEFAULT_EDGES,
            note="固定边界 0..100，步长 10，左闭右开；桶外双侧单独计数",
        )

    service = RevisionService(store)
    rng = random.Random(20261116)
    stats = {"inserts": 0, "corrects": 0, "revokes": 0}

    # 1) 温控箱-B：小样本精确分位，覆盖边界值与极端值
    boundary_values = [0.0, 10.0, 20.0, 50.0, 90.0, 100.0]  # 末值 100 落入桶外上界
    for i, value in enumerate(boundary_values):
        service.insert(
            group_name="温控箱-B",
            value=value,
            measured_at=f"2026-11-10T08:{i:02d}:00Z",
            reason="边界值样例：验证左闭右开与桶外计数",
        )
        stats["inserts"] += 1
    service.insert(
        group_name="温控箱-B",
        value=-273.15,  # 桶外下界极端值
        measured_at="2026-11-10T09:00:00Z",
        reason="极端值样例：桶外下界",
    )
    stats["inserts"] += 1
    service.insert(
        group_name="温控箱-B",
        value=1e9,  # 桶外上界极端值
        measured_at="2026-11-10T09:05:00Z",
        reason="极端值样例：桶外上界",
    )
    stats["inserts"] += 1

    # 2) 光谱仪-A：重复修订链——插入后更正两次、再撤销另一条
    first = service.insert(
        group_name="光谱仪-A",
        value=42.0,
        measured_at="2026-11-11T10:00:00Z",
        reason="首次录入",
    )
    stats["inserts"] += 1
    # 更正产生的是新记录，对新记录再更正一次（重复修订）
    second = service.correct(
        record_id=first["record_id"],
        new_value=43.5,
        new_measured_at="2026-11-11T10:00:00Z",
        reason="重复修订样例：第一次更正（校准偏移）",
    )
    stats["corrects"] += 1
    service.correct(
        record_id=second["record_id"],
        new_value=44.1,
        new_measured_at="2026-11-11T10:00:00Z",
        reason="重复修订样例：第二次更正（复核修正）",
    )
    stats["corrects"] += 1

    doomed = service.insert(
        group_name="光谱仪-A",
        value=77.0,
        measured_at="2026-11-11T11:00:00Z",
        reason="待撤销样例录入",
    )
    stats["inserts"] += 1
    service.revoke(
        record_id=doomed["record_id"],
        reason="重复修订样例：该读数来自未标定探头，整笔撤销",
    )
    stats["revokes"] += 1

    # 同值重复记录：验证多重集合计数与“只移除一个”的撤销语义
    for i in range(3):
        service.insert(
            group_name="光谱仪-A",
            value=55.0,
            measured_at=f"2026-11-12T09:{10 + i:02d}:00Z",
            reason="重复值样例：同一读数三次录入",
        )
        stats["inserts"] += 1

    # 3) 压力台-C：1200 条随机记录，n > 近似阈值 1000，触发桶插值分位与误差界
    for i in range(1200):
        day = 9 + (i // 300) + 1  # 跨 4 天，便于按日/周/月切周期
        hour = (i % 24)
        minute = (i * 7) % 60
        value = rng.gauss(55.0, 18.0)
        # 偶发桶外值，保证大组里也有桶外计数
        if i % 97 == 0:
            value = -5.0 - rng.random() * 10
        if i % 131 == 0:
            value = 105.0 + rng.random() * 20
        service.insert(
            group_name="压力台-C",
            value=round(value, 3),
            measured_at=f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00Z",
            reason="大样本样例：触发近似分位与误差口径展示",
        )
        stats["inserts"] += 1

    with store.transaction() as conn:
        stats["final_version"] = conn.execute(
            "SELECT MAX(version) AS v FROM summary_versions"
        ).fetchone()["v"]
        stats["active_records"] = conn.execute(
            "SELECT COUNT(*) AS c FROM records WHERE status='active'"
        ).fetchone()["c"]
    return stats


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else "data/pointwise.db"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    stats = seed(db_path)
    print(f"已写入 {db_path}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
