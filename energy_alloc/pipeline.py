"""端到端编排：校验 → 积分矩阵 A → 约束取整 B → 原子落盘。

排序统一采用 Python 字符串比较；标识被限制为 ASCII 字母数字串，
其码点序与要求的 ASCII 序一致（数字 < 大写 < 小写）。
"""

import json
import os
import tempfile
from fractions import Fraction
from typing import Any, Dict, List

from .graph import build_matrix_a
from .rounding import round_matrix
from .validation import validate_and_load


def _fraction_to_pq(value: Fraction) -> str:
    """约分后的正分母 p/q 串；零格保留为 0/1。"""
    return f"{value.numerator}/{value.denominator}"


def process_text(text: str) -> Dict[str, Any]:
    """处理一段已读入的 JSON 文本，返回可 json.dumps 的结果字典。"""
    ends, facts, edges = validate_and_load(text)

    fact_order = sorted(range(len(facts)), key=lambda i: facts[i]["id"])
    end_order = sorted(range(len(ends)), key=lambda i: ends[i])

    fact_ids = [facts[i]["id"] for i in fact_order]
    end_ids = [ends[i] for i in end_order]

    if not facts:
        return {"fact_ids": fact_ids, "ends": end_ids, "A": [], "B": []}

    raw_a = build_matrix_a(facts, edges, ends)
    matrix_a: List[List[Fraction]] = [
        [raw_a[fi][ej] for ej in end_order] for fi in fact_order
    ]
    matrix_b = round_matrix(matrix_a)

    a_out = [[_fraction_to_pq(cell) for cell in row] for row in matrix_a]
    return {
        "fact_ids": fact_ids,
        "ends": end_ids,
        "A": a_out,
        "B": matrix_b,
    }


def _atomic_write(path: str, payload: str) -> None:
    """同目录临时文件 + os.replace 原子替换；失败不触碰旧文件。"""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".energy_alloc.", suffix=".tmp",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def process_files(input_path: str, output_path: str) -> Dict[str, Any]:
    """读文件、处理并原子替换输出文件；任何失败都保留旧输出。"""
    with open(input_path, "r", encoding="utf-8") as handle:
        text = handle.read()
    result = process_text(text)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(output_path, payload)
    return result
