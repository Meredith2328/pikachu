# -*- coding: utf-8 -*-
import sys
import os
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet import pet_state
from tests.helpers import gui_available, isolated_home


class TestPetState(unittest.TestCase):
    def setUp(self):
        # 隔离运行时目录：绝不碰用户真实的 pet_state.json
        self._home = isolated_home()
        self.home = self._home.__enter__()

    def tearDown(self):
        self._home.__exit__(None, None, None)

    def test_roundtrip(self):
        pet_state.save_state(scale=1.5, muted=True, x=100, y=200)
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.5)
        self.assertTrue(s["muted"])
        self.assertEqual((s["x"], s["y"]), (100, 200))
        # 未写的字段保持默认
        self.assertEqual(s["bubble_scale"], 1.0)

    def test_partial_update_keeps_others(self):
        pet_state.save_state(scale=2.0, x=10, y=20)
        pet_state.save_state(muted=True)          # 只改静音
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 2.0)         # 之前的还在
        self.assertEqual((s["x"], s["y"]), (10, 20))
        self.assertTrue(s["muted"])

    def test_missing_file_returns_defaults(self):
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.0)
        self.assertFalse(s["muted"])
        self.assertIsNone(s["x"])

    def test_corrupt_file_returns_defaults_and_logs(self):
        """坏文件回退默认值，但要留一条日志——不静默。"""
        pet_state.state_file().parent.mkdir(parents=True, exist_ok=True)
        pet_state.state_file().write_text("{这不是json", encoding="utf-8")
        with self.assertLogs("pikachu.pet_state", level="WARNING") as cm:
            s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.0)
        self.assertTrue(any("读取失败" in m for m in cm.output))

    def test_out_of_range_values_clamped(self):
        pet_state.state_file().parent.mkdir(parents=True, exist_ok=True)
        pet_state.state_file().write_text(
            json.dumps({"scale": 99, "bubble_scale": 0.01}),
            encoding="utf-8")
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 3.0)
        self.assertEqual(s["bubble_scale"], 0.5)

    def test_bad_types_return_defaults_and_log(self):
        pet_state.state_file().parent.mkdir(parents=True, exist_ok=True)
        pet_state.state_file().write_text(
            json.dumps({"scale": "很大", "x": "左"}), encoding="utf-8")
        with self.assertLogs("pikachu.pet_state", level="WARNING") as cm:
            s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.0)
        self.assertIsNone(s["x"])
        self.assertTrue(any("类型异常" in m for m in cm.output))

    def test_non_object_json_returns_defaults_and_logs(self):
        pet_state.state_file().parent.mkdir(parents=True, exist_ok=True)
        pet_state.state_file().write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertLogs("pikachu.pet_state", level="WARNING"):
            s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.0)


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestPetStateGuiIntegration(unittest.TestCase):
    def test_set_scale_persists(self):
        from pikapet.pet import PikaPet
        with isolated_home():
            pet = PikaPet(port=0, subscribe_only=True)
            try:
                pet.set_scale(1.6)
                self.assertAlmostEqual(pet_state.load_state()["scale"], 1.6)
            finally:
                pet._quit()

    def test_mute_persists_via_toggle(self):
        from pikapet.pet import PikaPet
        with isolated_home():
            pet = PikaPet(port=0, subscribe_only=True)
            try:
                pet._controller.muted = False
                pet._toggle_mute()
                self.assertTrue(pet_state.load_state()["muted"])
            finally:
                pet._quit()


if __name__ == "__main__":
    unittest.main()
