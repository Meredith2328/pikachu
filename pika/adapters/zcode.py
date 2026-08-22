# -*- coding: utf-8 -*-
"""adapter: zcode → 皮卡丘总线的适配器。

ZCode 自动化激活时调用本脚本，把"哪个自动化、处于什么阶段、结果如何"
整理成一条气泡消息发到总线。本适配器只依赖 pika.bus / pika.protocol，
对 zcode 无任何代码依赖——是 zcode 那侧来调用我们。

用法（任意 shell）:
    python -m pika.adapters.zcode "daily-brief" --stage start
    python -m pika.adapters.zcode "每日简报" --stage done --detail "生成 3 个文件"
    python -m pika.adapters.zcode "watch-inbox" --stage error --detail "权限不足"

退出码：0 发送成功；2 参数/协议错误；3 总线不可达。
"""
import argparse
import json
import sys

from .. import bus
from ..protocol import Notification
from .common import STAGE_STYLE, stage_level, stage_title

def _title(args) -> str:
    return stage_title(args.stage, args.name)


def _body(args) -> str:
    return args.detail or ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pika-adapter-zcode", description="ZCode 自动化 → 皮卡丘通知")
    parser.add_argument("name", help="自动化名称（如 daily-brief）")
    parser.add_argument("--stage", choices=tuple(STAGE_STYLE),
                        default="done", help="自动化所处阶段")
    parser.add_argument("--detail", default="", help="补充说明")
    parser.add_argument("--level", choices=Notification.VALID_LEVELS, default=None,
                        help="覆盖 level（默认由 stage 决定）")
    parser.add_argument("--source", default="zcode")
    parser.add_argument("--ttl", type=float, default=15.0)
    parser.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    args = parser.parse_args(argv)

    level = args.level or stage_level(args.stage)
    n = Notification(title=_title(args), body=_body(args), level=level,
                     source=args.source, ttl=args.ttl)
    try:
        resp = bus.send_notification(n, port=args.port)
    except Exception as e:
        print(f"总线不可达：{e}", file=sys.stderr)
        return 3
    print(json.dumps(resp, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
