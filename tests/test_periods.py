"""期间结账验收：结算端 ``POST /close``、账本 ``POST /close``、``GET /periods``。

覆盖
====
1. 单元：结账信封校验（仅 {id}、标识规则、语法/重复键 ``$``、未知成员路径）；
2. HTTP：题面示例——``f`` 直记 ``X``，``n=2`` 后结账、改 1 再结账，
   两期 ``d`` 为 ``[["f","X",2]]``、``[["f","X",-1]]``；
3. HTTP：空期间、相同 cut、多次结账待投；提交不受阻、重试不改原期间；
4. HTTP：结账 id 独立幂等（后一期结账后重放仍回原期间/cut）；
5. 账本：同期间同 cut 回原确认；异 cut / 非当前期间 / cut≠v 均 409；
   非法 400 带字段路径且拒绝不改状态；旧批重试沿用原规则；
6. 并发：提交与结账、结账与结账同时竞争，串行等价（期间严格递增、
   cut 单调不减）；
7. 崩溃：账本结账落盘后、本地确认前强退 → 重启续投，期间不重复、
   ``d`` 不重复冻结；且下一期批次在结账确认前不会越过 cut 投递。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from settlement.core import parse_close_envelope  # noqa: E402
from settlement.errors import EnvelopeError  # noqa: E402
from ledger.store import LedgerStore  # noqa: E402

from test_settlement import (  # noqa: E402
    ServiceProcess, _free_port, http_json, run_delivery,
)


def direct_data(n, ends=("X",)):
    """事实 f 以权重 1 直记终点 X（题面示例）。"""
    return {"ends": list(ends),
            "facts": [["f", "s", 0, 2, n]],
            "edges": [["e", "s", ends[0], 0, 2, "1"]]}


def raw_post(url, body_bytes):
    req = urllib.request.Request(
        url, data=body_bytes, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ----------------------------------------------------------------------
# 单元：结账信封
# ----------------------------------------------------------------------
class CloseEnvelopeTest(unittest.TestCase):
    def _bad(self, text, path):
        with self.assertRaises(EnvelopeError) as ctx:
            parse_close_envelope(text)
        self.assertEqual(ctx.exception.path, path)

    def test_valid(self):
        self.assertEqual(parse_close_envelope('{"id":"c1"}'), {"id": "c1"})

    def test_syntax_and_duplicate_keys_are_dollar(self):
        self._bad("{", "$")
        self._bad('{"id":"c1","id":"c1"}', "$")
        self._bad("[]", "$")

    def test_only_id_allowed(self):
        self._bad('{"id":"c1","period":1}', "$.period")
        self._bad('{"id":"c1","cut":0}', "$.cut")
        self._bad('{"id":"c1","base":0}', "$.base")

    def test_id_rules(self):
        self._bad("{}", "$")
        self._bad('{"id":""}', "$.id")
        self._bad('{"id":"bad-id"}', "$.id")
        self._bad('{"id":7}', "$.id")
        self._bad('{"id":true}', "$.id")


# ----------------------------------------------------------------------
# HTTP 端到端
# ----------------------------------------------------------------------
class PeriodHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s_db = os.path.join(self.tmp.name, "settlement.db")
        self.l_db = os.path.join(self.tmp.name, "ledger.db")
        self.sport = _free_port()
        self.lport = _free_port()
        self.settlement = ServiceProcess(
            "run_settlement.py", self.s_db, self.sport)
        self.ledger = ServiceProcess(
            "run_ledger.py", self.l_db, self.lport)
        self.settlement.wait_ready()
        self.ledger.wait_ready()

    def tearDown(self):
        self.settlement.stop()
        self.ledger.stop()
        self.tmp.cleanup()

    def submit(self, sub_id, base, data):
        return http_json(self.settlement.url("/submit"),
                         {"id": sub_id, "base": base, "data": data})

    def close(self, close_id):
        return http_json(self.settlement.url("/close"), {"id": close_id})

    def query(self):
        status, body = http_json(self.settlement.url("/query"))
        self.assertEqual(status, 200)
        return body

    def periods(self):
        status, body = http_json(self.ledger.url("/periods"))
        self.assertEqual(status, 200)
        return body

    def deliver(self):
        proc = run_delivery(self.s_db, self.ledger.url("/ledger"))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_spec_example_two_periods(self):
        # f 直记 X：n=2 → B=[[2]]，首期差额 +2
        status, body = self.submit("s1", 0, direct_data(2))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 1})
        status, body = self.close("c1")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"period": 1, "cut": 1})

        # 改 1 → 第二期差额 -1
        status, body = self.submit("s2", 1, direct_data(1))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 2})
        status, body = self.close("c2")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"period": 2, "cut": 2})

        self.deliver()
        self.assertEqual(self.periods(), [
            {"period": 1, "cut": 1, "d": [["f", "X", 2]]},
            {"period": 2, "cut": 2, "d": [["f", "X", -1]]},
        ])
        # 提交与结账完全不改变原有投递/版本语义
        q = self.query()
        self.assertEqual((q["v"], q["p"]), (2, 2))

    def test_empty_periods_and_same_cut(self):
        # 未启用（从未结账）兼容：期间数组为空
        self.assertEqual(self.periods(), [])
        self.assertEqual(self.query(), {"v": 0, "p": 0, "r": None})

        # 无任何提交即结账：空期间，cut=0
        status, body = self.close("e1")
        self.assertEqual((status, body), (200, {"period": 1, "cut": 0}))
        # 连续结账：期间严格递增，cut 可相同
        status, body = self.close("e2")
        self.assertEqual((status, body), (200, {"period": 2, "cut": 0}))
        status, body = self.close("e3")
        self.assertEqual((status, body), (200, {"period": 3, "cut": 0}))

        self.deliver()
        self.assertEqual(self.periods(), [
            {"period": 1, "cut": 0, "d": []},
            {"period": 2, "cut": 0, "d": []},
            {"period": 3, "cut": 0, "d": []},
        ])

    def test_submission_not_blocked_and_close_is_separately_idempotent(self):
        self.submit("s1", 0, direct_data(1))
        self.close("c1")

        # 结账待投期间提交照常受理，版本继续递增
        status, body = self.submit("s2", 1, direct_data(3))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 2})
        status, body = self.submit("s3", 2, direct_data(0))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 3})

        # 同 id 重试回原回执，不改原期间（即使此后已有新结账）
        self.assertEqual(self.close("c1"),
                         (200, {"period": 1, "cut": 1}))
        self.assertEqual(self.close("c2"),
                         (200, {"period": 2, "cut": 3}))
        self.assertEqual(self.close("c1"),
                         (200, {"period": 1, "cut": 1}))

        self.deliver()
        self.assertEqual(self.periods(), [
            {"period": 1, "cut": 1, "d": [["f", "X", 1]]},
            # 第二期覆盖版本 2、3：+2 与 -3 汇总为 -1
            {"period": 2, "cut": 3, "d": [["f", "X", -1]]},
        ])

    def test_many_pending_closes_delivered_in_order(self):
        # 一次结账待投期间又结账多次：投递按期间升序、逐期确认
        self.submit("s1", 0, direct_data(2))
        self.close("c1")            # period 1, cut 1
        self.close("c2")            # period 2, cut 1（空期间）
        self.submit("s2", 1, direct_data(5))
        self.close("c3")            # period 3, cut 2
        self.close("c4")            # period 4, cut 2（空期间）

        before = self.query()
        self.assertEqual((before["v"], before["p"]), (2, 0))
        self.deliver()
        after = self.query()
        self.assertEqual((after["v"], after["p"]), (2, 2))
        self.assertEqual(self.periods(), [
            {"period": 1, "cut": 1, "d": [["f", "X", 2]]},
            {"period": 2, "cut": 1, "d": []},
            {"period": 3, "cut": 2, "d": [["f", "X", 3]]},
            {"period": 4, "cut": 2, "d": []},
        ])

    def test_close_rejects_invalid_requests_without_state_change(self):
        before = (self.query(), self.periods())
        cases = [
            (b"{", "$"),
            (b"[]", "$"),
            (b'{"id":"c1","period":1}', "$.period"),
            (b"{}", "$"),
            (b'{"id":""}', "$.id"),
            (b'{"id":"bad-id"}', "$.id"),
            (b'{"id":7}', "$.id"),
            (b'{"id":true}', "$.id"),
            (b'{"id":"c1","id":"c1"}', "$"),
        ]
        for raw, path in cases:
            status, body = raw_post(self.settlement.url("/close"), raw)
            self.assertEqual(status, 400, (raw, body))
            self.assertEqual(body["path"], path, raw)
        self.assertEqual((self.query(), self.periods()), before)

    def test_ledger_close_validation_and_conflicts(self):
        # 账本先校验：非法请求 400 且拒绝不改状态
        cases = [
            (b"{", "$"),
            (b"[]", "$"),
            (b'{"period":1}', "$"),
            (b'{"cut":0}', "$"),
            (b'{"period":0,"cut":0}', "$.period"),
            (b'{"period":-1,"cut":0}', "$.period"),
            (b'{"period":1,"cut":-1}', "$.cut"),
            (b'{"period":true,"cut":0}', "$.period"),
            (b'{"period":1,"cut":false}', "$.cut"),
            (b'{"period":1,"cut":0,"x":1}', "$.x"),
        ]
        for raw, path in cases:
            status, body = raw_post(self.ledger.url("/close"), raw)
            self.assertEqual(status, 400, (raw, body))
            self.assertEqual(body["path"], path, raw)
        self.assertEqual(self.periods(), [])
        self.assertEqual(http_json(self.ledger.url("/query"))[1]["v"], 0)

        # 新结账必须恰为当前期间且 cut == 入账版本
        status, body = http_json(self.ledger.url("/close"),
                                 {"period": 2, "cut": 0})
        self.assertEqual(status, 409)
        self.assertEqual(body["path"], "$.period")
        status, body = http_json(self.ledger.url("/close"),
                                 {"period": 1, "cut": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["path"], "$.cut")
        self.assertEqual(self.periods(), [])

        # 合法空期间结账；同期间同 cut 幂等回原确认
        self.assertEqual(http_json(self.ledger.url("/close"),
                                   {"period": 1, "cut": 0}),
                         (200, {"period": 1, "cut": 0}))
        self.assertEqual(http_json(self.ledger.url("/close"),
                                   {"period": 1, "cut": 0}),
                         (200, {"period": 1, "cut": 0}))
        # 同期间异 cut → 409
        status, body = http_json(self.ledger.url("/close"),
                                 {"period": 1, "cut": 3})
        self.assertEqual(status, 409)
        self.assertEqual(body["path"], "$.cut")

        # 旧批重试沿用原规则：结账后仍可幂等重放，异批 409
        self.assertEqual(http_json(self.ledger.url("/ledger"),
                                   {"v": 1, "d": [["f", "X", 4]]}),
                         (200, {"v": 1, "status": "confirmed"}))
        self.assertEqual(http_json(self.ledger.url("/ledger"),
                                   {"v": 1, "d": [["f", "X", 4]]}),
                         (200, {"v": 1, "status": "replayed"}))
        status, body = http_json(self.ledger.url("/ledger"),
                                 {"v": 1, "d": [["f", "X", 5]]})
        self.assertEqual(status, 409)
        # 结账后提交继续：下一期 cut 可等于新入账版本
        self.assertEqual(http_json(self.ledger.url("/close"),
                                   {"period": 2, "cut": 1}),
                         (200, {"period": 2, "cut": 1}))
        self.assertEqual(self.periods(), [
            {"period": 1, "cut": 0, "d": []},
            {"period": 2, "cut": 1, "d": [["f", "X", 4]]},
        ])


class PeriodConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s_db = os.path.join(self.tmp.name, "settlement.db")
        self.l_db = os.path.join(self.tmp.name, "ledger.db")
        self.sport = _free_port()
        self.lport = _free_port()
        self.settlement = ServiceProcess(
            "run_settlement.py", self.s_db, self.sport)
        self.ledger = ServiceProcess(
            "run_ledger.py", self.l_db, self.lport)
        self.settlement.wait_ready()
        self.ledger.wait_ready()

    def tearDown(self):
        self.settlement.stop()
        self.ledger.stop()
        self.tmp.cleanup()

    # -- 复用 PeriodHttpTest 的辅助方法 --
    submit = PeriodHttpTest.submit
    close = PeriodHttpTest.close
    query = PeriodHttpTest.query
    periods = PeriodHttpTest.periods

    def test_concurrent_delivery_workers_do_not_duplicate_periods(self):
        # 多次结账待投 + 两个投递进程并发：租约保证同一结账不被重复
        # 认领，账本幂等回原确认，期间数组与 d 仍然唯一且有序
        for i in range(1, 6):
            status, body = self.submit(f"s{i}", i - 1, direct_data(i))
            self.assertEqual(status, 200, body)
            status, body = self.close(f"c{i}")
            self.assertEqual(status, 200, body)

        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        cmd = [sys.executable, "-I",
               os.path.join(ROOT, "run_delivery.py"),
               "--settlement-db", self.s_db,
               "--ledger-url", self.ledger.url("/ledger"),
               "--once", "--max-rounds", "200",
               "--backoff-min", "0.02", "--backoff-max", "0.1",
               "--lease-seconds", "2.0"]
        procs = [subprocess.Popen(cmd, cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
                 for _ in range(2)]
        for p in procs:
            p.wait(timeout=60)
        for p in procs:
            _out, err = p.communicate()
            self.assertEqual(p.returncode, 0, err.decode(errors="replace"))

        status, q = http_json(self.settlement.url("/query"))
        self.assertEqual((q["v"], q["p"]), (5, 5))
        status, periods = http_json(self.ledger.url("/periods"))
        self.assertEqual([p["period"] for p in periods], [1, 2, 3, 4, 5])
        # 每期只有一版，d 即该版 B 的差额（X 列依次为 1..5 的取整值）
        self.assertEqual([p["cut"] for p in periods], [1, 2, 3, 4, 5])
        self.assertEqual(periods[0]["d"], [["f", "X", 1]])
        self.assertEqual(periods[4]["d"], [["f", "X", 1]])

    def test_concurrent_submit_and_close_serial_equivalent(self):
        submit_n = 20
        close_n = 6

        def submit_worker(i):
            while True:
                status, q = http_json(self.settlement.url("/query"))
                status, body = http_json(
                    self.settlement.url("/submit"),
                    {"id": f"s{i:02d}", "base": q["v"],
                     "data": direct_data(1)})
                if status == 200:
                    return body["v"]
                if status == 409:
                    time.sleep(0.005)
                    continue
                raise AssertionError((status, body))

        def close_worker(i):
            status, body = http_json(self.settlement.url("/close"),
                                     {"id": f"c{i:02d}"})
            self.assertEqual(status, 200)
            return body["period"], body["cut"]

        with ThreadPoolExecutor(max_workers=submit_n + close_n) as pool:
            submit_futures = [pool.submit(submit_worker, i)
                              for i in range(submit_n)]
            close_futures = [pool.submit(close_worker, i)
                             for i in range(close_n)]
            versions = [f.result() for f in submit_futures]
            closes = [f.result() for f in close_futures]

        # 提交串行等价：版本 1..N 无重号无丢失
        self.assertEqual(sorted(versions), list(range(1, submit_n + 1)))
        # 结账串行等价：期间严格从 1 递增，cut 单调不减且 ≤ 当前 v
        periods = sorted(closes)
        self.assertEqual([p for p, _ in periods],
                         list(range(1, close_n + 1)))
        cuts = [c for _, c in periods]
        self.assertEqual(cuts, sorted(cuts))
        self.assertTrue(all(0 <= c <= submit_n for c in cuts))
        status, q = http_json(self.settlement.url("/query"))
        self.assertEqual((q["v"], q["p"]), (submit_n, 0))

        # 投递后期间数组严格升序，且各期 cut 单调不减
        proc = run_delivery(self.s_db, self.ledger.url("/ledger"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status, ledger_periods = http_json(self.ledger.url("/periods"))
        self.assertEqual(status, 200)
        self.assertEqual([p["period"] for p in ledger_periods],
                         list(range(1, close_n + 1)))
        pcuts = [p["cut"] for p in ledger_periods]
        self.assertEqual(pcuts, sorted(pcuts))
        # 期间差额无重叠：d 之和等于结账 cut 之前所有批次净额，
        # 且最后 cut 之后不再有已结账批次
        totals = {}
        for p in ledger_periods:
            for fid, end, delta in p["d"]:
                totals[(fid, end)] = totals.get((fid, end), 0) + delta
        balances = {(b["fact_id"], b["end_id"]): b["amount"]
                    for b in http_json(self.ledger.url("/query"))[1]["balances"]}
        if ledger_periods and ledger_periods[-1]["cut"] == submit_n:
            self.assertEqual({k: v for k, v in totals.items() if v},
                             balances)


class LegacyLedgerMigrationTest(unittest.TestCase):
    """全新集群可选启用：旧账本库（无期间表/列）升级后行为不变。"""

    def test_old_schema_is_migrated_and_usable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "ledger.db")
        # 构造旧版库结构（meta 只有 v，无 periods 表）
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK (id = 1),
                           v INTEGER NOT NULL);
        CREATE TABLE batches (version INTEGER PRIMARY KEY,
                              batch_json TEXT NOT NULL);
        CREATE TABLE balances (fact_id TEXT NOT NULL,
                               end_id TEXT NOT NULL,
                               amount INTEGER NOT NULL,
                               PRIMARY KEY (fact_id, end_id));
        INSERT INTO meta (id, v) VALUES (1, 0);
        """)
        conn.commit()
        conn.close()

        store = LedgerStore(db)
        # 迁移后：期间数组为空（未启用兼容），版本仍从 0 起
        self.assertEqual(store.periods_snapshot(), [])
        self.assertEqual(store.current_version(), 0)
        self.assertEqual(store.current_period(), 1)

        # 未结账时旧批规则原样可用
        self.assertEqual(store.post_batch(1, [["f", "X", 2]]),
                         ("confirmed", 1))
        self.assertEqual(store.balances_snapshot(),
                         [{"fact_id": "f", "end_id": "X", "amount": 2}])
        # 启用后首期 cut 等于当前版本
        self.assertEqual(store.close_period(1, 1),
                         ("confirmed", 1, 1, [["f", "X", 2]]))
        self.assertEqual(store.periods_snapshot(),
                         [{"period": 1, "cut": 1, "d": [["f", "X", 2]]}])


class PeriodCrashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s_db = os.path.join(self.tmp.name, "settlement.db")
        self.l_db = os.path.join(self.tmp.name, "ledger.db")
        self.sport = _free_port()
        self.lport = _free_port()

    def tearDown(self):
        self.tmp.cleanup()

    def test_crash_after_ledger_close_commit_then_recover(self):
        # 账本在期间 1 结账落盘后、响应前强退（模拟远端落盘、本地确认前）
        self.settlement = ServiceProcess(
            "run_settlement.py", self.s_db, self.sport)
        self.ledger = ServiceProcess(
            "run_ledger.py", self.l_db, self.lport,
            extra_env={"LEDGER_CRASH_AFTER_CLOSE_PERIOD": "1"})
        self.settlement.wait_ready()
        self.ledger.wait_ready()

        self.submit("s1", 0, direct_data(2))
        self.close("c1")
        self.submit("s2", 1, direct_data(0))
        self.close("c2")

        proc = run_delivery(self.s_db, self.ledger.url("/ledger"),
                            max_attempts=8, max_rounds=10,
                            backoff_max=0.05, lease_seconds=0.5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("LedgerUnavailable", proc.stderr + proc.stdout)

        self.ledger.stop(expect_crash_rc=27)
        self.ledger = ServiceProcess("run_ledger.py", self.l_db, self.lport)
        self.ledger.wait_ready()

        # 远端：期间 1 已冻结落盘，且此时账本版本恰好等于 cut=1——
        # 说明下一期（v=2）的批次没有在结账确认前被越过投递
        status, periods = http_json(self.ledger.url("/periods"))
        self.assertEqual(periods, [{"period": 1, "cut": 1,
                                    "d": [["f", "X", 2]]}])
        self.assertEqual(http_json(self.ledger.url("/query"))[1]["v"], 1)
        # 结算端尚未确认结账：水位停在 1
        self.assertEqual(self.query()["p"], 1)

        self.settlement.restart()
        proc2 = run_delivery(self.s_db, self.ledger.url("/ledger"),
                             lease_seconds=0.5)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(self.query()["p"], 2)
        # 重投期间 1 只回原确认，不重复冻结；期间 2 随后确认
        self.assertEqual(http_json(self.ledger.url("/periods")),
                         (200, [
                             {"period": 1, "cut": 1, "d": [["f", "X", 2]]},
                             {"period": 2, "cut": 2, "d": [["f", "X", -2]]},
                         ]))
        # 再次投递：幂等，无新增期间
        proc3 = run_delivery(self.s_db, self.ledger.url("/ledger"))
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        self.assertEqual(http_json(self.ledger.url("/periods"))[1],
                         [{"period": 1, "cut": 1, "d": [["f", "X", 2]]},
                          {"period": 2, "cut": 2, "d": [["f", "X", -2]]}])

    # -- 复用 PeriodHttpTest 的辅助方法（避免重复样板） --
    submit = PeriodHttpTest.submit
    close = PeriodHttpTest.close
    query = PeriodHttpTest.query
    periods = PeriodHttpTest.periods


if __name__ == "__main__":
    unittest.main(verbosity=2)
