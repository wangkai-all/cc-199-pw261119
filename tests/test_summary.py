"""贡献台账、周期切分、空/单条表现与增量/全量等价性测试。"""

import unittest

from app.buckets import BucketScheme
from app.summary import (
    ContributionLedger,
    build_summary,
    build_summary_incremental,
    period_key,
)

SCHEME = BucketScheme(
    scheme_id=1,
    version=1,
    name="t",
    edges=(0.0, 10.0, 20.0, 30.0),
    created_at="now",
    note=None,
)
GROUPS = [
    {"group_id": 1, "name": "G1", "created_at": "now"},
    {"group_id": 2, "name": "G2", "created_at": "now"},
]


def rec(record_id, group_id, value, when, status="active"):
    return {
        "record_id": record_id,
        "group_id": group_id,
        "value": float(value),
        "measured_at": when,
        "status": status,
        "note": None,
    }


class LedgerTest(unittest.TestCase):
    def test_add_remove_multiset(self):
        ledger = ContributionLedger()
        for _ in range(3):
            ledger.add(5.0)
        ledger.add(8.0)
        ledger.remove(5.0)  # 重复值只移除一个
        self.assertEqual(len(ledger), 3)
        self.assertEqual(sorted(ledger.values), [5.0, 5.0, 8.0])

    def test_remove_missing_raises(self):
        with self.assertRaises(ValueError):
            ContributionLedger().remove(1.0)

    def test_snapshot_counts_and_out_of_range(self):
        ledger = ContributionLedger()
        for value in (-1.0, 0.0, 9.0, 10.0, 29.0, 30.0):
            ledger.add(value)
        snap = ledger.snapshot(SCHEME)
        self.assertEqual(snap["count"], 6)
        self.assertEqual(snap["below_count"], 1)       # -1
        self.assertEqual(snap["above_count"], 1)       # 30 命中末边
        # [0,10): 0,9；[10,20): 10；[20,30): 29
        self.assertEqual([b["count"] for b in snap["bins"]], [2, 1, 1])
        self.assertEqual(snap["min"], -1.0)
        self.assertEqual(snap["max"], 30.0)

    def test_empty_snapshot_shape(self):
        snap = ContributionLedger().snapshot(SCHEME)
        self.assertEqual(snap["count"], 0)
        self.assertIsNone(snap["mean"])
        self.assertTrue(all(q["method"] == "empty" for q in snap["quantiles"]))
        self.assertTrue(all(b["count"] == 0 for b in snap["bins"]))
        self.assertIn("空", snap["quantile_note"])

    def test_single_record_snapshot(self):
        ledger = ContributionLedger()
        ledger.add(15.0)
        snap = ledger.snapshot(SCHEME)
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["mean"], 15.0)
        self.assertEqual(snap["min"], 15.0)
        self.assertEqual(snap["max"], 15.0)
        for q in snap["quantiles"]:
            self.assertEqual(q["method"], "exact")
            self.assertEqual(q["value"], 15.0)
            self.assertEqual(q["max_abs_error"], 0.0)

    def test_large_sample_uses_approximation_with_error(self):
        ledger = ContributionLedger()
        for i in range(1001):
            ledger.add(float(i % 30))
        snap = ledger.snapshot(SCHEME, approx_threshold=1000)
        p99 = next(q for q in snap["quantiles"] if q["p"] == 0.99)
        self.assertEqual(p99["method"], "interpolated")
        self.assertIsNotNone(p99["max_abs_error"])
        self.assertEqual(p99["max_abs_error"], 10.0)  # 桶宽即最坏误差界


class PeriodKeyTest(unittest.TestCase):
    def test_granularities(self):
        when = "2026-11-11T10:00:00Z"
        self.assertEqual(period_key(when, "all"), "all")
        self.assertEqual(period_key(when, "day"), "2026-11-11")
        self.assertEqual(period_key(when, "month"), "2026-11")
        self.assertEqual(period_key(when, "week"), "2026-W46")

    def test_unknown_granularity(self):
        with self.assertRaises(ValueError):
            period_key("2026-11-11T10:00:00Z", "hour")


