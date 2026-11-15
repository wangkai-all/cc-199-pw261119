"""本地应用入口：``python -I -m app.main [--db data/pointwise.db] [--port 8000]``。"""

from __future__ import annotations

import argparse

from .seed import DEFAULT_EDGES, seed
from .server import create_server
from .storage import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="设备测量数据分布汇总本地应用")
    parser.add_argument("--db", default="data/pointwise.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="数据库为空时自动写入样例数据（含重复修订、边界值、极端值）",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="删除已有数据库后重新写入样例数据",
    )
    args = parser.parse_args()

    if args.reset:
        from pathlib import Path

        for suffix in ("", "-wal", "-shm"):
            Path(args.db + suffix).unlink(missing_ok=True)
        args.seed = True

    store = Store(args.db)
    need_seed = args.seed
    if need_seed:
        with store.transaction() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM bucket_schemes").fetchone()["c"]:
                need_seed = False
    if need_seed:
        stats = seed(args.db)
        print(f"样例数据就绪：{stats}")
    else:
        # 确保至少存在桶方案，否则无法接受修订
        with store.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM bucket_schemes LIMIT 1"
            ).fetchone():
                store.create_scheme(
                    conn, "默认测量桶 v1", DEFAULT_EDGES,
                    "固定边界 0..100，步长 10",
                )

    server = create_server(args.db, args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port} （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
