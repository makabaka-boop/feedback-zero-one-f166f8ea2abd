"""账本的 SQLite 持久层：版本幂等、顺序控制与整批累加。

库表
====
``meta``     单行：已入账的最大连续版本 ``v``（初态 0）。
``batches``  每个已确认版本的整批载荷（幂等重放时逐字节比对）。
``balances`` ``(事实id, 终点id) -> 累计整数差额``。

规则
====
* 同版同批：返回原确认（幂等），余额不再变动；
* 同版异批、越号（``v != 当前 v + 1``）：``409``，拒绝不改状态；
* 合法批次在单事务内整批累加余额并推进版本，提交后即可安全断连。

每次事务使用独立短连接，配合 ``synchronous=FULL`` + WAL，保证提交
返回时已落盘，且并发连接总能读到最新已提交版本。
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .errors import LedgerConflict


class LedgerStore:
    def __init__(self, db_path: str, timeout: float = 30.0):
        self.db_path = db_path
        self.timeout = timeout
        init = self._connect()
        try:
            init.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                v  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                version    INTEGER PRIMARY KEY,
                batch_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS balances (
                fact_id TEXT NOT NULL,
                end_id  TEXT NOT NULL,
                amount  INTEGER NOT NULL,
                PRIMARY KEY (fact_id, end_id)
            );
            """)
            if init.execute("SELECT 1 FROM meta WHERE id=1").fetchone() is None:
                init.execute("INSERT INTO meta (id, v) VALUES (1, 0)")
        finally:
            init.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout,
                               isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=%d" % int(self.timeout * 1000))
        return conn

    def close(self) -> None:
        return None

    def current_version(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute(
                "SELECT v FROM meta WHERE id=1").fetchone()[0])
        finally:
            conn.close()

    def balances_snapshot(self) -> List[Dict[str, Any]]:
        """按 (事实id, 终点id) ASCII 序返回非零累计余额。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT fact_id, end_id, amount FROM balances "
                "WHERE amount <> 0 ORDER BY fact_id, end_id").fetchall()
        finally:
            conn.close()
        return [{"fact_id": f, "end_id": e, "amount": a}
                for f, e, a in rows]

    def post_batch(self, version: int, batch: List[List[Any]],
                   batch_json: Optional[str] = None
                   ) -> Tuple[str, int]:
        """单事务处理一版投递。

        返回 ``("confirmed", v)`` 或 ``("replayed", v)``；
        越号/异批抛 :class:`LedgerConflict`，回滚不改状态。
        """
        if batch_json is None:
            batch_json = json.dumps(batch, ensure_ascii=False,
                                    separators=(",", ":"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = int(conn.execute(
                "SELECT v FROM meta WHERE id=1").fetchone()[0])

            # 幂等重放：同版同批回原确认，异批 409
            old = conn.execute(
                "SELECT batch_json FROM batches WHERE version=?",
                (version,)).fetchone()
            if old is not None:
                if old[0] != batch_json:
                    raise LedgerConflict(
                        f"版本 {version} 已存在不同批次", path="$.d")
                conn.execute("COMMIT")
                return "replayed", version

            # 新批次必须恰好是下一个连续版本
            if version != current + 1:
                raise LedgerConflict(
                    f"越号投递：期望版本 {current + 1}，收到 {version}",
                    path="$.v")

            # 整批累加（缺格作零由 UPSERT 自然处理）
            for entry in batch:
                fid, end_id, delta = entry
                conn.execute(
                    "INSERT INTO balances (fact_id, end_id, amount) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(fact_id, end_id) DO UPDATE SET "
                    "amount = amount + excluded.amount",
                    (fid, end_id, delta))
            conn.execute(
                "INSERT INTO batches (version, batch_json) VALUES (?, ?)",
                (version, batch_json))
            conn.execute("UPDATE meta SET v=? WHERE id=1", (version,))
            conn.execute("COMMIT")
            return "confirmed", version
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
