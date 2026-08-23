# -*- coding: utf-8 -*-
"""状态气泡：结构化内容模型、缩放联动信息量、排版不越界。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.pet_core import PetController
from pikapet.protocol import Notification
from tests.helpers import gui_available, isolated_home


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class StatusModelTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.c = PetController(clock=self.clock, dedup_sec=0)

    def feed(self, source, title, body="", level="info"):
        self.clock.t += 1
        self.c.handle(Notification(title=title, body=body, source=source,
                                   level=level, ts=self.clock.t))


class TestStatusModel(StatusModelTestCase):
    def test_empty(self):
        m = self.c.status_model()
        self.assertEqual(m.total, 0)
        self.assertEqual(m.items, [])
        self.assertEqual(m.sources, {})
        self.assertEqual(m.hidden, 0)

    def test_counts_total_and_sources(self):
        self.feed("a", "1")
        self.feed("a", "2")
        self.feed("b", "3")
        m = self.c.status_model()
        self.assertEqual(m.total, 3)
        self.assertEqual(m.sources, {"a": 2, "b": 1})

    def test_items_are_newest_first(self):
        for i in range(3):
            self.feed("s", f"第{i}条")
        titles = [it.title for it in self.c.status_model().items]
        self.assertEqual(titles, ["第2条", "第1条", "第0条"])

    def test_max_items_limits_and_reports_hidden(self):
        for i in range(6):
            self.feed("s", f"t{i}")
        m = self.c.status_model(max_items=2)
        self.assertEqual(len(m.items), 2)
        self.assertEqual(m.hidden, 4)

    def test_dedups_identical_messages(self):
        """同来源同内容重复多次，状态里只占一条（否则一条刷屏消息挤满气泡）。"""
        for _ in range(5):
            self.feed("s", "一样的", "一样的正文")
        self.feed("s", "不一样")
        m = self.c.status_model()
        self.assertEqual(len(m.items), 2)
        self.assertEqual(m.total, 6)      # 总数仍是真实收到的条数

    def test_preview_is_single_line(self):
        """摘要必须压成一行：多行会让条目对不齐。"""
        self.feed("s", "标题", "第一行\n第二行\n第三行")
        preview = self.c.status_model().items[0].preview
        self.assertNotIn("\n", preview)
        self.assertIn("第一行", preview)

    def test_preview_truncated_with_ellipsis(self):
        self.feed("s", "标题", "很长的正文" * 40)
        preview = self.c.status_model(preview_len=20).items[0].preview
        self.assertLessEqual(len(preview), 21)
        self.assertTrue(preview.endswith("…"))

    def test_preview_can_be_disabled(self):
        self.feed("s", "标题", "有正文")
        m = self.c.status_model(preview=False)
        self.assertEqual(m.items[0].preview, "")

    def test_level_carried_for_coloring(self):
        self.feed("s", "错误的", level="error")
        self.assertEqual(self.c.status_model().items[0].level, "error")

    def test_muted_flag(self):
        self.assertFalse(self.c.status_model().muted)
        self.c.toggle_mute()
        self.assertTrue(self.c.status_model().muted)

    def test_max_items_zero(self):
        self.feed("s", "t")
        m = self.c.status_model(max_items=0)
        self.assertEqual(m.items, [])
        self.assertEqual(m.hidden, 1)


class TestStatusText(StatusModelTestCase):
    """纯文本版仍可用（无 GUI 的调试路径），且每条自成一行。"""

    def test_lines_are_separate(self):
        self.feed("a", "第一件事", "细节一")
        self.feed("b", "第二件事", "细节二")
        text = self.c.status_text()
        self.assertIn("[a] 第一件事", text)
        self.assertIn("[b] 第二件事", text)
        # 两个条目不能挤在同一行里
        for line in text.splitlines():
            self.assertFalse("第一件事" in line and "第二件事" in line)

    def test_reports_total(self):
        self.feed("a", "x")
        self.assertIn("已接收 1 条通知", self.c.status_text())

    def test_muted_shown(self):
        self.c.toggle_mute()
        self.assertIn("静音", self.c.status_text())


class TestStatusBudget(unittest.TestCase):
    """气泡缩放 → 信息量：缩放调的是内容多少，不只是字号。"""

    def test_monotonic_in_scale(self):
        from pikapet.pet import status_budget
        counts = [status_budget(s)[0]
                  for s in (0.5, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5)]
        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], counts[-1])

    def test_small_scale_drops_preview(self):
        from pikapet.pet import status_budget
        self.assertFalse(status_budget(0.5)[1])
        self.assertFalse(status_budget(0.75)[1])
        self.assertTrue(status_budget(1.0)[1])

    def test_extremes_are_covered(self):
        from pikapet.pet import status_budget
        for s in (0.0, 0.5, 99.0):
            count, _ = status_budget(s)
            self.assertGreaterEqual(count, 1)


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestStatusLayout(unittest.TestCase):
    """真 Tk 排版：每条一行、不越界、颜色分层。"""

    def _bubble_and_model(self, scale=1.0, **model_kw):
        import tkinter as tk
        from pikapet.bubble import Bubble
        c = PetController(dedup_sec=0)
        for i, (src, title, body, lvl) in enumerate([
                ("zcode", "会话完成 · 很长很长的一个标题" * 2, "正文一", "success"),
                ("reminder", "该休息一下了", "看看远处吧" * 10, "warn"),
                ("codex", "会话完成 · 审查", "发现问题", "error")]):
            c.handle(Notification(title=title, body=body, source=src,
                                  level=lvl, ts=1000.0 + i))
        root = tk.Tk()
        b = Bubble(root, on_clicked=lambda: None)
        b._cur_scale = scale
        return root, b, c.status_model(**model_kw)

    def test_no_row_exceeds_maxw(self):
        """任何一行都不许超过折行上限——超出会画到卡片外面去。"""
        root, b, model = self._bubble_and_model()
        try:
            maxw = 300
            res = b._layout_status(model, b._font(size=10), maxw)
            for row in res["lines"]:
                w = sum(c.font.measure(c.text) for c in row["cells"])
                self.assertLessEqual(w, maxw + 1,
                                     f"行宽 {w} 越界：{[c.text for c in row['cells']]}")
        finally:
            root.destroy()

    def test_each_item_starts_its_own_row(self):
        """条目不能被并进同一行（旧实现走 Markdown 就是这么挤成一坨的）。"""
        root, b, model = self._bubble_and_model()
        try:
            res = b._layout_status(model, b._font(size=10), 300)
            rows_with_source = [
                row for row in res["lines"]
                if any(c.text in ("zcode", "reminder", "codex")
                       for c in row["cells"])]
            self.assertEqual(len(rows_with_source), len(model.items))
        finally:
            root.destroy()

    def test_level_color_on_dot(self):
        """级别色只落在圆点上，标题保持墨色（不给正文整体上色）。"""
        from pikapet.pixtokens import LEVEL_STYLE, PIX_INK
        root, b, model = self._bubble_and_model()
        try:
            res = b._layout_status(model, b._font(size=10), 300)
            dots = [c for row in res["lines"] for c in row["cells"]
                    if c.text.startswith("●")]
            self.assertEqual(len(dots), len(model.items))
            wanted = [LEVEL_STYLE[it.level][0] for it in model.items]
            self.assertEqual([d.fg for d in dots], wanted)
            titles = [c for row in res["lines"] for c in row["cells"]
                      if "bold" in c.style and c.fg == PIX_INK]
            self.assertTrue(titles)
        finally:
            root.destroy()

    def test_more_items_means_more_rows(self):
        root, b, model2 = self._bubble_and_model(max_items=2, preview=False)
        try:
            few = b._layout_status(model2, b._font(size=10), 300)
            root.destroy()
        except Exception:
            root.destroy()
            raise
        root, b, model3 = self._bubble_and_model(max_items=3, preview=True)
        try:
            many = b._layout_status(model3, b._font(size=10), 300)
            self.assertGreater(len(many["lines"]), len(few["lines"]))
            self.assertGreater(many["height"], few["height"])
        finally:
            root.destroy()

    def test_empty_state_says_so(self):
        import tkinter as tk
        from pikapet.bubble import Bubble
        c = PetController()
        root = tk.Tk()
        try:
            b = Bubble(root, on_clicked=lambda: None)
            res = b._layout_status(c.status_model(), b._font(size=10), 300)
            texts = "".join(c.text for row in res["lines"]
                            for c in row["cells"])
            self.assertIn("还没收到通知", texts)
        finally:
            root.destroy()


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestStatusBubbleScaling(unittest.TestCase):
    def test_zoom_changes_item_count(self):
        """放大气泡后状态里的条目变多，不只是字变大。"""
        from pikapet.pet import PikaPet
        with isolated_home():
            pet = PikaPet(port=0, subscribe_only=True, with_reminder=False)
            try:
                for i in range(8):
                    pet._controller.handle(Notification(
                        title=f"消息 {i}", body=f"正文 {i}",
                        source=f"s{i}", ts=1000.0 + i))
                pet.bubble_scale = 0.75
                pet._status_visible = True
                pet._draw_status_bubble()
                small = len(pet.bubble._last_status.items)

                pet.bubble_scale = 1.6
                pet._draw_status_bubble()
                big = len(pet.bubble._last_status.items)
                self.assertGreater(big, small)
            finally:
                pet._quit()


if __name__ == "__main__":
    unittest.main()
