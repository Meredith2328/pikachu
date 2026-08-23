import sys
import os
import unittest
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.bus import BusServer, fetch_history
from pikapet.adapters.zcode import _title, _body
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
        return subprocess.run([PY, "-m", "pikapet.adapters.zcode", "--port", str(self.port),
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
        from tests.helpers import isolated_runtime_port
        with isolated_runtime_port():   # 隔离真实回退文件，防止协商到活总线
            r = subprocess.run([PY, "-m", "pikapet.adapters.zcode", "--port", "1",
                                "x"], cwd=ROOT, capture_output=True,
                               text=True, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn("总线", r.stderr)


class TestUnifiedCli(unittest.TestCase):
    """统一 CLI：三条等价入口（pikachu.py / -m pikapet / 装好的 pikachu）
    走的都是 pikapet.cli:main，这里测前两条。"""

    def _cli(self, *args, timeout=30):
        return subprocess.run([PY, "-m", "pikapet", *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=timeout)

    def _script(self, *args, timeout=30):
        return subprocess.run([PY, "pikachu.py", *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=timeout)

    def test_doctor(self):
        port = free_port()
        bus = BusServer(port=port).start()
        try:
            r = self._cli("doctor", "--port", str(port), timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        finally:
            bus.stop()

    def test_send_via_module(self):
        port = free_port()
        bus = BusServer(port=port).start()
        try:
            r = self._cli("send", "顶层", "--port", str(port))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(fetch_history(port=port)[-1]["title"], "顶层")
        finally:
            bus.stop()

    def test_send_via_repo_script(self):
        """仓库根的 pikachu.py 只是薄壳，行为必须与 -m pikapet 一致。"""
        port = free_port()
        bus = BusServer(port=port).start()
        try:
            r = self._script("send", "薄壳", "--port", str(port))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(fetch_history(port=port)[-1]["title"], "薄壳")
        finally:
            bus.stop()

    def test_all_subcommands_accept_port(self):
        """每个子命令都必须接受 --port（回归：顶层/子命令端口接线）。"""
        port = free_port()
        bus = BusServer(port=port).start()
        try:
            for args in (("send", "x"), ("history",), ("health",),
                         ("zcode", "t")):
                r = self._cli(*args, "--port", str(port))
                self.assertEqual(r.returncode, 0,
                                 f"{args} 失败：{r.stderr}")
            r = self._cli("doctor", "--port", str(port), timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            # pet 子命令 --port（启动即连内嵌总线，验证不报错）
            import subprocess as _sp
            p = _sp.Popen([PY, "-m", "pikapet", "pet", "--port", str(port)],
                          cwd=ROOT, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            try:
                self.assertTrue(wait_http(port), "pet 子命令 --port 未生效")
            finally:
                p.terminate()
                p.wait(10)
        finally:
            bus.stop()

    def test_codex_and_dsh_subcommands_reachable(self):
        """codex / dsh 以前走 argparse.REMAINDER 透传，现在是正常子命令，
        --help 必须能出（透传时顶层 --help 看不到它们的参数）。"""
        for args in (("codex", "--help"), ("codex", "report", "--help"),
                     ("dsh", "--help"), ("dsh", "run", "--help")):
            r = self._cli(*args)
            self.assertEqual(r.returncode, 0, f"{args}: {r.stderr}")
            self.assertTrue(r.stdout.strip(), f"{args} 没输出帮助")

    def test_unknown_subcommand_is_error(self):
        r = self._cli("不存在的子命令")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
