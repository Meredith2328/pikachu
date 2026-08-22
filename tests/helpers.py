# -*- coding: utf-8 -*-
"""测试共享小工具（free_port / wait_http 等），收敛各测试文件里的重复定义。

本文件不匹配 test_*.py，unittest discover 不会把它当测试收集。
用法（测试文件里）：from tests.helpers import free_port, wait_http
"""
import socket
import time
import urllib.request

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


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
