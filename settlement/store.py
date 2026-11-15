"""结算服务的 SQLite 持久层。

库表
====
``meta``        单行：当前受理版 ``v``、连续确认水位 ``p``、
                最新受理版原结果 ``r``（初态 0/0/NULL）。
``submissions`` 受理幂等表：信封 id → 版本、规范化 data、原结果、回执。
``batches``     版本账：每版的待投批次 ``d`` 与投递确认标记。
``inflight``    在途租约：多进程投递时同一版本的排他认领。
``closes``      结账登记：期间号 → 结账 id、cut（结账时的 ``v``）、投递标记。
``close_inflight`` 结账投递的在途租约。

期间（period）与结账（close）
============================
期间从 1 递增。结账在受理事务串行语义下把当前 ``v`` 固定为 ``cut``，
登记后开启下一期间；期间内版本区间为 ``(上一 cut, cut]``。允许连续
多次结账（空期间、相同 cut）。结账不阻塞提交；同结账 id 重试回原
回执、不改变原期间。

并发与崩溃
==========
* WAL + ``synchronous=FULL``，写事务用 ``BEGIN IMMEDIATE`` 立即取得
  保留锁，多线程/多进程的并发受理在事务层串行化，语义等价于串行受理；
* 每次事务使用独立短连接：投递进程与结算服务是不同进程，短连接保证
  总能读到已提交的最新水位（避免 WAL 长连接快照过期），天然适配
  多进程按序重试；
* 受理的"原结果、回执、待投批次、版本推进、r 更新"在同一事务提交，
  要么全部可见、要么全部不可见；
* 投递确认先逐版落盘再重算连续前缀水位，崩溃后重放不重复记账；
* 仅依赖标准库 ``sqlite3``。
"""

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .core import canonical_json, diff_batch
from .errors import ConflictError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    v  INTEGER NOT NULL,
    p  INTEGER NOT NULL,
    r  TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
    sub_id      TEXT PRIMARY KEY,
    version     INTEGER NOT NULL UNIQUE,
    data_json   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    version    INTEGER PRIMARY KEY,
    batch_json TEXT NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inflight (
    version    INTEGER PRIMARY KEY,
    leased_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS closes (
    close_id      TEXT PRIMARY KEY,
    period        INTEGER NOT NULL UNIQUE,
    cut           INTEGER NOT NULL,
    delivered     INTEGER NOT NULL DEFAULT 0,
    receipt_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS close_inflight (
    period     INTEGER PRIMARY KEY,
    leased_at  REAL NOT NULL
);
"""


class SettlementStore:
    """单个 SQLite 文件上的结算状态；事务级短连接，跨进程安全。"""

    def __init__(self, db_path: str, timeout: float = 30.0):
        self.db_path = db_path
        self.timeout = timeout
        init = self._connect()
        try:
            init.executescript(_SCHEMA)
            if init.execute("SELECT 1 FROM meta WHERE id=1").fetchone() is None:
                init.execute(
                    "INSERT INTO meta (id, v, p, r) VALUES (1, 0, 0, NULL)")
        finally:
            init.close()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None：手工 BEGIN/COMMIT；WAL 下短连接的读
        # 始终取语句开始时的最新已提交快照。
        conn = sqlite3.connect(self.db_path, timeout=self.timeout,
                               isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=%d" % int(self.timeout * 1000))
        return conn

    def close(self) -> None:
        # 短连接实现下无常驻资源；保留方法以兼容服务关闭流程
        return None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def current_state(self) -> Tuple[int, int, Optional[Dict[str, Any]]]:
        """原子返回 ``(v, p, r)``；``r`` 为最新受理版的原结果对象。"""
        conn = self._connect()
        try:
            v, p, r_text = conn.execute(
                "SELECT v, p, r FROM meta WHERE id=1").fetchone()
        finally:
            conn.close()
        r = json.loads(r_text) if r_text is not None else None
        return v, p, r

    def get_submission(self, sub_id: str
                       ) -> Optional[Tuple[int, str]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT version, data_json FROM submissions "
                "WHERE sub_id=?", (sub_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else (row[0], row[1])

    def get_batch(self, version: int) -> Optional[List[List[Any]]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT batch_json FROM batches WHERE version=?",
                (version,)).fetchone()
        finally:
            conn.close()
        return None if row is None else json.loads(row[0])

    # ------------------------------------------------------------------
    # 结账（与受理同库串行：cut 固定为事务内看到的 v）
    # ------------------------------------------------------------------
    def commit_close(self, close_id: str) -> Tuple[str, int, int]:
        """在单个立即事务内登记一次结账。

        返回 ``(status, period, cut)``：``status`` 为 ``"replayed"``（同
        id 回原回执，不换期）或 ``"accepted"``（新登记，期间 = 已有最大
        期间 + 1，cut = 事务内当前 ``v``）。期间从 1 递增，允许空期间与
        相同 cut。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT period, cut FROM closes WHERE close_id=?",
                (close_id,)).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                return "replayed", int(row[0]), int(row[1])

            v = int(conn.execute(
                "SELECT v FROM meta WHERE id=1").fetchone()[0])
            last = conn.execute(
                "SELECT MAX(period) FROM closes").fetchone()[0]
            period = (int(last) if last is not None else 0) + 1
            receipt_json = canonical_json({"period": period, "cut": v})
            conn.execute(
                "INSERT INTO closes (close_id, period, cut, delivered, "
                "receipt_json) VALUES (?, ?, ?, 0, ?)",
                (close_id, period, v, receipt_json))
            conn.execute("COMMIT")
            return "accepted", period, v
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 结账投递（先投完本期批次，再投结账，最后才进入下一期）
    # ------------------------------------------------------------------
    def next_pending_close(self) -> Optional[Tuple[int, int]]:
        """最低的未确认结账 ``(period, cut)``；无则 ``None``。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT period, cut FROM closes WHERE delivered=0 "
                "ORDER BY period LIMIT 1").fetchone()
        finally:
            conn.close()
        return None if row is None else (int(row[0]), int(row[1]))

    def claim_close(self, period: int, lease_seconds: float = 30.0
                    ) -> Optional[Tuple[int, int]]:
        """认领一次结账投递；本期批次尚未全部确认时返回 ``None``。

        租约保证多进程不重复认领；确认状态不改，租约到期可重新认领。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT cut, delivered FROM closes WHERE period=?",
                (period,)).fetchone()
            if row is None or row[1] != 0:
                conn.execute("COMMIT")
                return None
            cut = int(row[0])
            _v, p, _r = conn.execute(
                "SELECT v, p, r FROM meta WHERE id=1").fetchone()
            if p < cut:
                # 本期仍有未确认批次，必须先投完批次
                conn.execute("COMMIT")
                return None
            now = time.monotonic()
            lease = conn.execute(
                "SELECT leased_at FROM close_inflight WHERE period=?",
                (period,)).fetchone()
            if lease is not None and now - lease[0] < lease_seconds:
                conn.execute("COMMIT")
                return None
            if lease is None:
                conn.execute(
                    "INSERT INTO close_inflight (period, leased_at) "
                    "VALUES (?, ?)", (period, now))
            else:
                conn.execute(
                    "UPDATE close_inflight SET leased_at=? WHERE period=?",
                    (now, period))
            conn.execute("COMMIT")
            return period, cut
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def confirm_close(self, period: int) -> None:
        """账本确认后持久标记该期已结账并释放租约（幂等）。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE closes SET delivered=1 WHERE period=?", (period,))
            conn.execute(
                "DELETE FROM close_inflight WHERE period=?", (period,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 受理（单事务串行化）
    # ------------------------------------------------------------------
    def commit_submission(self, sub_id: str, base: int, data_json: str,
                          result_json: str, result_obj: Dict[str, Any]
                          ) -> Tuple[str, int, Optional[List[List[Any]]]]:
        """在单个立即事务内完成幂等/版本判定与原子落盘。

        返回 ``(status, version, batch)``：
          * ``("replayed", v, None)``：同 id 同结构，回放原回执；
          * ``("accepted", v, batch)``：新受理，版本号 v、待投批次 batch。
        冲突抛 :class:`ConflictError` 并回滚，状态不变。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version, data_json FROM submissions WHERE sub_id=?",
                (sub_id,)).fetchone()
            if row is not None:
                old_version, old_data_json = row
                if old_data_json == data_json:
                    conn.execute("COMMIT")
                    return "replayed", old_version, None
                raise ConflictError(
                    f"id {sub_id} 已存在不同内容的提交")

            v, _p, r_text = conn.execute(
                "SELECT v, p, r FROM meta WHERE id=1").fetchone()
            if base != v:
                raise ConflictError(
                    f"新 id 的 base 必须等于当前版本 v={v}，"
                    f"得到 base={base}", path="$.base")

            prev_result = json.loads(r_text) if r_text is not None else None
            batch = diff_batch(result_obj, prev_result)
            new_version = v + 1
            batch_json = canonical_json(batch)
            receipt_json = canonical_json({"v": new_version})

            conn.execute(
                "INSERT INTO submissions (sub_id, version, data_json, "
                "result_json, receipt_json) VALUES (?, ?, ?, ?, ?)",
                (sub_id, new_version, data_json, result_json, receipt_json))
            conn.execute(
                "INSERT INTO batches (version, batch_json, delivered) "
                "VALUES (?, ?, 0)", (new_version, batch_json))
            conn.execute(
                "UPDATE meta SET v=?, r=? WHERE id=1",
                (new_version, result_json))
            conn.execute("COMMIT")
            return "accepted", new_version, batch
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 投递（多进程按序重试，只推进连续前缀）
    # ------------------------------------------------------------------
    def claim_next_pending(self, lease_seconds: float = 30.0
                           ) -> Optional[Tuple[int, List[List[Any]]]]:
        """在立即事务内认领下一个待投版本 ``p+1``。

        返回 ``(version, batch)``；已全部确认返回 ``False`` 语义的
        ``None``；若下一版被其他进程持有有效租约也返回 ``None``。
        认领不改确认状态：崩溃/断连后租约到期可重新认领，由账本端
        幂等保证重投不重复记账。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            v, p, _r = conn.execute(
                "SELECT v, p, r FROM meta WHERE id=1").fetchone()
            if p >= v:
                conn.execute("COMMIT")
                return None
            target = p + 1
            # 期间栅栏：存在未确认结账时，绝不越过其 cut 投递后续版本。
            # 与结账登记同库事务串行，保证"先投完本期批次、确认结账、
            # 再投下一期"严格成立，晚到版本不会串到已结账期间之前。
            pending_cut = conn.execute(
                "SELECT MIN(cut) FROM closes WHERE delivered=0").fetchone()[0]
            if pending_cut is not None and target > int(pending_cut):
                conn.execute("COMMIT")
                return None
            now = time.monotonic()
            row = conn.execute(
                "SELECT leased_at FROM inflight WHERE version=?",
                (target,)).fetchone()
            if row is not None and now - row[0] < lease_seconds:
                conn.execute("COMMIT")
                return None
            if row is None:
                conn.execute(
                    "INSERT INTO inflight (version, leased_at) "
                    "VALUES (?, ?)", (target, now))
            else:
                conn.execute(
                    "UPDATE inflight SET leased_at=? WHERE version=?",
                    (now, target))
            batch_row = conn.execute(
                "SELECT batch_json FROM batches WHERE version=?",
                (target,)).fetchone()
            conn.execute("COMMIT")
            if batch_row is None:
                # 理论不可达：版本与批次同事务落盘
                raise RuntimeError(f"版本 {target} 缺少待投批次记录")
            return target, json.loads(batch_row[0])
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def confirm_delivery(self, version: int) -> int:
        """标记某版已由账本确认，并把水位推进到连续前缀末端。

        仅当 ``version == p+1`` 时接受推进；乱序确认（多进程竞争下
        可能出现）忽略，水位只由连续前缀决定，杜绝跳号与重复记账。
        确认同时释放该版本的在途租约。返回推进后的水位。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            v, p, _r = conn.execute(
                "SELECT v, p, r FROM meta WHERE id=1").fetchone()
            if version <= p:
                conn.execute("COMMIT")
                return p  # 重复确认，幂等忽略
            if version != p + 1:
                conn.execute("COMMIT")
                return p  # 越号确认，不推进
            conn.execute(
                "UPDATE batches SET delivered=1 WHERE version=?",
                (version,))
            conn.execute("DELETE FROM inflight WHERE version=?", (version,))
            # 重算连续已确认前缀：从 p+1 起逐版检查，遇首个未确认即停
            new_p = p
            while new_p < v:
                row = conn.execute(
                    "SELECT delivered FROM batches WHERE version=?",
                    (new_p + 1,)).fetchone()
                if row is None or row[0] == 0:
                    break
                new_p += 1
            conn.execute("UPDATE meta SET p=? WHERE id=1", (new_p,))
            conn.execute("COMMIT")
            return new_p
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
