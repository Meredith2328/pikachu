# -*- coding: utf-8 -*-
"""本地通知总线：外部软件 POST 消息进来，订阅者（桌宠）被实时推送。

- 纯标准库，仅监听 127.0.0.1，不暴露到局域网；
- 推送基于 SSE（HTTP 长连接），桌宠挂一个连接即可实时收消息，不是轮询；
- 断线重连用 ?after=<mid> 增量补拉：只补错过的消息，不重放旧消息；
- 慢订阅者不阻塞发布方：队列满即断开该订阅者；
- 桌面桌宠默认把本服务内嵌在自身进程里（同一个端口），
  也可以独立跑一个 bus 进程，让多个显示端共享。

用法（standalone）:
    python -m pikapet.bus --port 7452 --port-file runtime/port
"""
import argparse
import http.client
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import paths
from .logs import configure as configure_logging
from .logs import get_logger, swallow
from .protocol import Notification, ProtocolError, PROTOCOL_VERSION

log = get_logger("bus")

DEFAULT_HOST = "127.0.0.1"
# 7452 = PIKA 的手机九键键序（P=7 I=4 K=5 A=2），刻意挑的冷门端口，
# 避开 3000/5000/8000/8080/8765 这类常用开发端口，降低撞车概率
DEFAULT_PORT = 7452
SSE_HEARTBEAT = 15.0
# SSE 线程每次 recv 的时长上限。Windows 上无法打断"已经阻塞住"的 recv
# （shutdown 与 settimeout 都不生效，实测要等满读超时），所以只能让每次
# recv 短一点，线程才能频繁回到循环顶部检查停止标志——否则退出桌宠时它
# 会比 Tk 活得更久，最后从非主线程释放 Tk 对象，触发 Tcl_AsyncDelete。
POLL_SEC = 0.5
SSE_REPLAY_LIMIT = 200  # 与历史容量一致：增量补拉不因回放窗口丢消息
MIN_TOKEN_LEN = 32      # 短于此视为损坏，重新生成
HISTORY_LIMIT = 200     # 总线保留的历史条数（= SSE 回放窗口）
MAX_BODY_BYTES = 1_000_000
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
        # 客户端断连/重置是常态，按 DEBUG 记一行摘要，不刷堆栈；
        # 其余异常带 traceback 记 WARNING，便于事后定位
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, TimeoutError)):
            log.debug("客户端 %s 断开连接：%s", client_address, exc)
            return
        log.warning("处理 %s 的请求时异常", client_address, exc_info=True)


class BusHandler(BaseHTTPRequestHandler):
    server_version = "pika-bus"
    protocol_version = "HTTP/1.1"
    timeout = 30  # 慢连接（只发一半请求）不长期占线程

    # ---- 内部状态通过 server 暴露 ----
    @property
    def bus(self) -> "BusServer":
        return self.server.bus

    def log_message(self, fmt, *args):
        # 访问日志走 DEBUG：总线不该把每条请求都打到 stderr，
        # 但排查时能用 PIKACHU_LOG_LEVEL=DEBUG 全部看到
        log.debug("%s - %s", self.address_string(), fmt % args)

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
        # token 鉴权：只有能读到 runtime/token 的己方工具（CLI/适配器/
        # 钩子，均自动附带）才能投递；其他进程的误投/端口撞车直接拒绝
        if (self.headers.get("X-Pika-Token") or "") != self.bus.token:
            self._send_json(403, {"ok": False,
                                  "error": "缺少或错误的 X-Pika-Token 头"})
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
        if length <= 0 or length > MAX_BODY_BYTES:
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
        raw = qs.get("n", ["20"])[0]
        try:
            n = int(raw)
        except ValueError:
            # 不静默退回 20：调用方写错了参数，应当知道
            self._send_json(400, {"ok": False,
                                  "error": f"n 必须是整数，收到 {raw!r}"})
            return
        n = max(1, min(n, HISTORY_LIMIT))
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
        raw_after = qs.get("after", [""])[0]
        if raw_after:
            try:
                after = int(raw_after)
            except ValueError:
                # 不静默当成"无游标"：那会让客户端以为在增量补拉，
                # 实际收到全量回放，重复弹泡且无从察觉
                self._send_json(400, {"ok": False,
                                      "error": f"after 必须是整数，收到 {raw_after!r}"})
                return
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
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # 订阅者断开是常态（客户端重连/退出），DEBUG 留痕即可
            log.debug("SSE 连接结束：%s", e)
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
        self.token = _load_or_create_token()
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
                    # 队列已满的订阅者收不到哨兵，但 server_close 会切断
                    # 它的连接，客户端照样会走重连；记一行便于对照
                    log.debug("停机哨兵投递失败（订阅者队列已满）")
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
            if len(self._history) > HISTORY_LIMIT:
                del self._history[:-HISTORY_LIMIT]
            subs = list(self._subscribers)
        dead = []
        for s in subs:
            try:
                s.put_nowait((mid, notif))
            except queue.Full:
                dead.append(s)
        if dead:
            log.warning("踢出 %d 个慢订阅者（队列已满，改由其重连补拉）",
                        len(dead))
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
                    pass          # 清空到底即达成目的，非异常路径
                try:
                    s.put_nowait((SENTINEL_KICK, None))
                except queue.Full:
                    log.debug("踢出哨兵投递失败（队列刚被填满）")
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

