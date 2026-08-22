# -*- coding: utf-8 -*-
"""ZCode Stop 钩子 → 皮卡丘气泡：会话完成时汇报会话标题与最新进展开头。

由 ZCode 的 hooks 机制以 process 方式调用（stdin 收到事件 JSON）。设计约束：
- 永远快速退出且 exit 0——任何异常都不能阻塞或报错拖累会话；
- 发送复用 pika.bus.send_notification（本机固定默认端口），桌宠不在时
  静默放弃，不重试不告警；
- 把原始 stdin 追加进 runtime/hook_stdin.log，便于后续核对字段。

标题的取材顺序（人能看懂优先）：
  1. stdin 自带的标题类字段；
  2. 客户端会话库 ~/.zcode/cli/db/db.sqlite 的 session.title（只读查询，
     与界面里显示的标题一致）；
  3. 转录文件里第一条用户消息的开头（会话由什么需求发起）；
  4. 工作目录名 / 会话 ID 前缀兜底。
进展摘录取 stdin 回复字段或转录尾部最后一条 assistant 文本的开头一小段。
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pika.bus import send_notification          # noqa: E402
from pika.protocol import Notification          # noqa: E402

STDIN_LOG = ROOT / "runtime" / "hook_stdin.log"
ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

SNIPPET_LEN = 120
TITLE_LEN = 32
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 256 * 1024

# stdin 载荷里可能存放回复文本/会话 ID/标题的键（不同版本字段名不一，逐个试探）
PAYLOAD_TEXT_KEYS = ("last_response", "last_assistant_message", "responseText",
                     "response", "responsePreview", "lastMessage")
PAYLOAD_SID_KEYS = ("session_id", "sessionId", "conversation_id")
PAYLOAD_TITLE_KEYS = ("title", "session_title", "sessionTitle")


def collapse(text: str, limit: int = SNIPPET_LEN) -> str:
    """压平空白并截到 limit 字符（超出补省略号）。实现收敛在
    pika.adapters.common.collapse，这里保留默认值方便调用点。"""
    from pika.adapters.common import collapse as _collapse
    return _collapse(text, limit)


def text_from_content(content) -> str:
    """Claude 风格 message.content：字符串或 [{type:'text',text},...]。"""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" \
                    and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)


def last_assistant_text(transcript_path: str) -> str:
    """从 JSONL 转录文件尾部取最后一条 assistant 文本消息。"""
    return _scan_transcript(transcript_path, "assistant", TAIL_BYTES,
                            from_end=True)


def first_user_text(transcript_path: str) -> str:
    """从 JSONL 转录文件头部取第一条用户消息文本（会话的发起需求）。"""
    return _scan_transcript(transcript_path, "user", HEAD_BYTES,
                            from_end=False)


def _scan_transcript(path: str, role: str, window: int,
                     from_end: bool) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > window:
                if from_end:
                    f.seek(-window, os.SEEK_END)
                data = f.read(window).decode("utf-8", "replace")
            else:
                data = f.read().decode("utf-8", "replace")
    except Exception:
        return ""
    lines = data.splitlines()
    if from_end:
        lines = reversed(lines)
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != role:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        text = text_from_content(msg.get("content"))
        if text.strip():
            return text
    return ""


def extract_session_id(payload: dict, environ=None) -> str:
    env = environ if environ is not None else os.environ
    for key in PAYLOAD_SID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("CLAUDE_SESSION_ID", "ZCODE_SESSION_ID"):
        value = env.get(key)
        if value:
            return value
    return ""


def extract_snippet(payload: dict) -> str:
    """优先用 stdin 自带的回复字段；否则解析 transcript_path。"""
    for key in PAYLOAD_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    tp = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(tp, str) and tp:
        return last_assistant_text(tp)
    return ""


def session_title_from_db(session_id: str, db_path=None) -> str:
    """按会话 ID 查客户端会话库里的标题（与 ZCode 界面显示一致）。

    只读连接 + 1 秒超时，任何异常都返回空字符串走兜底链。"""
    if not session_id:
        return ""
    db = Path(db_path) if db_path else ZCODE_DB
    if not db.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                              timeout=1.0)
        try:
            row = con.execute("SELECT title FROM session WHERE id = ?",
                              (session_id,)).fetchone()
        finally:
            con.close()
        if row and isinstance(row[0], str) and row[0].strip():
            return row[0].strip()
    except Exception:
        pass
    return ""


def resolve_title(payload: dict) -> str:
    """标题兜底链：payload 字段 → 会话库 → 转录首条用户消息 → 目录名。"""
    sid = extract_session_id(payload)
    for key in PAYLOAD_TITLE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    title = session_title_from_db(sid)
    if title:
        return title
    tp = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(tp, str) and tp:
        text = first_user_text(tp)
        if text.strip():
            return text
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd).name or cwd
    return sid[:8] if sid else ""


def append_stdin_log(event: str, raw: str):
    try:
        STDIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": round(time.time(), 3), "event": event,
                 "raw": raw[:4000]}
        with open(STDIN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ZCode hook → pika pet")
    parser.add_argument("--event", default="Stop")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""
    payload = {}
    stripped = (raw or "").strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except Exception:
            payload = {}
    append_stdin_log(args.event, stripped)

    sid = extract_session_id(payload)
    snippet = collapse(extract_snippet(payload))
    title = collapse(resolve_title(payload), TITLE_LEN)
    title = f"会话完成 · {title}" if title else "会话完成"
    body = snippet or "（未读取到进展内容）"
    try:
        send_notification(
            Notification(title=title, body=body, level="success",
                         source="zcode", ttl=12.0))
    except Exception:
        pass  # 总线不在/网络失败：钩子静默通过
    return 0  # 无论成败都让钩子通过


if __name__ == "__main__":
    sys.exit(main())
