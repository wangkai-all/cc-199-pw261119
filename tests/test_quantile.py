"""分位数定义与近似误差口径测试。"""

import math
import unittest

from app.quantile import (
    QUANTILE_DEFINITION,
    quantile_exact,
    quantile_from_bins,
    quantile_report,
)


class ExactQuantileTest(unittest.TestCase):
    def test_type7_known_values(self):
        # numpy.quantile 的 type-7 基准值
        data = sorted([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(quantile_exact(data, 0.0), 1.0)
        self.assertAlmostEqual(quantile_exact(data, 0.5), 2.5)
        self.assertAlmostEqual(quantile_exact(data, 1.0), 4.0)
        self.assertAlmostEqual(quantile_exact(data, 0.25), 1.75)

    def test_single_value(self):
        # 单条记录：任何分位都是它本身
        self.assertEqual(quantile_exact([42.5], 0.5), 42.5)
        self.assertEqual(quantile_exact([42.5], 0.99), 42.5)

    def test_two_values_interpolation(self):
        self.assertAlmostEqual(quantile_exact([0.0, 10.0], 0.9), 9.0)

    def test_empty_and_bad_prob(self):
        with self.assertRaises(ValueError):
            quantile_exact([], 0.5)
        with self.assertRaises(ValueError):
            quantile_exact([1.0], 1.5)

    def test_definition_documented(self):
        self.assertIn("type 7", QUANTILE_DEFINITION)


class BinApproxTest(unittest.TestCase):
    EDGES = [0.0, 10.0, 20.0, 30.0]

    def test_interpolation_midpoint_and_error_bound(self):
        # 10 个值：2 个桶外下界，3 个在 [0,10)、2 个在 [10,20)、3 个在 [20,30)
        # P50 目标位置 5 -> 落入 [10,20)
        result = quantile_from_bins(
            0.5, total_count=10, below_count=2,
            bin_counts=[3, 2, 3], edges=self.EDGES,
        )
        self.assertEqual(result["method"], "interpolated")
        # 目标在桶内位置 (5-5)/2 -> 桶左边界 + 0*宽度
        self.assertAlmostEqual(result["value"], 10.0)
        self.assertEqual(result["max_abs_error"], 10.0)  # 误差界 = 桶宽

    def test_below_side_unbounded(self):
        # 10 个值中 5 个在桶外下界；P10 目标位置 1 -> 桶外下界
        result = quantile_from_bins(
            0.1, total_count=10, below_count=5, bin_counts=[5], edges=self.EDGES
        )
        self.assertEqual(result["method"], "unbounded")
        self.assertEqual(result["side"], "below")
        self.assertIsNone(result["value"])
        self.assertIsNone(result["max_abs_error"])

    def test_above_side_unbounded(self):
        # 5 个值中只有 3 个在桶内，另 2 个在桶外上界；P90 目标位置 4.5，
        # 越过桶内值上界 -> 桶外上界，无有限误差界
        result = quantile_from_bins(
            0.9, total_count=5, below_count=0, bin_counts=[3], edges=self.EDGES
        )
        self.assertEqual(result["method"], "unbounded")
        self.assertEqual(result["side"], "above")

    def test_p100_above_unbounded(self):
        # 最大值分位 P100：目标位置恰为 n，超过最后一个桶的累计计数
        result = quantile_from_bins(
            1.0, total_count=5, below_count=0, bin_counts=[5], edges=self.EDGES
        )
        self.assertEqual(result["method"], "unbounded")
        self.assertEqual(result["side"], "above")

    def test_error_bound_holds_against_exact(self):
        # 任意分布下近似与精确值之差不超过桶宽
        values = [1.0, 11.0, 12.0, 21.0, 22.0, 23.0, 29.0]
        edges = [0.0, 10.0, 20.0, 30.0]
        bin_counts = [1, 2, 4]
        for p in (0.5, 0.9, 0.95):
            approx = quantile_from_bins(
                p, total_count=len(values), below_count=0,
                bin_counts=bin_counts, edges=edges,
            )
            exact = quantile_exact(sorted(values), p)
            if approx["method"] == "interpolated":
                self.assertLessEqual(
                    abs(approx["value"] - exact),
                    approx["max_abs_error"] + 1e-12,
                )

    def test_report_switches_by_threshold(self):
        small = quantile_report([1.0, 2.0], 0.5, 0, [2], [0.0, 10.0], 1000)
        self.assertEqual(small["method"], "exact")
        self.assertEqual(small["max_abs_error"], 0.0)

        big_values = [float(i) for i in range(1, 2001)]
        edges = [0.0, 500.0, 1000.0, 2000.0]
        bin_counts = [499, 500, 1000]
        big = quantile_report(big_values, 0.9, 0, bin_counts, edges, 1000)
        self.assertEqual(big["method"], "interpolated")
        self.assertTrue(math.isfinite(big["max_abs_error"]))
        self.assertGreater(big["max_abs_error"], 0)


if __name__ == "__main__":
    unittest.main()
