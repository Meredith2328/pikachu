# -*- coding: utf-8 -*-
"""皮卡丘统一命令行入口。

一个 `pikachu` 命令覆盖全部功能：起桌宠 / 起独立总线 / 起提醒 / 发通知 /
看历史 / 查健康 / 环境自检 / 三个适配器。

以前这里是两层：仓库根的 pikachu.py 自己解析一遍参数，再手工拼 argv 转发
给各模块的 main()，send 的参数定义因此写了两遍，codex/dsh 又走
argparse.REMAINDER 透传，三种风格并存。现在各模块自己提供
`register(sub)` 把子命令注册进来，顶层只负责分发，参数定义只有一处。

用法:
    pikachu pet          # 启动桌宠（内嵌总线 + 内嵌健康提醒）
    pikachu bus          # 只启动独立总线
    pikachu reminder     # 启动健康提醒（需要总线在跑）
    pikachu send "标题" "正文" [--level info] [--port 7452]
    pikachu history / health / doctor
    pikachu zcode <名称> [--stage start|done|error] [--detail 说明]
    pikachu codex event '<JSON>' / codex report <名称> [--stage ...]
    pikachu dsh run <名称> --cwd <目录> --timeout 420 -- "任务文本"

未安装时等价用 `python -m pikapet <子命令>`。
"""
import argparse
import json
import sys
import urllib.error

from . import bus
from . import paths
from .logs import configure as configure_logging
from .protocol import Notification, ProtocolError

PORT_HELP = f"总线端口（默认 {bus.DEFAULT_PORT}）"


def _add_port(p):
    p.add_argument("--port", type=int, default=bus.DEFAULT_PORT, help=PORT_HELP)
    return p


# ----------------------------------------------------------------------
# 通知客户端子命令
# ----------------------------------------------------------------------
def cmd_send(args) -> int:
    # 先按协议校验（ttl 负数/NaN 等在客户端就报错，而不是等总线 400）
    try:
        notif = Notification.from_dict({"title": args.title,
                                        "body": args.body or "",
                                        "level": args.level,
                                        "source": args.source,
                                        "ttl": args.ttl})
    except ProtocolError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return 1
    try:
        resp = bus.send_notification(notif, port=args.port)
    except urllib.error.HTTPError as e:
        # 服务在但拒绝（如 token 不符）：把状态码带出来，比"发送失败"有用
        print(f"总线拒绝了这条消息（HTTP {e.code} {e.reason}）", file=sys.stderr)
        return 1
    except (OSError, bus.PortFileError, bus.TokenError) as e:
        print(f"发送失败：{e}", file=sys.stderr)
        return 1
    print(json.dumps(resp, ensure_ascii=False))
    return 0


def cmd_history(args) -> int:
    try:
        items = bus.fetch_history(n=args.n, port=args.port)
    except urllib.error.HTTPError as e:
        print(f"获取历史失败（HTTP {e.code} {e.reason}）", file=sys.stderr)
        return 1
    except (OSError, bus.PortFileError, ValueError) as e:
        print(f"获取历史失败：{e}", file=sys.stderr)
        return 1
    for it in items:
        line = f"[{it.get('source', '?')}] {it.get('title', '')}"
        if it.get("body"):
            line += f" — {it['body']}"
        print(line)
    return 0


