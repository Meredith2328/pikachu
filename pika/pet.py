# -*- coding: utf-8 -*-
"""皮卡丘桌宠：透明置顶小窗口 + 气泡通知显示端。

- 只负责"显示"：收到总线消息就冒泡，不感知消息来源、不做轮询；
- Windows 专用代码（透明窗口 / topmost / 隐藏到角落 / 空闲检测）都在本文件；
- 总线两种接法：本进程内嵌（默认，外部软件总能 POST 到 8765）；
  或 --subscribe-only 订阅外部已运行的总线（此时本进程不开端口）。
- 鼠标跟随：30fps 轮询全局光标，皮卡丘连续转身看向鼠标（assets/turn 帧资产，
  右向为镜像）；资产缺失时自动回退静态贴图。

交互：
  双击       显示"关于"
  右键       状态 / 立即提醒一次 / 静音开关 / 隐藏到角落 / 退出
  悬浮       显示当前状态气泡；通知气泡悬浮期间不自动消失
  气泡点击   立即关闭
"""
import ctypes
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:
    tk = None  # 无 GUI 环境（CI）下 import 本模块不报错
    tkfont = None

from . import bus
from .pet_core import PetController
from .protocol import Notification
from .turn import TurnDirector, frame_index, get_cursor_pos, turn_frame_paths
from .win.idle import get_idle_seconds, WinIdleSource  # noqa: F401

BG = "magenta"            # 透明色：所有透明区域填充该颜色，靠 -transparentcolor 挖掉
YELLOW = "#FFD93B"
ASSET = Path(__file__).resolve().parent.parent / "assets" / "pikachu.png"
TURN_DIR = Path(__file__).resolve().parent.parent / "assets" / "turn"
TURN_TICK_MS = 33         # ~30fps 的跟随刷新
DEFAULT_PORT = bus.DEFAULT_PORT


# ----------------------------------------------------------------------
# 像素风设计 tokens —— 致敬 pilog（pixel / minimal 主题）
#   调色板取自 Chrome 离线小恐龙：纸白 paper · 墨色 ink · 灰 text/mute
#   边框为 2px 实色硬边，阴影为纯偏移硬阴影（无模糊），等宽字体。
# ----------------------------------------------------------------------
PIX_PAPER = "#FBF8EE"      # 纸白（微暖，贴皮卡丘）；pilog 是 #f7f7f7
PIX_PANEL = "#F3EFE2"      # 面板底
PIX_INK = "#3C4043"        # 主墨色：边框 / 标题 / 强调文字
PIX_TEXT = "#5F6368"       # 正文
PIX_MUTE = "#9AA0A6"       # 次要文字（来源 / 时间）
PIX_SHADOW = "#DAD6C6"     # 硬阴影色（偏移无模糊）
PIX_BORDER_W = 2           # 硬边框宽
PIX_SHADOW_GAP = 5         # 硬阴影偏移
# 级别强调色：pilog 语法高亮 token 四色
PIX_INFO = "#FBBC04"       # 皮卡丘黄（强调蓝换成了主题黄）
PIX_SUCCESS = "#188038"
PIX_WARN = "#B06000"
PIX_ERROR = "#C5221F"

# 气泡字体：拉丁/数字走等宽（pilog 像素风），中文由 Tk 自动回退到微软雅黑
BUBBLE_FONT = "Cascadia Code"
# 级联回退：Cascadia 缺失时的等宽备胎（同属现代等宽族，观感一致）
BUBBLE_FONT_FALLBACK = "Consolas"

MAX_TEXT_W = 300  # 正文折行宽度上限（像素）

# 每级别：强调色 + 徽章内白色符号（None=画一道小闪电）
LEVEL_STYLE = {
    "info": (PIX_INFO, None),
    "success": (PIX_SUCCESS, "✓"),
    "warn": (PIX_WARN, "!"),
    "error": (PIX_ERROR, "✕"),
}


def _wrap(text, font, maxw):
    """按像素宽折行：拉丁词保持完整，其余逐字符折。"""
    out = []
    for para in str(text or "").split("\n"):
        cur = ""
        for tok in re.findall(r"[A-Za-z0-9_./:@+-]+|.", para):
            trial = cur + tok
            if font.measure(trial) > maxw and cur.strip():
                out.append(cur.rstrip())
                cur = "" if tok == " " else tok
            else:
                cur = trial
        out.append(cur)
    return out


