"""服务层端到端测试：修订链、版本不可变、故障注入原子性、重算校验。"""

import tempfile
import unittest
from pathlib import Path

from app.service import RevisionError, RevisionService, SummaryMismatchError
from app.storage import Store


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.store = Store(self.db_path)
        with self.store.transaction() as conn:
            self.scheme = self.store.create_scheme(
                conn, "v1", [0.0, 10.0, 20.0, 30.0], "测试桶"
            )
        self.service = RevisionService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _latest(self, conn):
        version = self.store.latest_summary_version(conn)
        return self.store.get_summary_version(conn, version)


class RevisionFlowTest(ServiceTestBase):
    def test_insert_publishes_first_version(self):
        result = self.service.insert("G1", 15.0, "2026-11-11T10:00:00Z", "录入")
        self.assertEqual(result["version"], 1)
        with self.store.transaction() as conn:
            summary = self._latest(conn)
            row = next(
                r
                for r in summary["details"]["granularities"]["all"]
                if r["group_name"] == "G1"
            )
            self.assertEqual(row["stats"]["count"], 1)
            self.assertEqual(row["stats"]["bins"][1]["count"], 1)

    def test_repeated_corrections_keep_full_chain(self):
        first = self.service.insert("G1", 42.0, "2026-11-11T10:00:00Z", "录入")
        c1 = self.service.correct(
            first["record_id"], 43.5, "2026-11-11T10:00:00Z", "更正1"
        )
        c2 = self.service.correct(
            c1["record_id"], 44.1, "2026-11-11T10:00:00Z", "更正2"
        )
        self.assertEqual(c2["version"], 3)
        with self.store.transaction() as conn:
            # 原始两条记录被标记 superseded，当前只有一条 active
            statuses = [
                r["status"]
                for r in self.store.list_records(conn)
            ]
            self.assertEqual(statuses.count("superseded"), 2)
            self.assertEqual(statuses.count("active"), 1)
            summary = self._latest(conn)
            row = next(
                r
                for r in summary["details"]["granularities"]["all"]
                if r["group_name"] == "G1"
            )
            self.assertEqual(row["stats"]["count"], 1)
            self.assertEqual(row["stats"]["max"], 44.1)
            # 历史版本不可变：v1 仍然记录 42.0
            v1 = self.store.get_summary_version(conn, 1)
            v1_row = next(
                r
                for r in v1["details"]["granularities"]["all"]
                if r["group_name"] == "G1"
            )
            self.assertEqual(v1_row["stats"]["max"], 42.0)
            revisions = self.store.list_revisions(conn)
            self.assertEqual([r["event_type"] for r in revisions],
                             ["insert", "correct", "correct"])

    def test_revoke_removes_contribution_and_keeps_history(self):
        r1 = self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入1")
        self.service.insert("G1", 5.0, "2026-11-11T11:00:00Z", "录入2")
        before_versions = None
        with self.store.transaction() as conn:
            before_versions = len(self.store.list_summary_versions(conn))
        self.service.revoke(r1["record_id"], "探头未标定")
        with self.store.transaction() as conn:
            summary = self._latest(conn)
            row = next(
                r
                for r in summary["details"]["granularities"]["all"]
                if r["group_name"] == "G1"
            )
            # 重复值只移除一个
            self.assertEqual(row["stats"]["count"], 1)
            self.assertEqual(row["stats"]["bins"][0]["count"], 1)
            revoked = self.store.get_record(conn, r1["record_id"])
            self.assertEqual(revoked["status"], "revoked")
            self.assertIsNotNone(revoked["revoked_at"])
            # 新版本号递增，旧版本数量保留
            self.assertEqual(
                len(self.store.list_summary_versions(conn)), before_versions + 1
            )

    def test_cannot_revise_inactive_record(self):
        r1 = self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        self.service.revoke(r1["record_id"], "撤销")
        with self.assertRaises(RevisionError):
            self.service.revoke(r1["record_id"], "再次撤销")
        with self.assertRaises(RevisionError):
            self.service.correct(
                r1["record_id"], 6.0, "2026-11-11T10:00:00Z", "改已撤销"
            )

    def test_validation_rejects_bad_inputs(self):
        with self.assertRaises(RevisionError):
            self.service.insert("G1", float("nan"), "2026-11-11T10:00:00Z", "x")
        with self.assertRaises(RevisionError):
            self.service.insert("G1", float("inf"), "2026-11-11T10:00:00Z", "x")
        with self.assertRaises(RevisionError):
            self.service.insert("G1", "abc", "2026-11-11T10:00:00Z", "x")
        with self.assertRaises(RevisionError):
            self.service.insert("G1", 1.0, "not-a-date", "x")
        with self.assertRaises(RevisionError):
            self.service.insert("G1", 1.0, "2026-11-11T10:00:00Z", "  ")

    def test_reason_is_mandatory(self):
        with self.assertRaises(RevisionError):
            self.service.insert("G1", 1.0, "2026-11-11T10:00:00Z", "")


