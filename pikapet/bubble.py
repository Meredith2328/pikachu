# -*- coding: utf-8 -*-
"""气泡窗口：像素风切角卡片 + 级别色条 + 徽章 + 正对皮卡丘脑袋的尾巴。

kind 区分"通知气泡"与"状态气泡"：状态气泡常驻不自动隐藏，通知气泡由
PetController 管理生命周期，二者互不误关。Canvas 手绘（Tk 无逐像素
alpha，靠 transparentcolor 挖掉品红背景）。"""
import ctypes
import re
import sys
import time
import webbrowser
from typing import NamedTuple

import tkinter as tk
from tkinter import font as tkfont

from .logs import get_logger, swallow
from .protocol import Notification
from .pixtokens import (BG, BUBBLE_FONT, BUBBLE_FONT_FALLBACK, LEVEL_STYLE,
                        MAX_TEXT_W, PIX_ACCENT, PIX_BORDER_W, PIX_INFO,
                        PIX_INK, PIX_MUTE, PIX_PANEL, PIX_PAPER, PIX_SHADOW,
                        PIX_SHADOW_GAP, PIX_TEXT, PIX_WARN, _cut_rect, _wrap)
from . import mdflush

log = get_logger("bubble")

# 同一链接在这段时间内的重复点击只开一次浏览器（桌面双击会触发两次）
LINK_DEDUP_SEC = 0.8
# 折行切词：连续非空白并吞掉尾随空白，或纯空白串
_WRAP_TOKEN = re.compile(r"[^\s]+[\s]*|[\s]+")
# 锚点窗口尺寸读不到时的兜底（与桌宠默认画布同尺寸）
DEFAULT_ANCHOR_SIZE = 180


def _ellipsize(text, font, maxw):
    """按像素宽把单行截到 maxw，超出补省略号。

    状态条目刻意"截断而不折行"：每条固定占一行，眼睛才能沿着左边缘
    一路往下数出有几项；折行会让条目边界消失。
    """
    text = str(text or "")
    if maxw <= 0 or font.measure(text) <= maxw:
        return text
    ell = "…"
    budget = maxw - font.measure(ell)
    if budget <= 0:
        return ell
    lo, hi = 0, len(text)
    while lo < hi:                      # 二分找放得下的最长前缀
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell


class Cell(NamedTuple):
    """正文里一个可绘制片段：一段同字体同颜色的文字。

    原先是变长 tuple，靠 len(cell) > 4 / > 5 判断有没有底色和链接，
    加一个字段就要改所有下标。"""

    text: str
    style: frozenset          # {'bold','italic','code','link'} 的子集
    font: object              # tkfont.Font
    fg: str
    bg: str = None            # 底色（None = 透明，保持纸白主调）
    url: str = None           # 链接目标（None = 不可点）

# SetWindowPos 参数（Win32）：置顶、不改尺寸/位置、不抢焦点、显示窗口
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