# 端口协商：桌宠内嵌总线被占端口时回退随机端口并写运行时 port 文件；
# 发送方连接失败后读它重试一次，适配器链不再被回退打断。
# 投递鉴权：token 文件里存一串运行时随机生成的密钥（首次启动生成，
# 绝不进仓库/源码）。总线要求 POST /notify 携带 X-Pika-Token 头与之
# 相符；己方发送方（CLI/适配器/钩子）读同一文件自动附带，对用户无感。
# 它挡的是误投和端口撞车，不是同用户恶意进程（那读得到文件）。
# 两个文件都在运行时目录（见 pikapet.paths），不再写源码目录旁。


class TokenError(RuntimeError):
    """token 不可用（读不出且写不进去）。"""


def _load_or_create_token() -> str:
    """读 token 文件；没有或过短则生成新的写回。

    写盘失败直接抛 TokenError，不退回"仅本进程内存有效"——那样总线拿着
    内存 token、发送方读不到文件，表现是所有 POST 都 403，比启动就报错
    难查得多。

    生成前先跑一次旧目录迁移：否则新装的副本会先造一个新 token，与仍在
    跑的旧桌宠（持旧 token）互不认识。
    """
    paths.migrate_legacy_once()
    path = paths.token_file()
    if path.is_file():
        t = path.read_text(encoding="utf-8").strip()
        if len(t) >= MIN_TOKEN_LEN:
            return t
    t = secrets.token_hex(32)
    try:
        paths.write_text_atomic(paths.token_file(create_dir=True), t)
    except (OSError, paths.RuntimeDirError) as e:
        raise TokenError(
            f"无法写入 token 文件 {path}：{e}；"
            f"没有它发送方与总线无法互认（POST 会全部 403）") from e
    return t


def _client_token():
    """发送方读 token。读不到返回 None：此时不附带头，由服务端裁决并
    给出 403 + 明确原因，而不是在客户端假装成功。"""
    paths.migrate_legacy_once()
    path = paths.token_file()
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _fallback_port():
    """读端口协商文件。文件不存在返回 None（正常情况：没发生过回退）；
    内容不是合法端口则抛——那说明有人写坏了它，静默忽略会让"连不上"
    变成无从下手的谜题。"""
    path = paths.port_file()
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    try:
        p = int(raw)
    except ValueError as e:
        raise PortFileError(
            f"端口文件 {path} 内容不是数字：{raw!r}") from e
    if not 0 < p < 65536:
        raise PortFileError(f"端口文件 {path} 里的端口越界：{p}")
    return p


class PortFileError(RuntimeError):
    """端口协商文件内容非法。"""


def _with_port_negotiation(port: int, host: str, negotiate: bool, attempt):
    """执行 attempt(port)；连接失败时读端口协商文件换端口重试一次。

    只对 OSError（连不上/超时/连接被重置）做重试。HTTPError 是"服务在
    但拒绝"，属协议层问题，换端口无意义；PortFileError 说明协商文件被
    写坏，直接向上抛而不是当作"没有回退端口"——静默忽略会让"消息发不
    出去"完全失去线索。
    """
    try:
        return attempt(port)
    except urllib.error.HTTPError:
        raise
    except OSError as first:
        if not negotiate:
            raise
        fb = _fallback_port()
        if fb is None or fb == port:
            raise
        log.debug("端口 %s 连接失败（%s），改用协商端口 %s 重试",
                  port, first, fb)
        return attempt(fb)


