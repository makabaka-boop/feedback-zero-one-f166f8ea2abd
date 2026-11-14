"""约束取整：自写双目标最小费用流，不依赖第三方库。

模型（每个事实一行，共 M 行、K 列）
-----------------------------------
设 ``A_ij = floor(A_ij) + f_ij``，取整格 ``B_ij = floor(A_ij) + x_ij``，
``x_ij ∈ {0,1}``。则

* 行和：``Σ_j x_ij = T_i``，其中 ``T_i = Σ_j f_ij``（行总量为整数 n，
  故 ``T_i`` 恒为整数）；
* 列和：``L_j ≤ Σ_i x_ij ≤ U_j``，``L_j = floor(S_j)``、
  ``U_j = ceil(S_j)``，``S_j = Σ_i f_ij``；
* 目标一：最小化逐格绝对误差。逐格有 ``|x-f| = f + x(1-2f)``，
  选 1 总数 N 固定，故等价于最小化 ``Σ x_ij·(1-f_ij)``（有理数）；
* 目标二（平局）：在目标一最优解中取行展开字典序最小者。

带下界运输流的超源/超汇变换
---------------------------
列→SNK 边拆成"下界预流（残量容量 0，计入节点平衡）+ 容量 U_j-L_j 的
可选边"；SRC→行的强制供应同样计入平衡；叠加 SNK→SRC 回流边使整体
成为循环；由节点盈亏连超源 SS / 超汇 ZZ，对 SS→ZZ 求满流即同时
满足全部行供应与列下界，列上界由可选边容量严格限制。

双目标费用（避免大整数分数）
----------------------------
每条行→列边携带一对费用 ``(p, q)``：

* 主费用 ``p = 1-f_ij``（:class:`Fraction`），表达绝对误差目标；
* 次费用 ``q = big^(C-rank)``（整数），rank 是行展开序号、C 是待决策
  格数、big=C+1。指数权重保证任一靠前格的 q 大于所有更靠后格 q 之和，
  因而最小化 ``Σq`` 等价于行展开字典序最小。

最短距离按元组 ``(Σp, Σq)`` 的字典序比较：先压主费用，主费用相同再
压次费用，一次最小费用流同时得到两个目标的最优解。主、次费用各维护
一套 Johnson 势函数，约化距离也是元组，Dijkstra 用 heapq 按元组弹出。
全程主费用是普通分数（无放大因子），次费用是整数，运算量小、可扩展。
"""

import heapq
from fractions import Fraction
from typing import List, Optional, Tuple

# 距离/费用元组：(主费用 Fraction, 次费用 int)
Cost = Tuple[Fraction, int]


class _Edge:
    __slots__ = ("to", "rev", "cap", "init_cap", "pcost", "qcost")

    def __init__(self, to: int, rev: int, cap: int,
                 pcost: Fraction, qcost: int):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.init_cap = cap
        self.pcost = pcost
        self.qcost = qcost


def _add_edge(graph: List[List[_Edge]], u: int, v: int, cap: int,
              pcost: Fraction, qcost: int = 0) -> None:
    graph[u].append(_Edge(v, len(graph[v]), cap, pcost, qcost))
    graph[v].append(_Edge(u, len(graph[u]) - 1, 0, -pcost, -qcost))


def _min_cost_flow(graph: List[List[_Edge]], s: int, t: int,
                   need: int) -> Optional[Cost]:
    """双目标带势逐最短路增广，推送恰好 need 单位；不可行返回 None。"""
    n_nodes = len(graph)
    pot_p = [Fraction(0)] * n_nodes
    pot_q = [0] * n_nodes
    total_p = Fraction(0)
    total_q = 0
    pushed = 0

    while pushed < need:
        dist_p: List[Optional[Fraction]] = [None] * n_nodes
        dist_q: List[Optional[int]] = [None] * n_nodes
        prev_v = [-1] * n_nodes
        prev_e = [-1] * n_nodes
        dist_p[s] = Fraction(0)
        dist_q[s] = 0
        # 堆元素 (主费用, 次费用, 节点)；元组天然字典序比较
        heap: List[Tuple[Fraction, int, int]] = [(Fraction(0), 0, s)]
        while heap:
            dp, dq, u = heapq.heappop(heap)
            if dp != dist_p[u] or dq != dist_q[u]:
                continue
            for k, edge in enumerate(graph[u]):
                if edge.cap <= 0:
                    continue
                rp = edge.pcost + pot_p[u] - pot_p[edge.to]
                rq = edge.qcost + pot_q[u] - pot_q[edge.to]
                np_ = dp + rp
                nq = dq + rq
                cur_p = dist_p[edge.to]
                if (cur_p is None
                        or (np_, nq) < (cur_p, dist_q[edge.to])):
                    dist_p[edge.to] = np_
                    dist_q[edge.to] = nq
                    prev_v[edge.to] = u
                    prev_e[edge.to] = k
                    heapq.heappush(heap, (np_, nq, edge.to))
        if dist_p[t] is None:
            return None

        for v in range(n_nodes):
            if dist_p[v] is not None:
                pot_p[v] += dist_p[v]
                pot_q[v] += dist_q[v]

        add = need - pushed
        v = t
        while v != s:
            add = min(add, graph[prev_v[v]][prev_e[v]].cap)
            v = prev_v[v]
        v = t
        while v != s:
            edge = graph[prev_v[v]][prev_e[v]]
            edge.cap -= add
            graph[v][edge.rev].cap += add
            total_p += add * edge.pcost
            total_q += add * edge.qcost
            v = prev_v[v]
        pushed += add
    return total_p, total_q


