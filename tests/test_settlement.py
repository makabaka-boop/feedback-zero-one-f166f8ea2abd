"""端到端验收：结算服务 + 投递进程 + 独立持久账本。

覆盖
====
1. 单元：待投批次差分（对齐、缺格作零、只留非零、ASCII 序、空批保留）；
2. 单元：信封 400（缺键/未知键/base 布尔负数/id 非法/语法错 $）；
3. HTTP：受理幂等、同 id 异内容 409、base 不符 409、冻结错误路径透传；
4. HTTP：查询 {v,p,r} 初态与推进、空批次版本保留；
5. 投递：按序重试、账本幂等、连续前缀水位、多进程不重复记账；
6. 崩溃：账本落盘后强退（验收钩子）→ 重启续投 → 余额只入一次；
7. 并发：多线程同时受理，版本号 1..N 无重号、无丢失，串行等价。
"""

import json
import os
import socket
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

from settlement.core import diff_batch, parse_envelope  # noqa: E402
from settlement.errors import EnvelopeError  # noqa: E402
from energy_alloc.errors import AllocationError  # noqa: E402


# ----------------------------------------------------------------------
# 测试数据
# ----------------------------------------------------------------------
def split_data(n=1, ends=("X", "Y")):
    """n 微瓦时从 s 各半分到两个终点（冻结管线手算例）。"""
    return {
        "ends": list(ends),
        "facts": [["f", "s", 0, 2, n]],
        "edges": [
            ["e", "s", ends[0], 0, 2, "1/2"],
            ["g", "s", ends[1], 0, 2, "1/2"],
        ],
    }


def envelope(sub_id, base, data):
    return json.dumps({"id": sub_id, "base": base, "data": data})


# ----------------------------------------------------------------------
# 单元：差分与信封
# ----------------------------------------------------------------------
class DiffBatchTest(unittest.TestCase):
    def _result(self, fact_ids, end_ids, b_rows):
        return {"fact_ids": list(fact_ids), "ends": list(end_ids),
                "A": [], "B": [list(row) for row in b_rows]}

    def test_first_version_missing_cells_are_zero(self):
        new = self._result(["f"], ["X", "Y"], [[0, 1]])
        self.assertEqual(diff_batch(new, None), [["f", "Y", 1]])

    def test_revision_minus_one_spec_example(self):
        # 题面：f 直记 X，n 由 1 改 0 → d=[["f","X",-1]]
        old = self._result(["f"], ["X"], [[1]])
        new = self._result(["f"], ["X"], [[0]])
        self.assertEqual(diff_batch(new, old), [["f", "X", -1]])

    def test_alignment_missing_cells_and_ascii_order(self):
        old = self._result(["a", "b"], ["X"], [[2], [3]])
        # 新增事实 c、新增终点 Y；旧格对齐取值，缺格作零
        new = self._result(["a", "b", "c"], ["X", "Y"],
                           [[2, 1], [0, 3], [5, 0]])
        d = diff_batch(new, old)
        self.assertEqual(d, [
            ["a", "Y", 1],          # 新增终点格
            ["b", "X", -3],         # 3 -> 0
            ["b", "Y", 3],          # 新增
            ["c", "X", 5],          # 新增事实，旧作零
        ])
        # 显式校验 ASCII 双重排序
        keys = [(row[0], row[1]) for row in d]
        self.assertEqual(keys, sorted(keys))

    def test_empty_batch_retained(self):
        old = self._result(["f"], ["X"], [[4]])
        new = self._result(["f"], ["X"], [[4]])
        self.assertEqual(diff_batch(new, old), [])


