# -*- coding: utf-8 -*-
"""适配器共享的纯函数：文案截断与阶段映射。

各适配器仍独立可运行（只依赖 pika.bus / pika.protocol），但不再各抄
一份 collapse 和 stage 表——统一标题语法「{事件词} · {名称}」也由
这里的 stage_title 一处定义。
"""

# 阶段 → （级别，中文事件词）。级别的视觉语义由气泡徽章/配色表达，
# 标题不放 emoji
STAGE_STYLE = {
    "start": ("info", "开始"),
    "done": ("success", "完成"),
    "error": ("error", "失败"),
    "run": ("info", "进行中"),
}


def stage_level(stage: str) -> str:
    return STAGE_STYLE.get(stage, ("info", "进行中"))[0]


def stage_title(stage: str, name: str) -> str:
    """统一标题语法：「{事件词} · {名称}」。"""
    return f"{STAGE_STYLE.get(stage, ('info', '进行中'))[1]} · {name}"


def collapse(text, limit):
    """压平空白并截断到 limit，末尾加省略号。"""
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")
