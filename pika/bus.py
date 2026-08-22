# -*- coding: utf-8 -*-
"""本地通知总线：外部软件 POST 消息进来，订阅者（桌宠）被实时推送。

- 纯标准库，仅监听 127.0.0.1，不暴露到局域网；
- 推送基于 SSE（HTTP 长连接），桌宠挂一个连接即可实时收消息，不是轮询；
- 断线重连用 ?after=<mid> 增量补拉：只补错过的消息，不重放旧消息；
- 慢订阅者不阻塞发布方：队列满即断开该订阅者；
- 桌面桌宠默认把本服务内嵌在自身进程里（同一个端口），
  也可以独立跑一个 bus 进程，让多个显示端共享。

用法（standalone）:
    python -m pika.bus --port 7452 --port-file runtime/port
"""
import argparse
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .protocol import Notification, ProtocolError, PROTOCOL_VERSION

DEFAULT_HOST = "127.0.0.1"
# 7452 = PIKA 的手机九键键序（P=7 I=4 K=5 A=2），刻意挑的冷门端口，
# 避开 3000/5000/8000/8080/8765 这类常用开发端口，降低撞车概率
DEFAULT_PORT = 7452
SSE_HEARTBEAT = 15.0
SSE_REPLAY_LIMIT = 200  # 与历史容量一致：增量补拉不因回放窗口丢消息
# 哨兵：队列里出现特殊 mid 表示"连接被踢出 / 总线停机"，handler 收到后退出
SENTINEL_KICK = "__kick__"
SENTINEL_STOP = "__stop__"
# SSE 带内身份注释行：": gen=3 pid=1234"
_IDENTITY_RE = re.compile(r"^: gen=(-?\d+) pid=(\d+)$")

# 全局代次：每次 BusServer.start() 递增（含同进程重建），
# 客户端据此识别"总线重启"并重置增量游标
_generation_counter = [0]
_generation_lock = threading.Lock()


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """线程化 HTTP 服务器：客户端异常断开等 IO 错误不打整页堆栈。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        import traceback
        exc = traceback.format_exc()
        # 客户端断连/重置是常态，只打一行摘要，不刷堆栈
        short = exc.strip().splitlines()[-1] if exc.strip() else "?"
        print(f"[pika-bus] 连接异常 {client_address}: {short}")


class BusHandler(BaseHTTPRequestHandler):
    server_version = "pika-bus"
    protocol_version = "HTTP/1.1"
    timeout = 30  # 慢连接（只发一半请求）不长期占线程

    # ---- 内部状态通过 server 暴露 ----
    @property
    def bus(self) -> "BusServer":
        return self.server.bus

    def log_message(self, fmt, *args):
        # 静默访问日志：总线不该把每条请求都打到 stderr
        pass

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            return self._handle_health()
        if path == "/history":
            return self._handle_history(parsed.query)
        if path == "/events":
            return self._handle_events()
        self._send_json(404, {"ok": False, "error": f"未知路径 {path}"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/notify":
            self._send_json(404, {"ok": False, "error": f"未知路径 {parsed.path}"})
            return
        # 只接受 JSON：挡掉浏览器表单式(text/plain)简单请求的误投
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {"ok": False, "error": "Content-Type 必须是 application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "Content-Length 非法"})
            return
        if length <= 0 or length > 1_000_000:
            self._send_json(400, {"ok": False, "error": "请求体长度非法"})
            return
        raw = self.rfile.read(length)
        try:
            notif = Notification.from_json(raw.decode("utf-8"))
        except ProtocolError as e:
            self._send_json(400, {"ok": False, "error": f"协议错误: {e}"})
            return
        except UnicodeDecodeError as e:
            self._send_json(400, {"ok": False, "error": f"编码错误: {e}"})
            return
        mid = self.bus.publish(notif)
        self._send_json(200, {"ok": True, "id": mid, "ts": notif.ts})

    # ---- 各端点 ----
    def _handle_health(self):
        info = self.bus.health()
        info["ok"] = True
        info["protocol_version"] = PROTOCOL_VERSION
        self._send_json(200, info)

    def _handle_history(self, query: str):
        qs = urllib.parse.parse_qs(query)
        try:
            n = max(1, min(int(qs.get("n", ["20"])[0]), 200))
        except ValueError:
            n = 20
        items = [n.to_dict() for _, n in self.bus.history(n)]
        self._send_json(200, {"ok": True, "count": len(items), "items": items})

    def _handle_events(self):
        """SSE 长连接。

        - ?after=<mid>：只回放 id 大于 mid 的历史（断线重连增量补拉）；
        - 不带 after：回放最近 SSE_REPLAY_LIMIT 条（保护"连接建立前消息丢失"竞态）。
        注册订阅者与取历史快照是原子的（subscribe_with_snapshot），
        窗口内的消息不重不漏；队列中的哨兵（踢出/停机）让本连接退出。
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        after = None
        try:
            after = int(qs.get("after", [""])[0]) if qs.get("after") else None
        except ValueError:
            after = None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # 带内身份：连接一建立就告知 generation/pid。客户端据此发现
        # "连上的是重启后的总线"——快速重启时客户端可能在 /health 探测
        # 到新身份之前就带着旧游标连了过来，仅靠 /health 检测有竞态
        self.wfile.write(f": gen={self.bus._generation} pid={os.getpid()}\n\n"
                         .encode("utf-8"))
        self.wfile.flush()

        sub, snap = self.bus.subscribe_with_snapshot()
        if after is not None:
            replay = [(m, n) for m, n in snap if m > after]
        else:
            replay = snap
        try:
            for mid, notif in replay:
                self._write_event(mid, notif)
            while True:
                try:
                    item = sub.get(timeout=SSE_HEARTBEAT)
                except queue.Empty:
                    item = None
                if item is None:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                mid, notif = item
                if isinstance(mid, str):
                    # 哨兵：被踢出或总线停机，连接就此结束并关闭（否则
                    # keep-alive 下客户端 read1 会一直阻塞，不会重连）
                    self.close_connection = True
                    break
                self._write_event(mid, notif)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.bus.unsubscribe(sub)

    def _write_event(self, mid: int, notif: Notification):
        payload = json.dumps(notif.to_dict(), ensure_ascii=False)
        self.wfile.write(f"id: {mid}\nevent: notify\ndata: {payload}\n\n"
                         .encode("utf-8"))
        self.wfile.flush()


