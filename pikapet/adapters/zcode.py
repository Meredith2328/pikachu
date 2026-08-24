# -*- coding: utf-8 -*-
"""adapter: zcode → 皮卡丘总线的适配器。

ZCode 自动化激活时调用本脚本，把"哪个自动化、处于什么阶段、结果如何"
整理成一条气泡消息发到总线。本适配器只依赖 pikapet.bus / pikapet.protocol，
对 zcode 无任何代码依赖——是 zcode 那侧来调用我们。

用法（任意 shell）:
    python -m pikapet.adapters.zcode "daily-brief" --stage start
    python -m pikapet.adapters.zcode "每日简报" --stage done --detail "生成 3 个文件"
    python -m pikapet.adapters.zcode "watch-inbox" --stage error --detail "权限不足"

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


def add_arguments(p):
    """把本适配器的参数挂到给定 parser 上。

    顶层统一 CLI（pikapet.cli）与独立入口（python -m pikapet.adapters.zcode）
    共用这一处定义，不再各写一遍。"""
    p.add_argument("name", nargs="?", help="自动化名称（如 daily-brief）")
    p.add_argument("--stage", choices=tuple(STAGE_STYLE),
                   default="done", help="自动化所处阶段")
    p.add_argument("--detail", default="", help="补充说明")
    p.add_argument("--level", choices=Notification.VALID_LEVELS, default=None,
                   help="覆盖 level（默认由 stage 决定）")
    p.add_argument("--source", default="zcode")
    p.add_argument("--ttl", type=float, default=15.0)
    p.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    p.add_argument("--check", action="store_true",
                   help="与 setup 一起使用：只检查 ZCode 集成，不写入")
    p.add_argument("--zcode-config", default=None,
                   help="ZCode config.json 路径（仅 setup 使用）")
    return p


def register(sub):
    """注册 `pikachu zcode <名称>` 与 `pikachu zcode setup`。"""
    p = sub.add_parser("zcode", help="ZCode 自动化通知与集成配置")
    add_arguments(p)
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    """发送一条阶段通知。返回 0 成功 / 3 总线不可达。"""
    if args.name == "setup":
        from .. import zcode_integration
        return zcode_integration.run_setup(args)
    if not args.name:
        print("请提供通知名称，或使用：pikachu zcode setup", file=sys.stderr)
        return 2
    if args.check or args.zcode_config:
        print("--check 和 --zcode-config 仅用于：pikachu zcode setup",
              file=sys.stderr)
        return 2
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pikapet-adapter-zcode", description="ZCode 自动化 → 皮卡丘通知")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
