"""受理核心：信封校验、冻结实现复用、版本状态机与待投批次差分。

受理协议
========
提交体顶层仅允许三个键 ``id`` / ``base`` / ``data``：

* ``id``：沿用原标识规则——非空 ASCII 字母数字串；
* ``base``：非负整数，布尔显式拒绝（``bool`` 是 ``int`` 子类）；
* ``data``：冻结实现（``energy_alloc``）的完整输入对象，按其既有
  规则解析与校验，错误路径沿用冻结实现（语法/重复键为 ``$``）。

状态机（初态 ``v=0 p=0 r=null``）
--------------------------------
1. 同 ``id`` 同 JSON 结构：幂等回放，返回原回执 ``{v}``；
2. 同 ``id`` 异内容：``409``，状态不变；
3. 新 ``id``：``base`` 必须等于当前版本 ``v``，否则 ``409``；
   通过则 ``v += 1``，原子保存原结果、回执与待投批次，返回 ``{v}``。

待投批次（投递消息体 ``{v,d}`` 的 ``d``）
-----------------------------------------
按事实标识、终点标识双重对齐，取**新 B 减旧 B**；缺格作零；
仅保留非零的 ``[事实id, 终点id, 整数差额]``，并按两个标识的 ASCII
序（先事实后终点）排序；空批次保留为 ``[]``。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from energy_alloc.errors import AllocationError
from energy_alloc.validation import _ID_RE, parse_json
from energy_alloc.pipeline import process_text

from .errors import EnvelopeError

_ENVELOPE_KEYS = frozenset(("id", "base", "data"))


def _is_plain_int(value: Any) -> bool:
    """int 且排除布尔（冻结实现同款规则）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def canonical_json(value: Any) -> str:
    """确定性 JSON 串，用于"同 JSON 结构"判定。

    冻结域内只含对象/数组/字符串/整数（无浮点），按键排序、紧凑分隔即可
    得到与输入键序无关的稳定表示。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def parse_envelope(body_text: str) -> Dict[str, Any]:
    """解析提交体并完成信封层校验，返回 ``{"id","base","data"}``。

    信封语法错误（顶层非对象、重复键等）路径为 ``$``；
    字段问题报具体 JSON 路径（``$.id`` / ``$.base`` / ``$.data``）。
    本步骤不触碰冻结实现、不触碰持久层。
    """
    try:
        root = parse_json(body_text)
    except AllocationError as exc:
        # 冻结解析器对语法错与重复键统一报 $，信封层直接沿用
        raise EnvelopeError("$", exc.message) from None

    if not isinstance(root, dict):
        raise EnvelopeError("$", "提交体顶层必须为 JSON 对象")

    for key in root:
        if key not in _ENVELOPE_KEYS:
            raise EnvelopeError(f"$.{key}", f"未知顶层成员: {key}")
    for key in _ENVELOPE_KEYS:
        if key not in root:
            raise EnvelopeError("$", f"缺少必需的顶层成员: {key}")

    req_id, base, data = root["id"], root["base"], root["data"]

    if not isinstance(req_id, str) or _ID_RE.match(req_id) is None:
        raise EnvelopeError("$.id",
                            "id 必须为非空 ASCII 字母数字串")
    if not _is_plain_int(base) or base < 0:
        raise EnvelopeError("$.base",
                            "base 必须为非负整数（布尔不是整数）")
    if not isinstance(data, dict):
        raise EnvelopeError("$.data", "data 必须为 JSON 对象")

    return {"id": req_id, "base": base, "data": data}


def validate_frozen_data(data: Any) -> Dict[str, Any]:
    """按冻结实现校验并计算原结果。

    先将 data 重新序列化为文本交给冻结管线（其校验路径体系与 CLI 完全
    一致），任何 :class:`AllocationError` 原样上抛，由 HTTP 层转 400。
    """
    text = json.dumps(data, ensure_ascii=False)
    return process_text(text)


def _b_index(result: Dict[str, Any]) -> Dict[Tuple[str, str], int]:
    """把冻结结果的 B 矩阵展平为 ``{(事实id, 终点id): 整数格值}``。"""
    cells: Dict[Tuple[str, str], int] = {}
    fact_ids = result["fact_ids"]
    end_ids = result["ends"]
    for i, fid in enumerate(fact_ids):
        row = result["B"][i]
        for j, end in enumerate(end_ids):
            cells[(fid, end)] = row[j]
    return cells


def diff_batch(new_result: Dict[str, Any],
               prev_result: Optional[Dict[str, Any]]) -> List[List[Any]]:
    """计算新 B 相对旧 B 的非零差额批次。

    ``prev_result`` 为 ``None`` 时旧 B 视为全零（首个版本）。
    双重标识对齐、缺格作零、只留非零，结果按 (事实id, 终点id) 的
    ASCII 序排列——Python 字符串比较即码点序，与冻结实现排序一致。
    """
    new_cells = _b_index(new_result)
    old_cells = _b_index(prev_result) if prev_result is not None else {}

    keys = set(new_cells) | set(old_cells)
    batch: List[List[Any]] = []
    for fid, end in sorted(keys):
        delta = new_cells.get((fid, end), 0) - old_cells.get((fid, end), 0)
        if delta != 0:
            batch.append([fid, end, delta])
    return batch


class AcceptOutcome:
    """受理结果：状态码语义 + 版本号 + 落盘内容。"""

    __slots__ = ("version", "result", "receipt", "batch", "replayed")

    def __init__(self, version: int, result: Optional[Dict[str, Any]],
                 receipt: Dict[str, int], batch: Optional[List[List[Any]]],
                 replayed: bool):
        self.version = version
        self.result = result          # 原结果（回放时为 None，无需再落盘）
        self.receipt = receipt        # 回执 {"v": v}
        self.batch = batch            # 待投批次（回放时为 None）
        self.replayed = replayed


def accept(body_text: str, store: "Any") -> AcceptOutcome:
    """受理一次提交，先校验后动状态；冲突/非法均不改变持久状态。

    冻结计算在事务外完成（纯 CPU、无 I/O）；版本判定与落盘由
    ``store.commit_submission`` 在单个 ``BEGIN IMMEDIATE`` 事务内串行
    完成，保证多线程/多连接下并发受理串行等价。
    """
    envelope = parse_envelope(body_text)
    sub_id = envelope["id"]
    base = envelope["base"]
    data = envelope["data"]

    # 1) 冻结实现校验（含全部静态/动态语义），失败直接 400、无状态变更
    result = validate_frozen_data(data)
    data_json = canonical_json(data)
    result_json = canonical_json(result)

    # 2) 单事务：幂等/冲突判定 → base 版本校验 → 批次差分 → 原子落盘
    status, version, batch = store.commit_submission(
        sub_id=sub_id, base=base, data_json=data_json,
        result_json=result_json, result_obj=result)

    if status == "replayed":
        # 同 id 同 JSON 结构：回放原回执
        return AcceptOutcome(version, None, {"v": version}, None,
                             replayed=True)
    return AcceptOutcome(version, result, {"v": version}, batch,
                         replayed=False)
