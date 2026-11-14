"""取整模块的小矩阵全枚举/随机枚举验收。

对随机生成的小数部分矩阵 F（行小数和为整数 T_i），用 0/1 组合
暴力枚举全部满足行/列约束的 B，独立计算最优目标值与行展开字典序
最小的最优解，与 :func:`energy_alloc.rounding.round_matrix` 比对。

枚举规模小（约 2**(M*K)），随机大量重复以覆盖：
换归属边界、列下界强制、多路径汇合带来的小数、平局字典序。
"""

import itertools
import random
import unittest
from fractions import Fraction

from energy_alloc.rounding import round_matrix


def _brute_force(fracs):
    """暴力枚举所有 0/1 矩阵，返回 (最优B, 最小绝对误差和)。"""
    m = len(fracs)
    k = len(fracs[0])
    row_need = [int(sum(row, Fraction(0))) for row in fracs]
    col_sum = [sum((fracs[i][j] for i in range(m)), Fraction(0))
               for j in range(k)]
    col_low = [c.numerator // c.denominator for c in col_sum]
    col_high = [lo + (0 if col_sum[j].denominator == 1 else 1)
                for j, lo in enumerate(col_low)]

    best_feasible = []
    best_err = None
    for bits in itertools.product((0, 1), repeat=m * k):
        x = [list(bits[r * k:(r + 1) * k]) for r in range(m)]
        if [sum(row) for row in x] != row_need:
            continue
        col_tot = [sum(x[i][j] for i in range(m)) for j in range(k)]
        if any(col_tot[j] < col_low[j] or col_tot[j] > col_high[j]
               for j in range(k)):
            continue
        err = Fraction(0)
        for i in range(m):
            for j in range(k):
                err += abs(x[i][j] - fracs[i][j])
        if best_err is None or err < best_err:
            best_err = err
            best_feasible = [x]
        elif err == best_err:
            best_feasible.append(x)
    assert best_feasible, "枚举未找到可行解，测试构造有误"
    # 行展开（嵌套表字典序）最小者为平局规则要求的解
    best_b = min(best_feasible)
    return best_b, best_err


def _random_fracs(rng, m, k, max_den=6):
    """生成行小数和为整数的随机真分数 Fraction 矩阵（值在 [0,1)）。

    每行先随机取 k-1 个真分数，最后一格取为使行和为整数的补足值，
    保证补足值落在 [0,1)；否则重抽该行（单行命中率高，不会久等）。
    """
    fracs = []
    for _ in range(m):
        if k == 1:
            fracs.append([Fraction(0)])  # 单格行和须为整数，真分数只能取 0
            continue
        while True:
            row = [Fraction(rng.randrange(0, max_den),
                            rng.randrange(1, max_den + 1))
                   for _ in range(k - 1)]
            row = [f - Fraction(f.numerator // f.denominator, 1) for f in row]
            partial = sum(row, Fraction(0))
            base = partial.numerator // partial.denominator
            last = Fraction(base + 1, 1) - partial  # 使总和 = base+1
            if 0 <= last < 1:
                fracs.append(row + [last])
                break
    return fracs


class RoundingEnumerationTest(unittest.TestCase):
    def test_spec_example_tie(self):
        # 题面示例：两行 1/2,1/2，平局要求 [[0,1],[1,0]]
        f = [[Fraction(1, 2), Fraction(1, 2)],
             [Fraction(1, 2), Fraction(1, 2)]]
        self.assertEqual(round_matrix(f),
                         [[0, 1], [1, 0]])

    def test_exhaustive_2x2_fixed_grids(self):
        # 对若干精心构造的 2x2 网格直接全枚举
        grids = [
            [[Fraction(1, 2), Fraction(1, 2)],
             [Fraction(1, 2), Fraction(1, 2)]],
            [[Fraction(1, 3), Fraction(2, 3)],
             [Fraction(3, 4), Fraction(1, 4)]],
            [[Fraction(1, 4), Fraction(3, 4)],
             [Fraction(2, 3), Fraction(1, 3)]],
            [[Fraction(1, 6), Fraction(5, 6)],
             [Fraction(5, 6), Fraction(1, 6)]],
        ]
        for fracs in grids:
            with self.subTest(fracs=fracs):
                expect_b, expect_err = _brute_force(fracs)
                got = round_matrix(fracs)
                self.assertEqual(got, expect_b)
                got_err = sum(abs(Fraction(got[i][j]) - fracs[i][j])
                              for i in range(2) for j in range(2))
                self.assertEqual(got_err, expect_err)

    def test_random_enumeration_2x3_and_3x2(self):
        rng = random.Random(20261114)
        for m, k, trials in ((2, 3, 120), (3, 2, 120), (2, 2, 80),
                             (1, 4, 40), (4, 1, 40)):
            for _ in range(trials):
                fracs = _random_fracs(rng, m, k)
                expect_b, expect_err = _brute_force(fracs)
                got = round_matrix(fracs)
                self.assertEqual(got, expect_b,
                                 msg=f"fracs={fracs}")
                got_err = sum(abs(Fraction(got[i][j]) - fracs[i][j])
                              for i in range(m) for j in range(k))
                self.assertEqual(got_err, expect_err)

    def test_larger_random_feasibility_and_constraints(self):
        # 更大规模不做暴力枚举，只校验所有硬约束与目标非劣性质
        rng = random.Random(777)
        for _ in range(60):
            m, k = rng.randrange(2, 6), rng.randrange(2, 6)
            fracs = _random_fracs(rng, m, k, max_den=10)
            got = round_matrix(fracs)

            row_need = [int(sum(row, Fraction(0))) for row in fracs]
            self.assertEqual([sum(row) for row in got], row_need)
            col_sum = [sum((fracs[i][j] for i in range(m)), Fraction(0))
                       for j in range(k)]
            for j, cs in enumerate(col_sum):
                col_total = sum(got[i][j] for i in range(m))
                self.assertGreaterEqual(
                    col_total, cs.numerator // cs.denominator)
                self.assertLessEqual(
                    col_total,
                    cs.numerator // cs.denominator
                    + (0 if cs.denominator == 1 else 1))
            # 逐格只能取 0 或 1
            self.assertTrue(all(v in (0, 1) for row in got for v in row))
            # 零小数格必为 0
            for i in range(m):
                for j in range(k):
                    if fracs[i][j] == 0:
                        self.assertEqual(got[i][j], 0)

    def test_empty(self):
        self.assertEqual(round_matrix([]), [])

    def test_scale_constraints_and_speed(self):
        # 较大规模只校验硬约束守恒与可接受耗时，不做暴力枚举
        import time
        rng = random.Random(99)
        m, k = 40, 8
        fracs = _random_fracs(rng, m, k, max_den=12)
        t0 = time.time()
        got = round_matrix(fracs)
        self.assertLess(time.time() - t0, 5.0)

        row_need = [int(sum(row, Fraction(0))) for row in fracs]
        self.assertEqual([sum(row) for row in got], row_need)
        for j in range(k):
            cs = sum((fracs[i][j] for i in range(m)), Fraction(0))
            lo = cs.numerator // cs.denominator
            hi = lo + (0 if cs.denominator == 1 else 1)
            col_total = sum(got[i][j] for i in range(m))
            self.assertTrue(lo <= col_total <= hi)
            self.assertTrue(all(v in (0, 1) for row in got for v in row))


if __name__ == "__main__":
    unittest.main(verbosity=2)
