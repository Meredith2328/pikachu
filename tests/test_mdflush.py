# -*- coding: utf-8 -*-
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika.mdflush import _parse_inline, render


def segs_text(segs):
    """拼接片段纯文本（忽略样式）。"""
    return "".join(s[0] for s in segs)


class TestInline(unittest.TestCase):
    def test_bold(self):
        segs = _parse_inline("a **b** c")
        self.assertEqual([s[1] for s in segs],
                         [frozenset(), frozenset({"bold"}), frozenset()])

    def test_italic(self):
        segs = _parse_inline("*i*")
        self.assertIn("italic", segs[0][1])

    def test_code(self):
        segs = _parse_inline("`x`")
        self.assertIn("code", segs[0][1])
        self.assertEqual(segs[0][0], "x")

    def test_link_carries_url(self):
        segs = _parse_inline("[t](https://a.b)")
        self.assertIn("link", segs[0][1])
        self.assertEqual(segs[0][2], "https://a.b")

    def test_plain(self):
        segs = _parse_inline("hello 世界")
        self.assertEqual(segs[0][1], frozenset())


class TestRender(unittest.TestCase):
    def test_para(self):
        rows = render("一个字")
        self.assertEqual(rows[0][0], "para")

    def test_h1_h2_h3(self):
        rows = render("# A\n## B\n### C")
        self.assertEqual([r[0] for r in rows], ["h1", "h2", "h3"])

    def test_heading_level_cap(self):
        rows = render("##### A")
        self.assertEqual(rows[0][0], "h3")   # 超过三级合并到 h3

    def test_list(self):
        rows = render("- a\n- b")
        self.assertEqual([r[0] for r in rows], ["list", "list"])

    def test_ordered_list(self):
        rows = render("1. a\n2. b")
        self.assertEqual([r[0] for r in rows], ["list", "list"])

    def test_quote(self):
        rows = render("> note")
        self.assertEqual(rows[0][0], "quote")
        self.assertEqual(segs_text(rows[0][1]), "note")

    def test_rule(self):
        rows = render("---")
        self.assertEqual(rows[0][0], "rule")

    def test_blank_folding(self):
        rows = render("a\n\n\nb")
        self.assertEqual([r[0] for r in rows], ["para", "para"])

    def test_continuation_merges(self):
        rows = render("第一行\n第二行")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "para")
        self.assertEqual(segs_text(rows[0][1]), "第一行 第二行")

    def test_none_returns_empty(self):
        self.assertEqual(render(None), [])

    def test_empty(self):
        self.assertEqual(render(""), [])

    def test_heading_anchor_wiped(self):
        rows = render("# 标题 #")
        self.assertEqual(segs_text(rows[0][1]), "标题")


if __name__ == "__main__":
    unittest.main()
