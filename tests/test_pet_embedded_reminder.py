# -*- coding: utf-8 -*-
import sys
import os
import unittest
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
class TestEmbeddedReminder(unittest.TestCase):
    def _pet(self, with_reminder=True):
        from pika.pet import PikaPet
        return PikaPet(port=0, subscribe_only=True,
                       with_reminder=with_reminder)

    def test_reminder_thread_starts_by_default(self):
        pet = self._pet()
        try:
            self.assertIsNotNone(pet._reminder_thread)
            self.assertTrue(pet._reminder_thread.is_alive())
        finally:
            pet._quit()

    def test_no_reminder_flag_skips_thread(self):
        pet = self._pet(with_reminder=False)
        try:
            self.assertIsNone(pet._reminder_thread)
        finally:
            pet._quit()

    def test_quit_stops_thread(self):
        pet = self._pet()
        thread = pet._reminder_thread
        pet._quit()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def test_sink_routes_to_controller(self):
        """提醒 sink 的通知走 UI 线程进控制器（静音/去重链路生效）。"""
        pet = self._pet()
        try:
            before = len(pet._controller.recent())
            pet._reminder_sink().send(title="该休息一下了",
                                      body="站起来走两步",
                                      level="warn", source="reminder")
            # after(0) 回调需要泵一次事件循环
            deadline = time.time() + 3
            while time.time() < deadline and \
                    len(pet._controller.recent()) <= before:
                pet.root.update()
                time.sleep(0.02)
            self.assertGreater(len(pet._controller.recent()), before)
            last = pet._controller.recent()[-1]
            self.assertEqual(last.source, "reminder")
            self.assertEqual(last.level, "warn")
        finally:
            pet._quit()


if __name__ == "__main__":
    unittest.main()