def _cut_rect(cv, x1, y1, x2, y2, cut, **kw):
    """切角矩形（像素风）：四个角各斜切 cut 像素，硬边无抗锯齿。

    替代圆角的 smooth 多边形 —— Tk 无抗锯齿，切角是干净的正折线，
    配合 2px 实色边框呈现 pilog 的硬边像素感。"""
    pts = [x1 + cut, y1, x2 - cut, y1, x2, y1 + cut,
           x2, y2 - cut, x2 - cut, y2, x1 + cut, y2,
           x1, y2 - cut, x1, y1 + cut]
    return cv.create_polygon(pts, **kw)



class Bubble:
    """气泡窗口。kind 区分"通知气泡"与"状态气泡"：状态气泡常驻不自动隐藏，
    通知气泡由 PetController 管理生命周期，二者互不误关。

    外观：奶油色圆角卡片 + 左侧级别色条 + 徽章图标 + 底部小尾巴，
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


# ----------------------------------------------------------------------
# 像素风右键菜单（自绘 Toplevel，替代原生 tk.Menu）
# ----------------------------------------------------------------------
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
        except tk.TclError:
            pass
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
        except tk.TclError:
            pass

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
            except Exception:
                pass
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
        except Exception:
            pass


# ----------------------------------------------------------------------
# 桌宠主程序
# ----------------------------------------------------------------------
class PikaPet:
    def __init__(self, port: int = DEFAULT_PORT, subscribe_only: bool = False):
        _make_dpi_aware()  # 进程级：让 Tk 走物理像素，measure 与渲染一致
        self.root = tk.Tk()
        self.root.title("皮卡丘")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", BG)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)

        self.size = 180
        self.scale = 1.0            # 桌宠缩放：影响画布/贴图/窗口尺寸
        self.bubble_scale = 1.0     # 气泡缩放：影响气泡字号/内距/尾巴
        self.canvas = tk.Canvas(self.root, width=self.size, height=self.size,
                                bg=BG, highlightthickness=0)
        self.canvas.pack()
        # 鼠标跟随状态（_load_asset 里尝试加载帧资产，失败则保持空并回退静态图）
        self._turn_left = []
        self._turn_right = []
        self._turn_real = None   # 真实关键帧下标（补帧除外）；静止时吸附到这些帧
        self._director = TurnDirector()
        self._last_turn_key = None
        self._img_id = None
        # 转身帧比静态图宽（身体居中后尾巴向两侧伸展），画布随资产加宽
        self.canvas_w = self.size
        self.canvas_h = self.size
        self._load_asset()
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        self.root.geometry(f"+{w - self.canvas_w - 20}+{h - self.canvas_h - 60}")

        # 控制器：显示决策全部在 PetController，UI 只挂回调
        self._controller = PetController(
            on_show=lambda n: self.root.after(0, lambda: self._bubble_show(n)),
            on_hide=lambda: self.root.after(0, self._bubble_hide))
        self.bubble = Bubble(self.root, on_clicked=self._bubble_click,
                             controller=self._controller, pet=self)
        self.menu = PikaMenu(self.root)
        self._tab_win = None
        self._tick_job = None
        self._turn_job = None
        self._status_bubble = None
        self._status_visible = False
        self._press = None
        self._moved = False

        self.canvas.bind("<ButtonPress-1>", self._start_press)
        self.canvas.bind("<B1-Motion>", self._on_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Double-Button-1>", lambda e: self._about())
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Enter>", self._pet_enter)
        self.canvas.bind("<Leave>", self._pet_leave)

        # 总线：内嵌或订阅外部
        self.server = None
        self.sse = None
        self._connect(port, subscribe_only)

        # 驱动 controller 的自动隐藏 tick
        self._tick_ui()
        # 驱动鼠标跟随的渲染 tick（~30fps）
        self._tick_turn()

    # ---- 总线连接 ----
    def _connect(self, port: int, subscribe_only: bool):
        external = False
        if not subscribe_only:
            try:
                info = bus.fetch_health(port=port, timeout=0.5)
                external = info.get("ok") is True
            except Exception:
                external = False
        if subscribe_only or external:
            # 已有独立总线在跑，订阅它
            self.sse = bus.SSEClient(
                port=port, on_event=self._on_bus_msg,
                on_error=lambda e: None)
            self.sse.start()
        else:
            # 内嵌总线：端口被非 pika 服务占用时由内核原子分配随机端口，
            # 并把实际端口写入 runtime/port（外部软件据此连接）
            fell_back = False
            try:
                self.server = bus.BusServer(port=port).start()
            except OSError:
                self.server = bus.BusServer(port=0).start()
                fell_back = True
            runtime_dir = Path(__file__).resolve().parent.parent / "runtime"
            runtime_dir.mkdir(exist_ok=True)
            (runtime_dir / "port").write_text(str(self.server.port),
                                              encoding="utf-8")
            if fell_back:
                print(f"pika-pet 总线端口 {self.server.port} "
                      f"(默认 {port} 被占用，已回退)", flush=True)
            self.sse = bus.SSEClient(
                port=self.server.port, on_event=self._on_bus_msg,
                on_error=lambda e: None)
            self.sse.start()

    def _on_bus_msg(self, n: Notification):
        # SSE 回调在 daemon 线程，桥接到 tk 主线程
        self.root.after(0, lambda: self._controller.handle(n))

    # ---- 控制器回调 ----
    def _bubble_show(self, n: Notification):
        # 悬浮标记的重置由 PetController.handle 在替换气泡时负责
        self._status_close()
        self.bubble.show(n)

    def _bubble_hide(self):
        self.bubble.close()

    def _bubble_click(self):
        # 状态气泡不经过控制器，直接关；通知气泡走 dismiss（清 hover/计时）
        if self.bubble.kind == "status":
            self._status_close()
        else:
            self._controller.dismiss()

    def _tick_ui(self):
        try:
            self._controller.tick()
        except Exception:
            pass
        try:
            self._tick_job = self.root.after(500, self._tick_ui)
        except Exception:
            pass  # root 已销毁

    # ---- 贴图 ----
    def _load_asset(self):
        if self._load_turn_assets():
            return
        path = ASSET
        if not path.exists():
            self._draw_fallback()
            return
        self._static_pil = None
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            px = im.load()
            changed = False
            for y in range(im.size[1]):
                for x in range(im.size[0]):
                    r, g, b, a = px[x, y]
                    if a < 255:
                        lum = 0.299 * r + 0.587 * g + 0.114 * b
                        if a < 32 or lum < 150:
                            px[x, y] = (255, 0, 255, 255)
                            changed = True
                        else:
                            px[x, y] = (r, g, b, 255)
            self._static_pil = im  # 缓存底图，缩放时 NEAREST 重渲染
        except Exception:
            self._static_pil = None
        if self._static_pil is None:
            try:
                self.photo = tk.PhotoImage(file=str(path))
            except Exception:
                self.photo = None
        self._rebuild_static_photo()

    def _rebuild_static_photo(self):
        """按 self.scale 用 NEAREST 重渲染静态贴图。"""
        from PIL import Image, ImageTk
        root = getattr(self, "_static_pil", None)
        if root is None:
            return
        scale = self.scale or 1.0
        im = root
        if scale != 1.0:
            im = im.resize((max(1, round(root.size[0] * scale)),
                            max(1, round(root.size[1] * scale))), Image.NEAREST)
        self.photo = ImageTk.PhotoImage(im)
        self.canvas_w = self.photo.width()
        self.canvas_h = self.photo.height()
        try:
            self.canvas.configure(width=self.canvas_w, height=self.canvas_h)
        except Exception:
            pass
        if self._img_id is not None:
            try:
                self.canvas.delete(self._img_id)
            except Exception:
                pass
        self._img_id = self.canvas.create_image(
            self.canvas_w // 2, self.canvas_h // 2, image=self.photo)

    def _load_turn_assets(self, directory=None) -> bool:
        """加载转身帧资产；成功时以第 0 帧（正面）作为基础贴图。

        帧资产以身体对称轴为画面中心（换边时身体重合、只有尾巴换边），
        画布按帧的实际尺寸加宽，窗口跟着变宽，贴图视觉尺寸不变。
        底帧以 PIL RGBA 缓存（保留 alpha 与透明品红），缩放时用 NEAREST
        重渲染，像素风不糊。"""
        paths = turn_frame_paths(directory or TURN_DIR)
        if paths is None:
            return False
        left_paths, right_paths = paths
        try:
            from PIL import Image
            self._turn_left_pil = [Image.open(str(p)).convert("RGBA")
                                   for p in left_paths]
            self._turn_right_pil = [Image.open(str(p)).convert("RGBA")
                                    for p in right_paths]
        except Exception:
            self._turn_left_pil = []
            self._turn_right_pil = []
            return False
        self._turn_real = self._load_real_indices(
            Path(directory or TURN_DIR), len(self._turn_left_pil))
        return self._rebuild_turn_photos()

    def _rebuild_turn_photos(self, recenter: bool = False):
        """按 self.scale 用 NEAREST 重渲染转身帧，并更新画布/窗口尺寸。"""
        pils = getattr(self, "_turn_left_pil", None)
        if not pils:
            return False
        from PIL import Image, ImageTk
        scale = self.scale or 1.0

        def build(pils):
            out = []
            for im in pils:
                if scale != 1.0:
                    nw = max(1, round(im.size[0] * scale))
                    nh = max(1, round(im.size[1] * scale))
                    im = im.resize((nw, nh), Image.NEAREST)
                out.append(ImageTk.PhotoImage(im))
            return out

        # 记录旧视觉中心，重建后把窗口挪回同一屏幕位置
        old_cx = self.root.winfo_rootx() + self.canvas_w // 2
        old_cy = self.root.winfo_rooty() + self.canvas_h // 2

        self._turn_left = build(self._turn_left_pil)
        self._turn_right = build(self._turn_right_pil)
        pw = max(p.width() for p in self._turn_left + self._turn_right)
        ph = max(p.height() for p in self._turn_left + self._turn_right)
        self.canvas_w, self.canvas_h = pw + 8, ph + 6
        try:
            self.canvas.configure(width=self.canvas_w, height=self.canvas_h)
        except Exception:
            pass
        if self._img_id is not None:
            try:
                self.canvas.delete(self._img_id)
            except Exception:
                pass
        self._img_id = self.canvas.create_image(
            self.canvas_w // 2, self.canvas_h // 2, image=self._turn_left[0])
        self._last_turn_key = None
        if recenter and self.canvas_w and self.canvas_h:
            self._recenter(old_cx, old_cy)
        return True

    def _recenter(self, cx, cy):
        """把窗口中心挪到屏幕坐标 (cx, cy)，并钳回屏幕内。"""
        try:
            x = cx - self.canvas_w // 2
            y = cy - self.canvas_h // 2
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(4, min(x, sw - self.canvas_w - 4))
            y = max(4, min(y, sh - self.canvas_h - 4))
            self.root.geometry(f"+{int(x)}+{int(y)}")
        except tk.TclError:
            pass

    def set_scale(self, scale: float, recenter: bool = True):
        """调整桌宠整体缩放并重渲染（像素风 NEAREST 不糊）。"""
        scale = max(0.4, min(scale, 3.0))
        if abs(scale - self.scale) < 1e-6:
            return
        self.scale = scale
        if getattr(self, "_turn_left_pil", None):
            self._rebuild_turn_photos(recenter=recenter)
        elif getattr(self, "_static_pil", None) is not None:
            old_cx = self.root.winfo_rootx() + self.canvas_w // 2
            old_cy = self.root.winfo_rooty() + self.canvas_h // 2
            self._rebuild_static_photo()
            if recenter:
                self._recenter(old_cx, old_cy)
        # 桌宠尺寸变了：气泡跟着重定位（仍贴脑袋）
        if getattr(self, "bubble", None) is not None:
            self.bubble.reposition()




    def _load_real_indices(self, directory: Path, count: int):
        """从 manifest 读补帧下标，返回真实关键帧下标集合。

        解析失败或数量对不上时返回 None（不吸附，行为与旧资产一致）。"""
        try:
            data = json.loads((directory / "manifest.json").read_text(
                encoding="utf-8"))
            blends = data["blend_indices"]
            if data.get("count") != count:
                return None
            if not isinstance(blends, list) or any(
                    not isinstance(i, int) for i in blends):
                return None
            return sorted(set(range(count)) - set(blends))
        except Exception:
            return None

    # ---- 鼠标跟随 ----
    def _tick_turn(self):
        try:
            if self._turn_left and self.root.state() == "normal":
                pos = get_cursor_pos()
                if pos is not None:
                    self._turn_step(*pos)
        except Exception:
            pass
        try:
            self._turn_job = self.root.after(TURN_TICK_MS, self._tick_turn)
        except Exception:
            pass  # root 已销毁

    def _turn_step(self, mx: float, my: float):
        """按全局光标位置更新一帧朝向（独立出来便于注入坐标做测试）。

        姿态收敛（鼠标停下）时吸附到最近的真实关键帧：混合补帧只在运动中
        充当动态模糊，静止画面永远清晰。"""
        px = self.root.winfo_rootx() + self.canvas_w // 2
        py = self.root.winfo_rooty() + self.canvas_h // 2
        pose, direction = self._director.update(mx, my, px, py)
        photos = self._turn_right if direction > 0 else self._turn_left
        real = self._turn_real if self._director.settled else None
        idx = frame_index(pose, len(photos), real)
        key = (id(photos), idx)
        if key != self._last_turn_key:
            self.canvas.itemconfig(self._img_id, image=photos[idx])
            self._last_turn_key = key

    def _draw_fallback(self):
        c = self.canvas
        c.create_oval(30, 22, 140, 118, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_oval(45, 78, 125, 152, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_polygon(40, 34, 52, 6, 70, 34, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_polygon(44, 32, 54, 14, 64, 32, fill="#2E2E2E")
        c.create_polygon(100, 34, 118, 6, 130, 34, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_polygon(106, 32, 116, 14, 126, 32, fill="#2E2E2E")
        c.create_oval(58, 60, 74, 76, fill="#2E2E2E")
        c.create_oval(96, 60, 112, 76, fill="#2E2E2E")
        c.create_oval(63, 64, 68, 69, fill="white")
        c.create_oval(101, 64, 106, 69, fill="white")
        c.create_oval(44, 82, 66, 102, fill="#FF7B7B")
        c.create_oval(104, 82, 126, 102, fill="#FF7B7B")
        c.create_arc(74, 72, 96, 94, start=0, extent=180, style=tk.ARC,
                     outline="#3B2F2F", width=2)

    # ---- 拖动 ----
    def _start_press(self, e):
        self._press = (e.x_root, e.y_root)
        self._moved = False

    def _on_move(self, e):
        if self._press:
            self._moved = True
            dx = e.x_root - self._press[0]
            dy = e.y_root - self._press[1]
            self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")
            self._press = (e.x_root, e.y_root)
            # 皮卡丘挪动了，气泡/状态菜单跟着走，贴着脑袋
            self.bubble.reposition()
            self.menu.reposition(self.menu._anchor_x + dx, self.menu._anchor_y + dy)

    def _on_release(self, e):
        self._press = None

    def _on_wheel(self, e):
        """滚轮缩放桌宠：上滚放大，下滚缩小，像素风 NEAREST 不糊。"""
        step = 0.15 if e.delta > 0 else -0.15
        self.set_scale(self.scale + step)

    # ---- 悬浮状态气泡 ----
    def _pet_enter(self, e):
        if not self.bubble.visible:
            self._status_show()

    def _pet_leave(self, e):
        self._status_close()

    def _status_show(self):
        # 通知气泡显示中：先 dismiss 它（走控制器正常收尾），再弹状态气泡，
        # 避免通知的 ttl 计时器回头误关状态气泡
        if self.bubble.visible and self.bubble.kind == "notice":
            self._controller.dismiss()
        if self._status_visible:
            return
        self._status_visible = True
        notif = Notification(title="皮卡丘", body=self._controller.status_text(),
                             level="info", source="pika", ttl=0)
        self.bubble.show(notif, kind="status")

    def _status_close(self):
        self._status_visible = False
        # 只关状态气泡，别误伤正在显示的通知气泡
        if self.bubble.visible and self.bubble.kind == "status":
            self.bubble.close()

    # ---- 右键菜单 ----
    def _menu(self, e):
        self.menu.set_items([
            ("显示状态", self._status_show),
            ("立即提醒休息一次", self._manual_remind),
            ("静音开关", self._toggle_mute),
            ("放大桌宠", lambda: self._zoom_pet(0.25)),
            ("缩小桌宠", lambda: self._zoom_pet(-0.25)),
            ("放大气泡", lambda: self._zoom_bubble(0.25)),
            ("缩小气泡", lambda: self._zoom_bubble(-0.25)),
            ("隐藏到角落", self._hide),
            ("关于", self._about),
            ("退出", self._quit),
        ])
        self.menu.popup(e.x_root, e.y_root)

    def _zoom_pet(self, step):
        self.set_scale(self.scale + step)

    def _zoom_bubble(self, step):
        self.bubble_scale = max(0.5, min(self.bubble_scale + step, 2.5))
        # 立即把当前气泡按新尺寸重弹，用户能立刻看到效果
        if self.bubble.visible:
            self.bubble.show(self.bubble._last_notif,
                             kind=self.bubble.kind)

    def _manual_remind(self):
        n = Notification(title="该休息一下了",
                         body="手动提醒：站起来走两步，看看窗外。",
                         level="warn", source="reminder", ttl=12)
        self._controller.handle(n)

    def _toggle_mute(self):
        muted = self._controller.toggle_mute()
        n = Notification(title="🔇 已静音" if muted else "🔊 已取消静音",
                         body="静音期间消息只记录、不弹气泡。" if muted else "新消息会正常弹出。",
                         level="info", source="pika", ttl=6)
        self._controller.handle(n)

    def _about(self):
        from . import __version__
        n = Notification(title=f"皮卡丘 {__version__}",
                         body="本地通知总线 + 桌宠 + 健康提醒。\n"
                              "右键桌宠查看更多操作；外部软件通过总线 POST 消息即可弹气泡。",
                         level="info", source="pika", ttl=12)
        self._controller.handle(n)

    # ---- 隐藏到角落 ----
    def _hide(self):
        self.bubble.close()
        self.root.withdraw()
        tab = tk.Toplevel(self.root)
        tab.overrideredirect(True)
        tab.attributes("-topmost", True)
        try:
            tab.attributes("-transparentcolor", BG)
        except tk.TclError:
            pass
        tab.configure(bg=BG)
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        tab.geometry(f"36x44+{w - 44}+{h - 92}")
        tk.Label(tab, text="⚡", font=("Segoe UI Emoji", 20),
                 bg=BG, fg=YELLOW, cursor="hand2").pack()
        tab.bind("<Button-1>", lambda e: self._show())
        self._tab_win = tab

    def _show(self):
        self.bubble.close()
        if self._tab_win is not None:
            try:
                self._tab_win.destroy()
            except Exception:
                pass
        self._tab_win = None
        self.root.deiconify()
        self.root.lift()

    # ---- 退出 ----
    def _quit(self):
        for attr in ("_tick_job", "_turn_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
            setattr(self, attr, None)
        if self.sse is not None:
            self.sse.stop()
        if self.server is not None:
            self.server.stop()
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="pika-pet", description="皮卡丘桌宠")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--subscribe-only", action="store_true",
                        help="只订阅已有总线，不在本进程开端口")
    args = parser.parse_args(argv)
    if tk is None:
        print("本环境没有 tkinter，无法启动桌宠 GUI", file=sys.stderr)
        return 1
    _make_dpi_aware()
    pet = PikaPet(port=args.port, subscribe_only=args.subscribe_only)
    pet.mainloop()
    return 0


_DPI_SET = False


def _make_dpi_aware():
    """进程级 DPI awareness：让 Tk 的 'tk scaling' 与系统真实 DPI 一致。

    不设置时 Windows 会按 96dpi 裁切/模糊，或 tk scaling 与系统 DPI 不一致
    导致 fonts.measure（逻辑像素）和实际渲染（物理像素）错位——词长测量与
    卡片宽度对不上。必须在任何 Tk 窗口创建之前调用（进程级、一次生效、
    幂等：重复调用不报错）。"""
    global _DPI_SET
    if _DPI_SET:
        return
    _DPI_SET = True
    import ctypes as _c
    try:
        _c.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            _c.windll.user32.SetProcessDPIAware()
        except Exception:
            pass



if __name__ == "__main__":
    raise SystemExit(main())