def send_notification(notif: Notification, port: int = DEFAULT_PORT,
                      host: str = DEFAULT_HOST, timeout: float = 5.0) -> dict:
    """POST 一条消息到总线，返回总线响应 JSON。

    自动附带 X-Pika-Token（读运行时 token 文件，与服务端同源）；目标端口
    连不上时读端口协商文件（桌宠端口回退时写入的实际端口）重试一次；
    服务明确拒绝（HTTP 4xx/5xx）不换端口重试。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    tok = _client_token()
    if tok:
        headers["X-Pika-Token"] = tok
    else:
        log.warning("读不到 token 文件 %s，本次投递不附带鉴权头，"
                    "总线会以 403 拒绝", paths.token_file())
    body = notif.to_json().encode("utf-8")
    return _with_port_negotiation(
        port, host, True,
        lambda p: _http_json(p, host, "POST", "/notify", body=body,
                             headers=headers, timeout=timeout))


def fetch_health(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST,
                 timeout: float = 3.0, negotiate: bool = True) -> dict:
    """查询总线健康。negotiate=False 时不做端口回退——用于"探测这个端口
    是不是皮卡丘总线"的场景（协商会把探测带偏）。"""
    return _with_port_negotiation(
        port, host, negotiate,
        lambda p: _http_json(p, host, "GET", "/health", timeout=timeout))


def fetch_history(n: int = 20, port: int = DEFAULT_PORT,
                  host: str = DEFAULT_HOST, timeout: float = 3.0) -> list:
    data = _with_port_negotiation(
        port, host, True,
        lambda p: _http_json(p, host, "GET", f"/history?n={n}",
                             timeout=timeout))
    return data.get("items", [])


def _http_json(port: int, host: str, method: str, path: str,
               body: bytes = None, headers: dict = None,
               timeout: float = 5.0) -> dict:
    """对总线做一次 HTTP 请求并解析 JSON 响应。

    用 http.client 显式连 host:port（不构造 URL、不跟随重定向），
    与"只连本机回环总线"的语义一致；4xx/5xx 转成 HTTPError，保持与
    原 urllib 版本相同的异常语义。"""
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
        self.on_event = on_event
        self.on_error = on_error or (lambda e: None)
        self.retry_sec = retry_sec
        # 读超时：服务端心跳间隔 15 秒；超过该值无任何数据（含心跳）视为
        # 连接已死（如总线进程被 kill），socket 超时后走重连
        self.read_timeout = read_timeout or (SSE_HEARTBEAT + 5)
        self._last_mid = None          # 已处理的最大消息 id，断线重连用
        self._stop = threading.Event()
        self._thread = None
        self._conn = None              # 当前 HTTP 连接（stop 时关掉）
        self._sock = None              # 该连接的 socket（stop 时 shutdown 打断 recv）
        self._conn_lock = threading.Lock()

    def start(self) -> "SSEClient":
        self._thread = threading.Thread(target=self._run, name="pika-sse",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """请求停止，并主动切断当前连接把读取打断。

        只置标志位是不够的：SSE 线程通常正卡在 read1() 上，最长要等
        read_timeout（20 秒）才会醒来看一眼标志位。调用方（桌宠退出）
        等不了那么久，于是线程会在 Tk 销毁之后才醒来、从非主线程碰 Tk，
        触发 Tcl_AsyncDelete 崩溃。关掉 socket 让 read1 立刻抛错返回。
        """
        self._stop.set()
        with self._conn_lock:
            conn, self._conn = self._conn, None
            sock, self._sock = self._sock, None
        # 只 close() 打不断正在进行的 recv：getresponse() 之后 socket 归
        # 响应对象所有，conn.sock 已是 None，close 只是解引用。对 socket
        # 本身 shutdown() 才会让内核把阻塞中的 recv 立刻返回。
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                log.debug("shutdown SSE socket 时出错：%s", e)
        if conn is not None:
            try:
                conn.close()
            except OSError as e:
                log.debug("关闭 SSE 连接时出错（已在停止流程中）：%s", e)

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
                        log.info("总线身份从 %s 变为 %s（重启），清空增量游标",
                                 known_identity, identity)
                        self._last_mid = None
                    known_identity = identity
            except (OSError, urllib.error.HTTPError, ValueError) as e:
                # 总线暂不可达或响应异常：重连循环会继续等，DEBUG 留痕
                log.debug("总线身份探测失败：%s", e)
            try:
                path = "/events"
                if self._last_mid is not None:
                    path = f"{path}?after={self._last_mid}"
                # 与 _http_json 同源：用 http.client 显式连 host:port，
                # 不构造 URL、不跟随重定向，与"只连本机回环总线"的语义
                # 一致。读超时直接设在 socket 上，不必再去摸 urlopen 返回
                # 对象的私有内部（resp.fp.raw._sock）。
                conn = http.client.HTTPConnection(
                    self.host, self.port, timeout=self.read_timeout)
                try:
                    conn.request("GET", path,
                                 headers={"Accept": "text/event-stream"})
                    # 登记 socket 让 stop() 能打断正在进行的 recv。必须在
                    # getresponse() 之前取：那一步会把 socket 所有权交给
                    # 响应对象，之后 conn.sock 就是 None 了。
                    with self._conn_lock:
                        if self._stop.is_set():
                            conn.close()
                            break
                        self._conn = conn
                        self._sock = conn.sock
                    resp = conn.getresponse()
                    if resp.status != 200:
                        raise urllib.error.HTTPError(
                            path, resp.status, resp.reason or "", None, None)
                    # 把 socket 超时压到 POLL_SEC：Windows 上既没法 shutdown
                    # 也没法 settimeout 打断"已经阻塞住"的 recv，只能让每次
                    # recv 本身短一点，这样线程能频繁回到循环顶部看 stop 标志。
                    # 真正的"连接死了"判定仍按 read_timeout 累计计算。
                    if self._sock is not None:
                        self._sock.settimeout(POLL_SEC)
                    idle = 0.0
                    buf = b""
                    data_lines = []
                    cur_id = None
                    stale_cursor = False
                    while not self._stop.is_set():
                        try:
                            chunk = resp.read1(4096)
                        except socket.timeout:
                            # 这一小段没数据：累计空闲，超过读超时才当连接已死
                            idle += POLL_SEC
                            if idle >= self.read_timeout:
                                raise
                            continue
                        idle = 0.0
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
                                raw_id = line[3:].strip()
                                try:
                                    cur_id = int(raw_id)
                                except ValueError:
                                    # 服务端只会写整数 id；出现别的说明流
                                    # 被污染了，值得知道而不是悄悄丢游标
                                    log.warning("SSE id 行不是整数：%r", raw_id)
                                    cur_id = None
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].strip())
                        if stale_cursor:
                            break
                finally:
                    with self._conn_lock:
                        if self._conn is conn:
                            self._conn = None
                            self._sock = None
                    conn.close()
            except Exception as e:
                log.debug("SSE 连接中断，%s 秒后重连：%s", self.retry_sec, e)
                with swallow(log, "SSE on_error 回调"):
                    if self.on_error is not None:
                        self.on_error(e)
            if not self._stop.is_set():
                self._stop.wait(self.retry_sec)

    def _emit(self, data: str, mid):
        try:
            notif = Notification.from_json(data)
        except ProtocolError as e:
            # 收到不符合协议的事件：丢弃这一条但要留痕（服务端与客户端
            # 版本不一致时，这是唯一的线索）
            log.warning("丢弃不符合协议的 SSE 事件：%s", e)
            return
        if self.on_event:
            # 回调失败不在这里吞：让异常冒到 _run 的 except，连接断开后
            # 走重连增量补拉。游标不推进，重连后 after=<旧mid> 会重新
            # 拿到这条消息。
            self.on_event(notif)
        if mid is not None:
            self._last_mid = max(self._last_mid or 0, mid)


