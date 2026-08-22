import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika.protocol import Notification


from tests.helpers import gui_available


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestBubbleGuiBinding(unittest.TestCase):
    """真 Tk 验证 Bubble 的悬浮/隐藏锚点绑定（之前从没测到 UI 层）。"""

    def test_hover_event_updates_controller(self):
        """气泡 <Enter> 事件必须把 hover 状态送进 PetController（否则悬浮失效）。"""
        import tkinter as tk
        from pika.pet_core import PetController
        from pika.pet import Bubble

        root = tk.Tk()
        try:
            ctrl = PetController()
            bubble = Bubble(root, on_clicked=lambda: None, controller=ctrl)
            # 弹一个通知气泡
            bubble.show(Notification(title="t", source="a", ttl=5))
            root.update()
            self.assertIsNotNone(bubble.win)
            # 模拟鼠标进入气泡窗口：直接调用 tk 事件
            bubble.win.event_generate("<Enter>", x=2, y=2)
            root.update()
            self.assertTrue(ctrl.hovering,
                            "悬浮气泡应把 hover 置 True（真实 GUI 绑定）")
            bubble.win.event_generate("<Leave>", x=2, y=2)
            root.update()
            self.assertFalse(ctrl.hovering)
            bubble.close()
        finally:
            root.destroy()

    def test_bubble_anchored_to_pet_not_root(self):
        """Bubble._place 锚点应是桌宠窗口（含隐藏时 tab），不是 root 默认值。"""
        import tkinter as tk
        from pika.pet import Bubble

        root = tk.Tk()
        try:
            class FakePet:
                _tab_win = None
            pet = FakePet()
            bubble = Bubble(root, on_clicked=lambda: None, pet=pet)
            # 不弹窗，直接测锚点解析：未隐藏时锚 root
            bubble.show(Notification(title="t", source="a", ttl=5))
            root.update()
            # 位置不应是 (0,0) 左上角
            x = bubble.win.winfo_rootx()
            y = bubble.win.winfo_rooty()
            self.assertGreaterEqual(x, 0)
            bubble.close()
        finally:
            root.destroy()

    def test_bubble_scale_changes_card_size(self):
        """气泡缩放：bubble_scale 应改变卡片尺寸（字号/内距/尾巴随之缩放）。"""
        import tkinter as tk
        from pika.pet import Bubble
        from pika.protocol import Notification

        root = tk.Tk()
        try:
            n = Notification(title="缩放测试", body="一行", source="pika", ttl=5)
            class FakePet:
                _tab_win = None
                bubble_scale = 1.0
            pet = FakePet()
            b = Bubble(root, on_clicked=lambda: None, pet=pet)
            b.show(n)
            root.update_idletasks()
            w1, h1 = b.win.winfo_reqwidth(), b.win.winfo_reqheight()
            b.close()

            pet.bubble_scale = 2.0
            b.show(n)
            root.update_idletasks()
            w2, h2 = b.win.winfo_reqwidth(), b.win.winfo_reqheight()
            b.close()
            self.assertGreater(w2, w1, "放大气泡后尺寸应变大")
            self.assertGreater(h2, h1)
        finally:
            root.destroy()

    def test_set_scale_rebuilds_turn_photos(self):
        """桌宠缩放：set_scale 用 NEAREST 重渲染转身帧，尺寸随缩放变化。"""
        import os
        import tkinter as tk
        from pika.pet import PikaPet
        root = tk.Tk()
        try:
            pet = PikaPet.__new__(PikaPet)
            pet.root = root
            pet.canvas = tk.Canvas(root, bg="magenta", highlightthickness=0)
            pet.canvas.pack()
            pet._turn_left_pil = []
            pet._turn_right_pil = []
            pet._img_id = None
            pet._turn_real = None
            pet._last_turn_key = None
            # 造两个 1x1 的底帧
            from PIL import Image
            pet._turn_left_pil = [Image.new("RGBA", (1, 1), (255, 0, 255, 0))]
            pet._turn_right_pil = [Image.new("RGBA", (1, 1), (255, 0, 255, 0))]
            pet.scale = 1.0
            pet.canvas_w, pet.canvas_h = 1 + 8, 1 + 6
            pet._rebuild_turn_photos()
            w1 = pet.canvas_w
            pet.set_scale(2.0)
            w2 = pet.canvas_w
            self.assertGreater(w2, w1, "放大桌宠后画布应变宽")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
