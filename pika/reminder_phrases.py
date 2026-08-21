# -*- coding: utf-8 -*-
"""健康提醒文案池：分类存放，带权重随机挑选。

新增文案只需在这里加条目，不需要改代码。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Phrase:
    text: str
    weight: float = 1.0


PHRASES = {
    "eye": [
        Phrase("看看远处吧", 1.0),
        Phrase("眼睛休息一下，眺望 20 英尺外 20 秒", 1.0),
        Phrase("闭目养神 30 秒，眼睛会感谢你", 0.8),
        Phrase("眨眼频率低了吧？刻意多眨几下", 0.6),
        Phrase("视线离开屏幕，看看窗外的绿树", 0.8),
    ],
    "neck": [
        Phrase("活动一下颈椎，前后左右慢慢转一圈", 1.0),
        Phrase("脖子僵了吧？做个米字操", 0.8),
        Phrase("耸肩、绕肩，放松斜方肌", 0.6),
    ],
    "water": [
        Phrase("喝口水，补充水分", 1.0),
        Phrase("该喝水了，杯子就在手边", 0.8),
        Phrase("起身接杯水，顺便活动一下", 0.8),
        Phrase("今天喝够水了吗？再来一杯", 0.6),
    ],
    "stand": [
        Phrase("站起来走两步，伸个懒腰", 1.0),
        Phrase("离开椅子，去窗边站一会儿", 0.8),
        Phrase("久坐伤身，起来活动三分钟", 0.8),
        Phrase("站一会儿吧，腿会舒服很多", 0.7),
    ],
    "screen": [
        Phrase("离屏幕远一点，眼睛离得太近啦", 1.0),
        Phrase("往后靠靠，保持一臂距离", 0.9),
        Phrase("屏幕有点近了，退后一点再继续", 0.8),
        Phrase("身体坐直，和屏幕拉开点距离", 0.7),
    ],
    "walk": [
        Phrase("出门散散步吧，透透气", 1.0),
        Phrase("下楼走一圈，回来效率更高", 0.9),
        Phrase("去外面晒晒太阳，走个十分钟", 0.8),
        Phrase("走远一点，就当遛自己", 0.7),
    ],
    "posture": [
        Phrase("坐姿检查：背挺直，肩膀放松", 1.0),
        Phrase("别瘫着啦，腰后面垫个靠枕", 0.8),
        Phrase("手腕悬空太久会酸，垫一下", 0.6),
    ],
}

DEFAULT_PHRASES = PHRASES


def pick(category: str, pool=None, rng=None) -> str:
    """按权重从某分类随机挑一条文案。分类不存在时抛出 KeyError。"""
    pool = pool or DEFAULT_PHRASES
    rng = rng or __import__("random").Random()
    items = pool[category]
    total = sum(p.weight for p in items)
    pick_v = rng.random() * total
    acc = 0.0
    for p in items:
        acc += p.weight
        if pick_v <= acc:
            return p.text
    return items[-1].text


def pick_by_config(categories, pool=None, rng=None) -> str:
    """从多个分类里随机选一个分类，再按权重挑一条文案。"""
    rng = rng or __import__("random").Random()
    if not categories:
        raise KeyError("categories 不能为空")
    category = rng.choice(list(categories))
    return pick(category, pool=pool, rng=rng)
