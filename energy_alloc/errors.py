"""统一错误类型。

非法输入整批拒绝，错误信息为 JSON：
    {"path": "<JSON 路径，语法错为 $>", "message": "<说明>"}
环或出权重和错误按要求报事实下标，例如 ``facts[2]``。
"""

import json
from typing import Any


class AllocationError(Exception):
    """输入非法或处理失败时抛出。"""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(message)

    def to_json(self) -> str:
        return json.dumps({"path": self.path, "message": self.message},
                          ensure_ascii=False)

    @staticmethod
    def raise_(path: str, message: str) -> Any:  # pragma: no cover - 工具方法
        raise AllocationError(path, message)
