# -*- coding: utf-8 -*-
"""像素风右键菜单：切角硬边卡片，致敬 pilog 的 pixel-btn。

纸白面 + 墨色硬边 + 无模糊偏移阴影 + 等宽字体；悬停行反色，点击菜单外
/Esc/失焦自动关闭。自绘 Toplevel，替代原生 tk.Menu。"""
import tkinter as tk
from tkinter import font as tkfont

from .logs import get_logger
from .pixtokens import (BG, BUBBLE_FONT_FALLBACK, PIX_INK, PIX_INFO,
                        PIX_PAPER, PIX_SHADOW, PIX_TEXT, _cut_rect)

log = get_logger("menu")


class PikaMenu:
    """切角硬边卡片菜单，致敬 pilog 的 pixel-btn：纸白面 + 墨色硬边 +
    无模糊偏移阴影 + 等宽字体；悬停行反色，按下微位移。"""

    def __init__(self, parent: tk.Tk, font_scale: float = 1.0):
        self.parent = parent
        self.win = None
        self.canvas = None
        self.items = []       # (label, command)
        self._hover = None    # 当前悬停下标
        self._anchor_x = 0
        self._anchor_y = 0
        self.font_scale = font_scale
        self._font = tkfont.Font(family=BUBBLE_FONT_FALLBACK,
                                 size=max(9, round(10 * font_scale)))
        self._pad = 4
        self._text_x = self._pad + 6   # 文字向左缩进，避开左侧小色块
        self._item_h = self._font.metrics("linespace") + self._pad * 2
        self._w = self._text_x + (self._font.measure("显示状态") if self.items else 0) + self._pad

    def set_items(self, items):
        self.items = list(items)
        self._w = self._text_x + self._pad
        for lbl, _ in self.items:
            self._w = max(self._w, self._text_x + self._font.measure(lbl) + self._pad)

    def popup(self, x_root, y_root, on_done=None):
        self.close()
        win = tk.Toplevel(self.parent)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", BG)
        except tk.TclError as e:
            # 非 Windows 或不支持：菜单会带品红底，但仍可用
            log.debug("菜单透明色属性不可用：%s", e)
        h = len(self.items) * self._item_h + self._pad * 2
        # canvas 四周给硬阴影留出偏移量（右下露出 4px 阴影）
        shadow = 4
        canvas = tk.Canvas(win, width=self._w + shadow, height=h + shadow,
                           bg=BG, highlightthickness=0, bd=0)
        canvas.pack()
        self.win = win
        self.canvas = canvas
        self.on_done = on_done
        self._hover = None
        self._shadow = shadow

        # 硬阴影（先画，被卡片盖住一部分露出右下偏移）
        cut = max(5, round(7 * self.font_scale))
        _cut_rect(canvas, 1 + shadow, 1 + shadow, self._w + shadow, h + shadow, cut,
                  fill="", outline=PIX_SHADOW, width=2)
        # 卡片主体
        _cut_rect(canvas, 1, 1, self._w, h, cut,
                  fill=PIX_PAPER, outline=PIX_INK, width=2)
        self._cut = cut
        # 条目文字
        self._draw_items()
        # 绑定
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda e: self._set_hover(None))
        canvas.bind("<Button-1>", self._on_click)
        win.bind("<Button-1>", self._on_click)
        # 键盘 Esc / 失焦：关闭菜单（右键菜单的常规退出方式）
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", lambda e: self.close())

        win.update_idletasks()
        bw, bh = self._w + shadow, h + shadow
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = min(x_root, sw - bw - 4)
        y = min(y_root, sh - bh - 4)
        self._anchor_x = int(max(4, x))
        self._anchor_y = int(max(4, y))
        win.geometry(f"+{self._anchor_x}+{self._anchor_y}")
        win.focus_set()
        # 全局抓取：点击菜单外（坐标越出卡片）路由到这里并关闭菜单。
        # 原生 tk.Menu 的 tk_popup 自带等价机制，自绘菜单必须手动补。
        try:
            win.grab_set()
        except tk.TclError as e:
            # 抓取失败：点菜单外面不会自动关，得靠 Esc / 失焦。值得知道
            log.warning("菜单全局抓取失败，点击外部可能不会关闭菜单：%s", e)

    def _draw_items(self):
        c = self.canvas
        c.delete("item")
        for i, (lbl, _) in enumerate(self.items):
            y = self._pad + i * self._item_h
            if i == self._hover:
                c.create_rectangle(1, y, self._w - 1, y + self._item_h,
                                   fill=PIX_INK, outline="", tags="item")
                fg = PIX_PAPER
            else:
                c.create_rectangle(1, y, self._w - 1, y + self._item_h,
                                   fill="", outline="", tags="item")
                fg = PIX_TEXT
            # 左边一个小色点点缀
            c.create_rectangle(self._pad, y + self._item_h // 2 - 2,
                               self._pad + 4, y + self._item_h // 2 + 2,
                               fill=PIX_INFO, outline="", tags="item")
            c.create_text(self._text_x, y + self._item_h // 2, anchor="w",
                          text=lbl, font=self._font, fill=fg, tags="item")

    def _set_hover(self, i):
        if i == self._hover:
            return
        self._hover = i
        self._draw_items()

    def _on_motion(self, e):
        idx = (e.y - self._pad) // self._item_h
        i = max(0, min(idx, len(self.items) - 1))
        self._set_hover(i)

    def _on_click(self, e):
        # 点击落在菜单卡片外的区域（grab 让你点出去也收到事件）：直接关闭
        if e.x < 1 or e.x > self._w or e.y < 1 or e.y > len(self.items) * self._item_h + self._pad * 2:
            self.close()
            return
        idx = (e.y - self._pad) // self._item_h
        if 0 <= idx < len(self.items):
            cmd = self.items[idx][1]
            self.close()
            if self.on_done:
                self.on_done()
            if cmd:
                cmd()

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError as e:
                log.debug("菜单窗口已不存在：%s", e)
        self.win = None
        self.canvas = None

    def reposition(self, x_root=None, y_root=None):
        """皮卡丘被拖动时让菜单跟着走（保留相对锚点）。"""
        if self.win is None:
            return
        try:
            bw, bh = self.win.winfo_width(), self.win.winfo_height()
            if x_root is None or y_root is None:
                x_root = self._anchor_x
                y_root = self._anchor_y
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x = min(x_root, sw - bw - 4)
            y = min(y_root, sh - bh - 4)
            self.win.geometry(f"+{int(max(4, x))}+{int(max(4, y))}")
        except tk.TclError as e:
            # 拖动过程中菜单可能刚被关掉，位置就不用更新了
            log.debug("菜单重定位失败（窗口可能已关闭）：%s", e)

    @property
    def anchor(self):
        """菜单当前锚点屏幕坐标 (x, y)。拖动桌宠时据此平移菜单。"""
        return self._anchor_x, self._anchor_y