class EnvelopeValidationTest(unittest.TestCase):
    def _bad(self, text, path):
        with self.assertRaises(EnvelopeError) as ctx:
            parse_envelope(text)
        self.assertEqual(ctx.exception.path, path)

    def test_syntax_error_is_dollar(self):
        self._bad("{not json", "$")

    def test_duplicate_top_key_is_dollar(self):
        data = split_data()
        text = ('{"id":"s1","base":0,"data":%s,'
                '"data":%s}' % (json.dumps(data), json.dumps(data)))
        self._bad(text, "$")

    def test_duplicate_key_inside_data_is_dollar(self):
        # data 内部重复键由冻结解析器拦截，语法类错误统一 $
        text = ('{"id":"s1","base":0,"data":{'
                '"ends":["X"],"ends":["Y"],"facts":[],"edges":[]}}')
        self._bad(text, "$")

    def test_missing_and_unknown_keys(self):
        data = split_data()
        self._bad(json.dumps({"id": "s1", "base": 0}), "$")
        self._bad(json.dumps({"id": "s1", "base": 0,
                              "data": data, "extra": 1}), "$.extra")

    def test_base_rules(self):
        data = split_data()
        self._bad(envelope("s1", True, data), "$.base")
        self._bad(envelope("s1", -1, data), "$.base")
        self._bad(envelope("s1", 1.5, data), "$.base")
        self._bad(envelope("s1", "0", data), "$.base")

    def test_id_rules(self):
        data = split_data()
        self._bad(envelope("", 0, data), "$.id")
        self._bad(envelope("bad-id", 0, data), "$.id")
        self._bad(envelope(7, 0, data), "$.id")

    def test_data_must_be_object(self):
        self._bad(json.dumps({"id": "s1", "base": 0, "data": [1]}),
                  "$.data")

    def test_frozen_validation_path_passthrough(self):
        # 冻结实现拒绝：布尔出现在 facts[0][2]
        bad = split_data()
        bad["facts"][0][2] = True
        from settlement.core import validate_frozen_data
        with self.assertRaises(AllocationError) as ctx:
            validate_frozen_data(bad)
        self.assertEqual(ctx.exception.path, "facts[0][2]")


# ----------------------------------------------------------------------
# 进程与 HTTP 辅助
# ----------------------------------------------------------------------
def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServiceProcess:
    def __init__(self, script, db_path, port, extra_env=None):
        self.script = script
        self.db_path = db_path
        self.port = port
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, "-I", os.path.join(ROOT, script),
             "--db", db_path, "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "WARNING"],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out, err = self.proc.communicate(timeout=2)
                raise RuntimeError(
                    f"{self.script} 提前退出:\n{err.decode(errors='replace')}")
            try:
                with urllib.request.urlopen(self.url("/health"),
                                            timeout=1) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last = exc
            time.sleep(0.1)
        raise RuntimeError(f"{self.script} 未就绪: {last}")

    def stop(self, expect_crash_rc=None):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        # 排空管道，避免子进程文件描述符泄漏告警
        try:
            self.proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
        allowed = {0, -15, -9}
        if expect_crash_rc is not None:
            allowed.add(expect_crash_rc)
        if self.proc.returncode not in allowed:
            raise RuntimeError(
                f"{self.script} 非正常退出 rc={self.proc.returncode}")

    def restart(self, extra_env=None):
        self.stop()
        self.__init__(self.script, self.db_path, self.port, extra_env)
        self.wait_ready()


def http_json(url, payload=None, method=None, timeout=10):
    """发起 JSON HTTP 调用，返回 (status, parsed_body)。"""
    if payload is None:
        req = urllib.request.Request(url, method=method or "GET")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method=method or "POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def run_delivery(settlement_db, ledger_url, extra_env=None,
                 max_rounds=60, max_attempts=None,
                 backoff_max=0.1, lease_seconds=2.0):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-I",
           os.path.join(ROOT, "run_delivery.py"),
           "--settlement-db", settlement_db,
           "--ledger-url", ledger_url,
           "--once", "--max-rounds", str(max_rounds),
           "--backoff-min", "0.02",
           "--backoff-max", str(backoff_max),
           "--lease-seconds", str(lease_seconds)]
    if max_attempts is not None:
        cmd += ["--max-attempts", str(max_attempts)]
    return subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=60)