class Bubble:
    """气泡窗口。kind 区分"通知气泡"与"状态气泡"：状态气泡常驻不自动隐藏，
    通知气泡由 PetController 管理生命周期，二者互不误关。

    外观：像素风切角卡片 + 左侧级别色条 + 徽章图标 + 底部小尾巴，
    Canvas 手绘（Tk 无逐像素 alpha，靠 transparentcolor 挖掉品红背景）。"""

    def __init__(self, parent: tk.Tk, on_clicked, controller=None, pet=None):
        self.parent = parent
        self.controller = controller  # PetController：悬浮标记由它管
        self.pet = pet                # PikaPet：隐藏时的锚点（tab 窗口）由它提供
        self.on_clicked = on_clicked
        self.win = None
        self.frame = None  # 兼容旧字段：现在指向内容画布
        self.kind = None
        self._last_notif = None
        self._last_status = None   # 状态气泡的结构化内容（重画时复用）
        self._cur_scale = 1.0
        self._links = []   # [(x1,y1,x2,y2,url)] 供点击命中
        self._last_link_url = None   # 链接点击去重：上次打开的 url 与时刻
        self._last_link_ts = 0.0

    def show(self, notif: Notification, kind: str = "notice",
             status=None):
        """弹气泡。

        kind="status" 且给了 status（PetController.status_model() 的结果）时，
        正文走结构化排版而不是 Markdown——状态内容是"若干条目 + 统计"，
        用 \\n 拼成一段文字再走 Markdown 会被并成一行，列表全挤在一起。
        """
        self.close()
        self.kind = kind
        self._last_notif = notif
        self._last_status = status
        win = tk.Toplevel(self.parent)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", BG)
        except tk.TclError as e:
            # 非 Windows 或不支持该属性：气泡会带一块品红背景，但仍可读
            log.debug("透明色属性不可用，气泡背景不透明：%s", e)

        scale = getattr(self.pet, "bubble_scale", 1.0) if self.pet else 1.0

        accent, glyph = LEVEL_STYLE.get(notif.level, LEVEL_STYLE["info"])
        meta = notif.source
        if notif.ts:
            meta += f"  ·  {time.strftime('%H:%M', time.localtime(notif.ts))}"

        # ---- 排版：按像素宽折行后按 scale 缩放（字体尺寸 / 内距 / 尾巴 / 尖切）----
        s = scale
        fs_title = max(1, round(11 * s))
        fs_body = max(1, round(10 * s))
        fs_meta = max(1, round(8 * s))
        f_title = self._font(size=fs_title, weight="bold")
        f_body = self._font(size=fs_body)
        f_meta = self._font(size=fs_meta)
        # 折行宽度随缩放走：固定 300px 的话，气泡放大只是把字变大、每行
        # 反而塞不下几个字，标题会被截得七零八落
        maxw = max(120, round(MAX_TEXT_W * s))
        # 折行基准随缩放重算（字体变了，measure 也随之变化）
        title_lines = _wrap(notif.title, f_title, maxw)
        self._cur_scale = s
        if status is not None:
            body_rows = self._layout_status(status, f_body, maxw)
        else:
            # 正文：Markdown → 富文本行模型，逐行绘制（支持粗体/斜体/代码/
            # 标题/列表/引用/分隔线/链接）；纯文本也走同一条路，空白自动折叠。
            body_rows = self._layout_body(notif.body, f_body, maxw,
                                          accent)
        meta_lines = _wrap(meta, f_meta, maxw)

        pad_x, pad_t, pad_b = round(18 * s), round(13 * s), round(11 * s)
        accent_w, badge_r = round(4 * s), round(11 * s)
        gap1, gap2, tail_h, margin = round(6 * s), round(6 * s), round(12 * s), round(2 * s)
        text_x0 = pad_x + accent_w + round(8 * s)
        title_x = text_x0 + badge_r * 2 + round(9 * s)

        w_title = max((f_title.measure(line) for line in title_lines), default=0)
        w_body = body_rows["width"]
        w_meta = max((f_meta.measure(line) for line in meta_lines), default=0)
        card_w = max(title_x + w_title, text_x0 + max(w_body, w_meta),
                     round(190 * s)) + pad_x
        lsp_t = f_title.metrics("linespace")
        lsp_m = f_meta.metrics("linespace")
        h_title = len(title_lines) * lsp_t
        # 富文本行高：每条行有各自的行高（标题/代码更大），不用统一行距
        h_body = body_rows["height"] + (gap1 if body_rows["lines"] else 0)
        h_meta = len(meta_lines) * lsp_m + (gap2 if meta else 0)
        card_h = pad_t + h_title + h_body + h_meta + pad_b

        canvas = tk.Canvas(win, width=card_w + margin * 2,
                           height=card_h + tail_h + margin,
                           bg=BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        self.win = win
        self.frame = canvas

        x1, y1 = margin, margin
        x2, y2 = margin + card_w, margin + card_h
        cut = max(6, round(9 * s))
        _cut_rect(canvas, x1, y1, x2, y2, cut,
                  fill=PIX_PAPER, outline=PIX_INK, width=PIX_BORDER_W)
        # 硬阴影：描边右下方偏移一份（无模糊，像素风的招牌）
        _cut_rect(canvas, x1 + PIX_SHADOW_GAP, y1 + PIX_SHADOW_GAP,
                  x2 + PIX_SHADOW_GAP, y2 + PIX_SHADOW_GAP, cut,
                  fill="", outline=PIX_SHADOW, width=PIX_BORDER_W)
        # 尾巴：切角尖朝下的三角，正对皮卡丘脑袋（_place 里对齐其屏幕 x）
        tail_tx = x1 + card_w // 2
        tail_w = max(10, round(16 * s))
        canvas.create_polygon(
            tail_tx - tail_w, y2 - 1, tail_tx + tail_w, y2 - 1,
            tail_tx, y2 + tail_h,
            fill=PIX_PAPER, outline=PIX_INK, width=PIX_BORDER_W)
        canvas.create_rectangle(tail_tx - (tail_w - 2), y2 - 2,
                                tail_tx + (tail_w - 2), y2 - 1,
                                fill=PIX_PAPER, outline="")
        # 左侧强调色条（硬边，像素风）
        canvas.create_rectangle(x1 + round(7 * s), y1 + round(10 * s),
                                x1 + round(7 * s) + accent_w,
                                y2 - round(10 * s), fill=accent, outline="")
        # 徽章：方形切角（呼应整体），内放白色符号或手绘闪电
        bcx = text_x0 + badge_r
        bcy = y1 + pad_t + lsp_t // 2 - 1
        _cut_rect(canvas, bcx - badge_r, bcy - badge_r,
                  bcx + badge_r, bcy + badge_r, max(2, round(3 * s)),
                  fill=accent, outline="")
        if glyph:
            canvas.create_text(bcx, bcy, text=glyph, fill="white",
                               font=(BUBBLE_FONT, max(8, round(10 * s)), "bold"))
        else:  # info 级：手绘一道白色小闪电
            apex = max(4, round(6 * s))
            bolt = [bcx - .15 * apex, bcy - apex, bcx + .30 * apex, bcy - apex,
                    bcx + .06 * apex, bcy - .12 * apex, bcx + .34 * apex,
                    bcy - .12 * apex, bcx - .22 * apex, bcy + apex,
                    bcx - .04 * apex, bcy + .08 * apex,
                    bcx - .36 * apex, bcy + .08 * apex]
            canvas.create_polygon(bolt, fill="white", outline="")

        ty = y1 + pad_t
        for line in title_lines:
            canvas.create_text(title_x, ty, text=line, anchor="nw",
                               font=f_title, fill=PIX_INK)
            ty += lsp_t
        ty += gap1
        # 富文本正文：逐行绘制（行可能有几种字体的片段）
        self._links = []   # 行内链接的命中区（x1,y1,x2,y2,url）
        for row in body_rows["lines"]:
            ty += row["above"]   # 前置空隙（标题/代码块上下留白）
            cx = text_x0
            for cell in row["cells"]:
                w = cell.font.measure(cell.text)
                if cell.bg is not None:  # 先垫底色（切角药丸），再写文字
                    _cut_rect(canvas, cx - 1, ty - 1, cx + w + 1,
                              ty + cell.font.metrics("linespace") - 1, 3,
                              fill=cell.bg, outline="")
                canvas.create_text(cx, ty, text=cell.text, anchor="nw",
                                   font=cell.font, fill=cell.fg)
                if cell.url:
                    self._links.append((cx, ty, cx + w,
                                        ty + cell.font.metrics("linespace"),
                                        cell.url))
                cx += w
            ty += row["height"]
        ty += gap2
        for line in meta_lines:
            canvas.create_text(text_x0, ty, text=line, anchor="nw",
                               font=f_meta, fill=PIX_MUTE)
            ty += lsp_m

        # 只绑 canvas：气泡窗口与 canvas 双绑定会让一次点击触发两次
        # on_click（win 和 canvas 都会收到事件）——点击链接会连开两个
        # 浏览器。canvas 铺满窗口，收全部点击，无需再绑 win。
        canvas.bind("<Button-1>", self._on_click)
        win.bind("<Enter>", lambda e: self._hover(True))
        win.bind("<Leave>", lambda e: self._hover(False))
        self._place(win)
        self._no_activate(win)

    def _on_click(self, e):
        """点击：命中行内链接则开浏览器并本气泡不关闭；否则走原关闭逻辑。"""
        url = self._hit_link(e.x, e.y)
        if url is not None:
            self._open_link(url)
            return
        self.on_clicked()

    def _hit_link(self, x, y):
        """在文内坐标 (x,y) 找命中链接；无则 None。"""
        for x1, y1, x2, y2, url in getattr(self, "_links", []):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return url
        return None

    def _font(self, size, weight="normal", slant="roman"):
        """等宽字体带 CJK 回退：优先 Cascadia Code，缺失退回 Consolas。

        weight ∈ normal/bold；slant ∈ roman/italic（Tk 用 slant 表斜体，
        不接受 weight='italic'）。两个字体族都取不到时用 Tk 默认族——
        这一步会记 WARNING，因为字体族变了排版宽度也会变。"""
        for fam in (BUBBLE_FONT, BUBBLE_FONT_FALLBACK):
            try:
                return tkfont.Font(family=fam, size=size, weight=weight,
                                   slant=slant)
            except tk.TclError as e:
                log.debug("字体族 %s 不可用：%s", fam, e)
        log.warning("等宽字体族 %s / %s 都不可用，改用 Tk 默认族",
                    BUBBLE_FONT, BUBBLE_FONT_FALLBACK)
        return tkfont.Font(size=size, weight=weight, slant=slant)

    def _layout_body(self, body, f_body, maxw, accent):
        """把 Markdown 正文排成可绘制的行模型。

        返回 {"width": 最大行宽, "height": 总高, "lines": [行,...]}；
        每行是 {"cells": [Cell,...], "height": 行高, "above": 前置空隙}。
        accent 是级别强调色，用于列表符号/引用竖线等装饰性元素的点缀
        （正文文字仍是灰/墨主调，避免花哨）。
        """
        txt = body or ""
        if not txt.strip():
            return {"width": 0, "height": 0, "lines": []}

        f_size = f_body.cget("size")
        f_bold = self._font(size=f_size, weight="bold")
        f_code = self._font(size=max(1, f_size - 1))
        f_italic = self._font(size=f_size, slant="italic")
        f_h1 = self._font(size=f_size + 4, weight="bold")
        f_h2 = self._font(size=f_size + 2, weight="bold")
        f_lst = self._font(size=f_size)

        # 标题前置空隙（按缩放折半，克制）
        h1_above = max(0, round(4 * getattr(self, "_cur_scale", 1.0)))

        def style_font(style):
            if "code" in style:
                return f_code
            if "bold" in style and "italic" in style:
                return self._font(size=f_size, weight="bold", slant="italic")
            if "bold" in style:
                return f_bold
            if "italic" in style:
                return f_italic
            return f_body

        def style_fg(style):
            if "link" in style:
                return PIX_ACCENT
            if "code" in style or "bold" in style:
                return PIX_INK
            return PIX_TEXT

        def style_bg(style):
            """按样式给片段底色（None=透明，保持纸白主调）。"""
            if "code" in style:
                return PIX_PANEL      # 行内代码：浅琥珀药丸底
            return None

        lines = []
        width = 0
        height = 0

        def emit_line(cells, hgt, above=0):
            nonlocal height, width
            w = sum(cell.font.measure(cell.text) for cell in cells)
            # 单行宽度硬钳到 maxw：任何一行（含列表前缀叠加）都不允许
            # 把气泡撑到超过正文上限，否则"会话完成"这类长消息会撑成一条
            if w > maxw:
                w = maxw
            width = max(width, w)
            height += above + hgt
            lines.append({"cells": cells, "height": hgt, "above": above})

        def make_cell(text, st, font, url=None):
            return Cell(text=text, style=st, font=font, fg=style_fg(st),
                        bg=style_bg(st), url=url)

        def build_cells(pieces, base_font):
            """pieces: [(text,style[,url])]；折成若干行，每行是 Cell 列表。"""
            row_cells = []
            cur_w = 0
            out_rows = []
            for piece in pieces:
                text, st = piece[0], piece[1]
                url = piece[2] if len(piece) > 2 else None
                f = style_font(st)
                for token in self._split_for_wrap(text):
                    tw = f.measure(token)
                    if tw > maxw:
                        # 单个 token（长链接/长英文词）超宽：按字符硬拆，
                        # 避免它撑破 maxw 把整行/气泡拉宽
                        for ch in token:
                            if cur_w + f.measure(ch) > maxw and row_cells:
                                out_rows.append(row_cells)
                                row_cells = []
                                cur_w = 0
                            row_cells.append(make_cell(ch, st, f, url))
                            cur_w += f.measure(ch)
                        continue
                    if cur_w + tw > maxw and row_cells:
                        out_rows.append(row_cells)
                        row_cells = []
                        cur_w = 0
                    row_cells.append(make_cell(token, st, f, url))
                    cur_w += tw
            if row_cells:
                out_rows.append(row_cells)
            if not out_rows:      # 全空
                out_rows.append([Cell("", frozenset(), base_font, PIX_TEXT)])
            return out_rows

        rows = mdflush.render(txt)
        for kind, segs in rows:
            if kind == "rule":
                emit_line([Cell("─" * 26, frozenset(), f_body, PIX_MUTE)],
                          f_body.metrics("linespace"))
                continue
            if kind.startswith("h"):
                big = f_h1 if kind == "h1" else f_h2
                for cells in build_cells(segs, big):
                    emit_line(cells, big.metrics("linespace"),
                              above=h1_above)
                    h1_above = 0  # 标题只第一个留空
                continue
            if kind == "quote":
                cells = [Cell("│ ", frozenset(), f_body, accent)]
                for seg in segs:
                    cells.append(make_cell(
                        seg[0], seg[1], style_font(seg[1]),
                        seg[2] if len(seg) > 2 else None))
                emit_line(cells, f_body.metrics("linespace"))
                continue
            # para / list：列表加项目符号
            base_font = f_body
            if kind == "list":
                # 列表符号用级别强调色点缀（其余文字仍走 build_cells 灰/墨）
                prefix_cells = [Cell("· ", frozenset(), f_lst, accent)]
                body_cells = build_cells(segs, base_font)
                if body_cells:
                    emit_line(prefix_cells + body_cells[0],
                              f_body.metrics("linespace"))
                    for more in body_cells[1:]:
                        emit_line(more, f_body.metrics("linespace"))
                else:
                    emit_line(prefix_cells, f_body.metrics("linespace"))
            else:
                for cells in build_cells(segs, base_font):
                    emit_line(cells, f_body.metrics("linespace"))

        if not lines:
            return {"width": 0, "height": 0, "lines": []}
        return {"width": width, "height": height, "lines": lines}

    def _layout_status(self, model, f_body, maxw):
        """状态内容 → 行模型。与 _layout_body 输出同一种结构，共用绘制代码。

        排版目标是"一眼看清有几项、哪几项、每项讲什么"，所以：
        - 顶部一行汇总（总条数 + 来源数 + 静音标记），数字用墨色加粗，
          其余灰字——扫一眼就知道规模；
        - 每个条目占两行且左侧对齐：第一行是 `●` 级别色圆点 + 来源徽标 +
          标题，第二行缩进一格放摘要；条目之间留一行空隙，不再靠 `·`
          分隔符把几条挤在一行里；
        - 颜色只承载语义：级别色点 + 来源名用强调蓝、标题墨色、摘要灰、
          数字加粗。不给正文整体上色，避免花哨。
        """
        s = getattr(self, "_cur_scale", 1.0)
        f_size = f_body.cget("size")
        f_bold = self._font(size=f_size, weight="bold")
        f_small = self._font(size=max(1, f_size - 1))
        lsp = f_body.metrics("linespace")
        lsp_small = f_small.metrics("linespace")
        gap = max(2, round(5 * s))     # 条目之间的空隙
        indent = " " * 2               # 摘要相对标题缩进

        lines = []
        width = 0
        height = 0

        def emit(cells, hgt, above=0):
            nonlocal width, height
            w = sum(c.font.measure(c.text) for c in cells)
            width = max(width, min(w, maxw))
            height += above + hgt
            lines.append({"cells": cells, "height": hgt, "above": above})

        # ---- 汇总行 ----
        # "N 条通知" 与 "M 个来源" 分两段，放不进一行就换行摆第二段：
        # 硬塞一行会顶破卡片右边缘（card_w 按 min(w, maxw) 算，超出部分
        # 直接画到卡片外面去）
        seg_count = [
            Cell("⚡ ", frozenset(), f_body, PIX_INFO),
            Cell(str(model.total), frozenset({"bold"}), f_bold, PIX_INK),
            Cell(" 条通知", frozenset(), f_body, PIX_TEXT),
        ]
        seg_src = []
        if model.sources:
            seg_src = [
                Cell(str(len(model.sources)), frozenset({"bold"}), f_bold,
                     PIX_INK),
                Cell(" 个来源", frozenset(), f_body, PIX_TEXT),
            ]
        sep = [Cell("  ·  ", frozenset(), f_body, PIX_MUTE)]
        one_line = seg_count + sep + seg_src
        if not seg_src:
            emit(seg_count, lsp)
        elif sum(c.font.measure(c.text) for c in one_line) <= maxw:
            emit(one_line, lsp)
        else:
            emit(seg_count, lsp)
            # 第二段缩进对齐到 ⚡ 之后（空格不显色，用什么前景都一样）
            emit([Cell("   ", frozenset(), f_body, PIX_TEXT)] + seg_src, lsp)
        if model.muted:
            emit([Cell("🔇 静音中，只记录不弹泡", frozenset(), f_small,
                       PIX_WARN)], lsp_small, above=max(1, round(2 * s)))

        # ---- 条目 ----
        for i, item in enumerate(model.items):
            dot = LEVEL_STYLE.get(item.level, LEVEL_STYLE["info"])[0]
            head = [
                Cell("● ", frozenset(), f_small, dot),
                Cell(item.source, frozenset(), f_small, PIX_ACCENT),
                Cell("  ", frozenset(), f_small, PIX_MUTE),
            ]
            used = sum(c.font.measure(c.text) for c in head)
            head.append(Cell(_ellipsize(item.title, f_bold, maxw - used),
                             frozenset({"bold"}), f_bold, PIX_INK))
            emit(head, lsp, above=gap if i else max(2, round(4 * s)))
            if item.preview:
                pad = f_small.measure(indent)
                emit([Cell(indent + _ellipsize(item.preview, f_small,
                                               maxw - pad),
                           frozenset(), f_small, PIX_TEXT)],
                     lsp_small)

        # ---- 省略提示 ----
        if model.hidden:
            emit([Cell(f"…另有 {model.hidden} 条更早的", frozenset(),
                       f_small, PIX_MUTE)],
                 lsp_small, above=gap)
        if not model.items and not model.total:
            emit([Cell("还没收到通知", frozenset(), f_small, PIX_MUTE)],
                 lsp_small, above=gap)

        return {"width": width, "height": height, "lines": lines}

    def _open_link(self, url):
        """点击链接：用系统默认浏览器打开。

        加 800ms 时间去重：真实桌面双击会触发两次 <Button-1>，每次
        都命中链接区，不加去重会连开两个浏览器。仅对"同 url 相邻两次"
        去重，不同 url 或间隔稍长的再次点击仍会开。

        打不开要记 WARNING：用户点了链接却什么都没发生，是需要线索的。"""
        now = time.time()
        if (self._last_link_url == url
                and now - self._last_link_ts < LINK_DEDUP_SEC):
            log.debug("忽略 %ss 内对同一链接的重复点击：%s",
                      LINK_DEDUP_SEC, url)
            return
        self._last_link_url = url
        self._last_link_ts = now
        with swallow(log, f"打开链接 {url}"):
            webbrowser.open(url)

    @staticmethod
    def _split_for_wrap(text):
        """按词切分（保持连续字母/数字/中文字符串，空格并入前一词尾），
        供折行用；避免"词 + 空格"被拆开导致词尾孤悬。"""
        return _WRAP_TOKEN.findall(text)

    def _hover(self, on: bool):
        if self.controller is not None:
            with swallow(log, "更新悬浮状态"):
                self.controller.set_hover(on)

    def _place(self, win):
        # 隐藏到角落时以 ⚡ tab 窗口为锚点；否则锚在主宠窗口
        anchor = None
        if self.pet is not None and getattr(self.pet, "_tab_win", None) is not None:
            anchor = self.pet._tab_win
        if anchor is None:
            anchor = self.parent
        try:
            ax = anchor.winfo_rootx()
            ay = anchor.winfo_rooty()
            aw = anchor.winfo_width()
            ah = anchor.winfo_height()
        except tk.TclError as e:
            # 锚点窗口刚被销毁：按默认尺寸摆放，气泡仍然可见
            log.debug("读取锚点窗口几何失败，改用默认值：%s", e)
            ax, ay, aw, ah = 0, 0, DEFAULT_ANCHOR_SIZE, DEFAULT_ANCHOR_SIZE
        if aw <= 1:
            aw = DEFAULT_ANCHOR_SIZE
        if ah <= 1:
            ah = DEFAULT_ANCHOR_SIZE
        win.update_idletasks()
        bw_w = win.winfo_reqwidth()
        bw_h = win.winfo_reqheight()
        gap = 6
        x = ax + aw // 2 - bw_w // 2
        y = ay - bw_h - gap
        if y < 0:
            y = ay + ah + gap
        # 钳回屏幕内：桌宠贴边时气泡不能跑出屏幕
        sw = win.winfo_screenwidth()
        x = max(4, min(x, sw - bw_w - 4))
        win.geometry(f"+{int(x)}+{int(y)}")

    def _no_activate(self, win):
        """弹出气泡后不抢占前台焦点（SWP_NOACTIVATE）。

        非 Windows 平台没有这套 API，直接跳过（气泡会抢焦点，但能用）。"""
        if sys.platform != "win32":
            return
        with swallow(log, "设置气泡不抢焦点", once=True):
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def reposition(self):
        """皮卡丘移动/缩放后重定位气泡，让它始终贴着桌宠。

        仅当气泡可见时调用（内部对锚点/坐标做钳制，不会跑出屏幕）。"""
        if self.win is not None:
            with swallow(log, "重定位气泡", once=True):
                self._place(self.win)

    def redraw(self):
        """按当前缩放重画当前气泡（气泡字号变了要立刻看到效果）。

        气泡不可见时什么都不做。调用方不必自己去拿"上一条通知"。
        状态气泡会重新问一次内容：缩放变了，该显示的条目数也跟着变。"""
        if self.win is None or self._last_notif is None:
            return
        if self.kind == "status" and self.pet is not None:
            self.pet.refresh_status_bubble()
            return
        self.show(self._last_notif, kind=self.kind, status=self._last_status)

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError as e:
                log.debug("气泡窗口已不存在：%s", e)
        self.win = None
        self.frame = None
        self.kind = None

    @property
    def visible(self) -> bool:
        return self.win is not None
