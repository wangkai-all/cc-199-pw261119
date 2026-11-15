"""本地 HTTP 服务：JSON API + 静态前端，全部基于标准库。"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import RevisionError, RevisionService
from .storage import Store

STATIC_DIR = Path(__file__).parent / "static"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "PointwiseDelivery/1.0"

    # --- 基础工具 ---
    def _send_json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise RevisionError("请求体为空，需要 JSON")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RevisionError("请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise RevisionError("请求体必须是 JSON 对象")
        return payload

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items() if v}

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # --- 路由 ---
    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
        except Exception as exc:  # 畸形请求行/编码
            self._send_error(HTTPStatus.BAD_REQUEST, f"非法请求路径：{exc}")
            return
        try:
            if path == "/api/health":
                return self._send_json({"status": "ok"})
            if path == "/api/schemes":
                with self.server.store.transaction() as conn:
                    return self._send_json(
                        [
                            {
                                "scheme_id": s.scheme_id,
                                "version": s.version,
                                "name": s.name,
                                "edges": list(s.edges),
                                "created_at": s.created_at,
                                "note": s.note,
                            }
                            for s in self.server.store.list_schemes(conn)
                        ]
                    )
            if path == "/api/groups":
                with self.server.store.transaction() as conn:
                    return self._send_json(self.server.store.list_groups(conn))
            if path == "/api/records":
                q = self._query()
                group_id = int(q["group_id"]) if "group_id" in q else None
                include_inactive = q.get("include_inactive", "1") != "0"
                with self.server.store.transaction() as conn:
                    return self._send_json(
                        self.server.store.list_records(conn, group_id, include_inactive)
                    )
            if path == "/api/revisions":
                with self.server.store.transaction() as conn:
                    return self._send_json(self.server.store.list_revisions(conn))
            if path == "/api/summaries":
                with self.server.store.transaction() as conn:
                    versions = self.server.store.list_summary_versions(conn)
                    return self._send_json(versions)
            if path == "/api/summary":
                with self.server.store.transaction() as conn:
                    q = self._query()
                    latest = self.server.store.latest_summary_version(conn)
                    if latest is None:
                        return self._send_error(
                            HTTPStatus.NOT_FOUND, "尚无汇总版本，请先写入记录"
                        )
                    version = int(q.get("version") or latest)
                    return self._send_json(
                        self.server.store.get_summary_version(conn, version)
                    )
            if path == "/api/recompute":
                return self._send_json(self.server.service.full_recompute())
            if path == "/api/verify":
                version = int(self._query()["version"])
                with self.server.store.transaction() as conn:
                    return self._send_json(
                        self.server.service.verify_version(conn, version)
                    )
            if path == "/api/compare":
                q = self._query()
                with self.server.store.transaction() as conn:
                    return self._send_json(
                        self.server.service.compare_versions(
                            conn, int(q["a"]), int(q["b"])
                        )
                    )
            return self._serve_static(path)
        except RevisionError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except LookupError as exc:
            return self._send_error(HTTPStatus.NOT_FOUND, str(exc))
        except (KeyError, ValueError) as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, f"参数错误：{exc}")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # 兜底：任何未预期错误都返回 500 而不是断连
            return self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器内部错误：{exc}"
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            service = self.server.service
            if path == "/api/records":
                result = service.insert(
                    group_name=payload["group_name"],
                    value=payload["value"],
                    measured_at=payload["measured_at"],
                    reason=payload["reason"],
                    note=payload.get("note"),
                    fail_before_commit=bool(payload.get("fail_before_commit")),
                )
            elif path == "/api/correct":
                result = service.correct(
                    record_id=int(payload["record_id"]),
                    new_value=payload["new_value"],
                    new_measured_at=payload["measured_at"],
                    reason=payload["reason"],
                    note=payload.get("note"),
                    fail_before_commit=bool(payload.get("fail_before_commit")),
                )
            elif path == "/api/revoke":
                result = service.revoke(
                    record_id=int(payload["record_id"]),
                    reason=payload["reason"],
                    fail_before_commit=bool(payload.get("fail_before_commit")),
                )
            elif path == "/api/schemes":
                # 新增桶方案版本（旧版本冻结，不做迁移，仅用于后续汇总）
                edges = payload["edges"]
                with self.server.store.transaction() as conn:
                    scheme = self.server.store.create_scheme(
                        conn,
                        name=payload.get("name", "scheme"),
                        edges=edges,
                        note=payload.get("note"),
                    )
                    result = {
                        "scheme_id": scheme.scheme_id,
                        "version": scheme.version,
                        "edges": list(scheme.edges),
                    }
            else:
                return self._send_error(HTTPStatus.NOT_FOUND, f"未知接口 {path}")
            return self._send_json(result, HTTPStatus.CREATED)
        except RevisionError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except LookupError as exc:
            return self._send_error(HTTPStatus.NOT_FOUND, str(exc))
        except KeyError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, f"缺少必填字段：{exc}")
        except (TypeError, ValueError) as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, f"参数错误：{exc}")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            return self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器内部错误：{exc}"
            )

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._send_error(HTTPStatus.NOT_FOUND, f"无此文件 {path}")
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = RevisionService(store)
    server = ThreadingHTTPServer((host, port), AppHandler)
    server.store = store  # type: ignore[attr-defined]
    server.service = service  # type: ignore[attr-defined]
    server.verbose = False  # type: ignore[attr-defined]
    return server


def serve_in_thread(db_path: str, host: str = "127.0.0.1", port: int = 0):
    """测试用：随机端口 + 后台线程，返回 (server, url)。"""
    server = create_server(db_path, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    return server, f"http://{actual_host}:{actual_port}"
