"""投递进程：把结算库中待投版本按序投递到独立账本，随后投递结账。

投递语义
========
* 始终只认领先水位 ``p`` 的下一版（``p+1``），天然按版本序投递；
* 账本侧对同版同批幂等，因此"落盘后断连/强退"导致的重投安全，
  绝不重复记账；
* 网络错误（账本未达、连接重置、超时）退避重试，通信恢复后自动
  追平——停提期间待投批次已在受理事务中落盘，重启续投不丢；
* 仅当账本确认连续前缀后，结算库水位 ``p`` 才推进；
* 400（消息本身非法）属于编程/数据契约错误，不重试直接上抛；
* 409 在按序投递下只可能来自账本库被外部破坏，记录告警后长退避，
  不跳过、不重复入账。

结账投递（期间）
----------------
结算库中每次结账登记为 ``(period, cut)``。投递顺序被严格约束为：

1. **先投完本期批次**：``p`` 必须达到该期 ``cut``，否则不投结账；
2. 再向账本 ``POST /close`` 发送 ``{period, cut}``；
3. 只有账本 200 确认后才投下一期（已确认标记落盘）。

因此重启、丢确认只会让结账重投；账本对同期间同 cut 回原确认，
不会串期、不会重复冻结。本期批次可能属于更早的期间（前一次结账
尚未确认），按期间升序逐一确认，天然不会跳过。

用法
====
``python3 -m settlement.delivery --settlement-db S.db --ledger-url URL
   [--once] [--interval 0.2] [--max-rounds N] [--max-attempts N]``

``--once``/``--max-rounds`` 便于测试：追平（无待投批次与结账）即退出。
"""

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .store import SettlementStore

_LOG = logging.getLogger("settlement.delivery")


class DeliveryContractError(RuntimeError):
    """账本返回 400：消息契约被破坏，重试无意义。"""


class LedgerUnavailable(RuntimeError):
    """账本持续不可达（仅在有界模式达上限时抛出）。"""


