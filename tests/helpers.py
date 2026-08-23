# -*- coding: utf-8 -*-
"""测试共享小工具（free_port / wait_http 等），收敛各测试文件里的重复定义。

本文件不匹配 test_*.py，unittest discover 不会把它当测试收集。
用法（测试文件里）：from tests.helpers import free_port, wait_http
"""
import contextlib
import http.client
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet import paths  # noqa: E402  （要先插好 sys.path 才能 import）


def free_port():
    """让系统分配一个空闲端口（绑定后立即释放，有微小竞态但测试够用）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(port, timeout=10, path="/health"):
    """轮询直到该端口的 HTTP 端点可达。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if bus_request(port, "GET", path)[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def bus_request(port, method, path, body=None, headers=None, timeout=5):
    """对本机回环总线发一次请求，返回 (状态码, 响应体文本)。

    用 http.client 显式连 127.0.0.1:port，与被测代码（pikapet.bus._http_json）
    同源：不拼 URL、不跟随重定向，也不会因为 4xx 就抛异常——测试要断言
    状态码本身，用 urlopen 得靠 catch HTTPError 才能拿到，很绕。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def bus_post_json(port, payload_bytes, token=None, ctype="application/json",
                  timeout=5):
    """给总线 POST 一段原始字节，返回 (状态码, 响应体文本)。"""
    headers = {"Content-Type": ctype}
    if token is not None:
        headers["X-Pika-Token"] = token
    return bus_request(port, "POST", "/notify", body=payload_bytes,
                       headers=headers, timeout=timeout)


@contextlib.contextmanager
def bus_stream(port, path="/events", timeout=5):
    """打开一个流式 GET（SSE），yield 响应对象，退出时关连接。

    SSE 不能用 bus_request：那会 read() 到底、在长连接上永远阻塞。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "text/event-stream"})
        yield conn.getresponse()
    finally:
        conn.close()


def gui_available():
    """当前环境能否创建 Tk 窗口（无 GUI 时跳过相关测试）。"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


@contextlib.contextmanager
def isolated_home():
    """把运行时目录（token / port / pet_state）指向临时目录。

    通过 PIKACHU_HOME 环境变量生效，所以**子进程也一起隔离**——测试
    绝不会碰用户真实的 %LOCALAPPDATA%\\pikachu，也不会因为本机跑着桌宠
    而串台。退出时恢复原值。

    同时禁掉"旧目录迁移"：否则隔离目录里第一次读 token 会把仓库真实的
    runtime/token 搬进来，测试结果就取决于开发机上有没有那个文件了。
    本进程用模块标记、子进程用 PIKACHU_NO_MIGRATE 环境变量（子进程自己
    会跑一次迁移，patch 模块变量管不到）。要测迁移本身的用例请直接操作
    paths.migrate_legacy。
    """
    prev = os.environ.get(paths.HOME_ENV)
    prev_no_mig = os.environ.get(paths.NO_MIGRATE_ENV)
    prev_migrated = paths._migrated_once
    with tempfile.TemporaryDirectory(prefix="pika-home-") as td:
        os.environ[paths.HOME_ENV] = td
        os.environ[paths.NO_MIGRATE_ENV] = "1"
        paths._migrated_once = True
        try:
            yield Path(td)
        finally:
            paths._migrated_once = prev_migrated
            for key, val in ((paths.HOME_ENV, prev),
                             (paths.NO_MIGRATE_ENV, prev_no_mig)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


@contextlib.contextmanager
def isolated_runtime_port():
    """让"端口协商"在测试期间不可用（隔离的 home 里没有 port 文件）。

    子进程自己按 PIKACHU_HOME 解析运行时目录，patch 模块属性管不到它们，
    所以必须在环境变量层面隔离。
    """
    with isolated_home():
        yield
