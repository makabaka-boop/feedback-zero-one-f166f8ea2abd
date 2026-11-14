"""端到端集成测试：手算分摊、排序、原子写入、失败保留旧文件。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from energy_alloc.pipeline import process_text  # noqa: E402
from energy_alloc.errors import AllocationError  # noqa: E402

DATA = os.path.join(ROOT, "tests", "data")


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as handle:
        return handle.read()


class HandComputedTest(unittest.TestCase):
    def test_spec_example(self):
        out = process_text(_load("spec_example.json"))
        self.assertEqual(out["fact_ids"], ["f1"])
        self.assertEqual(out["ends"], ["X", "Y"])
        self.assertEqual(out["A"], [["1/2", "1/2"]])
        self.assertEqual(out["B"], [[0, 1]])

    def test_time_switch_attribution(self):
        # [0,2) 各 1/2；[2,4) X=3/4 Y=1/4；n=12，时长 4
        # A_X = 12 * (2/4*1/2 + 2/4*3/4) = 12*5/8 = 15/2
        # A_Y = 12 * (2/4*1/2 + 2/4*1/4) = 12*3/8 = 9/2
        out = process_text(_load("time_switch.json"))
        self.assertEqual(out["A"], [["15/2", "9/2"]])
        self.assertEqual(out["B"], [[7, 5]])
        self.assertEqual(sum(out["B"][0]), 12)

    def test_multipath_merging(self):
        # s->u 1/3, s->v 2/3；u->X 1/2；v->X 1/4
        # A_X = 12 * (1/3*1/2 + 2/3*1/4) = 12*1/3 = 4
        # A_Y = 12 * (1/3*1/2 + 2/3*3/4) = 12*2/3 = 8
        out = process_text(_load("multipath.json"))
        self.assertEqual(out["A"], [["4/1", "8/1"]])
        self.assertEqual(out["B"], [[4, 8]])

    def test_pseudo_cycle_across_periods(self):
        # [0,3) s->t->X 全量；[3,6) t->s 反向但 s->X 直走
        # 两个时段从 s 都无环且全部到达 X，A_X=10
        out = process_text(_load("pseudo_cycle.json"))
        self.assertEqual(out["A"], [["10/1"]])
        self.assertEqual(out["B"], [[10]])

    def test_source_is_end(self):
        out = process_text(_load("source_is_end.json"))
        self.assertEqual(out["A"], [["7/1", "0/1"]])
        self.assertEqual(out["B"], [[7, 0]])

    def test_zero_n(self):
        out = process_text(_load("zero_n.json"))
        self.assertEqual(out["A"], [["0/1", "0/1"]])
        self.assertEqual(out["B"], [[0, 0]])

    def test_empty_facts(self):
        out = process_text(_load("empty_facts.json"))
        self.assertEqual(out["A"], [])
        self.assertEqual(out["B"], [])

    def test_parallel_edges(self):
        # 两条平行边各 1/4 汇合到 X，合计 1/2；Y 占 1/2；n=8
        out = process_text(_load("parallel_edges.json"))
        self.assertEqual(out["A"], [["4/1", "4/1"]])
        self.assertEqual(out["B"], [[4, 4]])

    def test_edges_outside_fact_window(self):
        # e1/e2 完全在事实 [10,20) 外，被忽略；
        # [10,15) 走 X、[15,20) 走 Y，各占一半，n=10
        out = process_text(_load("edge_outside.json"))
        self.assertEqual(out["A"], [["5/1", "5/1"]])
        self.assertEqual(out["B"], [[5, 5]])

    def test_ascii_ordering_rows_and_cols(self):
        payload = {
            "ends": ["b", "A", "1"],
            "facts": [
                ["z", "s", 0, 2, 2],
                ["a", "s", 0, 2, 2],
            ],
            "edges": [
                ["e1", "s", "b", 0, 2, "1/3"],
                ["e2", "s", "A", 0, 2, "1/3"],
                ["e3", "s", "1", 0, 2, "1/3"],
            ],
        }
        out = process_text(json.dumps(payload))
        # ASCII 序：'1'(49) < 'A'(65) < 'b'(98)；事实 a < z
        self.assertEqual(out["ends"], ["1", "A", "b"])
        self.assertEqual(out["fact_ids"], ["a", "z"])
        for row in out["A"]:
            self.assertEqual(row, ["2/3", "2/3", "2/3"])
        # 每行 2 个单位分到 3 列，列和约束下字典序最小
        for row in out["B"]:
            self.assertEqual(sum(row), 2)
        # 列总量均为 4/3（下界 1、上界 2），字典序最小的最优解
        self.assertEqual(out["B"], [[0, 1, 1], [1, 0, 1]])


class ValidationRejectTest(unittest.TestCase):
    def _expect_path(self, name, path):
        with self.assertRaises(AllocationError) as ctx:
            process_text(_load(name))
        self.assertEqual(ctx.exception.path, path)

    def test_rejected_cases(self):
        cases = {
            "bad_syntax.json": "$",
            "bad_dup_key.json": "$",
            "bad_dup_end.json": "ends[1]",
            "bad_dup_fact.json": "facts[1][0]",
            "bad_n.json": "facts[0][4]",
            "bad_interval.json": "facts[0]",
            "bad_bool.json": "facts[0][2]",
            "bad_id.json": "facts[0][1]",
            "bad_end_out.json": "edges[0][1]",
            "bad_weight_zero.json": "edges[0][5]",
            "bad_weight_decimal.json": "edges[0][5]",
            "bad_cycle.json": "facts[0]",
            "bad_cycle_only.json": "facts[0]",
            "bad_weight_sum.json": "facts[0]",
            "bad_fact_len.json": "facts[0]",
            "bad_unknown_key.json": "$.extra",
        }
        for name, path in cases.items():
            with self.subTest(name=name):
                self._expect_path(name, path)


class AtomicWriteAndCliTest(unittest.TestCase):
    def _run_cli(self, in_path, out_path):
        return subprocess.run(
            [sys.executable, "-I",
             os.path.join(ROOT, "run_allocation.py"), in_path, out_path],
            capture_output=True, text=True, cwd=ROOT)

    def test_success_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "in.json")
            out_path = os.path.join(tmp, "out.json")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write(_load("spec_example.json"))
            proc = self._run_cli(in_path, out_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out_path, encoding="utf-8") as handle:
                result = json.load(handle)
            self.assertEqual(result["B"], [[0, 1]])

    def test_failure_keeps_old_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.json")
            bad = os.path.join(tmp, "bad.json")
            out_path = os.path.join(tmp, "out.json")
            with open(good, "w", encoding="utf-8") as handle:
                handle.write(_load("spec_example.json"))
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write(_load("bad_cycle_only.json"))

            ok = self._run_cli(good, out_path)
            self.assertEqual(ok.returncode, 0)
            with open(out_path, encoding="utf-8") as handle:
                before = handle.read()

            fail = self._run_cli(bad, out_path)
            self.assertEqual(fail.returncode, 1)
            err = json.loads(fail.stderr.strip())
            self.assertEqual(err["path"], "facts[0]")

            with open(out_path, encoding="utf-8") as handle:
                after = handle.read()
            self.assertEqual(before, after, "失败后旧输出文件被改动")

    def test_module_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "in.json")
            out_path = os.path.join(tmp, "out.json")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write(_load("empty_facts.json"))
            # 包在仓库根目录下，普通模式可从 cwd 定位；
            # 隔离模式(-I)请使用 run_allocation.py 入口（脚本自行定位包）。
            proc = subprocess.run(
                [sys.executable, "-m", "energy_alloc",
                 in_path, out_path],
                capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
