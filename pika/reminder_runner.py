# -*- coding: utf-8 -*-
"""健康提醒的进程入口：把调度逻辑 + Windows 空闲检测 + 总线发送组装起来。

用法:
    python -m pika.reminder_runner                 # 常驻运行
    python -m pika.reminder_runner --once          # 只跑一步（测试）
    python -m pika.reminder_runner --config 路径   # 自定义配置

Sink 实现（BusSink）POST 到总线；ActivitySource 默认 Windows 空闲检测，
可用 --fake 注入假数据源（测试用，见 tests）。
"""
import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict

from . import bus
from .protocol import Notification
from .reminder import ReminderConfig, ReminderScheduler

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "configs", "reminder.json")

# 配置字段 → (类型, 是否必填)。非法值启动即报错，不在运行时炸。
_FIELD_SPECS = {
    "interval_enabled": (bool, False),
    "interval_min": (float, False),
    "interval_max": (float, False),
    "categories": (list, False),
    "long_session_enabled": (bool, False),
    "long_session_min": (float, False),
    "rest_min": (float, False),
    "long_categories": (list, False),
    "title": (str, False),
}


class BusSink:
    """把提醒发到总线（气泡）。port 可自动发现。"""

    def __init__(self, port: int = None):
        self.port = port or bus.DEFAULT_PORT

    def send(self, title, body, level="info", source="reminder"):
        n = Notification(title=title, body=body, level=level, source=source)
        bus.send_notification(n, port=self.port)


def _coerce(k: str, v):
    typ, _required = _FIELD_SPECS.get(k, (None, False))
    if typ is None:
        return v
    if typ is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"配置 {k} 必须是数字，收到 {v!r}")
        v = float(v)
        if not math.isfinite(v):
            raise ValueError(f"配置 {k} 必须是有限数字")
        if k in ("interval_min", "interval_max", "long_session_min", "rest_min") \
                and v < 0:
            raise ValueError(f"配置 {k} 不能为负数，收到 {v}")
    elif typ is list:
        if not isinstance(v, list):
            raise ValueError(f"配置 {k} 必须是数组，收到 {v!r}")
    elif typ is bool and not isinstance(v, bool):
        raise ValueError(f"配置 {k} 必须是布尔值，收到 {v!r}")
    elif typ is str and not isinstance(v, str):
        raise ValueError(f"配置 {k} 必须是字符串，收到 {v!r}")
    return v


def _validate_relations(cfg: ReminderConfig):
    """跨字段约束：区间不倒挂、阈值非零、分类非空且必须存在。"""
    if cfg.interval_max < cfg.interval_min:
        raise ValueError(
            f"interval_max ({cfg.interval_max}) 不能小于 interval_min "
            f"({cfg.interval_min})")
    if cfg.interval_min <= 0:
        raise ValueError("interval_min 必须 > 0（否则每秒提醒风暴）")
    if cfg.long_session_min <= 0:
        raise ValueError("long_session_min 必须 > 0")
    if cfg.rest_min <= 0:
        raise ValueError("rest_min 必须 > 0（否则久坐通道永远清零）")
    from .reminder_phrases import PHRASES
    for field in ("categories", "long_categories"):
        cats = getattr(cfg, field)
        if not cats:
            raise ValueError(f"{field} 不能为空数组")
        if len(cats) != len(set(cats)):
            raise ValueError(f"{field} 含重复分类")
        unknown = [c for c in cats if c not in PHRASES]
        if unknown:
            raise ValueError(
                f"{field} 含未知分类 {unknown}，可选：{sorted(PHRASES)}")


def load_config(path: str = None) -> ReminderConfig:
    path = path or DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("配置必须是 JSON 对象")
    cfg = ReminderConfig()
    for k, v in d.items():
        if not hasattr(cfg, k) or k.startswith("_"):
            continue
        setattr(cfg, k, _coerce(k, v))
    _validate_relations(cfg)
    return cfg


class FakeActivitySource:
    """测试/演示用假数据源：从 JSON 文件读 idle_minutes。"""

    def __init__(self, path: str):
        self.path = path
        self._cache = None
        self._mtime = 0.0

    def _read(self):
        try:
            mtime = os.path.getmtime(self.path)
            if mtime != self._mtime:
                with open(self.path, encoding="utf-8") as f:
                    self._cache = json.load(f)
                self._mtime = mtime
        except OSError:
            pass
        return self._cache or {}

    def idle_minutes(self, now: float) -> float:
        return float(self._read().get("idle_minutes", 0))


def run(once: bool = False, config: str = None, fake: str = None,
        port: int = None, interval: float = 1.0, max_retries: int = 5,
        once_steps: int = 20):
    cfg = load_config(config)
    activity = FakeActivitySource(fake) if fake else _real_activity()
    sink = BusSink(port=port)
    scheduler = ReminderScheduler(activity=activity, sink=sink, config=cfg)
    fail_streak = 0
    step_count = 0
    while True:
        step_count += 1
        try:
            scheduler.step()
        except Exception as e:
            # 数据源/配置异常：退避重试，不让常驻进程整体退出
            fail_streak += 1
            if once and fail_streak >= max_retries:
                print(f"提醒器异常：{e}", file=sys.stderr)
                return 1
            time.sleep(interval * 5)
            continue
        if not scheduler.last_send_ok:
            # 发送失败（如总线不可达）：退避重试；--once 达上限非零退出
            fail_streak += 1
            if once and fail_streak >= max_retries:
                print("提醒发送失败（总线不可达？），已重试多次放弃",
                      file=sys.stderr)
                return 1
            time.sleep(interval * 5)
            continue
        fail_streak = 0
        if once:
            # --once 语义：跑一次完整调度（推进到首次触发或达到步数上限）
            if scheduler.sent or step_count >= once_steps:
                return 0 if scheduler.sent else 1
            time.sleep(interval)
            continue
        time.sleep(interval)


def _real_activity():
    from .win.idle import WinIdleSource
    return WinIdleSource()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pika-reminder",
                                     description="皮卡丘健康提醒")
    parser.add_argument("--once", action="store_true", help="只跑一步后退出")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--fake", default=None,
                        help="假数据源 JSON 路径（测试用）")
    parser.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        return run(once=args.once, config=args.config, fake=args.fake,
                   port=args.port, interval=args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
