# -*- coding: utf-8 -*-
"""桌宠状态持久化：缩放 / 静音 / 窗口位置，重启不丢。

状态文件在运行时目录（见 pikapet.paths）。读的一侧宽容——文件不存在是首次
启动的正常情况，内容坏了也不该挡住桌宠启动，回退默认值即可，但**坏内容
一定记一条日志**，不静默。写的一侧用原子替换，失败记 WARNING（丢一次
偏好不值得崩 UI，但用户改了设置却存不上应该有迹可循）。
"""
import json

from . import paths
from .logs import get_logger

log = get_logger("pet_state")

_DEFAULTS = {
    "scale": 1.0,          # 桌宠缩放
    "bubble_scale": 1.0,   # 气泡缩放
    "muted": False,        # 静音
    "x": None,             # 窗口位置（None = 用默认右下角）
    "y": None,
}

SCALE_RANGE = (0.4, 3.0)
BUBBLE_SCALE_RANGE = (0.5, 2.5)


def state_file():
    """状态文件路径（每次读取，便于测试改 PIKACHU_HOME）。"""
    return paths.pet_state_file()


def load_state() -> dict:
    """读状态；文件缺失走默认值，内容异常记日志后走默认值。"""
    paths.migrate_legacy_once()   # 老用户的偏好在旧 runtime/ 里，先搬过来
    state = dict(_DEFAULTS)
    path = state_file()
    if not path.is_file():
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("状态文件 %s 读取失败，改用默认值：%s", path, e)
        return state
    if not isinstance(data, dict):
        log.warning("状态文件 %s 不是 JSON 对象，改用默认值", path)
        return state
    for k in state:
        if k in data and data[k] is not None:
            state[k] = data[k]
    # 类型钳制：坏数据不允许进入 UI（如 scale 写成字符串）
    try:
        state["scale"] = _clamp(float(state["scale"]), *SCALE_RANGE)
        state["bubble_scale"] = _clamp(float(state["bubble_scale"]),
                                       *BUBBLE_SCALE_RANGE)
        state["muted"] = bool(state["muted"])
        for k in ("x", "y"):
            state[k] = int(state[k]) if state[k] is not None else None
    except (TypeError, ValueError) as e:
        log.warning("状态文件 %s 字段类型异常，整体改用默认值：%s", path, e)
        return dict(_DEFAULTS)
    return state


def save_state(**fields) -> None:
    """合并写入部分字段。失败记 WARNING 但不抛：丢一次偏好不该崩 UI。"""
    try:
        state = load_state()
        state.update(fields)
        paths.write_text_atomic(
            paths.pet_state_file(create_dir=True),
            json.dumps(state, ensure_ascii=False, indent=1))
    except (OSError, paths.RuntimeDirError) as e:
        log.warning("状态保存失败（本次偏好未落盘）：%s", e)


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))
