# -*- coding: utf-8 -*-
"""健康提醒调度器：纯逻辑、平台无关，所有系统 API 走注入接口。

两个提醒通道（各自独立可配）：
- interval: 每隔 interval_min~interval_max 分钟的随机时刻提醒一次
  （默认 60~120 分钟，即"每一两个小时随机提醒"）；
- long_session: 连续工作（期间没有一次休息达到 rest_min 分钟）累计超过
  long_session_min 分钟时提醒一次，发完重置计时，直到再次出现一次足够长的
  休息才重新武装。

"是否在休息"由 ActivitySource.idle_minutes 判定（平台实现用键盘/鼠标空闲
时间），累计逻辑放在本调度器（纯逻辑、可测），数据源只提供原始查询。

发送走 Sink 接口：桌面实现发到总线（气泡），也可换日志/终端等实现。
"""
import random
import time
from dataclasses import dataclass

from .logs import get_logger
from .reminder_phrases import pick_by_config

log = get_logger("reminder")

# 用户空闲超过该分钟数，interval 提醒顺延（人不在，不打扰）
IDLE_DEFER_MIN = 30.0


@dataclass
class ReminderConfig:
    interval_enabled: bool = True
    interval_min: float = 60.0
    interval_max: float = 120.0
    categories: tuple = ("eye", "neck", "water", "stand", "screen", "walk",
                          "posture")

    long_session_enabled: bool = True
    long_session_min: float = 90.0
    rest_min: float = 5.0
    long_categories: tuple = ("stand", "neck")

    title: str = "该休息一下了"


class ActivitySource:
    """判定"用户是否在休息"。平台实现负责提供数据。"""

    def idle_minutes(self, now: float) -> float:
        """返回 now 之前已连续空闲的分钟数。"""
        raise NotImplementedError


class Sink:
    """通知发送出口。桌面实现 = POST 到总线。"""

    def send(self, title: str, body: str, level: str = "info", source: str = "reminder"):
        raise NotImplementedError


class _Clock:
    """测试用假时钟：可 set/advance，构造时注入。生产用 time.time。"""

    def __init__(self):
        self._t = 0.0

    def set(self, t: float):
        self._t = t

    def advance(self, dt: float):
        self._t += dt

    def __call__(self) -> float:
        return self._t


class ReminderScheduler:
    def __init__(self, activity: ActivitySource, sink: Sink,
                 config: ReminderConfig = None, rng: random.Random = None,
                 clock=None, phrases=None):
        self.activity = activity
        self.sink = sink
        self.config = config or ReminderConfig()
        self.rng = rng or random.Random()
        self.clock = clock or time.time  # 生产默认真实时间；测试注入 _Clock
        self.phrases = phrases or pick_by_config

        # interval 通道
        self._next_interval_at = self.clock() + self._roll_interval()
        # long_session 通道 / interval 通道 状态
        self._active_accum = 0.0   # 当前连续工作累计秒数
        self._long_fired = False   # 本轮久坐提醒是否已发（等休息后重置）
        self._last_step = None     # 上次 step 的时刻（算 dt 用）
        self._prev_idle = 0.0      # 上一步的 idle，用于识别"休息开始"
        self.last_send_ok = True   # 最近一次发送是否成功（供上层退避/退出判断）

        self.sent = []  # 测试用：已发送 (ts, title, body)

    # ---- 主循环 ----
    def step(self, now: float = None) -> list:
        """推进一个调度周期。返回本次发送的通知列表（测试断言用）。

        发送失败（Sink 抛异常）时：不提交状态、不推进下次时间，返回 []，
        由上层（reminder_runner）退避重试。
        """
        if now is None:
            now = self.clock()
        sent_now = []

        idle_min = self.activity.idle_minutes(now)
        if self._last_step is None:
            self._last_step = now
            self._prev_idle = idle_min
            return sent_now

        dt = max(0.0, now - self._last_step)
        threshold = self.config.long_session_min * 60.0

        # ---- long_session 判定放在累计之前：休息期间（idle 增长但未到
        # rest_min）不会因"累计跨过阈值"而误触发 ----
        # 额外条件：idle 相对上一步出现增长（rest 刚开始）时冻结触发，
        # 避免"工作 89 分钟后休息，休息头几分钟累计触顶"的误判
        idle_growing = idle_min > self._prev_idle
        fire_long = (self.config.long_session_enabled and not self._long_fired
                     and self._active_accum >= threshold and not idle_growing)

        if idle_min >= self.config.rest_min:
            self._active_accum = 0.0
            self._long_fired = False
        else:
            # 封顶累计：工作到阈值后不再涨，避免休息临界点误判
            self._active_accum = min(self._active_accum + dt, threshold)
        self._last_step = now
        self._prev_idle = idle_min

        # ---- interval 通道 ----
        fire_interval = False
        new_interval_at = None
        if self.config.interval_enabled and now >= self._next_interval_at:
            if idle_min >= IDLE_DEFER_MIN:
                # 人不在：顺延到下一轮再掷（不打扰）
                self._next_interval_at = now + self._roll_interval()
            else:
                fire_interval = True
                new_interval_at = now + self._roll_interval()

        # 本步要发的内容（long 与 interval 可能同时命中，合并为一条）
        pending = []
        if fire_long:
            pending.append(self._pick(self.config.long_categories))
        if fire_interval:
            pending.append(self._pick(self.config.categories))
        if pending:
            body = "；".join(dict.fromkeys(pending))  # 合并 + 去重
            if self._try_send(now, body, sent_now):
                # 发送成功才提交状态（失败由上层退避重试，状态保持原样）
                if fire_long:
                    self._long_fired = True
                    self._active_accum = 0.0
                if new_interval_at is not None:
                    self._next_interval_at = new_interval_at
        return sent_now

    # ---- 手动触发 ----
    def manual_body(self) -> str:
        """给"用户主动要一条提醒"用的正文：从 interval 通道的文案池里挑。

        不碰任何调度状态（不重置久坐累计、也不推进下次定时时刻）——用户
        点一下菜单不该影响自动提醒的节奏。
        """
        return self._pick(self.config.categories)

    # ---- 内部 ----
    def _try_send(self, now: float, body: str, sent_now: list) -> bool:
        """发一条提醒。失败返回 False 交给上层退避重试，并记 WARNING。

        Sink 的实现可能是 HTTP 投递（总线不可达）也可能是队列（队列满），
        异常类型不确定，所以这里捕获 Exception；但绝不静默——发不出去的
        提醒就是没提醒，用户需要能查到原因。"""
        try:
            self.sink.send(self.config.title, body, source="reminder")
        except Exception as e:
            self.last_send_ok = False
            log.warning("提醒发送失败（将退避重试）：%s", e, exc_info=True)
            return False
        self.last_send_ok = True
        self.sent.append((now, self.config.title, body))
        sent_now.append((now, self.config.title, body))
        return True

    def _pick(self, categories):
        return self.phrases(categories, rng=self.rng)

    def _roll_interval(self) -> float:
        c = self.config
        lo, hi = c.interval_min, c.interval_max
        if hi <= lo:
            return lo * 60.0  # 等值/倒挂都按"固定间隔分钟"处理
        return self.rng.uniform(lo, hi) * 60.0
