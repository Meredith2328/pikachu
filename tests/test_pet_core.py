import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.pet_core import PetController
from pikapet.protocol import Notification


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def advance(self, dt):
        self.t += dt


class TestPetController(unittest.TestCase):
    def setUp(self):
        self.shown = []
        self.hidden = []
        self.clock = FakeClock()
        self.c = PetController(on_show=lambda n: self.shown.append(n),
                               on_hide=lambda: self.hidden.append(1),
                               clock=lambda: self.clock.t)

    def n(self, title="t", ttl=10.0, source="a"):
        return Notification(title=title, source=source, ttl=ttl)

    def test_show_calls_callback(self):
        r = self.c.handle(self.n())
        self.assertEqual(r, "shown")
        self.assertEqual(len(self.shown), 1)

    def test_auto_hide_after_ttl(self):
        self.c.handle(self.n(ttl=5))
        self.clock.advance(6)
        self.assertTrue(self.c.tick())
        self.assertEqual(len(self.hidden), 1)
        self.assertIsNone(self.c.current)

    def test_auto_hide_ttl_zero_never(self):
        self.c.handle(self.n(ttl=0))
        self.clock.advance(100000)
        self.assertFalse(self.c.tick())
        self.assertEqual(len(self.hidden), 0)

    def test_hover_prevents_auto_hide(self):
        self.c.handle(self.n(ttl=5))
        self.c.set_hover(True)
        self.clock.advance(100)
        self.assertFalse(self.c.tick())
        self.c.set_hover(False)
        self.clock.advance(1)
        self.assertTrue(self.c.tick())

    def test_manual_dismiss(self):
        self.c.handle(self.n())
        self.c.dismiss()
        self.assertIsNone(self.c.current)
        self.assertEqual(len(self.hidden), 1)

    def test_dismiss_after_hover_unsticks_auto_hide(self):
        """悬浮后点击关闭：hover 标记必须清除，否则下一条消息永远不自动消失。"""
        self.c.handle(self.n(title="first", ttl=5))
        self.c.set_hover(True)
        self.c.dismiss()
        # 下一条消息（非悬浮，不同内容避免被去重）应能正常自动消失
        self.c.handle(self.n(title="second", ttl=5))
        self.clock.advance(6)
        self.assertTrue(self.c.tick())
        self.assertEqual(len(self.hidden), 2)

    def test_replace_clears_hover(self):
        """新消息替换旧气泡：旧气泡的 hover 残留必须清除，否则新气泡永不消失。"""
        self.c.handle(self.n(title="A", source="a", ttl=5))
        self.c.set_hover(True)
        # 新消息（不同 source）替换当前气泡
        self.c.handle(self.n(title="B", source="b", ttl=5))
        self.clock.advance(6)
        self.assertTrue(self.c.tick())  # B 应能自动消失
        self.assertEqual(len(self.hidden), 1)

    def test_dedup(self):
        r1 = self.c.handle(self.n(source="a", ttl=5))
        r2 = self.c.handle(self.n(source="a", ttl=5))
        self.assertEqual(r1, "shown")
        self.assertEqual(r2, "deduped")
        self.assertEqual(len(self.shown), 1)

    def test_no_dedup_different_source(self):
        self.c.handle(self.n(source="a"))
        r = self.c.handle(self.n(source="b"))
        self.assertEqual(r, "shown")
        self.assertEqual(len(self.shown), 2)

    def test_no_dedup_after_window(self):
        self.c.handle(self.n(source="a", ttl=5))
        self.clock.advance(11)
        r = self.c.handle(self.n(source="a", ttl=5))
        self.assertEqual(r, "shown")
        self.assertEqual(len(self.shown), 2)

    def test_mute_records_but_no_bubble(self):
        self.c.toggle_mute()
        r = self.c.handle(self.n())
        self.assertEqual(r, "muted")
        self.assertEqual(len(self.shown), 0)
        self.assertEqual(len(self.c.recent()), 1)

    def test_mute_toggle_off(self):
        self.c.toggle_mute()
        self.c.toggle_mute()
        r = self.c.handle(self.n())
        self.assertEqual(r, "shown")

    def test_muted_dedup_still_works(self):
        self.c.toggle_mute()
        self.c.handle(self.n(source="a"))
        r = self.c.handle(self.n(source="a"))
        self.assertEqual(r, "deduped")

    def test_recent_and_source_stats(self):
        self.c.handle(self.n(title="1", source="a"))
        self.c.handle(self.n(title="2", source="b"))
        self.c.handle(self.n(title="3", source="a"))
        self.assertEqual(len(self.c.recent()), 3)
        self.assertEqual(self.c.source_stats(), {"a": 2, "b": 1})
        text = self.c.status_text()
        self.assertIn("3 条通知", text)
        self.assertIn("a 2", text)

    def test_history_limit(self):
        c = PetController(clock=lambda: self.clock.t, history_limit=3)
        for i in range(5):
            c.handle(self.n(title=f"m{i}"))
        self.assertEqual(len(c.recent()), 3)
        self.assertEqual(c.recent()[-1].title, "m4")


if __name__ == "__main__":
    unittest.main()
