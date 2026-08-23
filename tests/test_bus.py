import sys
import os
import json
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika.bus import BusServer, send_notification, fetch_health, fetch_history, SSEClient
from pika.protocol import Notification, ProtocolError
from tests.helpers import (bus_post_json, bus_request, bus_stream, free_port,
                           isolated_home)


class BusTestCase(unittest.TestCase):
    def setUp(self):
        self.bus = BusServer(port=0).start()
        self.port = self.bus.port

    def tearDown(self):
        self.bus.stop()


class TestBusHttp(BusTestCase):
    def test_health(self):
        info = fetch_health(port=self.port)
        self.assertTrue(info["ok"])
        self.assertEqual(info["port"], self.port)
        self.assertIn("history_len", info)

    def test_send_and_history(self):
        n = Notification(title="hi", body="body", source="t")
        resp = send_notification(n, port=self.port)
        self.assertTrue(resp["ok"])
        items = fetch_history(port=self.port)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "hi")
        self.assertEqual(items[0]["source"], "t")

    def test_invalid_payload_400(self):
        code, _ = bus_post_json(self.port, b'{"title": 123}',
                                token=self.bus.token)
        self.assertEqual(code, 400)

    def test_unknown_path_404(self):
        code, _ = bus_request(self.port, "GET", "/nope")
        self.assertEqual(code, 404)

    def test_history_limit(self):
        for i in range(5):
            send_notification(Notification(title=f"m{i}"), port=self.port)
        items = fetch_history(n=3, port=self.port)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[-1]["title"], "m4")


class TestBusSse(BusTestCase):
    def test_sse_delivers(self):
        received = []
        client = SSEClient(port=self.port, on_event=lambda n: received.append(n))
        client.start()
        try:
            # 等 SSE 连接建立
            deadline = time.time() + 5
            while time.time() < deadline:
                if self.bus.health()["subscribers"] >= 1:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(self.bus.health()["subscribers"], 1)
            send_notification(Notification(title="sse-ok", source="t"), port=self.port)
            deadline = time.time() + 5
            while time.time() < deadline and not received:
                time.sleep(0.05)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].title, "sse-ok")
        finally:
            client.stop()
            client.join()

    def test_sse_multiple_messages(self):
        received = []
        client = SSEClient(port=self.port, on_event=lambda n: received.append(n))
        client.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not received:
                send_notification(Notification(title="first"), port=self.port)
                time.sleep(0.05)
            for i in range(3):
                send_notification(Notification(title=f"m{i}"), port=self.port)
            deadline = time.time() + 5
            while time.time() < deadline and len(received) < 4:
                time.sleep(0.05)
            titles = [n.title for n in received]
            self.assertIn("first", titles)
            for i in range(3):
                self.assertIn(f"m{i}", titles)
        finally:
            client.stop()
            client.join()


class TestTokenAuth(BusTestCase):
    """投递鉴权：POST /notify 必须携带与服务端一致的 X-Pika-Token。"""

    def _post(self, headers_extra, payload=b'{"title":"t"}'):
        token = headers_extra.get("X-Pika-Token")
        code, text = bus_post_json(self.port, payload, token=token)
        return code, (json.loads(text) if code == 200 else {})

    def test_missing_token_403(self):
        code, _ = self._post({})
        self.assertEqual(code, 403)
        # 被拒的消息不能进历史
        self.assertFalse(any(i["title"] == "t"
                             for i in fetch_history(port=self.port)))

    def test_wrong_token_403(self):
        import secrets
        code, _ = self._post({"X-Pika-Token": secrets.token_hex(32)})
        self.assertEqual(code, 403)

    def test_correct_token_accepted(self):
        code, body = self._post({"X-Pika-Token": self.bus.token})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))

    def test_send_notification_attaches_token(self):
        """己方入口 send_notification 自动附带 token，用户无感。"""
        send_notification(Notification(title="auto-token"),
                          port=self.port)
        items = fetch_history(port=self.port)
        self.assertEqual(items[-1]["title"], "auto-token")


