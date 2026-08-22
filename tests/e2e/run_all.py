# -*- coding: utf-8 -*-
"""皮卡丘端到端测试：起真进程、走真 HTTP、验真结果。

覆盖：
1. 独立总线 + CLI 客户端往返；
2. 桌宠进程（内嵌总线）→ 外部 POST → 桌宠内部收到；
3. 健康提醒（假数据源）→ 总线 → 桌宠全链路；
4. 防抖：相同消息连续发只弹一次（桌宠侧）;
5. SSE 长连接 + 心跳保活。

用法: python tests/e2e/run_all.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PY = sys.executable
PASS = 0
FAIL = 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def wait_until(pred, timeout=10, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def http_post_headers():
    headers = {"Content-Type": "application/json"}
    try:
        from pika.bus import _client_token
        tok = _client_token()
        if tok:
            headers["X-Pika-Token"] = tok
    except Exception:
        pass
    return headers


def http_post(port, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/notify",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=http_post_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_json(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def spawn(cmd, **kw):
    """启动子进程，自动丢弃输出避免缓冲阻塞。"""
    return subprocess.Popen(
        [PY, *cmd], cwd=ROOT, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **kw)


def run_script(py_cmd, env_extra=None, timeout=30):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run([PY] + py_cmd, cwd=ROOT, capture_output=True, text=True,
                       env=env, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


# ----------------------------------------------------------------------
def test_standalone_bus_and_cli():
    print("\n[1] 独立总线 + CLI 客户端往返")
    port = free_port()
    bus_proc = spawn(["-m", "pika.bus", "--port", str(port)])
    try:
        ok = wait_until(lambda: http_get_json(port, "/health").get("ok"), timeout=8)
        check("总线进程启动并响应 /health", ok)

        rc, out, err = run_script(["-m", "pika.cli", "--port", str(port),
                                   "send", "E2E标题", "E2E正文", "--source", "e2e"])
        check("CLI send 退出码 0", rc == 0, err)

        items = http_get_json(port, "/history")["items"]
        check("历史包含消息", any(i["title"] == "E2E标题" for i in items))

        rc, out, err = run_script(["-m", "pika.cli", "--port", str(port), "health"])
        check("CLI health 退出码 0", rc == 0, err)
    finally:
        bus_proc.terminate()
        bus_proc.wait(10)


def test_pet_embedded_bus():
    print("\n[2] 桌宠进程（内嵌总线）→ 外部 POST → 桌宠内部收到")
    port = free_port()
    # 无 GUI 环境跳过；有 GUI 则真启动桌宠
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        probe.update()
        probe.destroy()
        gui_ok = True
    except Exception:
        gui_ok = False
    if not gui_ok:
        print("  ⏭  跳过（无 GUI 环境）")
        return
    pet_proc = spawn(["-m", "pika.pet", "--port", str(port)])
    try:
        ok = wait_until(lambda: http_get_json(port, "/health").get("ok"), timeout=10)
        check("桌宠内嵌总线可访问", ok)
        if not ok:
            return

        # 通过 SSE 从"桌宠总线"侧收到 → 模拟桌宠内部 on_event
        import sys as _sys
        _sys.path.insert(0, ROOT)
        from pika.bus import SSEClient
        from pika.protocol import Notification
        received = []
        client = SSEClient(port=port, on_event=lambda n: received.append(n))
        client.start()
        try:
            resp = http_post(port, {"title": "桌宠收到", "body": "来自外部",
                                    "source": "ext", "ttl": 5})
            check("POST 返回 ok", resp.get("ok"), str(resp))
            ok = wait_until(lambda: any(n.title == "桌宠收到" for n in received),
                            timeout=8)
            check("桌宠侧收到消息", ok)
        finally:
            client.stop()
            client.join()

        # 防抖：连续两次相同消息 → 桌宠控制器只弹一次
        from pika.pet_core import PetController
        ctrl = PetController()
        r1 = ctrl.handle(Notification(title="重复", source="x", ttl=5))
        r2 = ctrl.handle(Notification(title="重复", source="x", ttl=5))
        check("相同消息去重（第二次 deduped）",
              r1 == "shown" and r2 == "deduped", f"{r1}/{r2}")
    finally:
        pet_proc.terminate()
        pet_proc.wait(10)


def test_reminder_full_chain():
    print("\n[3] 健康提醒（真实装配）→ 总线 → 桌宠侧全链路")
    port = free_port()
    bus_proc = spawn(["-m", "pika.bus", "--port", str(port)])
    tmpdir = tempfile.mkdtemp(prefix="pika-e2e-")
    try:
        ok = wait_until(lambda: http_get_json(port, "/health").get("ok"), timeout=8)
        check("总线已启动", ok)
        if not ok:
            return

        import sys as _sys
        _sys.path.insert(0, ROOT)
        from pika.bus import SSEClient
        received = []
        client = SSEClient(port=port, on_event=lambda n: received.append(n))
        client.start()
        try:
            # 假数据源：idle=0（一直在工作）
            fake_path = os.path.join(tmpdir, "activity.json")
            with open(fake_path, "w", encoding="utf-8") as f:
                json.dump({"idle_minutes": 0}, f)
            # 短间隔配置：每 3 秒一次（interval_min/max 单位是分钟）
            config_path = os.path.join(tmpdir, "reminder.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "interval_enabled": True,
                    "interval_min": 0.05,
                    "interval_max": 0.05,
                    "long_session_enabled": False,
                    "categories": ["eye"],
                    "title": "该休息一下了",
                }, f)

            # 常驻进程（真实时钟），等它真触发
            proc = spawn(["-m", "pika.reminder_runner", "--config", config_path,
                          "--fake", fake_path, "--port", str(port),
                          "--interval", "0.2"])
            try:
                ok = wait_until(
                    lambda: any(n.source == "reminder" for n in received),
                    timeout=15)
                check("SSE 收到 reminder 消息（真实时钟触发）", ok, str(received))

                items = http_get_json(port, "/history")["items"]
                check("总线历史含 reminder 消息",
                      any(i["source"] == "reminder" for i in items))
            finally:
                proc.terminate()
                proc.wait(10)
        finally:
            client.stop()
            client.join()
    finally:
        bus_proc.terminate()
        bus_proc.wait(10)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_zcode_adapter_chain():
    print("\n[4] adapter-zcode → 总线 全链路")
    port = free_port()
    bus_proc = spawn(["-m", "pika.bus", "--port", str(port)])
    try:
        ok = wait_until(lambda: http_get_json(port, "/health").get("ok"), timeout=8)
        check("总线已启动", ok)
        if not ok:
            return
        rc, out, err = run_script(["-m", "pika.adapters.zcode", "--port", str(port),
                                   "daily-brief", "--stage", "done",
                                   "--detail", "生成 3 个文件"])
        check("zcode adapter 退出码 0", rc == 0, err)
        items = http_get_json(port, "/history")["items"]
        check("历史含 zcode 消息",
              any(i["source"] == "zcode" and "daily-brief" in i["title"]
                  for i in items))
    finally:
        bus_proc.terminate()
        bus_proc.wait(10)


def test_sse_heartbeat():
    print("\n[5] SSE 长连接保活（空闲后仍能接收消息）")
    port = free_port()
    bus_proc = spawn(["-m", "pika.bus", "--port", str(port)])
    try:
        ok = wait_until(lambda: http_get_json(port, "/health").get("ok"), timeout=8)
        check("总线已启动", ok)
        if not ok:
            return
        import socket as _sock
        s = _sock.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
        buf = b""
        deadline = time.time() + 5
        while b"\r\n\r\n" not in buf and time.time() < deadline:
            buf += s.recv(4096)
        check("SSE 连接建立（收到响应头）", b"200 OK" in buf)

        # 空闲一小段时间（模拟无消息期）
        time.sleep(2)
        # 连接应仍然存活：发一条消息，连接必须能收到事件
        http_post(port, {"title": "保活验证", "source": "hb", "ttl": 5})
        s.settimeout(6)
        try:
            data = b""
            deadline = time.time() + 6
            text = ""
            while "保活验证" not in text and time.time() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                text = data.decode("utf-8", "replace")
            check("空闲后连接仍能收到新消息", "保活验证" in text, repr(data[:120]))
        except Exception as e:
            check("空闲后连接仍能收到新消息", False, repr(e))
        s.close()
    finally:
        bus_proc.terminate()
        bus_proc.wait(10)


def main():
    print("=" * 50)
    print("皮卡丘端到端测试")
    print("=" * 50)
    tests = [test_standalone_bus_and_cli, test_pet_embedded_bus,
             test_reminder_full_chain, test_zcode_adapter_chain,
             test_sse_heartbeat]
    for t in tests:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print(f"  ❌ {t.__name__} 异常: {e!r}")
    print("=" * 50)
    print(f"通过 {PASS} · 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
