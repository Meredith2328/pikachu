# -*- coding: utf-8 -*-
"""`python -m pikapet` 的入口：转到统一 CLI。

装好包之后直接用 `pikachu`；没装（直接在仓库里跑）时用
`python -m pikapet <子命令>`，两者行为完全一致。
"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
