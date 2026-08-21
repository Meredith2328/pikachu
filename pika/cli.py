# -*- coding: utf-8 -*-
"""命令行客户端：任何软件一行命令就能给桌宠发气泡。

用法:
    python -m pika.cli send "标题" "正文" [--level info|success|warn|error]
                                  [--source 名字] [--ttl 秒] [--port 8765]
    python -m pika.cli history [--n 20] [--port 8765]
    python -m pika.cli health [--port 8765]

返回码：0 成功；1 发送失败（如总线没在跑）。
"""
import argparse
import json
import sys

from . import bus
from .protocol import Notification, ProtocolError


def send(args) -> int:
    # 先按协议校验（ttl 负数/NaN 等在客户端就报错，而不是等总线 400）
    try:
        notif = Notification.from_dict({"title": args.title, "body": args.body or "",
                                        "level": args.level, "source": args.source,
                                        "ttl": args.ttl})
    except ProtocolError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return 1
    try:
        resp = bus.send_notification(notif, port=args.port)
    except Exception as e:
        print(f"发送失败：{e}", file=sys.stderr)
        return 1
    print(json.dumps(resp, ensure_ascii=False))
    return 0


def history(args) -> int:
    try:
        items = bus.fetch_history(n=args.n, port=args.port)
    except Exception as e:
        print(f"获取历史失败：{e}", file=sys.stderr)
        return 1
    for it in items:
        line = f"[{it.get('source', '?')}] {it.get('title', '')}"
        if it.get("body"):
            line += f" — {it['body']}"
        print(line)
    return 0


def health(args) -> int:
    try:
        info = bus.fetch_health(port=args.port)
    except Exception as e:
        print(f"总线未响应：{e}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pika-notify",
                                     description="皮卡丘通知客户端")
    parser.add_argument("--port", type=int, default=bus.DEFAULT_PORT,
                        help=f"总线端口（默认 {bus.DEFAULT_PORT}）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="发送一条气泡通知")
    p_send.add_argument("title", help="气泡标题")
    p_send.add_argument("body", nargs="?", default="", help="气泡正文（可选）")
    p_send.add_argument("--level", choices=Notification.VALID_LEVELS, default="info")
    p_send.add_argument("--source", default="pika")
    p_send.add_argument("--ttl", type=float, default=Notification.DEFAULT_TTL)
    p_send.set_defaults(func=send)

    p_hist = sub.add_parser("history", help="查看总线最近消息")
    p_hist.add_argument("--n", type=int, default=20)
    p_hist.set_defaults(func=history)

    p_health = sub.add_parser("health", help="查看总线健康状态")
    p_health.set_defaults(func=health)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ProtocolError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
