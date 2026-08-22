# -*- coding: utf-8 -*-
"""皮卡丘统一入口：桌宠 / 总线 / 提醒 / 通知。

用法:
    python pikachu.py pet          # 启动桌宠（内嵌总线）
    python pikachu.py bus          # 只启动独立总线
    python pikachu.py reminder     # 启动健康提醒（需要总线在跑）
    python pikachu.py send "标题" "正文" [--level info] [--port 7452]
    python pikachu.py history / health
    python pikachu.py zcode <名称> [--stage start|done|error] [--detail 说明]
    python pikachu.py codex event '<JSON>' / codex report <名称> [--stage ...]
    python pikachu.py dsh run <名称> --cwd <目录> --timeout 420 -- "任务文本"
    python pikachu.py doctor       # 环境自检
"""
import argparse
import os
import sys

from pika.bus import DEFAULT_PORT

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def cmd_doctor(argv):
    import argparse as _ap
    p = _ap.ArgumentParser(prog="pikachu doctor")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args, _ = p.parse_known_args(argv)

    print("皮卡丘环境自检")
    print("-" * 40)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok))
        print(f"{'✅' if ok else '❌'} {name} {detail}")

    import pika
    check("版本", True, pika.__version__)

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

    from pika import bus
    try:
        info = bus.fetch_health(port=args.port, timeout=0.8)
        check("总线", True, f"端口 {info.get('port')}")
    except Exception:
        check("总线", False, "未运行（桌宠内置或独立 bus 未启动）")

    try:
        from pika.win.idle import get_idle_seconds
        v = get_idle_seconds()
        check("空闲检测", True, f"当前空闲 {v:.0f}s")
    except Exception:
        check("空闲检测", False, "不可用")

    print("-" * 40)
    failed = [n for n, ok in checks if not ok]
    if not failed:
        print("全部就绪 ✅")
        return 0
    print("以下项异常（不影响其他功能）：" + "、".join(failed))
    return 1


def build_parser():
    p = argparse.ArgumentParser(prog="pikachu", description="皮卡丘：桌宠+通知总线+健康提醒")
    sub = p.add_subparsers(dest="command", required=True)

    p_pet = sub.add_parser("pet", help="启动桌宠（内嵌总线 + 内嵌健康提醒）")
    p_pet.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_pet.add_argument("--subscribe-only", action="store_true",
                       help="只订阅已有总线，不在本进程开端口")
    p_pet.add_argument("--no-reminder", action="store_true",
                       help="不启动内嵌健康提醒")

    p_bus = sub.add_parser("bus", help="启动独立总线")
    p_bus.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_bus.add_argument("--port-file", default=None)

    p_rem = sub.add_parser("reminder", help="启动健康提醒（需总线在跑）")
    p_rem.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_rem.add_argument("--config", default=None)

    p_doc = sub.add_parser("doctor", help="环境自检")
    p_doc.add_argument("--port", type=int, default=DEFAULT_PORT)

    p_send = sub.add_parser("send", help="发送一条气泡通知")
    p_send.add_argument("title")
    p_send.add_argument("body", nargs="?", default="")
    p_send.add_argument("--level", choices=("info", "success", "warn", "error"),
                        default="info")
    p_send.add_argument("--source", default="pika")
    p_send.add_argument("--ttl", type=float, default=10.0)
    p_send.add_argument("--port", type=int, default=DEFAULT_PORT)

    for name in ("history", "health"):
        sub.add_parser(name, help=f"查看总线{name}").add_argument("--port",
                                                                   type=int,
                                                                   default=DEFAULT_PORT)

    p_z = sub.add_parser("zcode", help="ZCode 自动化通知适配器")
    p_z.add_argument("name")
    p_z.add_argument("--stage", choices=("start", "done", "error", "run"),
                     default="done")
    p_z.add_argument("--detail", default="")
    p_z.add_argument("--port", type=int, default=DEFAULT_PORT)

    # codex / dsh 适配器有自己的子命令（event/report、run/report），
    # 顶层只做透传，参数由适配器自己解析
    sub.add_parser("codex", help="Codex 通知适配器（event/report）") \
       .add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("dsh", help="DSH 子任务包装器（run/report）") \
       .add_argument("rest", nargs=argparse.REMAINDER)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "pet":
        from pika.pet import main as pmain
        return pmain(["--port", str(args.port)] +
                     (["--subscribe-only"] if args.subscribe_only else []) +
                     (["--no-reminder"] if args.no_reminder else []))
    if args.command == "bus":
        from pika.bus import main as bmain
        cmd = ["--port", str(args.port)]
        if args.port_file:
            cmd += ["--port-file", args.port_file]
        return bmain(cmd)
    if args.command == "reminder":
        from pika.reminder_runner import main as rmain
        cmd = ["--port", str(args.port)]
        if args.config:
            cmd += ["--config", args.config]
        return rmain(cmd)
    if args.command == "doctor":
        return cmd_doctor(["--port", str(args.port)])
    if args.command in ("send", "history", "health"):
        from pika.cli import main as climain
        if args.command == "send":
            sub = ["--port", str(args.port), "send", args.title, args.body,
                   "--level", args.level, "--source", args.source,
                   "--ttl", str(args.ttl)]
        else:
            sub = ["--port", str(args.port), args.command]
        return climain(sub)
    if args.command == "zcode":
        from pika.adapters.zcode import main as zmain
        return zmain([args.name,
                      "--stage", args.stage,
                      "--detail", args.detail,
                      "--port", str(args.port)])
    if args.command == "codex":
        from pika.adapters.codex import main as cmain
        return cmain(args.rest)
    if args.command == "dsh":
        from pika.adapters.dsh import main as dmain
        return dmain(args.rest)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
