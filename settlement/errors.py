"""结算受理层的统一错误类型。

- :class:`EnvelopeError`：请求非法（400），带 JSON 路径与零基下标，
  语法错误路径为 ``$``；拒绝不产生任何状态变更。
- :class:`ConflictError`：受理冲突（409），同 id 异内容、新 id 版本不符、
  或账本越号/异批。
"""

from typing import Any, Optional


class EnvelopeError(Exception):
    """非法请求：HTTP 400。"""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message}


class ConflictError(Exception):
    """版本/内容冲突：HTTP 409。"""

    def __init__(self, message: str, path: Optional[str] = None):
        self.path = path
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {"message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload
