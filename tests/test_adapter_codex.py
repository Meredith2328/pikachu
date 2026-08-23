# -*- coding: utf-8 -*-
import sys
import os
import json
import unittest
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.bus import BusServer, fetch_history
from pikapet.adapters.codex import parse_event, read_payload, collapse
from tests.helpers import free_port

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TURN_EVENT = {
    "type": "agent-turn-complete",
    "thread-id": "t-1",
    "turn-id": "r-1",
    "input_messages": ["帮我审查 adapters 目录", "补充：注意退出码"],
    "last_assistant_message": "审查完成，共发现 3 处问题。第二行结论……" * 5,
}


class TestParseEvent(unittest.TestCase):
    def test_turn_complete_basic(self):
        r = parse_event(TURN_EVENT)
        self.assertIsNotNone(r)
        title, body, level = r
        self.assertIn("审查 adapters 目录", title)
        self.assertIn("审查完成", body)
        self.assertEqual(level, "success")

    def test_title_prefers_thread_name(self):
        ev = dict(TURN_EVENT, **{"thread-name": "适配器重构"})
        title, _, _ = parse_event(ev)
        self.assertEqual(title, "适配器重构")

    def test_other_event_ignored(self):
        self.assertIsNone(parse_event({"type": "session-start"}))

    def test_payload_not_dict(self):
        self.assertIsNone(parse_event("not-a-dict"))

    def test_fallback_title_when_no_messages(self):
        r = parse_event({"type": "agent-turn-complete",
                         "last_assistant_message": "ok"})
        self.assertEqual(r[0], "Codex 回复完成")

    def test_collapse_truncates(self):
        self.assertTrue(collapse("a" * 500, 100).endswith("…"))
        self.assertEqual(len(collapse("a" * 500, 100)), 101)
        self.assertEqual(collapse("  x\n y \t z ", 50), "x y z")

    def test_read_payload_prefers_argv(self):
        p = read_payload(json.dumps({"a": 1}))
        self.assertEqual(p, {"a": 1})


class TestCodexAdapterE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.bus = BusServer(port=cls.port).start()

    @classmethod
    def tearDownClass(cls):
        cls.bus.stop()

    def _run(self, *args, stdin=None):
        return subprocess.run(
            [PY, "-m", "pikapet.adapters.codex", *args],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
            encoding="utf-8", input=stdin)

    def test_event_from_argv(self):
        r = self._run("event", json.dumps(TURN_EVENT, ensure_ascii=False),
                      "--port", str(self.port))
        self.assertEqual(r.returncode, 0, r.stderr)
        last = fetch_history(port=self.port)[-1]
        self.assertEqual(last["source"], "codex")
        self.assertEqual(last["level"], "success")
        self.assertIn("审查", last["title"])
        self.assertIn("审查完成", last["body"])

    def test_event_from_stdin(self):
        r = self._run("event", "--port", str(self.port),
                      stdin=json.dumps(TURN_EVENT, ensure_ascii=False))
        self.assertEqual(r.returncode, 0, r.stderr)
        last = fetch_history(port=self.port)[-1]
        self.assertEqual(last["source"], "codex")

    def test_non_turn_event_silent(self):
        before = len(fetch_history(port=self.port))
        r = self._run("event", json.dumps({"type": "session-start"}),
                      "--port", str(self.port))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(fetch_history(port=self.port)), before)

    def test_event_bus_down_returns_zero(self):
        """通知钩子绝不能阻塞 Codex：总线挂了也返回 0。"""
        from tests.helpers import isolated_runtime_port
        with isolated_runtime_port():   # 隔离回退文件，防协商到活着的默认端口
            r = self._run("event", json.dumps(TURN_EVENT, ensure_ascii=False),
                          "--port", "1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("总线", r.stderr)

    def test_report_mode(self):
        r = self._run("report", "每日简报", "--stage", "done",
                      "--detail", "生成 3 个文件", "--port", str(self.port))
        self.assertEqual(r.returncode, 0, r.stderr)
        last = fetch_history(port=self.port)[-1]
        self.assertEqual(last["level"], "success")
        self.assertIn("每日简报", last["title"])
        self.assertIn("生成 3 个文件", last["body"])

    def test_autodetect_json_goes_event(self):
        """不带子命令：{ 开头按事件处理。"""
        r = self._run(json.dumps(TURN_EVENT, ensure_ascii=False),
                      "--port", str(self.port))
        self.assertEqual(r.returncode, 0, r.stderr)
        last = fetch_history(port=self.port)[-1]
        self.assertEqual(last["source"], "codex")

    def test_top_level_passthrough(self):
        r = subprocess.run(
            [PY, "pikachu.py", "codex", "report", "t", "--stage", "error",
             "--port", str(self.port)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
            encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        last = fetch_history(port=self.port)[-1]
        self.assertEqual(last["level"], "error")


if __name__ == "__main__":
    unittest.main()