class BusServer:
    """消息总线本体。可内嵌进桌宠进程，也可独立运行。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._history: list = []          # [(id, Notification)]
        self._subscribers: set = set()    # {queue.Queue}
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self._counter = 0
        self._generation = 0             # 每次 start() 递增：客户端据此识别重启

    # ---- 生命周期 ----
    def start(self) -> "BusServer":
        """绑定端口并启动。port=0 时使用系统分配端口，可通过 .port 读取实际值。"""
        if self._httpd is not None:
            return self
        self._httpd = _QuietThreadingHTTPServer((self.host, self.port), BusHandler)
        self._httpd.daemon_threads = True
        self._httpd.bus = self
        self.port = self._httpd.server_address[1]
        self._started_at = time.time()
        with _generation_lock:
            _generation_counter[0] += 1
            self._generation = _generation_counter[0]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="pika-bus", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0):
        if self._httpd is None:
            return
        # 塞停机哨兵：让 SSE 连接识别后自行退出（不把哨兵当事件转发）
        with self._lock:
            for s in self._subscribers:
                try:
                    s.put_nowait((SENTINEL_STOP, None))
                except queue.Full:
                    pass
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout)
        self._httpd = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # ---- 消息流 ----
    def publish(self, notif: Notification) -> int:
        """入历史并推送给所有订阅者，返回消息 id。

        慢订阅者（队列满）不阻塞发布方：踢出并通知其断开（触发客户端重连）。
        """
        with self._lock:
            self._counter += 1
            mid = self._counter
            self._history.append((mid, notif))
            if len(self._history) > 200:
                del self._history[:-200]
            subs = list(self._subscribers)
        dead = []
        for s in subs:
            try:
                s.put_nowait((mid, notif))
            except queue.Full:
                dead.append(s)
        if dead:
            with self._lock:
                for s in dead:
                    self._subscribers.discard(s)
            for s in dead:
                # 被踢的队列已满：清空后塞哨兵，让 handler 识别并退出，
                # 客户端走重连增量补拉（而不是连接"活着"但静默断流）
                try:
                    while True:
                        s.get_nowait()
                except queue.Empty:
                    pass
                try:
                    s.put_nowait((SENTINEL_KICK, None))
                except queue.Full:
                    pass
        return mid

    def history(self, n: int = 20) -> list:
        with self._lock:
            return list(self._history[-n:])

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(q)
        return q

    def subscribe_with_snapshot(self) -> tuple:
        """原子地"注册订阅者 + 取历史快照"。

        与 publish 在同一把锁下串行，保证窗口内的消息要么在快照里、
        要么进队列，不重不漏。
        """
        with self._lock:
            snap = list(self._history[-SSE_REPLAY_LIMIT:])
            q = queue.Queue(maxsize=256)
            self._subscribers.add(q)
            return q, snap

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subscribers.discard(q)

    def health(self) -> dict:
        with self._lock:
            return {
                "pid": os.getpid(),
                "generation": self._generation,
                "uptime": round(time.time() - self._started_at, 1),
                "history_len": len(self._history),
                "subscribers": len(self._subscribers),
                "port": self.port,
            }


# ======================================================================
# 客户端（供 CLI / adapter / 桌宠订阅使用）
# ======================================================================

def _url(port: int, host: str = DEFAULT_HOST, path: str = "/") -> str:
    return f"http://{host}:{port}{path}"


# 端口协商：桌宠内嵌总线被占端口时回退随机端口并写 runtime/port；
# 发送方连接失败后读它重试一次，适配器链不再被回退打断。
RUNTIME_PORT_FILE = Path(__file__).resolve().parent.parent / "runtime" / "port"


def _fallback_port():
    try:
        p = int(RUNTIME_PORT_FILE.read_text(encoding="utf-8").strip())
        if 0 < p < 65536:
            return p
    except Exception:
        pass
    return None


def send_notification(notif: Notification, port: int = DEFAULT_PORT,
                      host: str = DEFAULT_HOST, timeout: float = 5.0) -> dict:
    """POST 一条消息到总线，返回总线响应 JSON。

    目标端口连不上时读 runtime/port（桌宠端口回退时写入的实际端口）
    重试一次；服务明确拒绝（HTTP 4xx/5xx）不换端口重试。"""
    try:
        return _http_json(port, host, "POST", "/notify",
                          body=notif.to_json().encode("utf-8"),
                          headers={"Content-Type":
                                   "application/json; charset=utf-8"},
                          timeout=timeout)
    except urllib.error.HTTPError:
        raise  # 服务在但拒绝：协议层问题，换端口无意义
    except Exception:
        fb = _fallback_port()
        if fb is not None and fb != port:
            return _http_json(fb, host, "POST", "/notify",
                              body=notif.to_json().encode("utf-8"),
                              headers={"Content-Type":
                                       "application/json; charset=utf-8"},
                              timeout=timeout)
        raise


def fetch_health(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST,
                 timeout: float = 3.0) -> dict:
    try:
        return _http_json(port, host, "GET", "/health", timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except Exception:
        fb = _fallback_port()
        if fb is not None and fb != port:
            return _http_json(fb, host, "GET", "/health", timeout=timeout)
        raise


def fetch_history(n: int = 20, port: int = DEFAULT_PORT,
                  host: str = DEFAULT_HOST, timeout: float = 3.0) -> list:
    try:
        data = _http_json(port, host, "GET", f"/history?n={n}",
                          timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except Exception:
        fb = _fallback_port()
        if fb is not None and fb != port:
            data = _http_json(fb, host, "GET", f"/history?n={n}",
                              timeout=timeout)
        else:
            raise
    return data.get("items", [])


def _http_json(port: int, host: str, method: str, path: str,
               body: bytes = None, headers: dict = None,
               timeout: float = 5.0) -> dict:
    """对总线做一次 HTTP 请求并解析 JSON 响应。

    用 http.client 显式连 host:port（不构造 URL、不跟随重定向），
    与"只连本机回环总线"的语义一致；4xx/5xx 转成 HTTPError，保持与
    原 urllib 版本相同的异常语义。"""
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        status, reason = resp.status, resp.reason
    finally:
        conn.close()
    if status >= 400:
        raise urllib.error.HTTPError(path, status, reason or "", None, None)
    return json.loads(data)


class SSEClient:
    """SSE 长连接订阅者：on_event(Notification) 被推送调用。

    断线后自动重连（默认 5 秒），直到 stop()。重连时带 ?after=<last_mid>
    增量补拉：只收错过的消息，不重放旧消息（不会造成重复气泡）。
    """

    def __init__(self, port: int, host: str = DEFAULT_HOST,
                 on_event=None, on_error=None, retry_sec: float = 5.0,
                 read_timeout: float = None):
        self.port = port
        self.host = host
        self.url = _url(port, host, "/events")
        self.on_event = on_event
        self.on_error = on_error or (lambda e: None)
        self.retry_sec = retry_sec
        # 读超时：服务端心跳间隔 15 秒；超过该值无任何数据（含心跳）视为
        # 连接已死（如总线进程被 kill），及时重连而非阻塞到 urlopen 的 60 秒
        self.read_timeout = read_timeout or (SSE_HEARTBEAT + 5)
        self._last_mid = None          # 已处理的最大消息 id，断线重连用
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> "SSEClient":
        self._thread = threading.Thread(target=self._run, name="pika-sse",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def join(self, timeout: float = 3.0):
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def last_mid(self):
        return self._last_mid

    def _run(self):
        known_identity = None  # (generation, pid)：两者任一变化都视为总线重启
        while not self._stop.is_set():
            # 检测总线重启：generation 覆盖同进程重建，pid 覆盖跨进程重启
            # （新进程 generation 会从 1 重新计，与旧进程相同）。
            # 检测到重启就清掉增量游标，全量补拉，否则 after=旧大值 永远补不到。
            try:
                h = fetch_health(port=self.port, host=self.host, timeout=1.0)
                identity = (h.get("generation"), h.get("pid"))
                if identity != (None, None):
                    if known_identity is None:
                        # 首次拿到身份：若已有游标（说明之前连过旧总线，
                        # 中间总线重启过），清掉游标全量补拉
                        if self._last_mid is not None:
                            self._last_mid = None
                    elif identity != known_identity:
                        self._last_mid = None
                    known_identity = identity
            except Exception:
                pass  # 总线暂不可达，重连循环会继续等
            try:
                url = self.url
                if self._last_mid is not None:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}after={self._last_mid}"
                with urllib.request.urlopen(url, timeout=60) as resp:
                    # 服务端 15 秒发一次心跳；超过 read_timeout 无任何数据
                    # （心跳也没有），说明连接已死（如总线被 kill），
                    # 及时断开走重连，而不是阻塞在 read1 直到 60 秒超时
                    resp.fp.raw._sock.settimeout(self.read_timeout)
                    buf = b""
                    data_lines = []
                    cur_id = None
                    stale_cursor = False
                    while not self._stop.is_set():
                        chunk = resp.read1(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.decode("utf-8", errors="replace").strip()
                            if not line:
                                if data_lines:
                                    self._emit("\n".join(data_lines), cur_id)
                                    data_lines = []
                                    cur_id = None
                                continue
                            if line.startswith(":"):
                                # 带内身份行 ": gen=N pid=P"：与已知身份
                                # 不同，说明这是重启后的总线，而本次连接
                                # 可能带着旧游标（快速重启竞态）——断开
                                # 重连，无游标全量补拉。心跳注释不匹配
                                m = _IDENTITY_RE.match(line)
                                if m:
                                    identity = (int(m.group(1)),
                                                int(m.group(2)))
                                    if known_identity is not None \
                                            and identity != known_identity:
                                        self._last_mid = None
                                        known_identity = identity
                                        stale_cursor = True
                                        break
                                    known_identity = identity
                                continue
                            if line.startswith("id:"):
                                try:
                                    cur_id = int(line[3:].strip())
                                except ValueError:
                                    cur_id = None
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].strip())
                        if stale_cursor:
                            break
            except Exception as e:
                try:
                    self.on_error(e)
                except Exception:
                    pass
            if not self._stop.is_set():
                self._stop.wait(self.retry_sec)

    def _emit(self, data: str, mid):
        try:
            notif = Notification.from_json(data)
        except ProtocolError:
            return
        if self.on_event:
            try:
                self.on_event(notif)
            except Exception:
                # 回调失败：主动断开连接，让外层走重连增量补拉。
                # 游标不推进，重连后 after=<旧mid> 会重新拿到这条消息。
                raise
        if mid is not None:
            self._last_mid = max(self._last_mid or 0, mid)


# ======================================================================
# standalone 运行
# ======================================================================

def _write_port_file(path: str, port: int):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(port))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pika-bus", description="皮卡丘本地通知总线")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="监听端口，0 表示随机分配（默认 7452）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port-file", default=None,
                        help="启动后把实际端口写入该文件（供测试/脚本读取）")
    args = parser.parse_args(argv)

    try:
        bus = BusServer(host=args.host, port=args.port).start()
    except OSError as e:
        print(f"总线启动失败：{e}", file=sys.stderr)
        print(f"端口 {args.port} 可能被占用：请换 --port 或关掉占用方",
              file=sys.stderr)
        return 1
    if args.port_file:
        _write_port_file(args.port_file, bus.port)
    print(f"pika-bus 已启动: http://{bus.host}:{bus.port} (pid={os.getpid()})",
          flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
