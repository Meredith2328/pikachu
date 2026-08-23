# -*- coding: utf-8 -*-
import sys
import os
import unittest
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.bus import BusServer, fetch_history
from pikapet.adapters.dsh import collapse
from tests.helpers import free_port

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_stub(name: str, body: str) -> str:
    """写一个桩批处理（ASCII 输出，避免代码页/编码干扰）。"""
    fd, path = tempfile.mkstemp(suffix=".cmd", prefix=name)
    with os.fdopen(fd, "w", encoding="ascii", newline="\r\n") as f:
        f.write(body)
    return path


OK_STUB = write_stub("dsh_ok_", "@echo off\r\necho ANSWER_OK_12345\r\nexit /b 0\r\n")
ERR_STUB = write_stub("dsh_err_", "@echo off\r\necho DSH_FAILED_BAD>&2\r\nexit /b 7\r\n")
SLOW_STUB = write_stub("dsh_slow_", "@echo off\r\n@ping -n 30 127.0.0.1 >nul\r\nexit /b 0\r\n")


class TestDshUnit(unittest.TestCase):
    def test_collapse(self):
        self.assertEqual(collapse("  a\n b ", 10), "a b")
        self.assertTrue(collapse("x" * 300, 100).endswith("…"))


class TestDshWrapperE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.bus = BusServer(port=cls.port).start()

    @classmethod
    def tearDownClass(cls):
        cls.bus.stop()

    def _run(self, *args):
        return subprocess.run(
            [PY, "-m", "pikapet.adapters.dsh", *args],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
            encoding="utf-8")

    def _items(self):
        return [i for i in fetch_history(port=self.port) if i["source"] == "dsh"]

    def test_wrapper_success_flow(self):
        before = len(self._items())
        r = self._run("run", "任务A", "做点调研",
                      "--dsh-exe", OK_STUB,
                      "--port", str(self.port), "--timeout", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ANSWER_OK_12345", r.stdout)  # stdout 透传
        items = self._items()[before:]
        self.assertEqual(len(items), 2)             # start + done
        self.assertEqual(items[0]["level"], "info")
        self.assertIn("任务A", items[0]["title"])
        self.assertEqual(items[1]["level"], "success")
        self.assertIn("ANSWER_OK_12345", items[1]["body"])

    def test_wrapper_task_file(self):
        before = len(self._items())
        fd, tf = tempfile.mkstemp(suffix=".md", prefix="dsh_task_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("  从文件读的超长任务文本  ")
        try:
            r = self._run("run", "任务F", "--task-file", tf,
                          "--dsh-exe", OK_STUB,
                          "--port", str(self.port), "--timeout", "20")
            self.assertEqual(r.returncode, 0, r.stderr)
            items = self._items()[before:]
            self.assertEqual(items[0]["level"], "info")
            self.assertIn("从文件读的超长任务文本", items[0]["body"])
        finally:
            os.remove(tf)

    def test_wrapper_error_flow(self):
        before = len(self._items())
        r = self._run("run", "任务B", "会失败的任务",
                      "--dsh-exe", ERR_STUB,
                      "--port", str(self.port), "--timeout", "20")
        self.assertEqual(r.returncode, 7)           # 透传 dsh 退出码
        items = self._items()[before:]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["level"], "error")
        self.assertIn("退出码 7", items[1]["body"])
        self.assertIn("DSH_FAILED_BAD", items[1]["body"])

    def test_wrapper_timeout(self):
        before = len(self._items())
        r = self._run("run", "任务C", "慢任务",
                      "--dsh-exe", SLOW_STUB,
                      "--port", str(self.port), "--timeout", "2")
        self.assertEqual(r.returncode, 124)
        items = self._items()[before:]
        self.assertEqual(items[-1]["level"], "error")
        self.assertIn("超时", items[-1]["body"])

    def test_wrapper_missing_exe(self):
        before = len(self._items())
        r = self._run("run", "任务D", "x",
                      "--dsh-exe", r"C:\不存在\dsh_没有.cmd",
                      "--port", str(self.port))
        self.assertEqual(r.returncode, 4)
        items = self._items()[before:]
        self.assertEqual(items[-1]["level"], "error")

    def test_wrapper_empty_task_is_arg_error(self):
        r = self._run("run", "任务E", "--port", str(self.port))
        self.assertEqual(r.returncode, 2)

    def test_report_mode(self):
        r = self._run("report", "调研X", "--stage", "start",
                      "--detail", "开始", "--port", str(self.port))
        self.assertEqual(r.returncode, 0, r.stderr)
        last = self._items()[-1]
        self.assertEqual(last["level"], "info")
        self.assertIn("调研X", last["title"])

    def test_report_bus_down_returns_3(self):
        from tests.helpers import isolated_runtime_port
        with isolated_runtime_port():   # 隔离真实回退文件，防止协商到活总线
            r = self._run("report", "x", "--port", "1")
        self.assertEqual(r.returncode, 3)
        self.assertIn("总线", r.stderr)

    def test_top_level_passthrough(self):
        r = subprocess.run(
            [PY, "pikachu.py", "dsh", "report", "t", "--stage", "done",
             "--port", str(self.port)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
            encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._items()[-1]["level"], "success")


def _cleanup_stubs():
    for p in (OK_STUB, ERR_STUB, SLOW_STUB):
        try:
            os.remove(p)
        except OSError:
            pass


import atexit
atexit.register(_cleanup_stubs)


if __name__ == "__main__":
    unittest.main()
