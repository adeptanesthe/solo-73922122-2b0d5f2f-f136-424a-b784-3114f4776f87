"""校时核心逻辑单元测试（仅依赖标准库）。"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.solver import Row, filter_viable, parse_local_time, utc_candidates

NY = ZoneInfo("America/New_York")


def mk(idx, seq, local, pinned=None):
    r = Row(id=idx, row_index=idx, seq=seq, local_raw=local, pm25_raw="1.0",
            pinned_utc=pinned)
    r.compute_candidates(NY)
    return r


def utc(hms, day=3, month=11, year=2024):
    return datetime(year, month, day, *map(int, hms.split(":")), tzinfo=timezone.utc)


class CandidatesTest(unittest.TestCase):
    def test_normal_time_unique(self):
        c = utc_candidates(parse_local_time("2024-11-03 00:59:00"), NY)
        self.assertEqual(c, [utc("04:59:00")])

    def test_fallback_overlap_two_candidates(self):
        c = utc_candidates(parse_local_time("2024-11-03 01:30:00"), NY)
        self.assertEqual(c, [utc("05:30:00"), utc("06:30:00")])

    def test_spring_forward_gap_no_candidate(self):
        c = utc_candidates(parse_local_time("2024-03-10 02:30:00"), NY)
        self.assertEqual(c, [])

    def test_bad_format_rejected(self):
        with self.assertRaises(ValueError):
            parse_local_time("2024/11/03 01:00")


class FilterTest(unittest.TestCase):
    def test_constraint_prunes_impossible_candidates(self):
        # 00:59 唯一 -> 04:59 UTC；02:00 唯一 -> 07:00 UTC
        # 01:00 的 fold0=05:00 与 fold1=06:00 都能参与完整解释 -> 都保留
        rows = [mk(0, 1, "2024-11-03 00:59:00"),
                mk(1, 2, "2024-11-03 01:00:00"),
                mk(2, 3, "2024-11-03 02:00:00")]
        viable, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
        self.assertTrue(solvable)
        self.assertEqual(viable[1], [utc("05:00:00"), utc("06:00:00")])

    def test_strictly_increasing_excludes_equal(self):
        # 两个相同本地时刻无法严格递增 -> 无解
        rows = [mk(0, 1, "2024-11-03 00:59:00"),
                mk(1, 2, "2024-11-03 00:59:00")]
        _, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
        self.assertFalse(solvable)

    def test_gap_row_makes_unsolvable(self):
        rows = [mk(0, 1, "2024-03-10 01:59:59"),
                mk(1, 2, "2024-03-10 02:30:00")]
        viable, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
        self.assertFalse(solvable)
        self.assertEqual(viable[1], [])

    def test_pin_propagates_to_neighbors(self):
        # 回拨三行全歧义；pin 中间行 fold0(05:30) 后：
        # 左侧行只剩 fold0(05:00)；右侧行两个候选都 > 05:30 故都保留
        rows = [mk(0, 1, "2024-11-03 01:00:00"),
                mk(1, 2, "2024-11-03 01:30:00", pinned=utc("05:30:00")),
                mk(2, 3, "2024-11-03 01:59:59")]
        viable, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
        self.assertTrue(solvable)
        self.assertEqual(viable[0], [utc("05:00:00")])
        self.assertEqual(viable[2], [utc("05:59:59"), utc("06:59:59")])

    def test_contradictory_pins_unsolvable(self):
        # 两端 pin 互相挤压，中间行无可行候选 -> 无解（用于矛盾选择拒绝）
        rows = [mk(0, 1, "2024-11-03 01:00:00", pinned=utc("06:00:00")),
                mk(1, 2, "2024-11-03 01:30:00"),
                mk(2, 3, "2024-11-03 01:59:59", pinned=utc("05:59:59"))]
        _, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
        self.assertFalse(solvable)

    def test_seq_order_not_file_order(self):
        # 文件中 seq 乱序：约束按序号排序生效
        rows = [mk(0, 5, "2024-11-03 02:00:00"),
                mk(1, 1, "2024-11-03 00:59:00"),
                mk(2, 3, "2024-11-03 01:30:00")]
        viable, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
        self.assertTrue(solvable)
        self.assertEqual(len(viable[0]), 1)  # seq1 唯一
        self.assertEqual(len(viable[1]), 2)  # seq3 回拨歧义保留两个候选
        self.assertEqual(len(viable[2]), 1)  # seq5 唯一


if __name__ == "__main__":
    unittest.main()
