"""账本的 SQLite 持久层：版本幂等、顺序控制、整批累加与期间结账。

库表
====
``meta``     单行：已入账的最大连续版本 ``v``、当前开放期间 ``period``
             （初态 0 / 1）。
``batches``  每个已确认版本的整批载荷（幂等重放时逐字节比对）。
``balances`` ``(事实id, 终点id) -> 累计整数差额``。
``periods``  已结账期间：期间号 → ``cut`` 与冻结的期间差额合计 ``d``。

批次规则
========
* 同版同批：返回原确认（幂等），余额不再变动；
* 同版异批、越号（``v != 当前 v + 1``）：``409``，拒绝不改状态；
* 合法批次在单事务内整批累加余额并推进版本，提交后即可安全断连。

结账规则
========
``POST /close {period, cut}`` 先校验，再在单事务内：

1. 该期间已存在：同 ``cut`` 回原确认（幂等重放），异 ``cut`` → ``409``；
2. 新期间必须恰为当前开放期间，且 ``cut`` 等于当前入账版本 ``v``，
   否则 ``409``；
3. 通过则**原子冻结**该期间差额合计并换期：期间版本区间为
   ``(上一期间 cut, cut]``，把其中各批次差额按 ``(事实id, 终点id)``
   汇总、删零、按标识 ASCII 序排列后写入 ``periods``，同时把
   ``meta.period`` 加一。空期间冻结为 ``[]``。

期间差额直接由已落盘的批次载荷重算，不另设累加器：结账事务只读批次、
只写期间表，天然与"旧批重试沿用原规则"兼容，崩溃/重放都不会重复冻结。

每次事务使用独立短连接，配合 ``synchronous=FULL`` + WAL，保证提交
返回时已落盘，且并发连接总能读到最新已提交版本。
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .errors import LedgerConflict

_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS periods (
    period INTEGER PRIMARY KEY,
    cut    INTEGER NOT NULL,
    d_json TEXT NOT NULL
);
"""


class LedgerStore:
    def __init__(self, db_path: str, timeout: float = 30.0):
        self.db_path = db_path
        self.timeout = timeout
        init = self._connect()
        try:
            init.executescript(_SCHEMA)
            # 旧库迁移：meta 增加开放期间列（默认 1），行为对未启用
            # 结账的旧集群完全不变。并发启动时可能彼此抢先 ALTER，
            # 重复列错误视为已迁移。
            columns = {row[1] for row in
                       init.execute("PRAGMA table_info(meta)")}
            if "period" not in columns:
                try:
                    init.execute("ALTER TABLE meta ADD COLUMN period "
                                 "INTEGER NOT NULL DEFAULT 1")
                except sqlite3.OperationalError:
                    pass
            if init.execute("SELECT 1 FROM meta WHERE id=1").fetchone() is None:
                init.execute(
                    "INSERT INTO meta (id, v, period) VALUES (1, 0, 1)")
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

    def current_period(self) -> int:
        """当前开放期间（已结账最大期间 + 1，初态 1）。"""
        conn = self._connect()
        try:
            return int(conn.execute(
                "SELECT period FROM meta WHERE id=1").fetchone()[0])
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

    def periods_snapshot(self) -> List[Dict[str, Any]]:
        """已结账期间，按期间升序；每项 ``{period, cut, d}``。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT period, cut, d_json FROM periods "
                "ORDER BY period").fetchall()
        finally:
            conn.close()
        return [{"period": int(p), "cut": int(c), "d": json.loads(d)}
                for p, c, d in rows]

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

    def close_period(self, period: int, cut: int
                     ) -> Tuple[str, int, int, Optional[List[List[Any]]]]:
        """单事务结账：校验 → 冻结期间差额合计 → 换期。

        返回 ``("confirmed"|"replayed", period, cut, d)``（重放时 ``d``
        为 ``None``）。非法期间/``cut`` 抛 :class:`LedgerConflict`，
        回滚不改状态。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_v, current_period = conn.execute(
                "SELECT v, period FROM meta WHERE id=1").fetchone()
            current_v, current_period = int(current_v), int(current_period)

            row = conn.execute(
                "SELECT cut FROM periods WHERE period=?",
                (period,)).fetchone()
            if row is not None:
                # 同期间同 cut：回原确认；异 cut：409（不改状态）
                if int(row[0]) != cut:
                    raise LedgerConflict(
                        f"期间 {period} 已以 cut={row[0]} 结账，"
                        f"收到 cut={cut}", path="$.cut")
                conn.execute("COMMIT")
                return "replayed", period, cut, None

            if period != current_period:
                raise LedgerConflict(
                    f"结账期间不符：当前开放期间 {current_period}，"
                    f"收到 period={period}", path="$.period")
            if cut != current_v:
                raise LedgerConflict(
                    f"cut 必须等于当前入账版本 v={current_v}，"
                    f"收到 cut={cut}", path="$.cut")

            prev_cut = int(conn.execute(
                "SELECT MAX(cut) FROM periods WHERE period < ?",
                (period,)).fetchone()[0] or 0)
            totals: Dict[Tuple[str, str], int] = {}
            rows = conn.execute(
                "SELECT batch_json FROM batches "
                "WHERE version > ? AND version <= ? ORDER BY version",
                (prev_cut, cut)).fetchall()
            for (batch_json,) in rows:
                for fid, end_id, delta in json.loads(batch_json):
                    key = (fid, end_id)
                    totals[key] = totals.get(key, 0) + int(delta)
            d = [[fid, end_id, totals[(fid, end_id)]]
                 for fid, end_id in sorted(totals)
                 if totals[(fid, end_id)] != 0]
            d_json = json.dumps(d, ensure_ascii=False, separators=(",", ":"))

            conn.execute(
                "INSERT INTO periods (period, cut, d_json) VALUES (?, ?, ?)",
                (period, cut, d_json))
            conn.execute("UPDATE meta SET period=? WHERE id=1",
                         (period + 1,))
            conn.execute("COMMIT")
            return "confirmed", period, cut, d
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
