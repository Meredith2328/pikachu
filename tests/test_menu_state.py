# -*- coding: utf-8 -*-
"""右键菜单与手动提醒：菜单文案要反映当前状态，手动提醒要走真实文案池。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.reminder import ReminderConfig, ReminderScheduler
from pikapet.reminder_phrases import PHRASES
from tests.helpers import gui_available, isolated_home


class TestSchedulerManualBody(unittest.TestCase):
    """manual_body 从 interval 通道文案池取，且不动调度状态。"""

    def _scheduler(self, **kw):
        class _Idle:
            def idle_minutes(self, now):
                return 0.0

        class _Sink:
            def send(self, *a, **k):
                pass

        return ReminderScheduler(_Idle(), _Sink(),
                                 config=ReminderConfig(**kw))

    def test_body_comes_from_configured_categories(self):
        s = self._scheduler(categories=("water",))
        pool = {p.text for p in PHRASES["water"]}
        for _ in range(20):
            self.assertIn(s.manual_body(), pool)

    def test_does_not_touch_schedule_state(self):
        """点一下菜单不该影响自动提醒的节奏。"""
        s = self._scheduler()
        before = (s._next_interval_at, s._active_accum, s._long_fired)
        s.manual_body()
        self.assertEqual((s._next_interval_at, s._active_accum,
                          s._long_fired), before)

    def test_does_not_count_as_sent(self):
        """手动文案不经过 sink，也不该进 sent 记录。"""
        s = self._scheduler()
        s.manual_body()
        self.assertEqual(s.sent, [])


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestMenuReflectsState(unittest.TestCase):
    def _pet(self, with_reminder=False):
        from pikapet.pet import PikaPet
        return PikaPet(port=0, subscribe_only=True,
                       with_reminder=with_reminder)

    def _labels(self, pet):
        """取右键菜单的条目文案（弹一下再关，不留窗口）。"""
        evt = type("E", (), {"x_root": 100, "y_root": 100})()
        pet._menu(evt)
        try:
            return [label for label, _cmd in pet.menu.items]
        finally:
            pet.menu.close()

    def test_mute_label_shows_current_state(self):
        """以前一律显示"静音开关"，看不出此刻是开还是关。"""
        with isolated_home():
            pet = self._pet()
            try:
                pet._controller.muted = False
                self.assertIn("静音", self._labels(pet))
                self.assertNotIn("取消静音", self._labels(pet))

                pet._controller.muted = True
                self.assertIn("取消静音", self._labels(pet))
            finally:
                pet._quit()

    def test_manual_remind_uses_scheduler_phrases(self):
        with isolated_home():
            pet = self._pet(with_reminder=True)
            try:
                if pet._scheduler is None:
                    self.skipTest("本环境提醒调度未启动")
                cats = pet._scheduler.config.categories
                pool = {p.text for cat in cats for p in PHRASES[cat]}
                pet._manual_remind()
                last = pet._controller.recent()[-1]
                self.assertEqual(last.source, "reminder")
                self.assertEqual(last.level, "warn")
                self.assertIn(last.body, pool)
            finally:
                pet._quit()

    def test_manual_remind_without_scheduler_explains_itself(self):
        """--no-reminder 时仍能弹一条，但要说明调度未启用，不假装正常。"""
        with isolated_home():
            pet = self._pet(with_reminder=False)
            try:
                pet._manual_remind()
                last = pet._controller.recent()[-1]
                self.assertEqual(last.source, "reminder")
                self.assertIn("提醒调度未启用", last.body)
            finally:
                pet._quit()


if __name__ == "__main__":
    unittest.main()
