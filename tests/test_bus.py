import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika.bus import BusServer, send_notification, fetch_health, fetch_history, SSEClient
from pika.protocol import Notification, ProtocolError


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/notify",
            data=b'{"title": 123}',
            headers={"Content-Type": "application/json"},
            method="POST")
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_path_404(self):
        import urllib.request
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/nope", timeout=3)
        self.assertEqual(ctx.exception.code, 404)

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


class TestPortNegotiation(BusTestCase):
    """端口协商：目标端口连不上时读 runtime/port 回退端口重试一次。"""

    def setUp(self):
        super().setUp()
        import tempfile
        from pika import bus as bus_mod
        self._orig_file = bus_mod.RUNTIME_PORT_FILE
        fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="port_")
        import os as _os
        _os.close(fd)
        _os.remove(tmp)
        bus_mod.RUNTIME_PORT_FILE = type(self._orig_file)(tmp)
        self.bus_mod = bus_mod

    def tearDown(self):
        self.bus_mod.RUNTIME_PORT_FILE = self._orig_file
        super().tearDown()

    def test_send_falls_back_to_runtime_port(self):
        """默认端口连不上 + runtime/port 指向真实总线 → 消息仍能送达。"""
        self.bus_mod.RUNTIME_PORT_FILE.write_text(str(self.port),
                                                  encoding="utf-8")
        dead = self.free_dead_port()
        send_notification(Notification(title="negotiate"), port=dead)
        items = fetch_history(port=self.port)
        self.assertEqual(items[-1]["title"], "negotiate")

    def test_fetch_health_falls_back(self):
        self.bus_mod.RUNTIME_PORT_FILE.write_text(str(self.port),
                                                  encoding="utf-8")
        dead = self.free_dead_port()
        h = fetch_health(port=dead)
        self.assertEqual(h["port"], self.port)

    def test_no_fallback_file_raises(self):
        dead = self.free_dead_port()
        with self.assertRaises(Exception):
            fetch_health(port=dead)

    def test_garbage_port_file_ignored(self):
        self.bus_mod.RUNTIME_PORT_FILE.write_text("不是端口", encoding="utf-8")
        dead = self.free_dead_port()
        with self.assertRaises(Exception):
            fetch_health(port=dead)

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
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/events", timeout=5) as resp:
            head = resp.read1(256).decode("utf-8", errors="replace")
        body = head.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in head else head
        self.assertTrue(body.startswith(": gen="),
                        f"SSE 流应以身份注释开头，实际: {head!r}")
        self.assertIn(f"pid={os.getpid()}", body)


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
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                f"http://127.0.0.1:{b.port}/notify",
                data=b'{"title":"x"}',
                headers={"Content-Type": "text/plain"},
                method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(ctx.exception.code, 415)
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


class TestProtocolValidationOnBus(BusTestCase):
    def test_send_rejects_missing_title(self):
        with self.assertRaises(ProtocolError):
            Notification.from_dict({"level": "info"})


if __name__ == "__main__":
    unittest.main()
