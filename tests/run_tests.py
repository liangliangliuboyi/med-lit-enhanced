# -*- coding: utf-8 -*-
"""独立单元测试入口。

用途：在**没有安装 pytest** 的环境里，一条命令跑完全部离线单元测试。

    python tests/run_tests.py

与 CI 里用的 `python -m unittest discover -s tests -v` 等价，
但这里显式把 `skills/medlit-agent/scripts` 加进 `sys.path`，
所以从任意工作目录执行都能正确 import 到被测模块。

退出码：全部通过 0，出现失败/错误 1。
"""
from __future__ import annotations

import os
import sys
import unittest

# 被测代码目录：<repo>/skills/medlit-agent/scripts
# 路径全部由 __file__ 推导，不写死绝对路径，保证 CI 上可用。
SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts')

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    """发现并运行 tests/ 下的全部单元测试，返回进程退出码。"""
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=TESTS_DIR, pattern='test_*.py')
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