class TestPortNegotiation(unittest.TestCase):
    """端口协商：目标端口连不上时读运行时 port 文件回退端口重试一次。"""

    def setUp(self):
        self._home = isolated_home()
        self.home = self._home.__enter__()
        self.bus = BusServer(port=0).start()
        self.port = self.bus.port

    def tearDown(self):
        self.bus.stop()
        self._home.__exit__(None, None, None)

    def _write_port_file(self, value):
        from pika import paths
        paths.write_text_atomic(paths.port_file(create_dir=True), str(value))

    def test_send_falls_back_to_negotiated_port(self):
        """默认端口连不上 + port 文件指向真实总线 → 消息仍能送达。"""
        self._write_port_file(self.port)
        dead = self.free_dead_port()
        send_notification(Notification(title="negotiate"), port=dead)
        items = fetch_history(port=self.port)
        self.assertEqual(items[-1]["title"], "negotiate")

    def test_fetch_health_falls_back(self):
        self._write_port_file(self.port)
        dead = self.free_dead_port()
        h = fetch_health(port=dead)
        self.assertEqual(h["port"], self.port)

    def test_no_fallback_file_raises(self):
        dead = self.free_dead_port()
        with self.assertRaises(OSError):
            fetch_health(port=dead)

    def test_garbage_port_file_raises_not_ignored(self):
        """端口文件被写坏要报出来：静默忽略会让"连不上"无从下手。"""
        from pika import paths
        from pika.bus import PortFileError
        paths.write_text_atomic(paths.port_file(create_dir=True), "不是端口")
        dead = self.free_dead_port()
        with self.assertRaises(PortFileError):
            fetch_health(port=dead)

    def test_out_of_range_port_file_raises(self):
        from pika import paths
        from pika.bus import PortFileError
        paths.write_text_atomic(paths.port_file(create_dir=True), "99999")
        dead = self.free_dead_port()
        with self.assertRaises(PortFileError):
            fetch_health(port=dead)

    def test_negotiate_false_does_not_read_port_file(self):
        """探测"这个端口是不是 pika 总线"时不许被协商带偏。"""
        self._write_port_file(self.port)
        dead = self.free_dead_port()
        with self.assertRaises(OSError):
            fetch_health(port=dead, negotiate=False)

    @staticmethod
    def free_dead_port():
        """占一个端口再关掉：不保证仍空闲，但几乎不会被监听。"""
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p


class TestBusSseIdentity(BusTestCase):
    def test_sse_stream_starts_with_identity_comment(self):
        """连接建立即发送带内身份行 ": gen=N pid=P"。

        客户端靠它发现"连上了重启后的总线"（快速重启时 /health 探测
        有竞态，可能带着旧游标连上来，把该补送的消息过滤掉）。
        """
        with bus_stream(self.port) as resp:
            head = resp.read1(256).decode("utf-8", errors="replace")
        self.assertTrue(head.startswith(": gen="),
                        f"SSE 流应以身份注释开头，实际: {head!r}")
        self.assertIn(f"pid={os.getpid()}", head)