def _post_json(url: str, payload: Dict[str, Any],
               timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
    """向账本 POST 一份 JSON，返回 ``(HTTP 状态, 响应字典)``。

    连接类异常以 ``(0, {})`` 形式返回交由重试循环处理；
    HTTP 4xx/5xx 正常返回状态码与解析后的响应体。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload_out = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload_out = {}
        return exc.code, payload_out
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        _LOG.debug("账本连接失败：%s", exc)
        return 0, {}


def post_batch(ledger_url: str, version: int, batch: list,
               timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
    """向账本投递一版（``POST /ledger``），返回 ``(HTTP 状态, 响应)``。"""
    return _post_json(ledger_url, {"v": version, "d": batch}, timeout)


def close_url_from_ledger_url(ledger_url: str) -> str:
    """由 ``.../ledger`` 推导同源账本的 ``.../close`` 地址。"""
    base = ledger_url.rstrip("/")
    if base.endswith("/ledger"):
        return base[:-len("/ledger")] + "/close"
    return base + "/close"


def post_close(ledger_url: str, period: int, cut: int,
               timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
    """向账本投递一次结账（``POST /close``），返回 ``(HTTP 状态, 响应)``。"""
    return _post_json(close_url_from_ledger_url(ledger_url),
                      {"period": period, "cut": cut}, timeout)


class DeliveryWorker:
    """绑定一个结算库与一个账本 URL 的投递循环。"""

    def __init__(self, store: SettlementStore, ledger_url: str,
                 backoff_min: float = 0.05, backoff_max: float = 5.0,
                 lease_seconds: float = 30.0):
        self.store = store
        self.ledger_url = ledger_url
        self._backoff = backoff_min
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._lease_seconds = lease_seconds
        # 有界模式下的总投递尝试上限（None=守护模式，无限重试）
        self._max_attempts: Optional[int] = None
        self._attempts = 0

    def _sleep_backoff(self) -> None:
        time.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, self._backoff_max)

    def deliver_one(self) -> Optional[bool]:
        """尝试投递一个待投版本；批次追平后转而投递下一切账。

        返回值：
          * ``True``：有版本或结账被账本确认；
          * ``False``：批次与结账均已确认（真正追平）；
          * ``None``：下一版/下一切账被其他投递者持有有效租约。

        网络/5xx 故障在本方法内退避重试（守护语义：通信恢复后
        自动追平）；有界模式由 ``_max_attempts`` 限制总尝试数。
        """
        v, p, _r = self.store.current_state()
        pending_close = self.store.next_pending_close()
        if pending_close is not None and p >= pending_close[1]:
            # 本期（含此前各期）批次已全部投完：先投结账，再进入下一期。
            # 本期 cut 之前的版本都已入账，账本当前 v 恰为 cut；
            # 晚到的、属于后续期间的版本在此处被有意挡在结账之后。
            claimed_close = self.store.claim_close(
                pending_close[0], lease_seconds=self._lease_seconds)
            if claimed_close is None:
                return None
            self._deliver_close(*claimed_close)
            self._backoff = self._backoff_min
            return True

        if p >= v:
            # 批次已追平且无待投结账：真正空闲
            self._backoff = self._backoff_min
            return False

        claimed = self.store.claim_next_pending(
            lease_seconds=self._lease_seconds)
        if claimed is None:
            # p+1 存在但租约在其他进程手中
            return None
        version, batch = claimed

        while True:
            self._attempts += 1
            if (self._max_attempts is not None
                    and self._attempts > self._max_attempts):
                raise LedgerUnavailable(
                    f"达到最大投递尝试次数 {self._max_attempts}")
            status, payload = post_batch(self.ledger_url, version, batch)
            if status == 200:
                new_p = self.store.confirm_delivery(version)
                _LOG.info("版本 %s 已入账，水位推进至 p=%s",
                          version, new_p)
                self._backoff = self._backoff_min
                return True
            if status == 409:
                # 按序投递下不应出现；不跳过，长退避后重放（同版同批
                # 在账本恢复/澄清后仍可幂等确认）
                _LOG.warning("版本 %s 收到 409：%s；退避后重试",
                             version, payload)
            elif status == 400:
                raise DeliveryContractError(
                    f"账本拒绝版本 {version}：{payload}")
            else:
                # 0=连接失败；5xx=账本临时故障，均退避重试
                _LOG.info("版本 %s 投递暂不成功（status=%s），退避重试",
                          version, status)
            self._sleep_backoff()

    def _deliver_close(self, period: int, cut: int) -> None:
        """投递一次结账；确认后持久标记并释放租约。

        200 回 ``{period, cut}`` 表示受理：同期间同 cut 的重放也回 200，
        因此丢确认后的重试幂等。409 与批次投递一样长退避重试，绝不
        跳过（跳过会串期）。
        """
        while True:
            self._attempts += 1
            if (self._max_attempts is not None
                    and self._attempts > self._max_attempts):
                raise LedgerUnavailable(
                    f"达到最大投递尝试次数 {self._max_attempts}")
            status, payload = post_close(self.ledger_url, period, cut)
            if status == 200:
                self.store.confirm_close(period)
                _LOG.info("期间 %s（cut=%s）结账已确认", period, cut)
                return
            if status == 409:
                # 按序投递下不应出现；不跳过，长退避后由账本澄清
                _LOG.warning("期间 %s 结账收到 409：%s；退避后重试",
                             period, payload)
            elif status == 400:
                raise DeliveryContractError(
                    f"账本拒绝期间 {period} 结账：{payload}")
            else:
                _LOG.info("期间 %s 结账暂不成功（status=%s），退避重试",
                          period, status)
            self._sleep_backoff()

    def run_forever(self, interval: float = 0.2) -> None:
        """守护循环：追平后周期性醒来继续投递。"""
        while True:
            outcome = self.deliver_one()
            if outcome is False:
                time.sleep(interval)

    def run_until_caught_up(self, idle_rounds: int = 2,
                            max_rounds: Optional[int] = None,
                            max_attempts: Optional[int] = None) -> int:
        """测试/单次模式：连续 ``idle_rounds`` 轮真正追平即退出。

        租约被他人持有（返回 ``None``）不计入空闲轮，只会短暂等待，
        因此多个投递进程并发时，空闲退出的进程不会留下未投版本。
        ``max_attempts`` 限制网络投递的总尝试次数（含重试），超出抛
        :class:`LedgerUnavailable`，供"账本不可达"场景有界退出。
        返回累计确认的批次数与结账数。
        """
        self._max_attempts = max_attempts
        self._attempts = 0
        confirmed = 0
        idle = 0
        rounds = 0
        while idle < idle_rounds:
            rounds += 1
            if max_rounds is not None and rounds > max_rounds:
                raise LedgerUnavailable("达到最大轮数仍未追平")
            outcome = self.deliver_one()
            if outcome is True:
                confirmed += 1
                idle = 0
            elif outcome is False:
                idle += 1
                time.sleep(self._backoff_min)
            else:
                # 租约竞争：等持有者推进，不消耗空闲计数
                time.sleep(self._backoff_min)
        return confirmed


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="能耗结算投递进程")
    parser.add_argument("--settlement-db", required=True)
    parser.add_argument("--ledger-url", required=True,
                        help="账本投递地址，如 http://127.0.0.1:8081/ledger")
    parser.add_argument("--once", action="store_true",
                        help="追平后退出（等价 --idle-rounds 1）")
    parser.add_argument("--idle-rounds", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--max-attempts", type=int, default=None,
                        help="网络投递总尝试上限（含重试）")
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--backoff-min", type=float, default=0.05)
    parser.add_argument("--backoff-max", type=float, default=5.0)
    parser.add_argument("--lease-seconds", type=float, default=30.0,
                        help="在途租约时长；持有者崩溃后多久可被重新认领")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    store = SettlementStore(args.settlement_db)
    worker = DeliveryWorker(store, args.ledger_url,
                            backoff_min=args.backoff_min,
                            backoff_max=args.backoff_max,
                            lease_seconds=args.lease_seconds)
    try:
        if args.once:
            worker.run_until_caught_up(
                idle_rounds=1, max_rounds=args.max_rounds,
                max_attempts=args.max_attempts)
        elif args.max_rounds is not None or args.max_attempts is not None:
            worker.run_until_caught_up(
                idle_rounds=args.idle_rounds,
                max_rounds=args.max_rounds,
                max_attempts=args.max_attempts)
        else:
            worker.run_forever(interval=args.interval)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
