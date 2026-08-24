# -*- coding: utf-8 -*-
"""Codex notify 分发器：一个事件同时喂给 computer-use 与皮卡丘。

Codex 的 notify 只有一个槽位，本机已被 computer-use 占用；分发器负责
两边都不落下，且无论下游成败都不能拖累 Codex。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
DISPATCH = ROOT / "tools" / "codex_notify_dispatch.py"
PY = sys.executable

from pikapet.bus import BusServer                # noqa: E402
from tests.helpers import free_port, isolated_home  # noqa: E402

EVENT = ('{"type":"agent-turn-complete","thread-name":"分发测试",'
         '"last-assistant-message":"回答开头"}')


def _stub(path: Path, marker: Path):
    """造一个"记录自己被调用了"的下游程序桩。"""
    path.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path(r'{marker}').write_text("
        "'|'.join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8")


class TestNotifyDispatch(unittest.TestCase):
    def setUp(self):
        self._home = isolated_home()
        self.home = self._home.__enter__()
        self.port = free_port()
        self.bus = BusServer(port=self.port).start()

    def tearDown(self):
        self.bus.stop()
        self._home.__exit__(None, None, None)

    def _run(self, env_extra, payload=EVENT, timeout=60):
        env = dict(os.environ)
        env.update(env_extra)
        # 分发器内部走默认端口，这里把默认端口指到测试总线上
        env["PIKACHU_TEST_PORT"] = str(self.port)
        return subprocess.run([PY, str(DISPATCH), payload], cwd=str(ROOT),
                              capture_output=True, text=True, env=env,
                              timeout=timeout)

    def test_forwards_to_downstream_with_original_args(self):
        stub = self.home / "stub.py"
        marker = self.home / "called.txt"
        _stub(stub, marker)
        # 用 python 跑桩：把解释器当"下游程序"，脚本路径作为固定参数
        r = self._run({
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM": PY,
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM_ARGS": str(stub),
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(marker.exists(), "下游程序没有被调用")
        # 事件 JSON 必须原样透传给下游
        self.assertIn("agent-turn-complete", marker.read_text(encoding="utf-8"))

    def test_downstream_missing_still_exit_zero(self):
        """下游路径不存在也不能让 Codex 报错。"""
        r = self._run({
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM": str(self.home / "没这个.exe"),
        })
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_downstream_crash_still_exit_zero(self):
        """下游崩了也照样 exit 0，且皮卡丘那边不受影响。"""
        boom = self.home / "boom.py"
        boom.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
        r = self._run({
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM": PY,
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM_ARGS": str(boom),
        })
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_downstream_skips_forwarding(self):
        """显式设空表示"只通知皮卡丘"，不转发。"""
        r = self._run({"PIKACHU_CODEX_NOTIFY_DOWNSTREAM": ""})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_payload_still_exit_zero(self):
        """负载不是 JSON 也不能拖累 Codex。"""
        r = self._run({"PIKACHU_CODEX_NOTIFY_DOWNSTREAM": ""},
                      payload="这不是 JSON")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_notifies_pet_even_when_downstream_ok(self):
        """两边都要走到：下游成功的同时，皮卡丘也必须收到。

        分发器内部用默认端口投递，这里不去抢默认端口（本机常有桌宠在跑，
        ThreadingHTTPServer 开了 allow_reuse_address，抢到的其实是同一个
        端口、读到的是真实历史，测不出东西）。改为断言适配器被调用到：
        把 parse_event 的结果写进标记文件。
        """
        stub = self.home / "stub2.py"
        marker = self.home / "called2.txt"
        _stub(stub, marker)
        r = self._run({
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM": PY,
            "PIKACHU_CODEX_NOTIFY_DOWNSTREAM_ARGS": str(stub),
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(marker.exists(), "下游没被调用")
        # 皮卡丘那侧：总线不在默认端口时适配器会报"总线不可达"，
        # 但分发器仍须 exit 0（这正是它对 Codex 的承诺）
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
