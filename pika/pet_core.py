# -*- coding: utf-8 -*-
"""桌宠核心逻辑：与 GUI / Windows 完全解耦，纯逻辑可单测。

职责：消息去重、气泡的显示/隐藏决策、静音、历史与状态文案。
任何界面（tkinter 桌宠、未来其他显示端）都通过 on_show / on_hide 回调
挂进来；界面代码不该重复实现这里的决策逻辑。
"""
import threading
import time
from collections import deque

from .protocol import Notification

INFINITE = float("inf")
# 超过该时长的历史消息视为"陈旧"：SSE 重连/重启回放时不再弹泡（只累计统计）
STALE_WINDOW_SEC = 60.0


class PetController:
    def __init__(self, on_show=None, on_hide=None,
                 dedup_sec: float = 10.0, history_limit: int = 50,
                 stale_window_sec: float = STALE_WINDOW_SEC,
                 clock=time.time):
        self._on_show = on_show or (lambda n: None)
        self._on_hide = on_hide or (lambda: None)
        self.dedup_sec = dedup_sec
        self.stale_window_sec = stale_window_sec
        self.history_limit = history_limit
        self.clock = clock
        self._lock = threading.Lock()
        self._history = deque(maxlen=history_limit)
        self.current = None            # 当前显示中的 Notification
        self._bubble_until = 0.0       # 自动隐藏时间点（INFINITE=常驻）
        self.hovering = False          # 鼠标悬浮中（不自动隐藏）
        self.muted = False             # 静音：记录历史但不弹气泡

    # ---- 消息入口（可从任意线程调用） ----
    def handle(self, n: Notification) -> str:
        """处理一条新消息。返回结果：shown / deduped / muted / stale。"""
        arrived = self.clock()
        # 陈旧消息（发送时间早于当前太多，如重启后的历史回放）只记录不弹泡
        if self.stale_window_sec and arrived - n.ts > self.stale_window_sec:
            with self._lock:
                self._history.append((arrived, n))
            return "stale"
        with self._lock:
            if self._is_duplicate_locked(n, arrived):
                self._history.append((arrived, n))
                return "deduped"
            self._history.append((arrived, n))
            if self.muted:
                return "muted"
            self.current = n
            self._bubble_until = (INFINITE if n.ttl <= 0
                                  else arrived + n.ttl)
            # 新气泡替换旧气泡时重置悬浮标记：
            # 旧气泡的 hover 残留会让新气泡永远不自动消失
            self.hovering = False
            cb = self._on_show
        cb(n)
        return "shown"

    def dismiss(self):
        """手动关掉当前气泡（点击 / 新消息替换时也调用）。"""
        with self._lock:
            if self.current is None:
                return
            self.current = None
            self._bubble_until = 0.0
            # 关闭时清掉悬浮标记：否则下一条消息在悬浮中到达会永远不自动消失
            self.hovering = False
            cb = self._on_hide
        cb()

    def tick(self, now: float = None) -> bool:
        """周期调用（由界面驱动）。返回 True 表示刚把气泡隐藏了。"""
        if now is None:
            now = self.clock()
        with self._lock:
            if (self.current is None or self.hovering
                    or self._bubble_until == INFINITE):
                return False
            if now < self._bubble_until:
                return False
            self.current = None
            cb = self._on_hide
        cb()
        return True

    def set_hover(self, on: bool):
        with self._lock:
            self.hovering = on

    def toggle_mute(self) -> bool:
        """切换静音，返回新的静音状态。静音时照常记录历史。"""
        with self._lock:
            self.muted = not self.muted
            return self.muted

    # ---- 查询 ----
    def recent(self, n: int = 10) -> list:
        with self._lock:
            return [item for _, item in list(self._history)[-n:]]

    def source_stats(self) -> dict:
        with self._lock:
            stats = {}
            for _, item in self._history:
                stats[item.source] = stats.get(item.source, 0) + 1
            return stats

    def status_text(self) -> str:
        """悬浮主宠时的状态气泡文案（也用于右键菜单）。"""
        with self._lock:
            stats = {}
            for _, item in self._history:
                stats[item.source] = stats.get(item.source, 0) + 1
            total = len(self._history)
            parts = [f"⚡ 已接收 {total} 条通知"]
            if self.muted:
                parts.append("🔇 静音中（不弹气泡，仍记录）")
            if stats:
                src = " · ".join(f"{k} {v}" for k, v in sorted(stats.items()))
                parts.append(f"来源：{src}")
            recent = list(self._history)[-20:]
            # 展示层按 (source, title, body) 去重，避免重复消息刷屏
            seen = set()
            unique = []
            for _, item in reversed(recent):
                key = (item.source, item.title, item.body)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(item)
                if len(unique) >= 3:
                    break
            if unique:
                parts.append("最近\n" + "\n".join(
                    f"· {it.source} {it.title}" for it in unique))
            return "\n\n".join(parts)

    # ---- 内部 ----
    def _is_duplicate_locked(self, n: Notification, arrived: float) -> bool:
        """同来源、同内容的消息在 dedup_sec（按到达时间）内重复则跳过。"""
        if self.dedup_sec <= 0:
            return False
        for at, item in self._history:
            if item.source != n.source:
                continue
            if (item.title, item.body) != (n.title, n.body):
                continue
            if abs(at - arrived) <= self.dedup_sec:
                return True
        return False
