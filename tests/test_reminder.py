import sys
import os
import random
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.reminder import ReminderConfig, ReminderScheduler, _Clock
from pikapet.reminder_phrases import PHRASES, pick, pick_by_config, Phrase


class FakeActivity:
    """FakeActivitySource 的同步版：直接控制 idle_minutes。"""

    def __init__(self):
        self.idle = 0.0

    def idle_minutes(self, now):
        return self.idle


class RecordingSink:
    def __init__(self):
        self.messages = []

    def send(self, title, body, level="info", source="reminder"):
        self.messages.append((title, body, level, source))


class BrokenSink(RecordingSink):
    """发送抛异常的 Sink：测发送失败不推进状态。"""

    def __init__(self):
        super().__init__()
        self.fail = False

    def send(self, title, body, level="info", source="reminder"):
        if self.fail:
            raise ConnectionError("bus down")
        super().send(title, body, level=level, source=source)


def make_scheduler(**kw):
    cfg = ReminderConfig(**kw)
    return ReminderScheduler(FakeActivity(), RecordingSink(), config=cfg,
                             rng=random.Random(42), clock=_Clock())


class TestPhrases(unittest.TestCase):
    def test_pick_returns_from_category(self):
        rng = random.Random(1)
        for cat in PHRASES:
            texts = [p.text for p in PHRASES[cat]]
            for _ in range(50):
                self.assertIn(pick(cat, rng=rng), texts)

    def test_pick_unknown_category(self):
        with self.assertRaises(KeyError):
            pick("nope")

    def test_pick_by_config_uses_only_given_categories(self):
        rng = random.Random(2)
        pool = {"a": [Phrase("only-a")]}
        for _ in range(30):
            self.assertEqual(pick_by_config(["a"], pool=pool, rng=rng), "only-a")


class TestIntervalChannel(unittest.TestCase):
    def test_interval_fires_once_within_range(self):
        s = make_scheduler(interval_min=60, interval_max=60,
                           long_session_enabled=False)
        out = s.step()
        self.assertEqual(out, [])  # 起点未到
        s.clock.advance(7200)       # 推到 [min,max] 上界之后
        out = s.step()
        self.assertEqual(len(out), 1)
        # 触发后重掷下一轮，同一时刻不再发
        out2 = s.step()
        self.assertEqual(out2, [])

    def test_interval_disabled(self):
        s = make_scheduler(interval_enabled=False, long_session_enabled=False)
        s.clock.advance(100000)
        self.assertEqual(s.step(), [])

    def test_interval_randomness_uses_uniform(self):
        hits = []
        for seed in range(20):
            cfg = ReminderConfig(interval_min=60, interval_max=120,
                                 long_session_enabled=False)
            s = ReminderScheduler(FakeActivity(), RecordingSink(), config=cfg,
                                  rng=random.Random(seed), clock=_Clock())
            s._next_interval_at = s.clock() + s._roll_interval()
            hits.append(s._next_interval_at)
        self.assertTrue(all(3600 <= h <= 7200 for h in hits), hits)
        self.assertGreater(len(set(round(h, 3) for h in hits)), 1)

    def test_interval_equal_bounds_is_minutes(self):
        """interval_min == interval_max（固定间隔）：必须按分钟换算成秒。"""
        for seed in range(10):
            cfg = ReminderConfig(interval_min=90, interval_max=90,
                                 long_session_enabled=False)
            s = ReminderScheduler(FakeActivity(), RecordingSink(), config=cfg,
                                  rng=random.Random(seed), clock=_Clock())
            self.assertEqual(s._roll_interval(), 90 * 60.0)

    def test_interval_defers_when_idle_long(self):
        """人长时间空闲：interval 提醒顺延，不打扰。"""
        s = make_scheduler(interval_min=60, interval_max=60,
                           long_session_enabled=False)
        s.activity.idle = 45.0  # 空闲超过 30 分钟
        s.clock.advance(7200)
        out = s.step()
        self.assertEqual(out, [])  # 不发，顺延
        # 恢复活跃，下一轮（等一个 interval 长度）触发
        s.activity.idle = 0.0
        s.clock.advance(3601)
        out = s.step()
        self.assertEqual(len(out), 1)


