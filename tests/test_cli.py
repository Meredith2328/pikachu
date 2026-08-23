import sys
import os
import unittest
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args, port):
    """跑一条 CLI 子命令。--port 挂在子命令上（不再是顶层全局参数）。"""
    env = dict(os.environ)
    return subprocess.run([PY, "-m", "pikapet", *args, "--port", str(port)],
                          cwd=ROOT, capture_output=True, text=True, env=env,
                          timeout=30)


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import socket
        from pikapet.bus import BusServer
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        cls.bus = BusServer(port=cls.port).start()

    @classmethod
    def tearDownClass(cls):
        cls.bus.stop()

    def test_send_ok(self):
        r = run_cli("send", "标题A", "正文B", "--source", "cli", port=self.port)
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        resp = json.loads(r.stdout)
        self.assertTrue(resp["ok"])

    def test_send_fail_when_bus_down(self):
        from tests.helpers import isolated_runtime_port
        with isolated_runtime_port():
            r = subprocess.run([PY, "-m", "pikapet", "send", "x", "--port", "1"],
                               cwd=ROOT, capture_output=True, text=True,
                               timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("失败", r.stderr)

    def test_history(self):
        run_cli("send", "历史测试", port=self.port)
        r = run_cli("history", port=self.port)
        self.assertEqual(r.returncode, 0)
        self.assertIn("历史测试", r.stdout)

    def test_health(self):
        r = run_cli("health", port=self.port)
        self.assertEqual(r.returncode, 0)
        self.assertIn("history_len", r.stdout)

    def test_reminder_once_bus_down_nonzero(self):
        """--once 遇到总线不可达：应退避重试后非零退出，不静默返回 0。"""
        import json
        import tempfile
        from pathlib import Path
        from tests.helpers import isolated_runtime_port
        with tempfile.TemporaryDirectory(prefix="pika-cli-") as td:
            fake = Path(td) / "activity.json"
            fake.write_text(json.dumps({"idle_minutes": 0}), encoding="utf-8")
            cfg = Path(td) / "reminder.json"
            # 短间隔：几步内就触发发送，从而暴露总线不可达
            cfg.write_text(json.dumps({
                "interval_min": 0.001, "interval_max": 0.001,
                "long_session_enabled": False,
                "categories": ["eye"]}), encoding="utf-8")
            with isolated_runtime_port():
                r = subprocess.run(
                    [PY, "-m", "pikapet.reminder_runner", "--once", "--port", "1",
                     "--fake", str(fake), "--config", str(cfg),
                     "--interval", "0.05"],
                    cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0, "总线故障应非零退出")
            self.assertTrue(r.stderr.strip(), "应打印失败原因")


if __name__ == "__main__":
    unittest.main()