class HttpEndToEndTest(unittest.TestCase):
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

    def query(self):
        status, body = http_json(self.settlement.url("/query"))
        self.assertEqual(status, 200)
        return body

    def ledger_query(self):
        status, body = http_json(self.ledger.url("/query"))
        self.assertEqual(status, 200)
        return body

    def test_initial_state_and_full_lifecycle(self):
        # 初态 v=p=0、r=null
        self.assertEqual(self.query(), {"v": 0, "p": 0, "r": None})

        # 版本 1：f 的 n=1，B=[0,1]，只留非零 → f/Y +1
        status, body = self.submit("s1", 0, split_data(1))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 1})
        q = self.query()
        self.assertEqual((q["v"], q["p"]), (1, 0))
        self.assertEqual(q["r"]["B"], [[0, 1]])

        # 幂等回放：同 id 同 JSON 结构回原 {v}
        status, body = self.submit("s1", 0, split_data(1))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 1})
        self.assertEqual(self.query()["v"], 1)

        # 同 id 异内容 → 409，状态不变
        status, body = self.submit("s1", 1, split_data(5))
        self.assertEqual(status, 409)
        self.assertEqual(self.query()["v"], 1)

        # 新 id 但 base 错误 → 409
        status, body = self.submit("s2", 0, split_data(0))
        self.assertEqual(status, 409)
        self.assertIn("$.base", body.get("path", ""))

        # 合法修订：n 1→0，base=1，版本 2，批次 f/Y -1
        status, body = self.submit("s2", 1, split_data(0))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 2})
        self.assertEqual(self.query()["r"]["B"], [[0, 0]])

        # 投递追平
        proc = run_delivery(self.s_db, self.ledger.url("/ledger"))
        self.assertEqual(proc.returncode, 0, proc.stderr)

        q = self.query()
        self.assertEqual((q["v"], q["p"]), (2, 2))
        balances = self.ledger_query()
        self.assertEqual(balances["v"], 2)
        # +1 与 -1 整批累加后净额为 0，余额表无残留
        self.assertEqual(balances["balances"], [])

    def test_frozen_error_paths_over_http_and_no_state_change(self):
        bad = split_data()
        bad["facts"][0][2] = True  # 布尔不是整数
        status, body = self.submit("bad1", 0, bad)
        self.assertEqual(status, 400)
        self.assertEqual(body["path"], "facts[0][2]")

        # 语法错误 → $
        req = urllib.request.Request(
            self.settlement.url("/submit"),
            data=b"{", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            body = json.loads(exc.read().decode())
            self.assertEqual(body["path"], "$")

        # 被拒绝后状态保持初态
        self.assertEqual(self.query(), {"v": 0, "p": 0, "r": None})

    def test_empty_batch_version_is_retained_and_delivered(self):
        # 零能耗：B 全零 → 空批，但版本仍递增且可投递
        zero = {
            "ends": ["Z"],
            "facts": [["q", "s", 0, 1, 0]],
            "edges": [["e", "s", "Z", 0, 1, "1"]],
        }
        status, body = self.submit("z1", 0, zero)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"v": 1})

        proc = run_delivery(self.s_db, self.ledger.url("/ledger"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.query()["p"], 1)
        ledger_state = self.ledger_query()
        self.assertEqual(ledger_state["v"], 1)
        self.assertEqual(ledger_state["balances"], [])

    def test_ledger_rejects_gap_and_foreign_batch(self):
        self.submit("s1", 0, split_data(1))
        # 直接向账本越号投递 v=2 → 409
        status, body = http_json(self.ledger.url("/ledger"),
                                 {"v": 2, "d": []})
        self.assertEqual(status, 409)
        self.assertEqual(body["path"], "$.v")
        # 非法消息 → 400 带零基下标
        status, body = http_json(self.ledger.url("/ledger"),
                                 {"v": 1, "d": [["f", "Y", True]]})
        self.assertEqual(status, 400)
        self.assertEqual(body["path"], "$.d[0][2]")
        # 账本未被污染
        self.assertEqual(self.ledger_query()["v"], 0)

    def test_concurrent_acceptance_is_serial_equivalent(self):
        n_threads = 12
        errors = []

        def worker(i):
            # 每个线程使用独立 id；base 取当前版本存在竞争，
            # 失败的一方按 409 重试读取新版本后再提交，最终全部成功。
            base = 0
            for attempt in range(50):
                status, body = http_json(
                    self.settlement.url("/submit"),
                    {"id": f"c{i:02d}", "base": base,
                     "data": split_data(1)})
                if status == 200:
                    return body["v"]
                if status == 409:
                    q_status, q = http_json(self.settlement.url("/query"))
                    base = q["v"]
                    time.sleep(0.01)
                    continue
                errors.append((i, status, body))
                return None
            errors.append((i, "exhausted", None))
            return None

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            versions = list(pool.map(worker, range(n_threads)))

        self.assertEqual(errors, [])
        self.assertEqual(sorted(versions), list(range(1, n_threads + 1)))
        q = self.query()
        self.assertEqual(q["v"], n_threads)
        self.assertEqual(q["p"], 0)  # 尚未投递，水位不动


class RestartAndCrashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s_db = os.path.join(self.tmp.name, "settlement.db")
        self.l_db = os.path.join(self.tmp.name, "ledger.db")
        self.sport = _free_port()
        self.lport = _free_port()

    def tearDown(self):
        if hasattr(self, "settlement"):
            self.settlement.stop()
        if hasattr(self, "ledger"):
            self.ledger.stop()
        self.tmp.cleanup()

    def _start_all(self, ledger_env=None):
        self.settlement = ServiceProcess(
            "run_settlement.py", self.s_db, self.sport)
        self.ledger = ServiceProcess(
            "run_ledger.py", self.l_db, self.lport,
            extra_env=ledger_env)
        self.settlement.wait_ready()
        self.ledger.wait_ready()

    def test_restart_resumes_delivery_and_crash_after_commit(self):
        # 第一轮：账本在 v=2 落盘后、响应前强退
        self._start_all(
            ledger_env={"LEDGER_CRASH_AFTER_COMMIT_VERSION": "2"})
        for i, n in enumerate((1, 2, 3), start=1):
            status, body = http_json(
                self.settlement.url("/submit"),
                {"id": f"s{i}", "base": i - 1, "data": split_data(n)})
            self.assertEqual(status, 200, body)
            self.assertEqual(body, {"v": i})

        # 投递进程：v1 正常确认；v2 落盘后账本强退，投递端连接失败，
        # 有界重试（8 次、退避上限 0.05s）后退出（v2 未在结算侧确认）。
        # 账本库中 v1、v2 均已持久化。
        proc = run_delivery(self.s_db, self.ledger.url("/ledger"),
                            max_rounds=10, max_attempts=8,
                            backoff_max=0.05, lease_seconds=0.5)
        self.assertNotEqual(proc.returncode, 0,
                            "账本强退后投递进程应在尝试上限报错退出")
        self.assertIn("LedgerUnavailable", proc.stderr + proc.stdout)

        # 强退的账本进程已不在（验收钩子 rc=27）；重启去掉钩子
        self.ledger.stop(expect_crash_rc=27)
        self.ledger = ServiceProcess(
            "run_ledger.py", self.l_db, self.lport)
        self.ledger.wait_ready()
        status, ledger_state = http_json(self.ledger.url("/query"))
        self.assertEqual(status, 200)
        # v=2 的批次已落盘，重启后版本仍为 2，不丢账
        self.assertEqual(ledger_state["v"], 2)

        # 结算服务也重启一次：待投批次、租约与水位从 SQLite 恢复
        self.settlement.restart()
        status, q = http_json(self.settlement.url("/query"))
        self.assertEqual(status, 200)
        self.assertEqual(q["v"], 3)
        # v2 断连前未确认；水位只停在 1（连续前缀）
        self.assertEqual(q["p"], 1)

        # 重启后续投：v2 租约到期后重新认领，账本幂等重放（同版同批
        # 回原确认）、v3 补齐
        proc2 = run_delivery(self.s_db, self.ledger.url("/ledger"),
                             lease_seconds=0.5)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

        status, q = http_json(self.settlement.url("/query"))
        self.assertEqual((q["v"], q["p"]), (3, 3))
        status, ledger_state = http_json(self.ledger.url("/query"))
        self.assertEqual(ledger_state["v"], 3)

        # 手算余额（三版 n=1,2,3，半分取整的 B 行）：
        #   v1 B=[0,1]      → 批次 f/Y +1
        #   v2 B=[1,1]      → 批次 f/X +1（f/Y 不变，空差额剔除）
        #   v3 B=[1,2]      → 批次 f/Y +1（f/X 不变）
        # 整批累加后：f/X=1、f/Y=2
        balances = {(b["fact_id"], b["end_id"]): b["amount"]
                    for b in ledger_state["balances"]}
        self.assertEqual(balances, {("f", "X"): 1, ("f", "Y"): 2})

        # 再跑一次投递：全部已确认，幂等无副作用
        proc3 = run_delivery(self.s_db, self.ledger.url("/ledger"))
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        status, ledger_state = http_json(self.ledger.url("/query"))
        self.assertEqual(ledger_state["v"], 3)
        self.assertEqual(
            {(b["fact_id"], b["end_id"]): b["amount"]
             for b in ledger_state["balances"]},
            {("f", "X"): 1, ("f", "Y"): 2})

    def test_multiple_workers_no_double_accounting(self):
        self._start_all()
        total = 6
        # 每版 n 递增：B 的 Y 列依次为 1,1,2,2,3,3（半分取整），
        # 相邻差额非零，确保每版都有真实入账，余额可逐版核对。
        for i in range(1, total + 1):
            status, body = http_json(
                self.settlement.url("/submit"),
                {"id": f"s{i}", "base": i - 1,
                 "data": split_data(i)})
            self.assertEqual(status, 200, body)

        # 两个投递进程并发跑：租约保证同一版本不被重复认领，
        # 账本幂等 + 连续前缀水位兜底，杜绝重复记账
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        cmds = [
            [sys.executable, "-I",
             os.path.join(ROOT, "run_delivery.py"),
             "--settlement-db", self.s_db,
             "--ledger-url", self.ledger.url("/ledger"),
             "--once", "--max-rounds", "100",
             "--backoff-min", "0.02", "--backoff-max", "0.1",
             "--lease-seconds", "2.0"]
            for _ in range(2)
        ]
        procs = [subprocess.Popen(c, cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for c in cmds]
        for p in procs:
            p.wait(timeout=60)
        outputs = [p.communicate() for p in procs]
        for p, (_out, err) in zip(procs, outputs):
            self.assertEqual(p.returncode, 0,
                             err.decode(errors="replace"))

        status, q = http_json(self.settlement.url("/query"))
        self.assertEqual((q["v"], q["p"]), (total, total))
        status, ledger_state = http_json(self.ledger.url("/query"))
        self.assertEqual(ledger_state["v"], total)
        # 最终版 n=6：B 的 X、Y 格各为 3；从 0 起逐版整批累加必然等于
        # 最终 B 格值，任何重复记账都会使余额偏大
        balances = {(b["fact_id"], b["end_id"]): b["amount"]
                    for b in ledger_state["balances"]}
        self.assertEqual(balances, {("f", "X"): 3, ("f", "Y"): 3})
        self.assertEqual(q["r"]["B"], [[3, 3]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
