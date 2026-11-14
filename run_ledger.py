#!/usr/bin/env python3
"""持久账本启动脚本（隔离模式可用：自行定位仓库根到 sys.path）。

用法：python3 -I run_ledger.py --db var/ledger.db --port 8081
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
