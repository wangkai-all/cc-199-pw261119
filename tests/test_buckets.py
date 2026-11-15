"""桶边界、版本冻结与边界值归属测试。"""

import unittest

from app.buckets import BucketScheme, validate_edges


def make_scheme(edges=(0.0, 10.0, 20.0)):
    return BucketScheme(
        scheme_id=1, version=1, name="t", edges=tuple(edges),
        created_at="now", note=None,
    )


class BucketAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.scheme = make_scheme()

    def test_lower_bound_inclusive(self):
        # 左闭：恰好等于边界归入右侧桶
        self.assertEqual(self.scheme.assign(0.0), 0)
        self.assertEqual(self.scheme.assign(10.0), 1)

    def test_upper_bound_exclusive(self):
        # 右开：末边及以上进入桶外上界
        self.assertEqual(self.scheme.assign(19.999), 1)
        self.assertEqual(self.scheme.assign(20.0), 3)  # len(edges)

    def test_out_of_range(self):
        self.assertEqual(self.scheme.assign(-0.001), -1)
        self.assertEqual(self.scheme.assign(1e9), 3)

    def test_labels(self):
        self.assertIn("<", self.scheme.bin_label(-1))
        self.assertIn(">=", self.scheme.bin_label(3))
        self.assertEqual(self.scheme.bin_label(0), "[0, 10)")

    def test_validate_edges(self):
        with self.assertRaises(ValueError):
            validate_edges([1.0])
        with self.assertRaises(ValueError):
            validate_edges([1.0, 1.0])
        with self.assertRaises(ValueError):
            validate_edges([2.0, 1.0])
        with self.assertRaises(ValueError):
            validate_edges([1.0, float("nan")])
        with self.assertRaises(ValueError):
            validate_edges([1.0, float("inf")])
        self.assertEqual(validate_edges([1, 2, 3]), [1.0, 2.0, 3.0])

    def test_scheme_is_frozen(self):
        # dataclass frozen：边界不可原地修改
        with self.assertRaises(Exception):
            self.scheme.edges = (1.0, 2.0)  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
