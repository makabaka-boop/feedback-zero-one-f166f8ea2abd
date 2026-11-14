#!/usr/bin/env python3
"""验收测试统一入口（可在 `python3 -I` 隔离模式下运行）。

用法：
    python3 -I tests/run_tests.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"),
                            pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
