# -*- coding: utf-8 -*-
"""pika.logs 的单元测试：级别解析、swallow 语义、handler 幂等。"""
import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika import logs


class LogsTestCase(unittest.TestCase):
    def setUp(self):
        logs.reset_for_tests()
        self._env = os.environ.pop(logs.LEVEL_ENV, None)

    def tearDown(self):
        logs.reset_for_tests()
        os.environ.pop(logs.LEVEL_ENV, None)
        if self._env is not None:
            os.environ[logs.LEVEL_ENV] = self._env


class TestResolveLevel(LogsTestCase):
    def test_default_is_warning(self):
        self.assertEqual(logs.resolve_level(), logging.WARNING)

    def test_reads_env(self):
        os.environ[logs.LEVEL_ENV] = "debug"
        self.assertEqual(logs.resolve_level(), logging.DEBUG)

    def test_accepts_numeric(self):
        self.assertEqual(logs.resolve_level(logging.ERROR), logging.ERROR)

    def test_bad_level_raises_not_silently_defaults(self):
        """级别拼错要报错：静默退回默认值会让人误以为调高了级别。"""
        os.environ[logs.LEVEL_ENV] = "VERBOSE"
        with self.assertRaises(logs.LogLevelError):
            logs.resolve_level()


class TestSwallow(LogsTestCase):
    def setUp(self):
        super().setUp()
        self.stream = io.StringIO()
        logs.configure(level="DEBUG", stream=self.stream)
        self.log = logs.get_logger("test.swallow")

    def test_exception_does_not_propagate(self):
        with logs.swallow(self.log, "做一件会失败的事"):
            raise RuntimeError("炸了")
        self.assertIn("做一件会失败的事", self.stream.getvalue())

    def test_logs_traceback(self):
        with logs.swallow(self.log, "动作 A"):
            raise ValueError("具体原因")
        out = self.stream.getvalue()
        self.assertIn("Traceback", out)
        self.assertIn("具体原因", out)

    def test_success_logs_nothing(self):
        with logs.swallow(self.log, "动作 B"):
            pass
        self.assertEqual(self.stream.getvalue(), "")

    def test_keyboard_interrupt_still_propagates(self):
        """Ctrl+C 不该被当成"可忽略的失败"吞掉。"""
        with self.assertRaises(KeyboardInterrupt):
            with logs.swallow(self.log, "动作 C"):
                raise KeyboardInterrupt

    def test_once_downgrades_repeat_to_debug(self):
        for _ in range(3):
            with logs.swallow(self.log, "高频动作", once=True):
                raise RuntimeError("x")
        out = self.stream.getvalue()
        self.assertEqual(out.count("WARNING"), 1)
        self.assertEqual(out.count("DEBUG"), 2)

    def test_once_still_records_every_occurrence(self):
        """降级不等于静默：后续失败仍以 DEBUG 留痕。"""
        for _ in range(4):
            with logs.swallow(self.log, "另一个高频动作", once=True):
                raise RuntimeError("x")
        self.assertEqual(self.stream.getvalue().count("另一个高频动作"), 4)


class TestConfigure(LogsTestCase):
    def test_repeated_configure_does_not_duplicate_handlers(self):
        stream = io.StringIO()
        logs.configure(stream=stream)
        logs.configure(stream=stream)
        root = logs.get_logger()
        self.assertEqual(len(root.handlers), 1)

    def test_file_handler_writes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nested" / "pika.log"
            logs.configure(level="INFO", stream=io.StringIO(),
                           file_path=path)
            logs.get_logger("test.file").warning("落盘测试")
            logs.reset_for_tests()
            self.assertIn("落盘测试", path.read_text(encoding="utf-8"))

    def test_file_handler_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pika.log"
            stream = io.StringIO()
            logs.configure(stream=stream, file_path=path)
            logs.configure(stream=stream, file_path=path)
            self.assertEqual(len(logs.get_logger().handlers), 2)
            # Windows 上 handler 不关掉，临时目录删不掉
            logs.reset_for_tests()

    def test_unwritable_file_path_raises(self):
        """日志落不了盘是要知道的问题，不该静默降级成只有 stderr。

        用"把普通文件当目录用"构造失败：makedirs 会抛 OSError。"""
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(OSError):
                logs.configure(stream=io.StringIO(),
                               file_path=blocker / "pika.log")


if __name__ == "__main__":
    unittest.main()
