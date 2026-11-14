"""命令行入口：``python -m energy_alloc <input.json> <output.json>``。

成功：原子替换输出文件，退出码 0。
失败：向 stderr 打印单个错误 JSON，退出码 1，旧输出文件原样保留。
"""

import json
import sys
from typing import List, Optional

from .errors import AllocationError
from .pipeline import process_files


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write(
            json.dumps(
                {"path": "$",
                 "message": "用法: python -m energy_alloc <输入JSON> <输出JSON>"},
                ensure_ascii=False) + "\n")
        return 1
    input_path, output_path = args
    try:
        process_files(input_path, output_path)
    except AllocationError as exc:
        sys.stderr.write(exc.to_json() + "\n")
        return 1
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(json.dumps(
            {"path": "$", "message": f"文件读写失败: {exc}"},
            ensure_ascii=False) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
