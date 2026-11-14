#!/usr/bin/env python3
"""作业启动脚本：转发到 energy_alloc.cli。

显式把脚本所在目录加入 sys.path，使 `python3 -I run_allocation.py`
在隔离模式（忽略 PYTHONPATH、当前目录、site 包）下也能定位本地包。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from energy_alloc.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
