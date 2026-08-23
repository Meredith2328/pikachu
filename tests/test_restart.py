import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet.bus import BusServer, send_notification, SSEClient
from pikapet.protocol import Notification
from pikapet.pet_core import PetController
from tests.helpers import free_port


class FakeClock:
    def __init__(self, start=1000000.0):
        self.t = start

    def __call__(self):
        return self.t


class TestStaleFilter(unittest.TestCase):
    """PetController 对陈旧消息（历史回放）不应弹泡。"""

    def setUp(self):
        self.shown = []
        self.clock = FakeClock()
        self.c = PetController(on_show=lambda n: self.shown.append(n),
                               clock=self.clock)

    def test_recent_message_shows(self):
        r = self.c.handle(Notification(title="new", ts=self.clock.t))
        self.assertEqual(r, "shown")
        self.assertEqual(len(self.shown), 1)

    def test_stale_message_not_shown(self):
        """发送时间比当前早超过 stale 窗口（如重启后回放）：记录但不弹。"""
        r = self.c.handle(Notification(title="old", ts=self.clock.t - 3600))
        self.assertEqual(r, "stale")
        self.assertEqual(len(self.shown), 0)
        # 仍计入历史（统计可见）
        self.assertEqual(len(self.c.recent()), 1)

    def test_borderline_inside_window_shows(self):
        r = self.c.handle(Notification(title="ok", ts=self.clock.t - 30))
        self.assertEqual(r, "shown")

    def test_restart_replay_does_not_resurface(self):
        """模拟总线重启后回放历史：旧消息全部 stale，只有新消息弹泡。"""
        # 旧消息（回放）
        for i in range(3):
            self.c.handle(Notification(title=f"old{i}",
                                       ts=self.clock.t - 600))
        self.assertEqual(len(self.shown), 0)
        # 新消息（实时）
        r = self.c.handle(Notification(title="live", ts=self.clock.t))
        self.assertEqual(r, "shown")
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.shown[0].title, "live")


