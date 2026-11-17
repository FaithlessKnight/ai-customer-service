#!/usr/bin/env python3
"""
知识库统一管理 CLI。

用法见 scripts/README.md 或: python scripts/kb.py -h
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
for _p in (_ROOT, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from kb.commands import main

if __name__ == "__main__":
    raise SystemExit(main())
