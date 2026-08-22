import sys
import os
import unittest
import subprocess
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika.bus import BusServer, fetch_history
from pika.adapters.zcode import _title, _body
from tests.helpers import free_port, wait_http

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestZcodeAdapterUnit(unittest.TestCase):
    def test_title_stage(self):
        """标题统一语法：「{事件词} · {名称}」，不放 emoji。"""
        self.assertEqual("完成 · x",
                         _title(type("A", (), {"stage": "done", "name": "x"})))
        self.assertEqual("失败 · x",
                         _title(type("A", (), {"stage": "error", "name": "x"})))
        self.assertEqual("开始 · x",
                         _title(type("A", (), {"stage": "start", "name": "x"})))

    def test_body(self):
        a = type("A", (), {"stage": "done", "detail": "生成 3 个文件"})
        self.assertIn("生成 3 个文件", _body(a))


class TestZcodeAdapterE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.bus = BusServer(port=cls.port).start()

    @classmethod
    def tearDownClass(cls):
        cls.bus.stop()

    def _run(self, *args):
        return subprocess.run([PY, "-m", "pika.adapters.zcode", "--port", str(self.port),
                               *args], cwd=ROOT, capture_output=True, text=True,
                              timeout=30)

    def test_start_stage(self):
        r = self._run("daily-brief", "--stage", "start", "--detail", "开始生成")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = fetch_history(port=self.port)
        last = items[-1]
        self.assertEqual(last["source"], "zcode")
        self.assertIn("daily-brief", last["title"])
        self.assertIn("开始生成", last["body"])

    def test_done_stage(self):
        r = self._run("每日简报", "--stage", "done", "--detail", "生成 3 个文件")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = fetch_history(port=self.port)
        self.assertEqual(items[-1]["level"], "success")

    def test_error_stage(self):
        r = self._run("watch-inbox", "--stage", "error", "--detail", "权限不足")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = fetch_history(port=self.port)
        self.assertEqual(items[-1]["level"], "error")

    def test_bus_down_returns_3(self):
        r = subprocess.run([PY, "-m", "pika.adapters.zcode", "--port", "1",
                            "x"], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn("总线", r.stderr)


class TestTopLevelScript(unittest.TestCase):
    def test_doctor(self):
        import socket
        from pika.bus import BusServer
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        bus = BusServer(port=port).start()
        try:
            r = subprocess.run([PY, "pikachu.py", "doctor", "--port", str(port)],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        finally:
            bus.stop()

    def test_send_via_toplevel(self):
        port = free_port()
        bus = BusServer(port=port).start()
        try:
            r = subprocess.run([PY, "pikachu.py", "send", "顶层", "--port", str(port)],
                               cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            items = fetch_history(port=port)
            self.assertEqual(items[-1]["title"], "顶层")
        finally:
            bus.stop()

    def test_all_subcommands_accept_port(self):
        """每个子命令都必须接受 --port（回归：顶层/子命令端口接线）。"""
        port = free_port()
        bus = BusServer(port=port).start()
        try:
            # send 子命令 --port
            r = subprocess.run([PY, "pikachu.py", "send", "x", "--port", str(port)],
                               cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            # history / health 子命令 --port
            for cmd in ("history", "health"):
                r = subprocess.run([PY, "pikachu.py", cmd, "--port", str(port)],
                                   cwd=ROOT, capture_output=True, text=True,
                                   timeout=30)
                self.assertEqual(r.returncode, 0, r.stderr)
            # doctor 子命令 --port
            r = subprocess.run([PY, "pikachu.py", "doctor", "--port", str(port)],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            # zcode 子命令 --port
            r = subprocess.run([PY, "pikachu.py", "zcode", "t", "--port", str(port)],
                               cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            # pet 子命令 --port（启动即连内嵌总线，验证不报错）
            import subprocess as _sp
            p = _sp.Popen([PY, "pikachu.py", "pet", "--port", str(port)],
                          cwd=ROOT, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            try:
                ok = wait_http(port)
                self.assertTrue(ok, "pet 子命令 --port 未生效")
            finally:
                p.terminate()
                p.wait(10)
        finally:
            bus.stop()


if __name__ == "__main__":
    unittest.main()