class AtomicityTest(ServiceTestBase):
    def _state(self):
        with self.store.transaction() as conn:
            return (
                conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"],
                conn.execute("SELECT COUNT(*) c FROM revisions").fetchone()["c"],
                conn.execute("SELECT COUNT(*) c FROM summary_versions").fetchone()["c"],
            )

    def test_failed_insert_publishes_nothing(self):
        before = self._state()
        with self.assertRaises(RuntimeError):
            self.service.insert(
                "G1", 1.0, "2026-11-11T10:00:00Z", "录入",
                fail_before_commit=True,
            )
        self.assertEqual(self._state(), before)

    def test_failed_correct_rolls_back_everything(self):
        self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        before = self._state()
        with self.assertRaises(RuntimeError):
            self.service.correct(
                1, 9.0, "2026-11-11T10:00:00Z", "更正",
                fail_before_commit=True,
            )
        self.assertEqual(self._state(), before)
        with self.store.transaction() as conn:
            record = self.store.get_record(conn, 1)
            self.assertEqual(record["status"], "active")
            self.assertEqual(record["value"], 5.0)

    def test_failed_revoke_rolls_back(self):
        self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        before = self._state()
        with self.assertRaises(RuntimeError):
            self.service.revoke(1, "撤销", fail_before_commit=True)
        self.assertEqual(self._state(), before)


class RecomputeAndCompareTest(ServiceTestBase):
    def test_verify_version_matches_full_recompute(self):
        self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        self.service.insert("G1", 15.0, "2026-11-12T10:00:00Z", "录入")
        r3 = self.service.insert("G2", 25.0, "2026-11-11T10:00:00Z", "录入")
        self.service.revoke(r3["record_id"], "撤销 G2 唯一记录")
        with self.store.transaction() as conn:
            latest = self.store.latest_summary_version(conn)
            for version in range(1, latest + 1):
                # 最新版本必须与当前数据的全量重算一致
                if version == latest:
                    report = self.service.verify_version(conn, version)
                    self.assertTrue(report["matches_full_recompute"], report)

    def test_compare_versions_reports_deltas(self):
        r1 = self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        self.service.insert("G1", 15.0, "2026-11-11T11:00:00Z", "录入")
        self.service.revoke(r1["record_id"], "撤销")
        with self.store.transaction() as conn:
            diff = self.service.compare_versions(conn, 1, 3)
        g1_all = next(
            r for r in diff["rows"]
            if r["granularity"] == "all" and r["group_name"] == "G1"
        )
        self.assertEqual(g1_all["count_a"], 1)
        self.assertEqual(g1_all["count_b"], 1)

    def test_full_recompute_after_many_revisions(self):
        for i in range(20):
            self.service.insert(
                "G1", float(i), "2026-11-11T10:00:00Z", f"录入{i}"
            )
        # 撤销 5 条、更正 2 条
        for rid in range(1, 6):
            self.service.revoke(rid, f"撤销 {rid}")
        self.service.correct(6, 99.0, "2026-11-11T10:00:00Z", "更正")
        self.service.correct(7, 98.0, "2026-11-11T10:00:00Z", "更正")
        with self.store.transaction() as conn:
            fresh = self.service.full_recompute(conn)
            stored = self.store.get_summary_version(
                conn, self.store.latest_summary_version(conn)
            )
        self.assertEqual(fresh, stored["details"])
        self.assertEqual(fresh["active_count"], 15)

    def test_mismatch_detection(self):
        # 直接构造一个不一致场景，验证双路径互校会拦截
        # （通过临时替换 threshold 使两条路径仍相等，改为手工篡改 details）
        self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE summary_versions SET details_json='{\"active_count\": 999}' "
                "WHERE version=1"
            )
            report = self.service.verify_version(conn, 1)
        self.assertFalse(report["matches_full_recompute"])
        self.assertTrue(report["mismatches"])

    def test_summary_mismatch_error_type_exists(self):
        # 文档化的完整性异常类型
        self.assertTrue(issubclass(SummaryMismatchError, RuntimeError))


class EmptyAndSingleTest(ServiceTestBase):
    def test_empty_group_after_revoke(self):
        r1 = self.service.insert("G1", 5.0, "2026-11-11T10:00:00Z", "录入")
        self.service.revoke(r1["record_id"], "撤销")
        with self.store.transaction() as conn:
            summary = self._latest(conn)
        row = next(
            r for r in summary["details"]["granularities"]["all"]
            if r["group_name"] == "G1"
        )
        self.assertEqual(row["stats"]["count"], 0)
        self.assertTrue(
            all(q["method"] == "empty" for q in row["stats"]["quantiles"])
        )

    def test_single_record_quantiles_all_equal(self):
        self.service.insert("G1", 17.3, "2026-11-11T10:00:00Z", "录入")
        with self.store.transaction() as conn:
            summary = self._latest(conn)
        row = next(
            r for r in summary["details"]["granularities"]["all"]
            if r["group_name"] == "G1"
        )
        self.assertEqual(row["stats"]["count"], 1)
        self.assertTrue(
            all(q["value"] == 17.3 and q["method"] == "exact"
                for q in row["stats"]["quantiles"])
        )


if __name__ == "__main__":
    unittest.main()