def round_matrix(matrix_a: List[List[Fraction]]) -> List[List[int]]:
    """给定精确矩阵 A，求满足全部行/列约束的最优整数矩阵 B。"""
    m = len(matrix_a)
    if m == 0:
        return []
    k = len(matrix_a[0])

    floors: List[List[int]] = []
    fracs: List[List[Fraction]] = []
    for row in matrix_a:
        f_row, r_row = [], []
        for value in row:
            base = value.numerator // value.denominator
            f_row.append(base)
            r_row.append(value - Fraction(base, 1))
        floors.append(f_row)
        fracs.append(r_row)

    # 行供应量 T_i
    row_supply: List[int] = []
    for i in range(m):
        supply = sum(fracs[i], Fraction(0))
        assert supply.denominator == 1, "行小数部分之和应为整数"
        row_supply.append(int(supply))

    # 列上下界
    col_lower, col_upper = [], []
    for j in range(k):
        col_sum = sum((fracs[i][j] for i in range(m)), Fraction(0))
        low = col_sum.numerator // col_sum.denominator
        col_lower.append(low)
        col_upper.append(low + (0 if col_sum.denominator == 1 else 1))

    total_units = sum(row_supply)
    if total_units == 0:
        return [row[:] for row in floors]

    assert sum(col_lower) <= total_units <= sum(col_upper), \
        "列上下界与行供应总量不一致，取整不可行"

    # 待决策格按行展开编号
    cell_rank: List[List[Optional[int]]] = [
        [None] * k for _ in range(m)]
    rank = 0
    for i in range(m):
        for j in range(k):
            if fracs[i][j] > 0:
                cell_rank[i][j] = rank
                rank += 1
    decision_cells = rank
    big = decision_cells + 1

    def q_weight(r: int) -> int:
        # 靠前（rank 小）权重更大；big 进制保证字典序支配
        return big ** (decision_cells - r)

    # 节点：行 0..m-1，列 m..m+k-1，SRC、SNK、SS、ZZ
    src = m + k
    snk = m + k + 1
    ss = m + k + 2
    zz = m + k + 3
    n_nodes = m + k + 4
    graph: List[List[_Edge]] = [[] for _ in range(n_nodes)]
    balance = [0] * n_nodes
    cell_edges: List[Tuple[int, int]] = []

    # 行 → 列：容量 1，主费用 1-f，次费用字典序权重
    for i in range(m):
        for j in range(k):
            r = cell_rank[i][j]
            if r is not None:
                idx = len(graph[i])
                _add_edge(graph, i, m + j, 1,
                          Fraction(1) - fracs[i][j], q_weight(r))
                cell_edges.append((i, idx))

    # SRC → 行：强制供应，残量容量 0，计入平衡
    for i in range(m):
        if row_supply[i] > 0:
            _add_edge(graph, src, i, 0, Fraction(0), 0)
            balance[src] -= row_supply[i]
            balance[i] += row_supply[i]

    # 列 → SNK：下界预流（容量 0）+ 可选边（容量 U-L）
    for j in range(k):
        low = col_lower[j]
        extra = col_upper[j] - low
        if low > 0:
            _add_edge(graph, m + j, snk, 0, Fraction(0), 0)
            balance[m + j] -= low
            balance[snk] += low
        if extra > 0:
            _add_edge(graph, m + j, snk, extra, Fraction(0), 0)

    # 回流边
    _add_edge(graph, snk, src, total_units, Fraction(0), 0)

    need = 0
    for v in range(n_nodes):
        if balance[v] > 0:
            _add_edge(graph, ss, v, balance[v], Fraction(0), 0)
            need += balance[v]
        elif balance[v] < 0:
            _add_edge(graph, v, zz, -balance[v], Fraction(0), 0)

    assert _min_cost_flow(graph, ss, zz, need) is not None, \
        "约束取整无可行流，输入约束异常"

    choose = [[0] * k for _ in range(m)]
    for i in range(m):
        for j in range(k):
            r = cell_rank[i][j]
            if r is None:
                continue
            u, eidx = cell_edges[r]
            if graph[u][eidx].cap == 0:
                choose[i][j] = 1

    for j in range(k):
        col_total = sum(choose[i][j] for i in range(m))
        assert col_lower[j] <= col_total <= col_upper[j], \
            f"列 {j} 取整结果 {col_total} 越界 [{col_lower[j]},{col_upper[j]}]"
    for i in range(m):
        assert sum(choose[i]) == row_supply[i], f"行 {i} 和守恒被破坏"

    return [[floors[i][j] + choose[i][j] for j in range(k)]
            for i in range(m)]
