# -*- coding: utf-8 -*-
"""pytest 入口：把插件目录与 tests 目录挂到 sys.path。

没装 pytest 也能跑：用 `python tests/run_tests.py`。
"""

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

for _p in (str(_TESTS_DIR), str(_TESTS_DIR.parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
