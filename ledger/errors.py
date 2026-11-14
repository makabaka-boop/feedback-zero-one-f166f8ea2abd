"""账本层错误：400 校验失败与 409 版本冲突。"""

from typing import Optional


class LedgerValidationError(Exception):
    """投递消息非法：HTTP 400，带字段路径。"""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message}


class LedgerConflict(Exception):
    """越号或同版异批：HTTP 409。"""

    def __init__(self, message: str, path: Optional[str] = None):
        self.path = path
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        payload = {"message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload
