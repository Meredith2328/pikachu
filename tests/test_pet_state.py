# -*- coding: utf-8 -*-
import sys
import os
import json
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika import pet_state
from tests.helpers import gui_available


class TestPetState(unittest.TestCase):
    def setUp(self):
        # 重定向到临时文件：绝不碰真实的 runtime/pet_state.json
        fd, self.tmp = tempfile.mkstemp(suffix=".json", prefix="pet_state_")
        os.close(fd)
        os.remove(self.tmp)
        self._orig = pet_state.STATE_FILE
        pet_state.STATE_FILE = type(self._orig)(self.tmp)

    def tearDown(self):
        pet_state.STATE_FILE = self._orig
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

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

    def test_corrupt_file_returns_defaults(self):
        pet_state.STATE_FILE.write_text("{这不是json", encoding="utf-8")
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.0)

    def test_out_of_range_values_clamped(self):
        pet_state.STATE_FILE.write_text(
            json.dumps({"scale": 99, "bubble_scale": 0.01}),
            encoding="utf-8")
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 3.0)
        self.assertEqual(s["bubble_scale"], 0.5)

    def test_bad_types_return_defaults(self):
        pet_state.STATE_FILE.write_text(
            json.dumps({"scale": "很大", "x": "左"}), encoding="utf-8")
        s = pet_state.load_state()
        self.assertEqual(s["scale"], 1.0)
        self.assertIsNone(s["x"])


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestPetStateGuiIntegration(unittest.TestCase):
    def test_set_scale_persists(self):
        from pika.pet import PikaPet
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            orig = pet_state.STATE_FILE
            pet_state.STATE_FILE = type(orig)(td) / "pet_state.json"
            try:
                pet = PikaPet(port=0, subscribe_only=True)
                try:
                    pet.set_scale(1.6)
                    self.assertAlmostEqual(pet_state.load_state()["scale"],
                                           1.6)
                finally:
                    pet._quit()
            finally:
                pet_state.STATE_FILE = orig

    def test_mute_persists_via_toggle(self):
        from pika.pet import PikaPet
        import tempfile as _tf2
        with _tf2.TemporaryDirectory() as td:
            orig = pet_state.STATE_FILE
            pet_state.STATE_FILE = type(orig)(td) / "pet_state.json"
            try:
                pet = PikaPet(port=0, subscribe_only=True)
                try:
                    pet._controller.muted = False
                    pet._toggle_mute()
                    self.assertTrue(pet_state.load_state()["muted"])
                finally:
                    pet._quit()
            finally:
                pet_state.STATE_FILE = orig


if __name__ == "__main__":
    unittest.main()