class TestLongSessionChannel(unittest.TestCase):
    def test_fires_when_accumulated_long(self):
        """真实装配：默认时钟 + 每步前进，累计超过阈值触发。"""
        s = make_scheduler(long_session_min=90, interval_enabled=False)
        # 模拟 95 分钟的连续工作（每步 1 分钟）
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(s.sent[0][1], s.config.title)
        self.assertTrue(s.sent[0][2])

    def test_does_not_fire_under_threshold(self):
        s = make_scheduler(long_session_min=90, interval_enabled=False)
        for _ in range(89):
            s.clock.advance(60)
            s.step()
        self.assertEqual(s.sent, [])

    def test_only_fires_once_until_rest(self):
        s = make_scheduler(long_session_min=90, interval_enabled=False)
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)
        # 继续工作，不再提醒
        for _ in range(30):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)

    def test_rest_resets_and_refires(self):
        s = make_scheduler(long_session_min=90, rest_min=5,
                           interval_enabled=False)
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)
        # 休息 10 分钟
        s.activity.idle = 10.0
        for _ in range(2):
            s.clock.advance(60)
            s.step()
        s.activity.idle = 0.0
        # 重新累计 95 分钟 → 第二次提醒
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 2)

    def test_short_idle_keeps_accum(self):
        s = make_scheduler(long_session_min=90, rest_min=5,
                           interval_enabled=False)
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)
        # 只休息 1 分钟（不足 rest_min）：累计不清零，不重新武装
        s.activity.idle = 1.0
        for _ in range(2):
            s.clock.advance(60)
            s.step()
        s.activity.idle = 0.0
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)  # 没有第二次

    def test_no_fire_while_resting_at_threshold(self):
        """工作 86 分钟后开始休息：休息期间（未到 rest_min）不得误触发久坐提醒。"""
        s = make_scheduler(long_session_min=90, rest_min=5,
                           interval_enabled=False)
        # 工作 86 分钟
        for _ in range(86):
            s.clock.advance(60)
            s.step()
        self.assertEqual(s.sent, [])
        # 开始休息：idle 逐渐增长（1..4 分钟，未到 rest_min=5）
        s.activity.idle = 1.0
        s.clock.advance(60)
        s.step()
        s.activity.idle = 2.0
        s.clock.advance(60)
        s.step()
        s.activity.idle = 3.0
        s.clock.advance(60)
        s.step()
        s.activity.idle = 4.0
        s.clock.advance(60)
        s.step()
        # 休息期间即使累计到 90 分钟边界，也不该触发（判定在累计之前）
        self.assertEqual(s.sent, [])

    def test_no_fire_resting_near_full_threshold(self):
        """工作 89 分钟（接近阈值）后开始休息：休息头几分钟不得触发。"""
        s = make_scheduler(long_session_min=90, rest_min=5,
                           interval_enabled=False)
        # 工作 89 分钟
        for _ in range(89):
            s.clock.advance(60)
            s.step()
        self.assertEqual(s.sent, [])
        # 休息开始：idle 从 0 转正（增长）→ 触发被冻结
        s.activity.idle = 1.0
        s.clock.advance(60)
        s.step()
        s.activity.idle = 2.0
        s.clock.advance(60)
        s.step()
        s.activity.idle = 3.0
        s.clock.advance(60)
        s.step()
        self.assertEqual(s.sent, [])  # 休息中累计触顶也不触发
        # 恢复工作后重新累计到阈值 → 正常触发
        s.activity.idle = 0.0
        for _ in range(91):
            s.clock.advance(60)
            s.step()
        self.assertEqual(len(s.sent), 1)

    def test_disabled(self):
        s = make_scheduler(long_session_enabled=False, interval_enabled=False)
        s.clock.advance(100000)
        s.activity.idle = 0.0
        self.assertEqual(s.step(), [])


class TestBothChannels(unittest.TestCase):
    def test_both_fire_at_same_step(self):
        s = make_scheduler(interval_min=60, interval_max=60, long_session_min=90)
        s.clock.advance(7200)
        for _ in range(95):
            s.clock.advance(60)
            s.step()
        self.assertGreaterEqual(len(s.sent), 1)  # interval 必然触发

    def test_send_failure_does_not_advance(self):
        """Sink 故障时：状态不推进，重试时能再次尝试发送。"""
        sink = BrokenSink()
        cfg = ReminderConfig(interval_min=60, interval_max=60,
                             long_session_enabled=False)
        s = ReminderScheduler(FakeActivity(), sink, config=cfg,
                              rng=random.Random(1), clock=_Clock())
        s.clock.advance(7200)
        sink.fail = True
        out = s.step()
        self.assertEqual(out, [])
        self.assertEqual(sink.messages, [])
        # 恢复后同一时刻重试应成功（_next_interval_at 未被推进）
        sink.fail = False
        out = s.step()
        self.assertEqual(len(out), 1)
        self.assertEqual(len(sink.messages), 1)


class TestConfigValidation(unittest.TestCase):
    def _load(self, **overrides):
        import json
        import tempfile
        from pathlib import Path
        base = {
            "interval_min": 60, "interval_max": 120,
            "long_session_min": 90, "rest_min": 5,
            "categories": ["eye", "neck"], "long_categories": ["stand"],
        }
        base.update(overrides)
        with tempfile.TemporaryDirectory(prefix="pika-cfg-") as td:
            path = Path(td) / "reminder.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            from pikapet.reminder_runner import load_config
            return load_config(str(path))

    def test_valid_config(self):
        cfg = self._load()
        self.assertEqual(cfg.interval_min, 60)
        self.assertEqual(cfg.long_session_min, 90)

    def test_negative_interval_rejected(self):
        with self.assertRaises(ValueError):
            self._load(interval_min=-30)

    def test_inverted_bounds_rejected(self):
        with self.assertRaises(ValueError):
            self._load(interval_min=120, interval_max=60)

    def test_zero_long_session_rejected(self):
        with self.assertRaises(ValueError):
            self._load(long_session_min=0)

    def test_zero_interval_rejected(self):
        with self.assertRaises(ValueError):
            self._load(interval_min=0)

    def test_zero_rest_min_rejected(self):
        with self.assertRaises(ValueError):
            self._load(rest_min=0)

    def test_empty_categories_rejected(self):
        with self.assertRaises(ValueError):
            self._load(categories=[])

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValueError):
            self._load(categories=["typo"])

    def test_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            self._load(interval_min="abc")


class TestRealClockAssembly(unittest.TestCase):
    def test_default_clock_is_time(self):
        """生产装配（不传 clock）用真实时间：step 的 now 会前进，interval 能触发。"""
        import time as _time
        s = ReminderScheduler(FakeActivity(), RecordingSink(),
                              config=ReminderConfig(interval_min=60, interval_max=60,
                                                    long_session_enabled=False),
                              rng=random.Random(3))
        self.assertIs(s.clock, _time.time)
        # 把下一次触发改到 1 秒后（真实时间推进）
        s._next_interval_at = _time.time() + 1
        deadline = _time.time() + 3
        sent = []
        while _time.time() < deadline:
            sent += s.step()
            _time.sleep(0.2)
            if sent:
                break
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
