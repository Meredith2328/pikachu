# -*- coding: utf-8 -*-
"""消息协议：总线、桌宠、提醒器之间唯一的通信载体。

一条 Notification 就是一个气泡的所有信息。协议只依赖标准库，任何语言/软件
只要能 POST 一个 JSON 到总线就能通知桌宠。

字段说明：
- title: 必填，气泡主标题（一行）
- body: 可选，补充说明（可换行）
- level: info / success / warn / error，决定气泡配色
- source: 来源标识，如 reminder / zcode / 任意软件名
- ttl: 气泡自动消失的秒数，0 表示常驻直到点击
- ts: 发送方时间戳（epoch 秒），不填由总线补齐
"""
import json
import math
import time
from dataclasses import dataclass, field, asdict

PROTOCOL_VERSION = 1

VALID_LEVELS = ("info", "success", "warn", "error")

DEFAULT_TTL = 10.0


class ProtocolError(ValueError):
    """消息不符合协议（缺字段 / 类型错误 / 非法取值）。"""


@dataclass
class Notification:
    VALID_LEVELS = VALID_LEVELS
    DEFAULT_TTL = DEFAULT_TTL

    title: str
    body: str = ""
    level: str = "info"
    source: str = "pika"
    ttl: float = DEFAULT_TTL
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Notification":
        if not isinstance(d, dict):
            raise ProtocolError("消息必须是 JSON 对象")
        title = d.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ProtocolError("title 必填且不能为空字符串")
        body = d.get("body", "")
        if not isinstance(body, str):
            raise ProtocolError("body 必须是字符串")
        level = d.get("level", "info")
        if level not in VALID_LEVELS:
            raise ProtocolError(f"level 必须是 {'/'.join(VALID_LEVELS)} 之一，收到 {level!r}")
        source = d.get("source", "pika")
        if not isinstance(source, str):
            raise ProtocolError("source 必须是字符串")
        ttl = d.get("ttl", DEFAULT_TTL)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ProtocolError("ttl 必须是数字")
        ttl = float(ttl)
        if not math.isfinite(ttl) or ttl < 0:
            raise ProtocolError("ttl 必须是有限的非负数")
        ts = d.get("ts", time.time())
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise ProtocolError("ts 必须是数字")
        ts = float(ts)
        if not math.isfinite(ts):
            raise ProtocolError("ts 必须是有限数字")
        return cls(title=title.strip(), body=body, level=level,
                   source=source, ttl=ttl, ts=ts)

    @classmethod
    def from_json(cls, s: str) -> "Notification":
        try:
            d = json.loads(s)
        except json.JSONDecodeError as e:
            raise ProtocolError(f"JSON 解析失败: {e}") from e
        return cls.from_dict(d)
