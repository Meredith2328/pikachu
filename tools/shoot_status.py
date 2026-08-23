# -*- coding: utf-8 -*-
"""开发期用：把状态气泡按几个缩放档位截图出来，肉眼核对排版。

用法: python tools/shoot_status.py 输出前缀.png 0.75,1.0,1.5
不进 CI，只是"改完立刻看一眼"的辅助脚本。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 截图跑在隔离的运行时目录里，不碰用户真实 token/偏好
os.environ.setdefault("PIKACHU_HOME",
                      str(Path(os.environ.get("TEMP", "/tmp")) / "pika-shot"))
os.environ.setdefault("PIKACHU_NO_MIGRATE", "1")

from pikapet.pet import PikaPet, status_budget   # noqa: E402
from pikapet.protocol import Notification       # noqa: E402

SAMPLES = [
    ("zcode", "会话完成 · 修桌宠气泡排版",
     "把状态气泡改成结构化排版，加了颜色层次", "success"),
    ("reminder", "该休息一下了", "看看远处吧，眺望 20 英尺外 20 秒", "warn"),
    ("codex", "会话完成 · 审查 bus.py",
     "发现 3 处问题：缺 import、静默 fallback、端口协商重复", "error"),
    ("dsh", "完成 · 调研 Qt 迁移",
     "结论：协议层不用动，只重写显示层约 1350 行", "success"),
    ("pika", "已取消静音", "新消息会正常弹出。", "info"),
    ("zcode", "开始 · 每日简报", "", "info"),
]


def main(argv):
    out = Path(argv[0])
    scales = [float(x) for x in argv[1].split(",")]
    pet = PikaPet(port=0, subscribe_only=True, with_reminder=False)
    try:
        for source, title, body, level in SAMPLES:
            pet._controller.handle(Notification(
                title=title, body=body, source=source, level=level, ttl=0))
        from PIL import ImageGrab
        for sc in scales:
            pet.bubble_scale = sc
            pet._status_visible = True
            # 画两次：第一次让 Tk 按新字号算出真实尺寸，第二次才截到最终
            # 布局（Toplevel 的 winfo_width 在首次 update 前是旧值）
            pet._draw_status_bubble()
            pet.root.update()
            pet._draw_status_bubble()
            pet.root.update()
            time.sleep(0.35)
            pet.root.update_idletasks()
            win = pet.bubble.win
            x, y = win.winfo_rootx(), win.winfo_rooty()
            w, h = win.winfo_width(), win.winfo_height()
            count, preview = status_budget(sc)
            print(f"scale={sc}: {w}x{h}px, 预算 {count} 条, 摘要={preview}")
            path = out.with_name(f"{out.stem}_{sc}{out.suffix}")
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
            print(f"  → {path}")
    finally:
        pet._quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
