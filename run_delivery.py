#!/usr/bin/env python3
"""投递进程启动脚本（隔离模式可用：自行定位仓库根到 sys.path）。

用法：
  python3 -I run_delivery.py --settlement-db var/settlement.db \
      --ledger-url http://127.0.0.1:8081/ledger
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from settlement.delivery import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
