# -*- coding: utf-8 -*-
"""偏好记忆：桌宠大小 / 气泡大小 / 退出位置，重启后要恢复。

这些原本就有持久化，但有两个漏：缩放会顺带挪窗口却只存 scale；被强杀时
走不到 _quit 那条保存路径。下面的用例把这两条钉住。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet import pet_state
from tests.helpers import gui_available, isolated_home


@unittest.skipUnless(gui_available(), "无 GUI 环境")
class TestPreferenceMemory(unittest.TestCase):
    def _pet(self):
        from pikapet.pet import PikaPet
        return PikaPet(port=0, subscribe_only=True, with_reminder=False)

    def _saved(self):
        return json.loads(pet_state.state_file().read_text(encoding="utf-8"))

    def test_pet_scale_survives_restart(self):
        with isolated_home():
            pet = self._pet()
            try:
                pet.set_scale(1.75)
            finally:
                pet._quit()
            self.assertAlmostEqual(self._saved()["scale"], 1.75, places=4)

            again = self._pet()
            try:
                self.assertAlmostEqual(again.scale, 1.75, places=4)
            finally:
                again._quit()

    def test_bubble_scale_survives_restart(self):
        with isolated_home():
            pet = self._pet()
            try:
                pet._zoom_bubble(0.5)
                expected = pet.bubble_scale
            finally:
                pet._quit()
            self.assertAlmostEqual(self._saved()["bubble_scale"], expected,
                                   places=4)

            again = self._pet()
            try:
                self.assertAlmostEqual(again.bubble_scale, expected, places=4)
            finally:
                again._quit()

    def test_position_survives_restart(self):
        with isolated_home():
            pet = self._pet()
            try:
                pet.root.geometry("+301+202")
                pet.root.update_idletasks()
            finally:
                pet._quit()
            saved = self._saved()
            self.assertEqual((saved["x"], saved["y"]), (301, 202))

            again = self._pet()
            try:
                again.root.update_idletasks()
                self.assertEqual(
                    (again.root.winfo_x(), again.root.winfo_y()), (301, 202))
            finally:
                again._quit()

    def test_zoom_also_persists_position(self):
        """缩放会 recenter 挪窗口——位置必须跟着存，否则下次启动会跳一下。"""
        with isolated_home():
            pet = self._pet()
            try:
                pet.root.geometry("+400+300")
                pet.root.update_idletasks()
                pet._save_state("x", "y")
                pet.set_scale(2.0)          # 内部会 recenter
                pet.root.update_idletasks()
                moved = (pet.root.winfo_x(), pet.root.winfo_y())
                saved = self._saved()
                self.assertEqual((saved["x"], saved["y"]), moved)
                self.assertAlmostEqual(saved["scale"], 2.0, places=4)
            finally:
                pet._quit()

    def test_mute_survives_restart(self):
        with isolated_home():
            pet = self._pet()
            try:
                pet._controller.muted = False
                pet._toggle_mute()
            finally:
                pet._quit()
            self.assertTrue(self._saved()["muted"])

            again = self._pet()
            try:
                self.assertTrue(again._controller.muted)
            finally:
                again._quit()

    def test_autosave_persists_without_clean_quit(self):
        """被强杀（走不到 _quit）时，定期自动落盘要已经把偏好存下来。"""
        with isolated_home():
            pet = self._pet()
            try:
                pet.set_scale(1.4, recenter=False)
                pet.bubble_scale = 1.8
                pet.root.geometry("+150+160")
                pet.root.update_idletasks()
                # 绕过节流，模拟"又过了 5 秒"
                pet._last_autosave -= 10.0
                pet._autosave_state()
                saved = self._saved()
                self.assertAlmostEqual(saved["scale"], 1.4, places=4)
                self.assertAlmostEqual(saved["bubble_scale"], 1.8, places=4)
                self.assertEqual((saved["x"], saved["y"]), (150, 160))
            finally:
                pet._quit()

    def test_autosave_writes_on_first_effective_tick(self):
        """首次有效落盘就得写：启动后头几秒改了大小又被强杀不能丢。"""
        with isolated_home():
            pet = self._pet()
            try:
                pet.bubble_scale = 2.25
                pet._last_autosave -= 10.0
                pet._autosave_state()
                self.assertAlmostEqual(self._saved()["bubble_scale"], 2.25,
                                       places=4)
            finally:
                pet._quit()

    def test_autosave_skips_write_when_unchanged(self):
        """没变化就不写盘：500ms 一次的 tick 不该一直磨硬盘。"""
        with isolated_home():
            pet = self._pet()
            try:
                pet._last_autosave -= 10.0
                pet._autosave_state()            # 第一次会写
                path = pet_state.state_file()
                before = path.stat().st_mtime_ns
                for _ in range(3):
                    pet._last_autosave -= 10.0
                    pet._autosave_state()        # 内容未变，应全部跳过
                self.assertEqual(before, path.stat().st_mtime_ns)
            finally:
                pet._quit()

    def test_autosave_throttled(self):
        """节流生效：距上次不足 AUTOSAVE_SEC 的调用直接返回。"""
        with isolated_home():
            pet = self._pet()
            try:
                pet._last_autosave -= 10.0
                pet._autosave_state()
                path = pet_state.state_file()
                before = path.stat().st_mtime_ns
                pet.bubble_scale = 2.4          # 有变化
                pet._autosave_state()           # 但被节流，不该写
                self.assertEqual(before, path.stat().st_mtime_ns)
            finally:
                pet._quit()

    def test_offscreen_position_clamped_on_restore(self):
        """换了显示器/改了分辨率后，旧坐标不能把皮卡丘放到屏幕外。"""
        with isolated_home():
            pet_state.save_state(x=999999, y=999999, scale=1.0,
                                 bubble_scale=1.0, muted=False)
            pet = self._pet()
            try:
                pet.root.update_idletasks()
                sw = pet.root.winfo_screenwidth()
                sh = pet.root.winfo_screenheight()
                self.assertLessEqual(pet.root.winfo_x(), sw)
                self.assertLessEqual(pet.root.winfo_y(), sh)
            finally:
                pet._quit()


if __name__ == "__main__":
    unittest.main()
