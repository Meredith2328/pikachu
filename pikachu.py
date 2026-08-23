# -*- coding: utf-8 -*-
"""仓库内的便捷入口：等价于装好包后的 `pikachu` 命令。

装过包（pip install -e . / pipx install .）之后直接用 `pikachu`；没装时
在仓库根目录跑 `python pikachu.py <子命令>`，或者 `python -m pikapet
<子命令>`——三条路都走 pikapet.cli:main，参数与行为完全一致。

以前这个文件自己解析一遍参数再手工拼 argv 转发给各模块的 main()，
send 的参数因此定义了两遍、codex/dsh 又走 REMAINDER 透传。现在参数
定义只在 pikapet.cli 与各适配器里各一处。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pikapet.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
