"""账本 HTTP 入口。

路由
====
* ``POST /ledger``：投递 ``{"v": <int>, "d": [[事实id, 终点id, 差额], ...]}``。
  同版同批回原确认；越号/同版异批 409；非法消息 400（带路径、零基下标）。
  合法批次整笔事务落盘后才回 200，客户端可在落盘后断连并重投，
  幂等保证不重复记账。
* ``GET /query``：``{"v": 最大连续入账版, "balances": [...]}``。
* ``GET /health``：存活探针。

验收钩子：设置环境变量 ``LEDGER_CRASH_AFTER_COMMIT_VERSION=N`` 时，
版本 N 的批次提交落盘后、写 HTTP 响应前进程立即 ``os._exit``，
模拟"远端已落盘、本地确认前强退"。
"""

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List

from energy_alloc.validation import _ID_RE

from .errors import LedgerConflict, LedgerValidationError
from .store import LedgerStore

_LOG = logging.getLogger("ledger")


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_message(payload: Any) -> tuple:
    """校验 ``{v, d}`` 消息，返回 ``(version, batch, batch_json)``。

    与冻结实现一致：布尔不是整数；标识为非空 ASCII 字母数字串；
    差额为非零整数；结构问题给出 JSON 路径与零基下标。
    """
    if not isinstance(payload, dict):
        raise LedgerValidationError("$", "投递体顶层必须为 JSON 对象")
    for key in payload:
        if key not in ("v", "d"):
            raise LedgerValidationError(f"$.{key}", f"未知顶层成员: {key}")
    if "v" not in payload:
        raise LedgerValidationError("$", "缺少必需的顶层成员: v")
    if "d" not in payload:
        raise LedgerValidationError("$", "缺少必需的顶层成员: d")

    version = payload["v"]
    if not _is_plain_int(version) or version < 1:
        raise LedgerValidationError("$.v",
                                    "v 必须为 >=1 的整数（布尔不是整数）")

    raw_batch = payload["d"]
    if not isinstance(raw_batch, list):
        raise LedgerValidationError("$.d", "d 必须为数组")

    batch: List[List[Any]] = []
    for i, entry in enumerate(raw_batch):
        path = f"$.d[{i}]"
        if not isinstance(entry, list):
            raise LedgerValidationError(path, "批次元素必须为 [事实id,终点id,差额] 数组")
        if len(entry) != 3:
            raise LedgerValidationError(path, "批次元素必须恰有 3 个元素")
        fid, end_id, delta = entry
        if not isinstance(fid, str) or _ID_RE.match(fid) is None:
            raise LedgerValidationError(f"{path}[0]",
                                        "事实 id 必须为非空 ASCII 字母数字串")
        if not isinstance(end_id, str) or _ID_RE.match(end_id) is None:
            raise LedgerValidationError(f"{path}[1]",
                                        "终点 id 必须为非空 ASCII 字母数字串")
        if not _is_plain_int(delta):
            raise LedgerValidationError(f"{path}[2]",
                                        "差额必须为整数（布尔不是整数）")
        if delta == 0:
            raise LedgerValidationError(f"{path}[2]", "零差额不得进入批次")
        batch.append([fid, end_id, delta])

    # 规范化载荷用于同版幂等比对（顺序即结算侧已排好的 ASCII 序）
    batch_json = json.dumps(batch, ensure_ascii=False,
                            separators=(",", ":"))
    return version, batch, batch_json


def make_handler(store: LedgerStore) -> type:
    crash_version_raw = os.environ.get("LEDGER_CRASH_AFTER_COMMIT_VERSION")

    class LedgerHandler(BaseHTTPRequestHandler):
        server_version = "Ledger/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            _LOG.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True})
                return
            if self.path == "/query":
                self._send(200, {"v": store.current_version(),
                                 "balances": store.balances_snapshot()})
                return
            self._send(404, {"path": "$", "message": f"未知路径: {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/ledger":
                self._send(404, {"path": "$",
                                 "message": f"未知路径: {self.path}"})
                return
            length_raw = self.headers.get("Content-Length")
            if length_raw is None:
                self._send(400, {"path": "$",
                                 "message": "缺少 Content-Length 请求头"})
                return
            try:
                length = int(length_raw)
            except (TypeError, ValueError):
                self._send(400, {"path": "$",
                                 "message": "Content-Length 必须为整数"})
                return
            raw = self.rfile.read(length) if length >= 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                self._send(400, {"path": "$", "message": "请求体不是合法 UTF-8"})
                return
            except json.JSONDecodeError as exc:
                self._send(400, {"path": "$",
                                 "message": f"JSON 语法错误: {exc.msg}"})
                return

            try:
                version, batch, batch_json = validate_message(payload)
                status, _ = store.post_batch(version, batch, batch_json)
            except LedgerValidationError as exc:
                self._send(400, exc.to_dict())
                return
            except LedgerConflict as exc:
                self._send(409, exc.to_dict())
                return

            # 验收钩子：事务已提交（FULL 同步已落盘），响应前强退
            if (crash_version_raw is not None
                    and str(version) == crash_version_raw):
                _LOG.warning("验收钩子触发：版本 %s 落盘后强退", version)
                os._exit(27)

            self._send(200, {"v": version, "status": status})

    return LedgerHandler


def serve(db_path: str, host: str, port: int) -> ThreadingHTTPServer:
    store = LedgerStore(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    httpd.ledger_store = store  # type: ignore[attr-defined]
    return httpd