class SummaryBuildTest(unittest.TestCase):
    def test_empty_database_lists_every_group(self):
        detail = build_summary([], GROUPS, SCHEME)
        self.assertEqual(detail["active_count"], 0)
        for granularity in ("all", "day", "week", "month"):
            rows = detail["granularities"][granularity]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["stats"]["count"] == 0 for r in rows))

    def test_group_with_all_records_revoked_shows_empty_row(self):
        records = [rec(1, 1, 5.0, "2026-11-11T10:00:00Z", status="revoked")]
        detail = build_summary(records, GROUPS, SCHEME)
        self.assertEqual(detail["active_count"], 0)
        all_rows = detail["granularities"]["all"]
        g1 = next(r for r in all_rows if r["group_id"] == 1)
        self.assertEqual(g1["stats"]["count"], 0)

    def test_period_partitioning(self):
        records = [
            rec(1, 1, 5.0, "2026-11-11T10:00:00Z"),
            rec(2, 1, 15.0, "2026-11-12T10:00:00Z"),
            rec(3, 2, 25.0, "2026-11-11T10:00:00Z"),
        ]
        detail = build_summary(records, GROUPS, SCHEME)
        day_rows = detail["granularities"]["day"]
        self.assertEqual(
            sorted((r["group_id"], r["period"], r["stats"]["count"]) for r in day_rows),
            [(1, "2026-11-11", 1), (1, "2026-11-12", 1), (2, "2026-11-11", 1)],
        )
        all_rows = detail["granularities"]["all"]
        self.assertEqual(
            {r["group_id"]: r["stats"]["count"] for r in all_rows}, {1: 2, 2: 1}
        )


class IncrementalEquivalenceTest(unittest.TestCase):
    def _records(self):
        return [
            rec(1, 1, 5.0, "2026-11-11T10:00:00Z"),
            rec(2, 1, 5.0, "2026-11-11T11:00:00Z"),  # 重复值
            rec(3, 1, 25.0, "2026-11-12T10:00:00Z"),
            rec(4, 2, 15.0, "2026-11-11T10:00:00Z"),
        ]

    def test_insert_equivalent(self):
        before = self._records()
        new = rec(5, 2, 7.0, "2026-11-13T10:00:00Z")
        incremental = build_summary_incremental(
            before, GROUPS, SCHEME, [("insert", None, new)]
        )
        full = build_summary(before + [new], GROUPS, SCHEME)
        self.assertEqual(incremental, full)

    def test_correct_removes_old_contribution(self):
        before = self._records()
        old = before[0]
        new = {**old, "value": 22.0, "record_id": 6}
        incremental = build_summary_incremental(
            before, GROUPS, SCHEME, [("correct", old, new)]
        )
        expected_records = [r for r in before if r["record_id"] != old["record_id"]] + [new]
        full = build_summary(expected_records, GROUPS, SCHEME)
        self.assertEqual(incremental, full)

        # 旧桶计数必须回落：5.0 原本两个，更正后只剩一个
        g1_all = next(
            r for r in incremental["granularities"]["all"] if r["group_id"] == 1
        )
        self.assertEqual(g1_all["stats"]["bins"][0]["count"], 1)  # [0,10)
        self.assertEqual(g1_all["stats"]["bins"][2]["count"], 2)  # [20,30)

    def test_revoke_duplicate_value_removes_one(self):
        before = self._records()
        old = before[0]  # 两个 5.0 之一
        incremental = build_summary_incremental(
            before, GROUPS, SCHEME, [("revoke", old, None)]
        )
        expected = [r for r in before if r["record_id"] != old["record_id"]]
        full = build_summary(expected, GROUPS, SCHEME)
        self.assertEqual(incremental, full)
        g1_all = next(
            r for r in incremental["granularities"]["all"] if r["group_id"] == 1
        )
        self.assertEqual(g1_all["stats"]["bins"][0]["count"], 1)

    def test_revoke_to_empty_then_reinsert(self):
        only = [rec(1, 1, 5.0, "2026-11-11T10:00:00Z")]
        revoked = build_summary_incremental(
            only, GROUPS, SCHEME, [("revoke", only[0], None)]
        )
        g1 = next(r for r in revoked["granularities"]["all"] if r["group_id"] == 1)
        self.assertEqual(g1["stats"]["count"], 0)
        # 空 -> 重新插入
        new = rec(2, 1, 9.0, "2026-11-11T12:00:00Z")
        refilled = build_summary_incremental(
            [], GROUPS, SCHEME, [("insert", None, new)]
        )
        self.assertEqual(
            build_summary([new], GROUPS, SCHEME), refilled
        )


if __name__ == "__main__":
    unittest.main()
