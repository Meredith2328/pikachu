# -*- coding: utf-8 -*-
"""业务气泡内容的最小 Markdown 渲染器。

把 Markdown 文本转成「行列表」模型，供 Tk Canvas 逐行绘制——纯标准库、
零依赖、离线可跑，专为"桌面气泡不能糊成一大坨"设计。支持的子集：

- 标题：# / ## / ###
- 列表：- 或 * 无序项，1. 有序项
- 引用：> 引导行
- 分隔线：--- / *** / ___
- 行内格式：**粗体**  *斜体*  `行内代码`  [链接文字](url)
- 前后空白行折叠，多余的 # 号一律降级为纯文本

每行是 (kind, segments)，segments 是 (text, style) 列表，style 是
{'bold','italic','code','link'} 的并集，link 额外带 url。分隔线行
kind='rule' 无 segments。这种"结构直出"的模型让气泡用最少的 Canvas
绘制命令就能画出带样式的富文本，又不需要引入完整 Markdown 引擎。
"""
import re

# 行级前缀
_H1 = re.compile(r"^#{1,6}\s+(.*)$")
_LIST = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OLIST = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")

# 行内 token
_TOKEN = re.compile(
    r"(\*\*([^*]+)\*\*)"          # 粗体
    r"|\*([^*\s][^*]*?)\*"        # 斜体
    r"|(`([^`]+)`)"               # 行内代码
    r"|(\[([^\]]+)\]\(([^)\s]+)\))"  # 链接
)
_STRIP_ANCHOR = re.compile(r"\s+#+$")  # 标题末尾自动锚点

# 行内代码里的原始文本（未加粗斜体干扰）
_CODE_LINE = re.compile(r"`([^`]*)`")


def _parse_inline(text: str) -> list:
    """把一行纯文本拆成 (text, style[, url]) 片段。

    普通片段为 (text, style)；链接片段为 (text, style, url)，
    供气泡绘制时做点击命中。后续消费方按 len==3 判断是否带 url。
    """
    segs = []
    pos = 0
    style_stack = set()
    for m in _TOKEN.finditer(text):
        start = m.start()
        if start > pos:
            segs.append((text[pos:start], frozenset(style_stack)))
        if m.group(2) is not None:      # **bold**
            segs.append((m.group(2), frozenset(style_stack | {"bold"})))
        elif m.group(3) is not None:    # *italic*
            segs.append((m.group(3), frozenset(style_stack | {"italic"})))
        elif m.group(5) is not None:    # `code`
            segs.append((m.group(5), frozenset(style_stack | {"code"})))
        elif m.group(6) is not None:    # [text](url)
            segs.append((m.group(7), frozenset(style_stack | {"link"}),
                         m.group(8)))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:], frozenset(style_stack)))
    return segs


def render(md: str) -> list:
    """Markdown → [(kind, segments)] 行模型。kind ∈
    h1/h2/h3/para/list/quote/rule；segments 见 _parse_inline。"""
    if md is None:
        return []
    lines = str(md).splitlines()
    out = []
    pending = []          # 收集的普通段落文字
    prev_blank = True     # 首行前视为有空白，避免开头多余空行

    def flush_para():
        nonlocal pending
        if pending:
            out.append(("para", _parse_inline(" ".join(pending))))
            pending = []

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if pending:
                flush_para()
            prev_blank = True
            continue

        m = _H1.match(line)
        if m and not _RULE.match(line):
            flush_para()
            text = _STRIP_ANCHOR.sub("", m.group(1)).strip() or m.group(1).strip()
            lvl = min(3, len(m.group(0)) - len(m.group(0).lstrip("#")))
            out.append((f"h{lvl}", _parse_inline(text)))
            prev_blank = False
            continue

        if _RULE.match(line):
            flush_para()
            out.append(("rule", []))
            prev_blank = False
            continue

        mq = _QUOTE.match(line)
        if mq:
            flush_para()
            out.append(("quote", _parse_inline(mq.group(1))))
            prev_blank = False
            continue

        ml = _LIST.match(line)
        if ml and ml.group(1).strip() == "":
            flush_para()
            out.append(("list", _parse_inline(ml.group(2))))
            prev_blank = False
            continue

        mo = _OLIST.match(line)
        if mo and mo.group(1).strip() == "":
            flush_para()
            out.append(("list", _parse_inline(mo.group(2))))  # 有序项并入列表行
            prev_blank = False
            continue

        # 普通文本（含带缩进的多行）先累积，遇空白/块结构再 flush
        if not prev_blank and pending:
            pending.append(line.strip())
        else:
            pending.append(line.strip())
        prev_blank = False

    flush_para()
    return out
