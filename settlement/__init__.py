"""能耗修订结算服务（纯标准库实现）。

模块划分
--------
- core:       受理校验（冻结实现复用）、版本状态机、待投批次差分
- store:      SQLite 持久化（版本/回执/待投批次/水位，原子落盘）
- http_server: HTTP 入口（POST /submit、GET /query）
- errors:     受理层错误（路径 + 消息）与 HTTP 状态码
"""

from .errors import EnvelopeError, ConflictError

__all__ = ["EnvelopeError", "ConflictError"]