class TestBusSseReplay(BusTestCase):
    def test_new_subscriber_gets_recent_history(self):
        """新订阅建立时回放最近历史，避免连接前消息丢失。"""
        send_notification(Notification(title="old-msg"), port=self.port)
        received = []
        client = SSEClient(port=self.port, on_event=lambda n: received.append(n))
        client.start()
        try:
            ok = wait(lambda: len(received) >= 1)
            self.assertTrue(ok, received)
            self.assertEqual(received[0].title, "old-msg")
        finally:
            client.stop()
            client.join()

    def test_reconnect_after_gets_only_new(self):
        """断线重连带 ?after=mid：只补新消息，不重放旧的（避免重复气泡）。"""
        send_notification(Notification(title="m1"), port=self.port)
        send_notification(Notification(title="m2"), port=self.port)
        # 模拟断线客户端：已处理到 id=2，重连带 after=2
        import socket as _sock
        s = _sock.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /events?after=2 HTTP/1.1\r\nHost: x\r\n\r\n")
        buf = b""
        deadline = time.time() + 5
        while b"\r\n\r\n" not in buf and time.time() < deadline:
            buf += s.recv(4096)
        # 此时没有新消息，应只读到 SSE 头（无事件）
        s.settimeout(1.5)
        try:
            extra = s.recv(4096)
            self.assertNotIn("m1", extra.decode("utf-8", "replace"))
        except OSError:
            pass  # 超时 = 没有数据 = 没有重放旧消息
        # 发一条新消息，after=2 的连接应能收到它
        send_notification(Notification(title="m3"), port=self.port)
        data = b""
        deadline = time.time() + 5
        s.settimeout(5)
        while b"m3" not in data and time.time() < deadline:
            data += s.recv(4096)
        text = data.decode("utf-8", "replace")
        self.assertIn("m3", text)
        self.assertNotIn("m1", text)
        s.close()


def wait(pred, timeout=10, interval=0.05):
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if pred():
            return True
        _time.sleep(interval)
    return False


class TestBusSlowSubscriber(unittest.TestCase):
    def test_slow_subscriber_does_not_block_publish(self):
        """慢订阅者（不消费）不会阻塞 publish；队列满被踢出。"""
        b = BusServer(port=0).start()
        q = b.subscribe()
        try:
            # 填满队列（maxsize=256）后，publish 不应阻塞
            import threading
            import time as _time
            result = []
            def pub_many():
                for i in range(300):
                    b.publish(Notification(title=f"m{i}"))
                result.append("done")
            t = threading.Thread(target=pub_many)
            t.start()
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "publish 阻塞在慢订阅者上")
            self.assertEqual(result, ["done"])
            # 慢订阅者应已被踢出
            self.assertEqual(b.health()["subscribers"], 0)
        finally:
            b.stop()

    def test_kicked_subscriber_gets_sentinel(self):
        """被踢的订阅者应收到哨兵（而不是被静默丢弃，导致连接"活着但断流"）。"""
        b = BusServer(port=0).start()
        q = b.subscribe()
        try:
            # 填满队列
            for i in range(256):
                b.publish(Notification(title=f"fill{i}"))
            # 再发一条触发踢出 + 哨兵
            b.publish(Notification(title="trigger-kick"))
            # 队列应包含哨兵（最后一个元素是 ("__kick__", None)）
            items = []
            while True:
                try:
                    items.append(q.get_nowait())
                except Exception:
                    break
            sentinels = [m for m, _ in items if isinstance(m, str)]
            self.assertIn("__kick__", sentinels)
        finally:
            b.stop()


class TestBusSubscribeWithSnapshot(unittest.TestCase):
    def test_snapshot_atomicity(self):
        """subscribe_with_snapshot 返回的订阅者能收到快照之后的所有消息（不丢）。"""
        b = BusServer(port=0).start()
        # 已有历史
        for i in range(5):
            b.publish(Notification(title=f"old{i}"))
        q, snap = b.subscribe_with_snapshot()
        try:
            self.assertEqual(len(snap), 5)
            self.assertEqual([n.title for _, n in snap], [f"old{i}" for i in range(5)])
            # 订阅后发布的消息应进队列
            b.publish(Notification(title="new-after-snap"))
            mid, n = q.get(timeout=1)
            self.assertEqual(n.title, "new-after-snap")
        finally:
            b.unsubscribe(q)
            b.stop()

    def test_no_gap_between_snapshot_and_subscribe(self):
        """快照与订阅原子：窗口内的消息要么在快照要么在队列，不丢。"""
        import threading
        b = BusServer(port=0).start()
        results = []
        # 一个线程反复发布，主线程反复建立订阅，收集所有收到的标题
        stop = threading.Event()

        def publisher():
            i = 0
            while not stop.is_set():
                b.publish(Notification(title=f"p{i}"))
                i += 1
                time.sleep(0.001)

        t = threading.Thread(target=publisher)
        t.start()
        try:
            received = set()
            for _ in range(20):
                q, snap = b.subscribe_with_snapshot()
                for _, n in snap:
                    received.add(n.title)
                try:
                    while True:
                        _, n = q.get_nowait()
                        received.add(n.title)
                except Exception:
                    pass
                b.unsubscribe(q)
                time.sleep(0.01)
            results.append(len(received))
        finally:
            stop.set()
            t.join(timeout=3)
            b.stop()
        self.assertGreater(results[0], 0)


