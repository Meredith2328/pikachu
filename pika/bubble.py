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
                        MAX_TEXT_W, PIX_BORDER_W, PIX_INK, PIX_MUTE,
                        PIX_PAPER, PIX_SHADOW, PIX_SHADOW_GAP, PIX_TEXT,
                        _cut_rect, _wrap)


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
        body_lines = _wrap(notif.body, f_body, MAX_TEXT_W) if notif.body else []
        meta_lines = _wrap(meta, f_meta, MAX_TEXT_W)

        pad_x, pad_t, pad_b = round(18 * s), round(13 * s), round(11 * s)
        accent_w, badge_r = round(4 * s), round(11 * s)
        gap1, gap2, tail_h, margin = round(6 * s), round(6 * s), round(12 * s), round(2 * s)
        text_x0 = pad_x + accent_w + round(8 * s)
        title_x = text_x0 + badge_r * 2 + round(9 * s)

        w_title = max((f_title.measure(l) for l in title_lines), default=0)
        w_body = max((f_body.measure(l) for l in body_lines), default=0)
        w_meta = max((f_meta.measure(l) for l in meta_lines), default=0)
        card_w = max(title_x + w_title, text_x0 + max(w_body, w_meta),
                     round(190 * s)) + pad_x
        lsp_t = f_title.metrics("linespace")
        lsp_b = f_body.metrics("linespace")
        lsp_m = f_meta.metrics("linespace")
        h_title = len(title_lines) * lsp_t
        h_body = len(body_lines) * lsp_b + (gap1 if body_lines else 0)
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
        for line in body_lines:
            canvas.create_text(text_x0, ty, text=line, anchor="nw",
                               font=f_body, fill=PIX_TEXT)
            ty += lsp_b
        ty += gap2
        for line in meta_lines:
            canvas.create_text(text_x0, ty, text=line, anchor="nw",
                               font=f_meta, fill=PIX_MUTE)
            ty += lsp_m

        win.bind("<Button-1>", lambda e: self.on_clicked())
        canvas.bind("<Button-1>", lambda e: self.on_clicked())
        win.bind("<Enter>", lambda e: self._hover(True))
        win.bind("<Leave>", lambda e: self._hover(False))
        self._place(win)
        self._no_activate(win)

    def _font(self, size, weight="normal"):
        """等宽字体带 CJK 回退：优先 Cascadia Code，缺失退回 Consolas。"""
        for fam in (BUBBLE_FONT, BUBBLE_FONT_FALLBACK):
            try:
                return tkfont.Font(family=fam, size=size, weight=weight)
            except Exception:
                continue
        return tkfont.Font(size=size, weight=weight)

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
