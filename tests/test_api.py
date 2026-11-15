"""HTTP 接口冒烟测试：真实起服、真实请求，覆盖主要页面 API。"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.server import serve_in_thread
from app.storage import Store


def request(url: str, payload=None, method=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "api.db")
        store = Store(self.db_path)
        with store.transaction() as conn:
            store.create_scheme(conn, "v1", [0.0, 10.0, 20.0], "测试")
        self.server, self.base = serve_in_thread(self.db_path, "127.0.0.1", 0)

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_health_and_static_page(self):
        status, body = request(f"{self.base}/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

        with urllib.request.urlopen(f"{self.base}/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
        self.assertIn("<!doctype html>", html.lower())

    def test_full_revision_flow_over_http(self):
        # 新增
        status, created = request(
            f"{self.base}/api/records",
            {
                "group_name": "G1",
                "value": 15.0,
                "measured_at": "2026-11-11T10:00:00Z",
                "reason": "HTTP 录入",
            },
        )
        self.assertEqual(status, 201)
        record_id = created["record_id"]

        # 重复修订：更正
        status, corrected = request(
            f"{self.base}/api/correct",
            {
                "record_id": record_id,
                "new_value": 18.0,
                "measured_at": "2026-11-11T10:00:00Z",
                "reason": "HTTP 更正",
            },
        )
        self.assertEqual(status, 201)

        # 撤销新记录
        status, revoked = request(
            f"{self.base}/api/revoke",
            {"record_id": corrected["record_id"], "reason": "HTTP 撤销"},
        )
        self.assertEqual(status, 201)

        # 版本列表与详情
        status, versions = request(f"{self.base}/api/summaries")
        self.assertEqual(status, 200)
        self.assertEqual(len(versions), 3)

        status, detail = request(f"{self.base}/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(detail["version"], versions[-1]["version"])

        # 全量重算与校验
        status, fresh = request(f"{self.base}/api/recompute")
        self.assertEqual(status, 200)
        self.assertEqual(fresh["active_count"], 0)

        status, verify = request(
            f"{self.base}/api/verify?version={versions[-1]['version']}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(verify["matches_full_recompute"])

        # 版本比较
        status, diff = request(f"{self.base}/api/compare?a=1&b=3")
        self.assertEqual(status, 200)
        self.assertTrue(diff["rows"])

        # 桶内原始记录
        status, records = request(f"{self.base}/api/records?group_id=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(records), 2)  # 一条 superseded + 一条 revoked

        # 修订历史
        status, revisions = request(f"{self.base}/api/revisions")
        self.assertEqual(status, 200)
        self.assertEqual(
            [r["event_type"] for r in revisions],
            ["insert", "correct", "revoke"],
        )

    def test_failure_injection_leaves_no_partial_publish(self):
        status, body = request(
            f"{self.base}/api/records",
            {
                "group_name": "G1",
                "value": 1.0,
                "measured_at": "2026-11-11T10:00:00Z",
                "reason": "注定失败",
                "fail_before_commit": True,
            },
        )
        self.assertEqual(status, 500)
        self.assertIn("故障注入", body["error"])
        status, versions = request(f"{self.base}/api/summaries")
        self.assertEqual(versions, [])
        status, records = request(f"{self.base}/api/records")
        self.assertEqual(records, [])

    def test_bad_requests_are_rejected(self):
        status, body = request(
            f"{self.base}/api/records",
            {
                "group_name": "G1",
                "value": "not-a-number",
                "measured_at": "2026-11-11T10:00:00Z",
                "reason": "x",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

        status, body = request(
            f"{self.base}/api/records",
            {
                "group_name": "G1",
                "value": 1.0,
                "measured_at": "2026-11-11T10:00:00Z",
                "reason": "x",
            },
        )
        self.assertEqual(status, 201)
        status, body = request(
            f"{self.base}/api/revoke", {"record_id": 999, "reason": "x"}
        )
        self.assertEqual(status, 404)

    def test_path_traversal_blocked(self):
        status, _ = request(f"{self.base}/../etc/passwd")
        self.assertIn(status, (404, 400))


if __name__ == "__main__":
    unittest.main()