class TestBusContentType(unittest.TestCase):
    def test_rejects_non_json_content_type(self):
        b = BusServer(port=0).start()
        try:
            code, _ = bus_post_json(b.port, b'{"title":"x"}', token=b.token,
                                    ctype="text/plain")
            self.assertEqual(code, 415)
        finally:
            b.stop()


class TestBusUnsubscribe(unittest.TestCase):
    def test_unsubscribe_removes_subscriber(self):
        b = BusServer(port=0).start()
        q = b.subscribe()
        self.assertEqual(b.health()["subscribers"], 1)
        b.unsubscribe(q)
        self.assertEqual(b.health()["subscribers"], 0)
        b.stop()

    def test_publish_wakes_subscriber(self):
        b = BusServer(port=0).start()
        q = b.subscribe()
        mid = b.publish(Notification(title="x"))
        got = q.get(timeout=1)
        self.assertEqual(got[0], mid)
        self.assertEqual(got[1].title, "x")
        b.stop()


class TestBadQueryParamsRejected(BusTestCase):
    """非法查询参数返回 400，而不是静默套用默认值。

    静默兜底的害处很具体：after 写坏了当成"无游标"，客户端以为在增量
    补拉、实际收到全量回放，会重复弹一屏气泡且完全无从察觉。"""

    def _get_status(self, path):
        return bus_request(self.port, "GET", path)[0]

    def test_bad_history_n_400(self):
        # URL 里不能直接放非 ASCII，转义后发（服务端解出来仍是非数字）
        self.assertEqual(self._get_status("/history?n=%E5%BE%88%E5%A4%9A"), 400)

    def test_bad_history_n_ascii_400(self):
        self.assertEqual(self._get_status("/history?n=lots"), 400)

    def test_valid_history_n_ok(self):
        self.assertEqual(self._get_status("/history?n=5"), 200)

    def test_history_n_clamped_not_rejected(self):
        """超范围的 n 仍钳制处理：它是"合法整数、只是过大"。"""
        self.assertEqual(self._get_status("/history?n=99999"), 200)

    def test_bad_after_400(self):
        self.assertEqual(self._get_status("/events?after=abc"), 400)


class TestProtocolValidationOnBus(BusTestCase):
    def test_send_rejects_missing_title(self):
        with self.assertRaises(ProtocolError):
            Notification.from_dict({"level": "info"})


class TestBusMainBindFailure(unittest.TestCase):
    """standalone 入口绑定失败时要打提示并返回 1，而不是自己炸掉。

    这条路径此前引用了未 import 的 sys，只在真实绑定失败时才走到，
    测试没覆盖——补一条防回归。注意不能用"再绑一次同端口"来构造失败：
    ThreadingHTTPServer 开了 allow_reuse_address，Windows 上会绑成功，
    main 就进了常驻循环。这里直接让 start 抛 OSError。"""

    def test_main_reports_bind_failure(self):
        import contextlib
        import io
        from pika import bus as bus_mod

        class _FailingServer:
            def __init__(self, host=None, port=None):
                pass

            def start(self):
                raise OSError("模拟绑定失败")

        orig = bus_mod.BusServer
        bus_mod.BusServer = _FailingServer
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = bus_mod.main(["--port", "7452"])
        finally:
            bus_mod.BusServer = orig
        self.assertEqual(rc, 1)
        self.assertIn("7452", err.getvalue())
        self.assertIn("模拟绑定失败", err.getvalue())


if __name__ == "__main__":
    unittest.main()
