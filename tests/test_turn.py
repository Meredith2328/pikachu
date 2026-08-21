# -*- coding: utf-8 -*-
"""鼠标跟随转身：TurnDirector 决策数学、帧路径加载、真 Tk 渲染集成。"""
import sys
import os
import time
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika.turn import TurnDirector, frame_index, turn_frame_paths


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def director_at(mx_rel, my_rel, **kw) -> TurnDirector:
    """构造已收敛到给定相对鼠标位置的导演（桌宠中心为原点）。"""
    clock = FakeClock()
    d = TurnDirector(clock=clock, **kw)
    for _ in range(400):
        clock.advance(0.033)
        d.update(mx_rel, my_rel, 0, 0)
    return d


class TestTurnDirector(unittest.TestCase):
    def test_front_when_mouse_directly_above(self):
        d = director_at(0, -300)
        self.assertEqual(d.direction, 0)
        self.assertAlmostEqual(d.pose, 0.0, places=3)

    def test_commits_left_beyond_enter_and_pose_rises(self):
        clock = FakeClock()
        d = TurnDirector(clock=clock)
        poses = []
        for _ in range(200):
            clock.advance(0.033)
            pose, direction = d.update(-200, -60, 0, 0)  # atan2(200,60)≈73°
            poses.append(pose)
        self.assertEqual(d.direction, -1)
        self.assertGreater(poses[-1], 0.9)
        # 平滑：姿态单调逼近，无过冲
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(poses, poses[1:])))
        self.assertLessEqual(poses[-1], 1.0)

    def test_mirror_symmetry(self):
        left = director_at(-300, -10)
        right = director_at(300, -10)
        self.assertEqual(left.direction, -1)
        self.assertEqual(right.direction, 1)
        self.assertAlmostEqual(left.pose, right.pose, places=3)

    def test_hysteresis_keeps_direction_in_band(self):
        """已认定朝向后，回到 enter/exit 之间的滞回带不丢方向。"""
        clock = FakeClock()
        d = TurnDirector(clock=clock)
        for _ in range(100):
            clock.advance(0.033)
            d.update(-100, -80, 0, 0)  # ≈51°，认定朝左
        self.assertEqual(d.direction, -1)
        for _ in range(100):
            clock.advance(0.033)
            d.update(-45, -90, 0, 0)   # ≈26°，在 exit(22)~enter(32) 之间
        self.assertEqual(d.direction, -1,
                         "滞回带内不应回到正面（否则头顶附近会抖动）")

    def test_flip_when_sweeping_to_opposite_side(self):
        """已认定朝左后鼠标扫到右侧：必须先平滑转回正面，在正面帧才换边，
        再转向右侧——任何时刻都不允许姿态 > 0 时直接镜像跳变。"""
        clock = FakeClock()
        d = TurnDirector(clock=clock)
        for _ in range(100):
            clock.advance(0.033)
            d.update(-500, 0, 0, 0)
        self.assertEqual(d.direction, -1)
        flip_prev = None    # 换边发生前那一瞬间的姿态
        flip_first = None   # 换边后第一帧的姿态
        pose = None
        prev_pose = d.pose
        for i in range(600):
            clock.advance(0.033)
            pose, direction = d.update(500, 0, 0, 0)
            if i == 2:
                # 刚扫过去时还在朝左回正的过程中，不能瞬间翻到右
                self.assertEqual(direction, -1)
                self.assertGreater(pose, 0.3)
            if direction == 1 and flip_prev is None:
                flip_prev = prev_pose
                flip_first = pose
            prev_pose = pose
        self.assertIsNotNone(flip_prev, "最终应转到右侧")
        self.assertLessEqual(flip_prev, TurnDirector.SWITCH_EPS,
                             "换边只能发生在正面帧（左右第 0 帧相同）")
        self.assertLess(flip_first, 0.5,
                        "换边后第一帧只前进一个平滑步长，不能瞬间满姿态")
        self.assertGreater(pose, 0.9)

    def test_returns_front_below_exit(self):
        d = director_at(-500, 0)
        self.assertEqual(d.direction, -1)
        clock = FakeClock()
        d2 = TurnDirector(clock=clock)
        for _ in range(100):
            clock.advance(0.033)
            d2.update(-500, 0, 0, 0)
        pose = None
        for _ in range(200):
            clock.advance(0.033)
            pose, direction = d2.update(0, -500, 0, 0)  # 正上方 → 回正面
        self.assertEqual(direction, 0)
        self.assertAlmostEqual(pose, 0.0, places=3)

    def test_smoothing_prevents_instant_jump(self):
        """从静止一步到水平位置：单帧姿态不能直接到满。"""
        clock = FakeClock()
        d = TurnDirector(clock=clock)
        pose, _ = d.update(-500, 0, 0, 0)
        self.assertLess(pose, 0.9)

    def test_reset(self):
        d = director_at(-500, 0)
        d.reset()
        self.assertEqual(d.pose, 0.0)
        self.assertEqual(d.direction, 0)

    def test_settled_flag(self):
        """静止判定：初始即静止；甩到远处后未收敛时 False，收敛后 True。"""
        d = TurnDirector()
        self.assertTrue(d.settled)  # 初始 pose=target=0
        clock = FakeClock()
        d2 = TurnDirector(clock=clock)
        clock.advance(0.033)
        d2.update(-500, 0, 0, 0)
        self.assertFalse(d2.settled, "刚起步姿态远未到目标，不应算静止")
        for _ in range(400):
            clock.advance(0.033)
            d2.update(-500, 0, 0, 0)
        self.assertTrue(d2.settled, "收敛后应判定为静止（吸附真实帧的依据）")

    def test_invalid_params_rejected(self):
        with self.assertRaises(ValueError):
            TurnDirector(level_deg=10, up_deg=20)
        with self.assertRaises(ValueError):
            TurnDirector(enter_deg=20, exit_deg=30)
        with self.assertRaises(ValueError):
            TurnDirector(tau=0)


