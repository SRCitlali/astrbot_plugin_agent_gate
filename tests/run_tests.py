# -*- coding: utf-8 -*-
"""不依赖 pytest 的极简测试运行器。

AstrBot 的运行环境里没有装 pytest，而测试又必须用 AstrBot 的解释器跑
（要 `import astrbot`）。所以这里自己收集 `tests/test_*.py` 里的 `test_*` 函数，
断言仍是普通 `assert`，装了 pytest 的话 `pytest tests/` 也能直接跑。

用法：
    /root/.local/share/uv/tools/astrbot/bin/python tests/run_tests.py
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent

for _p in (str(PLUGIN_DIR.parent), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _iter_test_module_names():
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        yield path.stem


def main() -> int:
    passed = []
    failed = []

    for module_name in _iter_test_module_names():
        try:
            module = importlib.import_module(module_name)
        except Exception:
            failed.append((module_name, "<import>", traceback.format_exc()))
            continue

        for attr in sorted(dir(module)):
            if not attr.startswith("test_"):
                continue
            func = getattr(module, attr)
            if not callable(func):
                continue
            try:
                func()
                passed.append(f"{module_name}.{attr}")
            except Exception:
                failed.append((module_name, attr, traceback.format_exc()))

    for label in passed:
        print(f"  PASS  {label}")
    for module_name, attr, tb in failed:
        print(f"  FAIL  {module_name}.{attr}")
        print(tb)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
