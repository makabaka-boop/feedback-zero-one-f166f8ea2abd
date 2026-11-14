"""结算服务 HTTP 入口。

路由
====
* ``POST /submit``：受理 ``{id, base, data}``，成功 200 ``{"v": v}``；
  非法 400（带路径/零基下标，语法错 ``$``）；冲突 409；
* ``GET  /query``：原子返回 ``{"v", "p", "r"}``，
  ``r`` 为最新受理版的原结果（初态 ``null``）；
* ``GET  /health``：存活探针。

线程模型：:class:`ThreadingHTTPServer` 每请求一线程；持久层用单连接 +
``BEGIN IMMEDIATE`` 串行化写事务，因此并发受理串行等价。
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from energy_alloc.errors import AllocationError

from .core import accept
from .errors import ConflictError, EnvelopeError
from .store import SettlementStore

_LOG = logging.getLogger("settlement")


def make_handler(store: SettlementStore) -> type:
    """构造绑定了持久层的 handler 类。"""

    class SettlementHandler(BaseHTTPRequestHandler):
        server_version = "Settlement/1.0"

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

        def do_GET(self) -> None:  # noqa: N802 - http.server 命名
            if self.path == "/health":
                self._send(200, {"ok": True})
                return
            if self.path == "/query":
                v, p, r = store.current_state()
                self._send(200, {"v": v, "p": p, "r": r})
                return
            self._send(404, {"path": "$", "message": f"未知路径: {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/submit":
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
            if length < 0:
                self._send(400, {"path": "$", "message": "非法 Content-Length"})
                return
            body_text = self.rfile.read(length).decode("utf-8", "replace")

            try:
                outcome = accept(body_text, store)
            except EnvelopeError as exc:
                self._send(400, exc.to_dict())
                return
            except AllocationError as exc:
                # 冻结实现校验失败：路径与 CLI 版完全一致，拒绝不改状态
                self._send(400, {"path": exc.path, "message": exc.message})
                return
            except ConflictError as exc:
                self._send(409, exc.to_dict())
                return
            self._send(200, outcome.receipt)

    return SettlementHandler


def serve(db_path: str, host: str, port: int) -> ThreadingHTTPServer:
    """创建并返回服务对象（调用方负责 ``serve_forever``/``shutdown``）。"""
    store = SettlementStore(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    httpd.settlement_store = store  # type: ignore[attr-defined]
    return httpd
