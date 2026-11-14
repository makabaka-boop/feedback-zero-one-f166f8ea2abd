"""UTF-8 JSON 语法解析与结构校验。

只允许三个顶层键 ``ends`` / ``facts`` / ``edges``，顺序不限；
顶层重复键按非法处理（路径 ``$``）。所有结构、类型、标识、权重、
成员重复/未知问题在此一次性拦截，错误带 JSON 路径与零基下标。
"""

import json
import re
from fractions import Fraction
from typing import Any, Dict, List, Tuple

from .errors import AllocationError

# 非空 ASCII 字母数字串
_ID_RE = re.compile(r"^[0-9A-Za-z]+$")
# 十进制正整数 p/q 串：允许 "p/q"（p、q 均为正整数十进制写法），
# 也接受不带分母的正整数（等价 p/1）；禁符号、空白、前导 +、布尔。
_WEIGHT_RE = re.compile(r"^(0|[1-9][0-9]*)/(0|[1-9][0-9]*)$")
_INT_WEIGHT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_TOP_KEYS = ("ends", "facts", "edges")
FACT_LEN = 5
EDGE_LEN = 6


class _DuplicateKey(ValueError):
    """object_pairs_hook 检测到对象内重复键。"""


def _hook(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise _DuplicateKey(str(key))
        seen.add(key)
    return dict(pairs)


def parse_json(text: str) -> Any:
    """解析 JSON 文本；语法错误（含重复键）统一报路径 ``$``。"""
    try:
        return json.loads(text, object_pairs_hook=_hook)
    except _DuplicateKey as exc:
        raise AllocationError("$", f"重复的 JSON 键: {exc}") from None
    except json.JSONDecodeError as exc:
        raise AllocationError("$", f"JSON 语法错误: {exc.msg}") from None


def _is_int(value: Any) -> bool:
    """int 类型且排除布尔（bool 是 int 子类，须显式拒绝）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.match(value) is not None


def parse_weight(raw: Any, path: str) -> Fraction:
    """解析边权重；仅接受正整数或正整数之比的字符串。"""
    if not isinstance(raw, str):
        raise AllocationError(path, "权重 w 必须为十进制正整数 p/q 字符串")
    m = _WEIGHT_RE.match(raw)
    if m:
        p_s, q_s = m.group(1), m.group(2)
        if p_s == "0" or q_s == "0":
            raise AllocationError(path, f"权重必须为正数: {raw}")
        return Fraction(int(p_s), int(q_s))
    m = _INT_WEIGHT_RE.match(raw)
    if m:
        if m.group(1) == "0":
            raise AllocationError(path, f"权重必须为正数: {raw}")
        return Fraction(int(m.group(1)), 1)
    raise AllocationError(path, f"权重不是合法的正整数 p/q 串: {raw}")


def validate_and_load(text: str) -> Tuple[List[str], List[dict], List[dict]]:
    """解析并完成全部静态结构校验，返回 (ends, facts, edges)。

    facts/edges 元素转为 dict，边权重转为 :class:`Fraction`，
    其余字段保持原始类型；动态语义（环、权重和）在 graph 模块校验。
    """
    root = parse_json(text)
    if not isinstance(root, dict):
        raise AllocationError("$", "顶层必须为 JSON 对象")

    for key in root:
        if key not in _TOP_KEYS:
            raise AllocationError(f"$.{key}", f"未知顶层成员: {key}")
    for key in _TOP_KEYS:
        if key not in root:
            raise AllocationError("$", f"缺少必需的顶层成员: {key}")

    ends_raw, facts_raw, edges_raw = root["ends"], root["facts"], root["edges"]
    if not isinstance(ends_raw, list):
        raise AllocationError("$.ends", "ends 必须为数组")
    if not isinstance(facts_raw, list):
        raise AllocationError("$.facts", "facts 必须为数组")
    if not isinstance(edges_raw, list):
        raise AllocationError("$.edges", "edges 必须为数组")

    # ---- ends：互异、合法标识 ----
    ends: List[str] = []
    seen_ends = set()
    for i, item in enumerate(ends_raw):
        path = f"ends[{i}]"
        if not _is_identifier(item):
            raise AllocationError(path, "终点标识必须为非空 ASCII 字母数字串")
        if item in seen_ends:
            raise AllocationError(path, f"终点标识重复: {item}")
        seen_ends.add(item)
        ends.append(item)
    end_set = set(ends)

    # ---- facts：[id, node, a, b, n] ----
    facts: List[dict] = []
    seen_fact_ids = set()
    for i, item in enumerate(facts_raw):
        path = f"facts[{i}]"
        if not isinstance(item, list):
            raise AllocationError(path, "事实必须为 [id,node,a,b,n] 数组")
        if len(item) != FACT_LEN:
            raise AllocationError(path, f"事实必须恰有 {FACT_LEN} 个元素")
        fid, node, a, b, n = item
        if not _is_identifier(fid):
            raise AllocationError(f"{path}[0]", "事实 id 必须为非空 ASCII 字母数字串")
        if fid in seen_fact_ids:
            raise AllocationError(f"{path}[0]", f"事实 id 重复: {fid}")
        seen_fact_ids.add(fid)
        if not _is_identifier(node):
            raise AllocationError(f"{path}[1]", "事实 node 必须为非空 ASCII 字母数字串")
        if not _is_int(a) or a < 0:
            raise AllocationError(f"{path}[2]", "a 必须为非负 UTC 整数秒")
        if not _is_int(b) or b < 0:
            raise AllocationError(f"{path}[3]", "b 必须为非负 UTC 整数秒")
        if not a < b:
            raise AllocationError(path, f"要求 a < b，得到 a={a}, b={b}")
        if not _is_int(n) or n < 0:
            raise AllocationError(f"{path}[4]", "n 必须为非负整数微瓦时")
        facts.append({"id": fid, "node": node, "a": a, "b": b, "n": n})

    # ---- edges：[id, from, to, a, b, w]，允许平行边 ----
    edges: List[dict] = []
    seen_edge_ids = set()
    for i, item in enumerate(edges_raw):
        path = f"edges[{i}]"
        if not isinstance(item, list):
            raise AllocationError(path, "边必须为 [id,from,to,a,b,w] 数组")
        if len(item) != EDGE_LEN:
            raise AllocationError(path, f"边必须恰有 {EDGE_LEN} 个元素")
        eid, src, dst, a, b, w = item
        if not _is_identifier(eid):
            raise AllocationError(f"{path}[0]", "边 id 必须为非空 ASCII 字母数字串")
        if eid in seen_edge_ids:
            raise AllocationError(f"{path}[0]", f"边 id 重复: {eid}")
        seen_edge_ids.add(eid)
        if not _is_identifier(src):
            raise AllocationError(f"{path}[1]", "边 from 必须为非空 ASCII 字母数字串")
        if not _is_identifier(dst):
            raise AllocationError(f"{path}[2]", "边 to 必须为非空 ASCII 字母数字串")
        if not _is_int(a) or a < 0:
            raise AllocationError(f"{path}[3]", "a 必须为非负 UTC 整数秒")
        if not _is_int(b) or b < 0:
            raise AllocationError(f"{path}[4]", "b 必须为非负 UTC 整数秒")
        if not a < b:
            raise AllocationError(path, f"要求 a < b，得到 a={a}, b={b}")
        weight = parse_weight(w, f"{path}[5]")
        edges.append({
            "id": eid, "src": src, "dst": dst,
            "a": a, "b": b, "w": weight,
        })

    # 终点禁出边（全局静态约束，与时间无关）
    for i, edge in enumerate(edges):
        if edge["src"] in end_set:
            raise AllocationError(f"edges[{i}][1]",
                                  f"终点 {edge['src']} 禁止拥有出边")
    return ends, facts, edges