def cmd_health(args) -> int:
    try:
        info = bus.fetch_health(port=args.port)
    except urllib.error.HTTPError as e:
        print(f"总线响应异常（HTTP {e.code} {e.reason}）", file=sys.stderr)
        return 1
    except (OSError, bus.PortFileError, ValueError) as e:
        print(f"总线未响应：{e}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------------
# 长驻进程子命令
# ----------------------------------------------------------------------
def cmd_pet(args) -> int:
    from .pet import main as pet_main
    argv = ["--port", str(args.port)]
    if args.subscribe_only:
        argv.append("--subscribe-only")
    if args.no_reminder:
        argv.append("--no-reminder")
    return pet_main(argv)


def cmd_bus(args) -> int:
    argv = ["--port", str(args.port)]
    if args.port_file:
        argv += ["--port-file", args.port_file]
    return bus.main(argv)


def cmd_reminder(args) -> int:
    from .reminder_runner import main as rem_main
    argv = ["--port", str(args.port)]
    if args.config:
        argv += ["--config", args.config]
    return rem_main(argv)


# ----------------------------------------------------------------------
# 环境自检
# ----------------------------------------------------------------------
def cmd_doctor(args) -> int:
    from . import __version__

    print("皮卡丘环境自检")
    print("-" * 40)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok))
        print(f"{'✅' if ok else '❌'} {name} {detail}")

    check("版本", True, __version__)

    try:
        import tkinter
        check("tkinter", True, f"Tk {tkinter.TkVersion}")
    except ImportError:
        check("tkinter", False, "未安装（桌宠 GUI 不可用，其余功能正常）")

    try:
        from PIL import Image
        check("Pillow", True, Image.__version__)
    except ImportError:
        check("Pillow", False, "未安装（贴图透明处理降级）")

    # 运行时目录：token 与端口文件都在这里，不可写等于通知链路不通
    try:
        base = paths.base_dir(create=True)
        check("运行时目录", True, str(base))
    except paths.RuntimeDirError as e:
        check("运行时目录", False, str(e))

    token = paths.token_file()
    check("投递 token", token.is_file(),
          str(token) if token.is_file() else f"{token} 尚未生成（总线首次启动时创建）")

    try:
        info = bus.fetch_health(port=args.port, timeout=0.8)
        check("总线", True, f"端口 {info.get('port')}")
    except Exception:
        check("总线", False, "未运行（桌宠内置或独立 bus 未启动）")

    try:
        from .win.idle import get_idle_seconds
        check("空闲检测", True, f"当前空闲 {get_idle_seconds():.0f}s")
    except Exception as e:
        check("空闲检测", False, f"不可用（{e}）")

    print("-" * 40)
    failed = [n for n, ok in checks if not ok]
    if not failed:
        print("全部就绪 ✅")
        return 0
    print("以下项异常（不影响其他功能）：" + "、".join(failed))
    return 1


# ----------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pikachu", description="皮卡丘：桌宠 + 通知总线 + 健康提醒")
    sub = parser.add_subparsers(dest="command", required=True)

    p = _add_port(sub.add_parser("pet", help="启动桌宠（内嵌总线 + 内嵌健康提醒）"))
    p.add_argument("--subscribe-only", action="store_true",
                   help="只订阅已有总线，不在本进程开端口")
    p.add_argument("--no-reminder", action="store_true",
                   help="不启动内嵌健康提醒")
    p.set_defaults(func=cmd_pet)

    p = _add_port(sub.add_parser("bus", help="启动独立总线"))
    p.add_argument("--port-file", default=None,
                   help="启动后把实际端口写入该文件（供测试/脚本读取）")
    p.set_defaults(func=cmd_bus)

    p = _add_port(sub.add_parser("reminder", help="启动健康提醒（需总线在跑）"))
    p.add_argument("--config", default=None, help="自定义提醒配置 JSON")
    p.set_defaults(func=cmd_reminder)

    p = _add_port(sub.add_parser("send", help="发送一条气泡通知"))
    p.add_argument("title", help="气泡标题")
    p.add_argument("body", nargs="?", default="", help="气泡正文（可选）")
    p.add_argument("--level", choices=Notification.VALID_LEVELS, default="info")
    p.add_argument("--source", default="pika")
    p.add_argument("--ttl", type=float, default=Notification.DEFAULT_TTL)
    p.set_defaults(func=cmd_send)

    p = _add_port(sub.add_parser("history", help="查看总线最近消息"))
    p.add_argument("--n", type=int, default=20)
    p.set_defaults(func=cmd_history)

    _add_port(sub.add_parser("health", help="查看总线健康状态")
              ).set_defaults(func=cmd_health)
    _add_port(sub.add_parser("doctor", help="环境自检")
              ).set_defaults(func=cmd_doctor)

    # 适配器各自注册自己的子命令（参数定义只在适配器里写一次）
    from .adapters import codex, dsh, zcode
    zcode.register(sub)
    codex.register(sub)
    dsh.register(sub)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()
    try:
        return args.func(args)
    except ProtocolError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