# ======================================================================
# standalone 运行
# ======================================================================

def _write_port_file(path, port: int):
    """把实际端口写到指定文件（原子替换，避免读到写一半的内容）。"""
    paths.write_text_atomic(Path(path), str(port))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pika-bus", description="皮卡丘本地通知总线")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="监听端口，0 表示随机分配（默认 7452）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port-file", default=None,
                        help="启动后把实际端口写入该文件（供测试/脚本读取）")
    args = parser.parse_args(argv)

    configure_logging(file_path=paths.log_file(create_dir=True))
    moved = paths.migrate_legacy()
    if moved:
        log.info("已从旧 runtime/ 目录迁移：%s", "、".join(moved))
    try:
        bus = BusServer(host=args.host, port=args.port).start()
    except OSError as e:
        print(f"总线启动失败：{e}", file=sys.stderr)
        print(f"端口 {args.port} 可能被占用：请换 --port 或关掉占用方",
              file=sys.stderr)
        return 1
    except (TokenError, paths.RuntimeDirError) as e:
        print(f"总线启动失败：{e}", file=sys.stderr)
        return 1
    if args.port_file:
        _write_port_file(args.port_file, bus.port)
    print(f"pika-bus 已启动: http://{bus.host}:{bus.port} (pid={os.getpid()})",
          flush=True)
    log.info("总线启动 host=%s port=%s pid=%s", bus.host, bus.port, os.getpid())
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
