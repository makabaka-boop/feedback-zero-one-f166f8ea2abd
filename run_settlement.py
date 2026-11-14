#!/usr/bin/env python3
"""结算服务启动脚本（隔离模式可用：自行定位仓库根到 sys.path）。

用法：python3 -I run_settlement.py --db var/settlement.db --port 8080
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from settlement.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
