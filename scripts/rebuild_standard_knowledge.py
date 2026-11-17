"""
清空并重建标准 products / sales_script 知识库。

实现已迁至 scripts/kb/rebuild_std.py；本文件保留为兼容入口。
推荐: python scripts/kb.py rebuild --products --scripts
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
for _p in (_ROOT, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from kb.rebuild_std import main

if __name__ == "__main__":
    main()