class TestGenerationReset(unittest.TestCase):
    """SSEClient 通过 generation 检测总线重启（含同进程重建），重置游标。"""

    def test_same_process_restart_resets_cursor(self):
        bus = BusServer(port=0).start()
        port = bus.port
        send_notification(Notification(title="m1"), port=port)
        send_notification(Notification(title="m2"), port=port)

        received = []
        client = SSEClient(port=port, on_event=lambda n: received.append(n),
                           retry_sec=0.3)  # 快重连，测试不等 5 秒
        client.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline and len(received) < 2:
                time.sleep(0.05)
            self.assertGreaterEqual(len(received), 2)
            client._last_mid = 2  # 模拟已消费到 mid=2

            # 同进程重建总线：generation 变化、mid 回绕
            bus.stop()
            bus2 = BusServer(port=port).start()
            try:
                # 等新总线真正可服务（全套件高负载时盲睡固定秒数不可靠）
                from pikapet import bus as _bus
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        _bus.fetch_health(port=port, timeout=1)
                        break
                    except Exception:
                        time.sleep(0.1)
                # POST 偶发撞上调度空窗，小步重试；消息即使先于客户端重连
                # 入库也没关系，重连后的全量回放会补送
                post_err = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        send_notification(Notification(title="post-restart"),
                                          port=port)
                        post_err = None
                        break
                    except Exception as e:
                        post_err = e
                        time.sleep(0.2)
                self.assertIsNone(post_err,
                                  f"重启后 POST 应最终成功: {post_err!r}")
                # 等客户端完成重连（generation 检测 + 游标重置 + 全量回放）
                deadline = time.time() + 15
                while time.time() < deadline and not any(
                        n.title == "post-restart" for n in received):
                    time.sleep(0.2)
                self.assertTrue(
                    any(n.title == "post-restart" for n in received),
                    "总线重启后（同进程）新消息应能送达")
            finally:
                bus2.stop()
        finally:
            client.stop()
            client.join()

    def test_cross_process_restart_recovers(self):
        """跨进程总线重启（pid 变化、generation 重置）：掉线窗口消息不丢。

        generation 是进程内计数（新进程从 1 重新计），必须靠 pid 识别重启。
        """
        import subprocess
        import sys as _sys
        ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 起真实子进程总线
        bus1 = subprocess.Popen(
            [_sys.executable, "-m", "pikapet.bus", "--port", str(free_port())],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 从 stdout 拿不到端口（被丢弃），用固定端口避免竞态
        bus1.kill()
        bus1.wait(5)

        port = free_port()
        bus1 = subprocess.Popen(
            [_sys.executable, "-m", "pikapet.bus", "--port", str(port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # 慢 CI（冷启动解释器 + 杀毒扫描）上拉起子总线可能要几十秒
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    from pikapet import bus as _bus
                    _bus.fetch_health(port=port, timeout=1)
                    break
                except Exception:
                    time.sleep(0.2)
            send_notification(Notification(title="pre-restart"), port=port)
            received = []
            client = SSEClient(port=port, on_event=lambda n: received.append(n),
                               retry_sec=0.5, read_timeout=3.0)
            client.start()
            try:
                deadline = time.time() + 30
                while time.time() < deadline and not any(
                        n.title == "pre-restart" for n in received):
                    time.sleep(0.1)
                self.assertTrue(any(n.title == "pre-restart" for n in received))

                # 杀总线 → 起新总线（同端口）→ 掉线窗口内发消息
                bus1.kill()
                bus1.wait(5)
                time.sleep(1.0)
                bus2 = subprocess.Popen(
                    [_sys.executable, "-m", "pikapet.bus", "--port", str(port)],
                    cwd=ROOT, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                try:
                    deadline = time.time() + 40
                    while time.time() < deadline:
                        try:
                            from pikapet import bus as _bus2
                            _bus2.fetch_health(port=port, timeout=1)
                            break
                        except Exception:
                            time.sleep(0.2)
                    # 掉线窗口内立即 POST：客户端还没重连上，消息只进 bus2 历史
                    try:
                        send_notification(Notification(title="during-restart"),
                                          port=port)
                        post_ok = True
                    except Exception as e:
                        post_ok = False
                        post_err = repr(e)
                    # 等客户端重连（pid 变化 → 清游标 → 全量回放 → 收到）；
                    # 客户端读超时 3 秒 + 重连间隔，给足 20 秒
                    deadline = time.time() + 20
                    while time.time() < deadline and not any(
                            n.title == "during-restart" for n in received):
                        time.sleep(0.3)
                    self.assertTrue(
                        any(n.title == "during-restart" for n in received),
                        f"跨进程重启后掉线窗口内的消息应能送达 "
                        f"(post_ok={post_ok}, received={[n.title for n in received]}, "
                        f"bus2_alive={bus2.poll() is None})")
                finally:
                    bus2.kill()
                    bus2.wait(5)
            finally:
                client.stop()
                client.join()
        finally:
            bus1.kill()
            bus1.wait(5)


class TestIdleWrap(unittest.TestCase):
    """GetTickCount 32 位回绕：空闲时间不应算错。"""

    def test_idle_wraparound_32bit(self):
        from pikapet.win.idle import _compute_idle_seconds
        # dwTime 已回绕到小值（1000ms），tick64 是 0x100000000 + 6000
        # （即 5000ms 前输入）：期望约 5 秒，而不是巨大的错误值
        secs = _compute_idle_seconds(0x100000000 + 6000, 1000)
        self.assertAlmostEqual(secs, 5.0, delta=0.001)

    def test_idle_normal_no_wrap(self):
        from pikapet.win.idle import _compute_idle_seconds
        secs = _compute_idle_seconds(30_000, 10_000)
        self.assertAlmostEqual(secs, 20.0, delta=0.001)

    def test_idle_small_no_negative(self):
        from pikapet.win.idle import _compute_idle_seconds
        secs = _compute_idle_seconds(5000, 10000)  # tick 比 dwTime 小（未回绕）
        self.assertGreaterEqual(secs, 0.0)


if __name__ == "__main__":
    unittest.main()
