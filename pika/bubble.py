# -*- coding: utf-8 -*-
"""气泡窗口：像素风切角卡片 + 级别色条 + 徽章 + 正对皮卡丘脑袋的尾巴。

kind 区分"通知气泡"与"状态气泡"：状态气泡常驻不自动隐藏，通知气泡由
PetController 管理生命周期，二者互不误关。Canvas 手绘（Tk 无逐像素
alpha，靠 transparentcolor 挖掉品红背景）。"""
import ctypes
import time

import tkinter as tk
from tkinter import font as tkfont

from .protocol import Notification
from .pixtokens import (BG, BUBBLE_FONT, BUBBLE_FONT_FALLBACK, LEVEL_STYLE,
                        MAX_TEXT_W, PIX_ACCENT, PIX_BORDER_W, PIX_INK,
                        PIX_MUTE, PIX_PANEL, PIX_PAPER, PIX_SHADOW,
                        PIX_SHADOW_GAP, PIX_TEXT, _cut_rect, _wrap)
from . import mdflush


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
        self._cur_scale = 1.0
        self._links = []   # [(x1,y1,x2,y2,url)] 供点击命中

    def show(self, notif: Notification, kind: str = "notice"):
        self.close()
        self.kind = kind
        self._last_notif = notif
        win = tk.Toplevel(self.parent)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", BG)
        except tk.TclError:
            pass

        scale = getattr(self.pet, "bubble_scale", 1.0) if self.pet else 1.0

        f_title = self._font(size=11, weight="bold")
        f_body = self._font(size=10)
        f_meta = self._font(size=8)

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
        # 折行基准随缩放重算（字体变了，measure 也随之变化）
        title_lines = _wrap(notif.title, f_title, MAX_TEXT_W)
        # 正文：Markdown → 富文本行模型，逐行绘制（支持粗体/斜体/代码/
        # 标题/列表/引用/分隔线/链接）；纯文本也走同一条路，空白自动折叠。
        self._cur_scale = s
        body_rows = self._layout_body(notif.body, f_body, MAX_TEXT_W,
                                      accent)
        meta_lines = _wrap(meta, f_meta, MAX_TEXT_W)

        pad_x, pad_t, pad_b = round(18 * s), round(13 * s), round(11 * s)
        accent_w, badge_r = round(4 * s), round(11 * s)
        gap1, gap2, tail_h, margin = round(6 * s), round(6 * s), round(12 * s), round(2 * s)
        text_x0 = pad_x + accent_w + round(8 * s)
        title_x = text_x0 + badge_r * 2 + round(9 * s)

        w_title = max((f_title.measure(l) for l in title_lines), default=0)
        w_body = body_rows["width"]
        w_meta = max((f_meta.measure(l) for l in meta_lines), default=0)
        card_w = max(title_x + w_title, text_x0 + max(w_body, w_meta),
                     round(190 * s)) + pad_x
        lsp_t = f_title.metrics("linespace")
        lsp_b = f_body.metrics("linespace")
        lsp_m = f_meta.metrics("linespace")
        h_title = len(title_lines) * lsp_t
        # 富文本行高：每条行有各自的行高（标题/代码更大）
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
                text, style, font, fg = cell[0], cell[1], cell[2], cell[3]
                bg = cell[4] if len(cell) > 4 else None
                w = font.measure(text)
                if bg is not None:      # 先垫底色（切角药丸），再写文字
                    _cut_rect(canvas, cx - 1, ty - 1, cx + w + 1,
                              ty + font.metrics("linespace") - 1, 3,
                              fill=bg, outline="")
                canvas.create_text(cx, ty, text=text, anchor="nw",
                                   font=font, fill=fg)
                if len(cell) > 5 and cell[5]:   # 带 url 的链接单元格
                    self._links.append((cx, ty, cx + w,
                                        ty + font.metrics("linespace"),
                                        cell[5]))
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
        不接受 weight='italic'）。"""
        for fam in (BUBBLE_FONT, BUBBLE_FONT_FALLBACK):
            try:
                return tkfont.Font(family=fam, size=size, weight=weight,
                                   slant=slant)
            except Exception:
                continue
        return tkfont.Font(size=size, weight=weight, slant=slant)

    def _layout_body(self, body, f_body, maxw, accent):
        """把 Markdown 正文排成可绘制的行模型。

        返回 {"width": 最大行宽, "height": 总高, "lines": [行,...]}；
        每行是 {"cells": [(text,style,font,fg,bg[,url])], "height": 行高,
        "above": 前置空隙}。accent 是级别强调色，用于列表符号/引用竖线
        等装饰性元素的点缀（正文文字仍是灰/墨主调，避免花哨）。
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
            w = sum(cell[2].measure(cell[0]) for cell in cells)
            # 单行宽度硬钳到 maxw：任何一行（含列表前缀叠加）都不允许
            # 把气泡撑到超过正文上限，否则"会话完成"这类长消息会撑成一条
            if w > maxw:
                w = maxw
            width = max(width, w)
            height += above + hgt
            lines.append({"cells": cells, "height": hgt, "above": above})

        def build_cells(pieces, base_font):
            """pieces: [(text,style[,url])]；折成若干行，每行是 cells 列表。

            cell 结构 (text, style, font, fg, bg[, url])——bg 是底色或 None。
            """
            row_cells = []
            cur_w = 0
            out_rows = []
            for piece in pieces:
                text, st = piece[0], piece[1]
                url = piece[2] if len(piece) > 2 else None
                f = style_font(st)
                bg = style_bg(st)
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
                            cell = (ch, st, f, style_fg(st), bg)
                            if url is not None:
                                cell = cell + (url,)
                            row_cells.append(cell)
                            cur_w += f.measure(ch)
                        continue
                    if cur_w + tw > maxw and row_cells:
                        out_rows.append(row_cells)
                        row_cells = []
                        cur_w = 0
                    cell = (token, st, f, style_fg(st), bg)
                    if url is not None:
                        cell = cell + (url,)
                    row_cells.append(cell)
                    cur_w += tw
            if row_cells:
                out_rows.append(row_cells)
            if not out_rows:      # 全空
                out_rows.append([("", "", base_font, PIX_TEXT, None)])
            return out_rows

        rows = mdflush.render(txt)
        for kind, segs in rows:
            if kind == "rule":
                emit_line([("─" * 26, "rule", f_body, PIX_MUTE, None)],
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
                cells = [("│ ", "quote", f_body, accent, None)]
                for seg in segs:
                    t, st = seg[0], seg[1]
                    cell = (t, st, style_font(st), style_fg(st),
                            style_bg(st))
                    if len(seg) > 2:            # 链接带 url
                        cell = cell + (seg[2],)
                    cells.append(cell)
                emit_line(cells, f_body.metrics("linespace"))
                continue
            # para / list：列表加项目符号
            base_font = f_body
            if kind == "list":
                # 列表符号用级别强调色点缀（其余文字仍走 build_cells 灰/墨）
                prefix_cells = [("· ", "list", f_lst, accent, None)]
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

    def _open_link(self, url):
        """点击链接：用系统默认浏览器打开（失败则忽略）。

        加 800ms 时间去重：真实桌面双击会触发两次 <Button-1>，每次
        都命中链接区，不加去重会连开两个浏览器。仅对"同 url 相邻两次"
        去重，不同 url 或间隔稍长的再次点击仍会开。"""
        try:
            now = time.time()
            if (getattr(self, "_last_link_url", None) == url
                    and now - getattr(self, "_last_link_ts", 0) < 0.8):
                return
            self._last_link_url = url
            self._last_link_ts = now
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    @staticmethod
    def _split_for_wrap(text):
        """按词切分（保持连续字母/数字/中文字符串，空格并入前一词尾），
        供折行用；避免"词 + 空格"被拆开导致词尾孤悬。"""
        import re
        return re.findall(r"[^\s]+[\s]*|[\s]+", text)



    def _hover(self, on: bool):
        if self.controller is not None:
            try:
                self.controller.set_hover(on)
            except Exception:
                pass

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
        except tk.TclError:
            ax, ay, aw, ah = 0, 0, 180, 180
        if aw <= 1:
            aw = 180
        if ah <= 1:
            ah = 180
        win.update_idletasks()
        bw_w = win.winfo_reqwidth()
        bw_h = win.winfo_reqheight()
        gap = 6
        x = ax + aw // 2 - bw_w // 2
        y = ay - bw_h - gap
        if y < 0:
            y = ay + ah + gap
        # 钳回屏幕内：桌宠贴边时气泡不能跑出屏幕
        try:
            sw = win.winfo_screenwidth()
            x = max(4, min(x, sw - bw_w - 4))
        except tk.TclError:
            pass
        win.geometry(f"+{int(x)}+{int(y)}")

    def _no_activate(self, win):
        """弹出气泡后不抢占前台焦点（SWP_NOACTIVATE）。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            ctypes.windll.user32.SetWindowPos(
                hwnd, -1, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0010 | 0x0040)
        except Exception:
            pass

    def reposition(self):
        """皮卡丘移动/缩放后重定位气泡，让它始终贴着桌宠。

        仅当气泡可见时调用（内部对锚点/坐标做钳制，不会跑出屏幕）。"""
        if self.win is not None:
            try:
                self._place(self.win)
            except Exception:
                pass

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
        self.win = None
        self.frame = None
        self.kind = None

    @property
    def visible(self) -> bool:
        return self.win is not None
