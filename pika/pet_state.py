# -*- coding: utf-8 -*-
"""桌宠状态持久化：缩放 / 静音 / 窗口位置，重启不丢。

状态文件在 runtime/pet_state.json（已被 .gitignore 排除）。读写都做成
"尽力而为"：文件损坏、缺字段、目录不存在一律回退默认值，绝不让持久化
问题挡住桌宠启动。
"""
import json
import os
from pathlib import Path

STATE_FILE = (Path(__file__).resolve().parent.parent
              / "runtime" / "pet_state.json")

_DEFAULTS = {
    "scale": 1.0,          # 桌宠缩放
    "bubble_scale": 1.0,   # 气泡缩放
    "muted": False,        # 静音
    "x": None,             # 窗口位置（None = 用默认右下角）
    "y": None,
}


def load_state() -> dict:
    """读状态；任何异常都返回默认值副本。"""
    state = dict(_DEFAULTS)
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in state:
                if k in data and data[k] is not None:
                    state[k] = data[k]
    except Exception:
        pass
    # 类型钳制：坏数据不允许进入 UI（如 scale 写成字符串）
    try:
        state["scale"] = max(0.4, min(float(state["scale"]), 3.0))
        state["bubble_scale"] = max(0.5, min(float(state["bubble_scale"]), 2.5))
        state["muted"] = bool(state["muted"])
        for k in ("x", "y"):
            state[k] = int(state[k]) if state[k] is not None else None
    except Exception:
        return dict(_DEFAULTS)
    return state


def save_state(**fields) -> None:
    """合并写入部分字段；失败静默（丢一次状态无所谓，别崩 UI）。"""
    try:
        state = load_state()
        state.update(fields)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, STATE_FILE)   # 原子替换，避免写一半损坏
    except Exception:
        pass
