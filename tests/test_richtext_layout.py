# -*- coding: utf-8 -*-
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def gui_available():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestRichTextLayout(unittest.TestCase):
    """富文本正文：长链接不撑破 maxw、不把行挤乱。"""

    def _bubble(self):
        from pika.bubble import Bubble
        import tkinter as tk
        root = tk.Tk()
        b = Bubble(root, on_clicked=lambda: None)
        return root, b

    def _layout(self, body):
        from pika.bubble import Bubble
        root, b = self._bubble()
        f_body = b._font(size=10)
        b._cur_scale = 1.0
        res = b._layout_body(body, f_body, 300, "#188038")
        # 记录行宽（font 在 root 销毁后失效，必须在此测量）
        widths = [sum(cell[2].measure(cell[0]) for cell in row["cells"])
                  for row in res["lines"]]
        root.destroy()
        res["_widths"] = widths
        return res

    def test_short_text_no_wrap(self):
        res = self._layout("普通文本一行")
        self.assertEqual(len(res["lines"]), 1)
        self.assertLessEqual(res["width"], 300)

    def test_long_link_does_not_exceed_maxw(self):
        """超长链接文字必须按字符折行，任何单行宽度都不超过 maxw。"""
        long = "这是一个非常非常长的链接文字啊" * 4
        body = f"看这里：[{long}](https://example.com/x)"
        res = self._layout(body)
        self.assertGreater(len(res["lines"]), 1)  # 被折成多行
        for w in res["_widths"]:
            self.assertLessEqual(w, 300 + 1,
                                 f"行宽 {w} 超过 maxw，会把气泡撑宽")

    def test_normal_link_stays_single_line(self):
        res = self._layout("点[这里](https://a.b)查看")
        self.assertEqual(len(res["lines"]), 1)
        self.assertLessEqual(res["width"], 300)

    def test_hit_link_single_click(self):
        """一次点击命中链接 → on_clicked 不触发、_open_link 只调用一次。"""
        from pika.bubble import Bubble
        import tkinter as tk
        root = tk.Tk()
        opened = []
        clicked = []
        b = Bubble(root, on_clicked=lambda: clicked.append(1))
        b._open_link = lambda url: opened.append(url)   # 桩：不真开浏览器
        body = "[文档](https://github.com)"
        b.show(type("N", (), {"title": "t", "body": body, "level": "info",
                              "source": "s", "ts": None})(), kind="notice")
        root.update_idletasks()
        # 模拟点击链接命中区中心（模拟 event）
        if b._links:
            x1, y1, x2, y2, url = b._links[0]
            e = type("E", (), {"x": (x1 + x2) / 2, "y": (y1 + y2) / 2})()
            b._on_click(e)
        self.assertEqual(len(opened), 1, "链接点一次应只开一次浏览器")
        self.assertEqual(len(clicked), 0, "命中链接不应关气泡")
        b.close()
        root.destroy()

    def test_click_bound_once_not_twice(self):
        """<Button-1> 只绑在 canvas 上，win 不应再绑——否则一次点击
        触发两次 on_click，点链接会连开两个浏览器。"""
        import tkinter as tk
        from pika.bubble import Bubble
        root = tk.Tk()
        b = Bubble(root, on_clicked=lambda: None)
        b.show(type("N", (), {"title": "t", "body": "[a](https://a.b)",
                              "level": "info", "source": "s", "ts": None})(),
               kind="notice")
        root.update_idletasks()
        win_bind = b.win.bind()
        # win 不应有 <Button-1>，避免与 canvas 双触发
        self.assertNotIn("<Button-1>", win_bind)
        b.close()
        root.destroy()


if __name__ == "__main__":
    unittest.main()
