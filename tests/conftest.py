# -*- coding: utf-8 -*-
"""pytest 入口：把插件目录与 tests 目录挂到 sys.path。

没装 pytest 也能跑：用 `python tests/run_tests.py`。
"""

import os
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

for _p in (str(_TESTS_DIR), str(_TESTS_DIR.parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# pytest 跑测试时也别往插件目录写 data/：把 AstrBot 根目录指到临时目录。
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="agentgate-test-"))
