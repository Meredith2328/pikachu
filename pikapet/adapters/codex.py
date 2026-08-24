# -*- coding: utf-8 -*-
"""adapter: codex → 皮卡丘总线的适配器。

两种用法：

1. 事件模式（Codex notify / hooks 调用，收 agent-turn-complete 事件）：
     python -m pikapet.adapters.codex event '<JSON>'      # JSON 作为参数
     echo '<JSON>' | python -m pikapet.adapters.codex event   # 或从 stdin 读
   Codex 每轮回复完成时会带着事件 JSON 调用本脚本，弹气泡显示
   「这轮问了什么 + 回答开头一小段」。

2. 报告模式（Codex automations 提示词里约定调用，与 zcode 适配器同构）：
     python -m pikapet.adapters.codex report "每日简报" --stage start
     python -m pikapet.adapters.codex report "每日简报" --stage done --detail "生成 3 个文件"

不带子命令时自动判别：首参以 `{` 开头按事件处理，否则按报告处理。

本适配器只依赖 pikapet.bus / pikapet.protocol，对 codex 无任何代码依赖——
是 codex 那侧（notify 配置 / hooks 配置 / 自动化提示词）来调用我们。

退出码：0 成功或已忽略；2 参数/负载错误；3 总线不可达（事件模式下
仍返回 0，通知钩子绝不能阻塞 Codex）。
"""
import argparse
import json
import os
import sys
from pathlib import Path

from .. import bus
from ..protocol import Notification
from .common import collapse, stage_level, stage_title

TRANSCRIPT_TAIL_BYTES = 256 * 1024   # 从转录尾部回溯读取的窗口

EVENT_TURN_DONE = "agent-turn-complete"
# Codex 0.147+ 的 hooks 事件名（payload 里是 hook_event_name）。
# 只有"这一轮结束了"这两个值该弹泡；其余事件安静忽略。
HOOK_EVENTS_DONE = ("Stop", "SubagentStop")
SNIPPET_LEN = 160          # 事件正文摘要长度
TITLE_LEN = 48             # 标题里用户输入摘要长度

# 事件负载字段链：不同版本/接入方式的键名不完全一致，按序取第一个非空
TYPE_KEYS = ("type", "event", "hook_event_name")
TITLE_KEYS = ("thread-name", "thread_title", "title", "name")
BODY_KEYS = ("last_assistant_message", "last-assistant-message",
             "lastAssistantMessage", "response", "message")


def _is_turn_done(etype: str) -> bool:
    """这个事件类型代表"一轮结束"吗？

    两套接入方式的事件名不一样：notify 走 `agent-turn-complete`，
    hooks 走 `Stop` / `SubagentStop`（Codex 0.147 起）。都要认。
    """
    if not etype:
        return True          # 没带类型：按老行为当作 turn-complete
    if EVENT_TURN_DONE in etype:
        return True
    return etype in HOOK_EVENTS_DONE


def parse_event(payload):
    """从事件 JSON 提取 (title, body, level)；非"一轮结束"返回 None。"""
    if not isinstance(payload, dict):
        return None
    etype = next((str(payload[k]) for k in TYPE_KEYS if payload.get(k)), "")
    if not _is_turn_done(etype):
        return None  # 其他事件类型（PreToolUse 等）：安静忽略
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
        # hooks 的 Stop 负载没有会话名，也没有用户输入——用工作目录名兜底
        # （和 zcode 钩子的兜底链一致，至少能看出是哪个项目）
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            title = collapse(Path(cwd).name or cwd, TITLE_LEN)
    if not title:
        title = "Codex 回复完成"
    body = next((collapse(payload[k], SNIPPET_LEN) for k in BODY_KEYS
                 if payload.get(k)), "")
    if not body:
        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp:
            body = collapse(_last_assistant_from_transcript(tp), SNIPPET_LEN)
    return title, body, "success"


def _last_assistant_from_transcript(path: str) -> str:
    """从 JSONL 转录尾部取最后一条 assistant 文本。

    hooks 的 Stop 负载里 last_assistant_message 可能是 null（例如这一轮
    以工具调用收尾），此时退到转录文件，气泡才不会只有一句"回复完成"。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(-TRANSCRIPT_TAIL_BYTES, os.SEEK_END)
            data = f.read(TRANSCRIPT_TAIL_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(data.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue      # 尾部截断的半条 JSON：预期情况，跳过
        text = _text_of(obj)
        if text:
            return text
    return ""


def _text_of(obj) -> str:
    """从一条转录记录里取 assistant 文本（兼容几种嵌套形状）。"""
    if not isinstance(obj, dict):
        return ""
    if obj.get("type") not in (None, "assistant", "message", "response_item"):
        return ""
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if msg.get("role") not in (None, "assistant"):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") in
                 ("text", "output_text") and isinstance(c.get("text"), str)]
        return "\n".join(p for p in parts if p).strip()
    return ""


def read_payload(argv_payload):
    """事件 JSON 来源：命令行参数优先，其次 stdin。"""
    raw = (argv_payload or "").strip()
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("事件负载为空（既无参数也无 stdin）")
    return json.loads(raw)


def run_report(args) -> int:
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


def run_event(args) -> int:
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


def add_subcommands(sub):
    """挂 event / report 两个模式。顶层 CLI 与独立入口共用这一处定义。"""
    p_ev = sub.add_parser("event", help="处理 notify/hooks 事件 JSON")
    p_ev.add_argument("payload", nargs="?", default="",
                      help="事件 JSON（缺省从 stdin 读）")
    p_ev.add_argument("--source", default="codex")
    p_ev.add_argument("--ttl", type=float, default=15.0)
    p_ev.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    p_ev.set_defaults(func=run_event)

    p_rp = sub.add_parser("report", help="自动化阶段报告（与 zcode 适配器同构）")
    p_rp.add_argument("name", help="任务名称")
    p_rp.add_argument("--stage", choices=("start", "done", "error", "run"),
                      default="done")
    p_rp.add_argument("--detail", default="")
    p_rp.add_argument("--level", choices=Notification.VALID_LEVELS, default=None)
    p_rp.add_argument("--source", default="codex")
    p_rp.add_argument("--ttl", type=float, default=15.0)
    p_rp.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    p_rp.set_defaults(func=run_report)


def register(sub):
    """注册 `pikachu codex event|report` 子命令。"""
    p = sub.add_parser("codex", help="Codex 通知适配器（event/report）")
    add_subcommands(p.add_subparsers(dest="mode", required=True))
    return p


def normalize_argv(argv):
    """无子命令时自动判别：`{` 开头是事件 JSON，否则是报告。

    Codex 的 notify 配置直接把事件 JSON 作为首参传进来，没有子命令词。"""
    argv = list(argv)
    if argv and argv[0] not in ("event", "report"):
        argv.insert(0, "event" if argv[0].lstrip().startswith("{") else "report")
    return argv


def main(argv=None) -> int:
    argv = normalize_argv(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="pikapet-adapter-codex", description="Codex → 皮卡丘通知适配器")
    add_subcommands(parser.add_subparsers(dest="mode", required=True))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