class TestFrameIndex(unittest.TestCase):
    def test_mapping_and_clamp(self):
        self.assertEqual(frame_index(0.0, 25), 0)
        self.assertEqual(frame_index(1.0, 25), 24)
        self.assertEqual(frame_index(0.5, 25), 12)
        self.assertEqual(frame_index(2.0, 25), 24)
        self.assertEqual(frame_index(-1.0, 25), 0)

    def test_degenerate_counts(self):
        self.assertEqual(frame_index(0.7, 1), 0)
        self.assertEqual(frame_index(0.7, 0), 0)

    def test_snap_to_real_indices(self):
        """给出真实帧集合时吸附到最近的真实帧（混合补帧不用于静止画面）。"""
        real = [0, 2, 4, 6]
        self.assertEqual(frame_index(0.5, 7, real), 2)   # 原始 3，平手取小
        self.assertEqual(frame_index(0.3, 7, real), 2)   # 原始即真实帧
        self.assertEqual(frame_index(0.9, 7, real), 4)   # 原始 5，夹在 4/6 之间
        self.assertEqual(frame_index(1.0, 7, real), 6)
        self.assertEqual(frame_index(0.0, 7, real), 0)
        # 空集合 / None：退回普通映射
        self.assertEqual(frame_index(0.5, 7, []), 3)
        self.assertEqual(frame_index(0.5, 7, None), 3)


def make_frame_dir(root: Path, n_left: int, n_right: int) -> Path:
    from PIL import Image
    for side, n in (("left", n_left), ("right", n_right)):
        d = root / side
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            Image.new("RGB", (4, 4), (i * 40 % 255, 100, 100)).save(d / f"{i:02d}.png")
    return root


class TestTurnFramePaths(unittest.TestCase):
    def test_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_frame_dir(root, 3, 3)
            got = turn_frame_paths(root)
            self.assertIsNotNone(got)
            left, right = got
            self.assertEqual([p.name for p in left], ["00.png", "01.png", "02.png"])
            self.assertEqual(len(right), 3)

    def test_missing_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(turn_frame_paths(Path(td)))

    def test_count_mismatch_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_frame_dir(root, 3, 5)
            self.assertIsNone(turn_frame_paths(root))

    def test_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_frame_dir(root, 0, 0)
            self.assertIsNone(turn_frame_paths(root))


def gui_available():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestPetTurnIntegration(unittest.TestCase):
    """真 Tk：PikaPet 加载帧资产后按坐标渲染对应帧。"""

    def _make_pet(self):
        import tkinter as tk
        from pika.bus import BusServer
        from pika.pet import PikaPet
        bus_srv = BusServer(port=0).start()
        pet = PikaPet(port=bus_srv.port)
        return pet, bus_srv

    def test_load_and_render_left_right_front(self):
        import tkinter as tk
        from pika.turn import TurnDirector
        from pika.pet import TURN_TICK_MS

        pet, bus_srv = self._make_pet()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                make_frame_dir(root, 5, 5)
                self.assertTrue(pet._load_turn_assets(root))
                # 停掉背景跟随 tick：真实全局光标位置会污染合成坐标序列
                if pet._turn_job is not None:
                    pet.root.after_cancel(pet._turn_job)
                    pet._turn_job = None
                # 注入极小 tau 的导演：一步收敛，断言确定性的末态帧
                pet._director = TurnDirector(tau=1e-6)
                tk_root = pet.root
                tk_root.update()
                cx = tk_root.winfo_rootx() + pet.size // 2
                cy = tk_root.winfo_rooty() + pet.size // 2

                cur = lambda: pet.canvas.itemcget(pet._img_id, "image")

                pet._turn_step(cx, cy - 500)  # 正上 → 正面第 0 帧
                self.assertEqual(cur(), str(pet._turn_left[0]))

                pet._turn_step(cx - 500, cy)  # 正左水平 → 满姿态原始帧
                self.assertEqual(cur(), str(pet._turn_left[-1]))

                # 直接扫到正右：第一步先回正面（换边必须经过正面帧），
                # 第二步才以镜像帧满姿态朝右
                pet._turn_step(cx + 500, cy)
                self.assertEqual(cur(), str(pet._turn_left[0]))
                pet._turn_step(cx + 500, cy)
                self.assertEqual(cur(), str(pet._turn_right[-1]))

                pet._turn_step(cx, cy - 500)  # 回正上：先收回正面（右侧第 0 帧）
                self.assertEqual(cur(), str(pet._turn_right[0]))
        finally:
            pet._quit()
            bus_srv.stop()

    def test_tick_turn_scheduled_and_cancelled(self):
        pet, bus_srv = self._make_pet()
        try:
            self.assertIsNotNone(pet._turn_job)
        finally:
            pet._quit()
            bus_srv.stop()
        # 退出后跟随 tick 任务应被取消
        self.assertIsNone(pet._turn_job)

    def test_real_assets_load_if_present(self):
        """仓库自带 assets/turn 时应能加载且两侧数量一致；没有则跳过。"""
        from pika.pet import TURN_DIR
        paths = turn_frame_paths(TURN_DIR)
        if paths is None:
            self.skipTest("assets/turn 未生成")
        left, right = paths
        self.assertGreaterEqual(len(left), 10)
        self.assertEqual(len(left), len(right))


if __name__ == "__main__":
    unittest.main()
