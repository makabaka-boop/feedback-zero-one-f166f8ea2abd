"""时间维可达子图校验与精确有理数积分。

每条事实的区间 ``[a,b)`` 按当时有效边集的变化时刻切段；每段内：

1. 从事实起点沿有效边做可达性展开；
2. 可达子图须无环（Kahn 拓扑排序判定）；
3. 可达子图内每个非终点节点的出权重和恰为 1；
4. 拓扑序上做 DP：``frac[v] = sum frac[u] * w(u,v)``，
   起点份额为 1，多路径同终点自然相加。

段长占事实总时长的比例乘以段内份额即该段归属比例，
各段累加后乘以 n 得到精确有理数矩阵 A 的一行。
环或权重和错误一律报对应事实下标。
"""

from collections import deque
from fractions import Fraction
from typing import Dict, List, Tuple

from .errors import AllocationError


def _boundaries(fact: dict, edges: List[dict]) -> List[int]:
    """收集事实区间内有效边集发生变化的全部时刻。"""
    a, b = fact["a"], fact["b"]
    points = {a, b}
    for edge in edges:
        if edge["a"] < b and edge["b"] > a:  # 与 [a,b) 有交集
            if a < edge["a"] < b:
                points.add(edge["a"])
            if a < edge["b"] < b:
                points.add(edge["b"])
    return sorted(points)


def _reachable(source: str, active: List[dict]) -> set:
    """沿有效边 BFS 求从 source 可达的节点集合。"""
    adj: Dict[str, List[dict]] = {}
    for edge in active:
        adj.setdefault(edge["src"], []).append(edge)
    seen = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for edge in adj.get(node, ()):
            if edge["dst"] not in seen:
                seen.add(edge["dst"])
                queue.append(edge["dst"])
    return seen


def _check_and_fractions(source: str, active: List[dict],
                         end_set: set) -> Dict[str, Fraction]:
    """校验单个时段的可达子图并返回各节点的份额 DP 结果。"""
    reach = _reachable(source, active)

    # 仅保留可达子图内部的边；平行边以邻接表形式共存
    adj: Dict[str, List[dict]] = {node: [] for node in reach}
    indeg: Dict[str, int] = {node: 0 for node in reach}
    out_weight: Dict[str, Fraction] = {node: Fraction(0) for node in reach}
    for edge in active:
        if edge["src"] in reach and edge["dst"] in reach:
            adj[edge["src"]].append(edge)
            indeg[edge["dst"]] += 1
            out_weight[edge["src"]] += edge["w"]

    # 非终点出权重和必须为 1
    for node in reach:
        if node not in end_set and out_weight[node] != 1:
            raise AllocationError(
                "__FACT__",
                f"节点 {node} 的有效出权重和为 {out_weight[node]}，要求为 1")

    # Kahn 拓扑排序：处理不完即存在环
    queue = deque(node for node in reach if indeg[node] == 0)
    topo: List[str] = []
    while queue:
        node = queue.popleft()
        topo.append(node)
        for edge in adj[node]:
            indeg[edge["dst"]] -= 1
            if indeg[edge["dst"]] == 0:
                queue.append(edge["dst"])
    if len(topo) != len(reach):
        raise AllocationError("__FACT__",
                              f"从 {source} 可达的有效子图存在环")

    # 拓扑序 DP：路径权重连乘、多路径相加
    frac: Dict[str, Fraction] = {node: Fraction(0) for node in reach}
    frac[source] = Fraction(1)
    for node in topo:
        share = frac[node]
        for edge in adj[node]:
            frac[edge["dst"]] += share * edge["w"]
    return frac


def build_matrix_a(facts: List[dict], edges: List[dict],
                   ends: List[str]) -> List[List[Fraction]]:
    """计算事实×终点的精确归属矩阵 A（行序同 facts 传入顺序）。"""
    end_set = set(ends)
    matrix: List[List[Fraction]] = []

    for fi, fact in enumerate(facts):
        a, b = fact["a"], fact["b"]
        duration = b - a
        # 各终点在整条事实区间上的平均归属比例（尚未乘 n）
        ratio: Dict[str, Fraction] = {end: Fraction(0) for end in ends}
        marks = _boundaries(fact, edges)

        for left, right in zip(marks, marks[1:]):
            active = [e for e in edges
                      if e["a"] <= left and e["b"] >= right]
            try:
                frac = _check_and_fractions(fact["node"], active, end_set)
            except AllocationError as exc:
                # 统一把动态语义错误挂到事实下标
                raise AllocationError(
                    f"facts[{fi}]", exc.message) from None
            seg_len = right - left
            for end in ends:
                if end in frac:
                    ratio[end] += (Fraction(seg_len, duration)
                                   * frac[end])

        n = fact["n"]
        matrix.append([n * ratio[end] for end in ends])
    return matrix
