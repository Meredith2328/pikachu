# -*- coding: utf-8 -*-
"""像素风设计令牌与绘制小工具。

致敬 pilog（pixel / minimal 主题）：调色板取自 Chrome 离线小恐龙——
纸白 paper · 墨色 ink · 灰 text/mute；2px 实色硬边 + 无模糊偏移硬阴影
+ 等宽字体。气泡（pikapet.bubble）与右键菜单（pikapet.menu）共用本模块。
"""
import re
from pathlib import Path

BG = "magenta"            # 透明色：所有透明区域填充该颜色，靠 -transparentcolor 挖掉
YELLOW = "#FFD93B"
ASSET = Path(__file__).resolve().parent.parent / "assets" / "pikachu.png"
TURN_DIR = Path(__file__).resolve().parent.parent / "assets" / "turn"
TURN_TICK_MS = 33         # ~30fps 的跟随刷新

PIX_PAPER = "#FBF8EE"      # 纸白（微暖，贴皮卡丘）；pilog 是 #f7f7f7
PIX_PANEL = "#F3EFE2"      # 面板底
PIX_INK = "#3C4043"        # 主墨色：边框 / 标题 / 强调文字
PIX_TEXT = "#5F6368"       # 正文
PIX_MUTE = "#9AA0A6"       # 次要文字（来源 / 时间）
PIX_ACCENT = "#1A73E8"     # 链接/强调（pilog 的 accent 蓝）
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
