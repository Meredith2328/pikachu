# -*- coding: utf-8 -*-
"""adapter: codex → 皮卡丘总线的适配器。

两种用法：

1. 事件模式（Codex notify / hooks 调用，收 agent-turn-complete 事件）：
     python -m pika.adapters.codex event '<JSON>'      # JSON 作为参数
     echo '<JSON>' | python -m pika.adapters.codex event   # 或从 stdin 读
   Codex 每轮回复完成时会带着事件 JSON 调用本脚本，弹气泡显示
   「这轮问了什么 + 回答开头一小段」。

2. 报告模式（Codex automations 提示词里约定调用，与 zcode 适配器同构）：
     python -m pika.adapters.codex report "每日简报" --stage start
     python -m pika.adapters.codex report "每日简报" --stage done --detail "生成 3 个文件"

不带子命令时自动判别：首参以 `{` 开头按事件处理，否则按报告处理。

本适配器只依赖 pika.bus / pika.protocol，对 codex 无任何代码依赖——
是 codex 那侧（notify 配置 / hooks 配置 / 自动化提示词）来调用我们。

退出码：0 成功或已忽略；2 参数/负载错误；3 总线不可达（事件模式下
仍返回 0，通知钩子绝不能阻塞 Codex）。
"""
import argparse
import json
import sys

from .. import bus
from ..protocol import Notification
from .common import collapse, stage_level, stage_title

EVENT_TURN_DONE = "agent-turn-complete"
SNIPPET_LEN = 160          # 事件正文摘要长度
TITLE_LEN = 48             # 标题里用户输入摘要长度

# 事件负载字段链：不同版本/接入方式的键名不完全一致，按序取第一个非空
TYPE_KEYS = ("type", "event")
TITLE_KEYS = ("thread-name", "thread_title", "title", "name")
BODY_KEYS = ("last_assistant_message", "last-assistant-message",
             "lastAssistantMessage", "response", "message")


def parse_event(payload):
    """从事件 JSON 提取 (title, body, level)；非 turn-complete 返回 None。"""
    if not isinstance(payload, dict):
        return None
    etype = next((str(payload[k]) for k in TYPE_KEYS if payload.get(k)), "")
    if etype and EVENT_TURN_DONE not in etype:
        return None  # 未来新增的事件类型：安静忽略
    title = next((collapse(payload[k], TITLE_LEN) for k in TITLE_KEYS
                  if payload.get(k)), "")
    if not title:
        msgs = payload.get("input_messages") or []
        if isinstance(msgs, list):
            for m in msgs:
                head = collapse(m, TITLE_LEN)
                if head:
                    title = head
                    break
    if not title:
        title = "Codex 回复完成"
    body = next((collapse(payload[k], SNIPPET_LEN) for k in BODY_KEYS
                 if payload.get(k)), "")
    return title, body, "success"


def read_payload(argv_payload):
    """事件 JSON 来源：命令行参数优先，其次 stdin。"""
    raw = (argv_payload or "").strip()
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("事件负载为空（既无参数也无 stdin）")
    return json.loads(raw)


def _report_main(args) -> int:
    level = args.level or stage_level(args.stage)
    n = Notification(title=stage_title(args.stage, args.name),
                     body=args.detail or "", level=level,
                     source=args.source, ttl=args.ttl)
    try:
        resp = bus.send_notification(n, port=args.port)
    except Exception as e:
        print(f"总线不可达：{e}", file=sys.stderr)
        return 3
    print(json.dumps(resp, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 无子命令时的自动判别：{ 开头是事件 JSON，否则是报告
    if argv and argv[0] not in ("event", "report"):
        argv.insert(0, "event" if argv[0].lstrip().startswith("{") else "report")

    parser = argparse.ArgumentParser(
        prog="pika-adapter-codex", description="Codex → 皮卡丘通知适配器")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_ev = sub.add_parser("event", help="处理 notify/hooks 事件 JSON")
    p_ev.add_argument("payload", nargs="?", default="",
                      help="事件 JSON（缺省从 stdin 读）")
    p_ev.add_argument("--source", default="codex")
    p_ev.add_argument("--ttl", type=float, default=15.0)
    p_ev.add_argument("--port", type=int, default=bus.DEFAULT_PORT)

    p_rp = sub.add_parser("report", help="自动化阶段报告（与 zcode 适配器同构）")
    p_rp.add_argument("name", help="任务名称")
    p_rp.add_argument("--stage", choices=("start", "done", "error", "run"),
                      default="done")
    p_rp.add_argument("--detail", default="")
    p_rp.add_argument("--level", choices=Notification.VALID_LEVELS, default=None)
    p_rp.add_argument("--source", default="codex")
    p_rp.add_argument("--ttl", type=float, default=15.0)
    p_rp.add_argument("--port", type=int, default=bus.DEFAULT_PORT)

    args = parser.parse_args(argv)

    if args.mode == "report":
        return _report_main(args)

    try:
        payload = read_payload(args.payload)
    except ValueError as e:
        print(f"事件负载错误：{e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"事件 JSON 解析失败：{e}", file=sys.stderr)
        return 2
    parsed = parse_event(payload)
    if parsed is None:
        return 0  # 非 turn-complete 事件：忽略
    title, body, level = parsed
    # 与 zcode Stop 钩子的「会话完成 · 标题」同构，来源走 meta 行
    n = Notification(title=f"会话完成 · {title}", body=body or "回复完成",
                     level=level, source=args.source, ttl=args.ttl)
    try:
        resp = bus.send_notification(n, port=args.port)
        print(json.dumps(resp, ensure_ascii=False))
    except Exception as e:
        # 通知钩子绝不能阻塞 Codex：失败只记 stderr，返回 0
        print(f"总线不可达：{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
