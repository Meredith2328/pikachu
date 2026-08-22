# -*- coding: utf-8 -*-
"""皮卡丘桌宠：透明置顶小窗口 + 气泡通知显示端。

- 只负责"显示"：收到总线消息就冒泡，不感知消息来源、不做轮询；
- Windows 专用代码（透明窗口 / topmost / 隐藏到角落 / 空闲检测）都在本文件；
- 总线两种接法：本进程内嵌（默认，外部软件总能 POST 到 7452）；
  或 --subscribe-only 订阅外部已运行的总线（此时本进程不开端口）。
- 鼠标跟随：30fps 轮询全局光标，皮卡丘连续转身看向鼠标（assets/turn 帧资产，
  右向为镜像）；资产缺失时自动回退静态贴图。

交互：
  双击       显示"关于"
  右键       状态 / 立即提醒一次 / 静音开关 / 隐藏到角落 / 退出
  悬浮       显示当前状态气泡；通知气泡悬浮期间不自动消失
  气泡点击   立即关闭
"""
import ctypes
import json
import os
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
except ImportError:
    tk = None  # 无 GUI 环境（CI）下 import 本模块不报错

from . import bus
from . import pet_state
from .bubble import Bubble
from .menu import PikaMenu
from .pet_core import PetController
from .pixtokens import ASSET, BG, TURN_DIR, TURN_TICK_MS, YELLOW
from .protocol import Notification
from .turn import TurnDirector, frame_index, get_cursor_pos, turn_frame_paths
from .win.idle import get_idle_seconds, WinIdleSource  # noqa: F401

DEFAULT_PORT = bus.DEFAULT_PORT



# ----------------------------------------------------------------------
# 桌宠主程序
# ----------------------------------------------------------------------
class PikaPet:
    def __init__(self, port: int = DEFAULT_PORT, subscribe_only: bool = False,
                 with_reminder: bool = True):
        _make_dpi_aware()  # 进程级：让 Tk 走物理像素，measure 与渲染一致
        self.root = tk.Tk()
        self.root.title("皮卡丘")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", BG)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)

        self.size = 180
        # 状态持久化：缩放 / 静音 / 位置从上次会话恢复（损坏则回默认）
        _state = pet_state.load_state()
        self.scale = _state["scale"]        # 桌宠缩放：影响画布/贴图/窗口尺寸
        self.bubble_scale = _state["bubble_scale"]  # 气泡缩放：字号/内距/尾巴
        self.canvas = tk.Canvas(self.root, width=self.size, height=self.size,
                                bg=BG, highlightthickness=0)
        self.canvas.pack()
        # 鼠标跟随状态（_load_asset 里尝试加载帧资产，失败则保持空并回退静态图）
        self._turn_left = []
        self._turn_right = []
        self._turn_real = None   # 真实关键帧下标（补帧除外）；静止时吸附到这些帧
        self._director = TurnDirector()
        self._last_turn_key = None
        self._img_id = None
        # 转身帧比静态图宽（身体居中后尾巴向两侧伸展），画布随资产加宽
        self.canvas_w = self.size
        self.canvas_h = self.size
        self._load_asset()
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        if _state["x"] is not None and _state["y"] is not None:
            # 钳回屏幕内（分辨率变了/拔了显示器时不能把桌宠放到屏幕外）
            x = max(0, min(int(_state["x"]), w - self.canvas_w))
            y = max(0, min(int(_state["y"]), h - self.canvas_h))
            self.root.geometry(f"+{x}+{y}")
        else:
            self.root.geometry(f"+{w - self.canvas_w - 20}+{h - self.canvas_h - 60}")

        # 控制器：显示决策全部在 PetController，UI 只挂回调
        self._controller = PetController(
            on_show=lambda n: self.root.after(0, lambda: self._bubble_show(n)),
            on_hide=lambda: self.root.after(0, self._bubble_hide))
        self._controller.muted = _state["muted"]
        self.bubble = Bubble(self.root, on_clicked=self._bubble_click,
                             controller=self._controller, pet=self)
        self.menu = PikaMenu(self.root)
        self._tab_win = None
        self._tick_job = None
        self._turn_job = None
        self._status_bubble = None
        self._status_visible = False
        self._press = None
        self._moved = False

        self.canvas.bind("<ButtonPress-1>", self._start_press)
        self.canvas.bind("<B1-Motion>", self._on_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Double-Button-1>", lambda e: self._about())
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Enter>", self._pet_enter)
        self.canvas.bind("<Leave>", self._pet_leave)

        # 总线：内嵌或订阅外部
        self.server = None
        self.sse = None
        self._connect(port, subscribe_only)

        # 内嵌健康提醒：后台线程跑调度循环（默认开启，--no-reminder 关闭）。
        # 此前提醒器是独立进程，忘了启动就等于没有（日志里 8/19 后就没跑过）
        self._reminder_stop = None
        self._reminder_thread = None
        if with_reminder:
            self._start_reminder()

        # 驱动 controller 的自动隐藏 tick
        self._tick_ui()
        # 驱动鼠标跟随的渲染 tick（~30fps）
        self._tick_turn()

    # ---- 内嵌健康提醒 ----
    def _start_reminder(self):
        """后台线程跑提醒调度循环。配置读 configs/reminder.json（与独立
        runner 同源），坏配置/缺依赖只打日志不挡桌宠启动。"""
        try:
            from .reminder import ReminderScheduler
            from .reminder_runner import load_config
            from .win.idle import WinIdleSource
            cfg = load_config()
        except Exception as e:
            print(f"提醒器未启动（配置或依赖问题）：{e}", file=sys.stderr)
            return

        scheduler = ReminderScheduler(
            activity=WinIdleSource(), sink=self._reminder_sink(),
            config=cfg)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    scheduler.step()
                except Exception:
                    pass  # 单步异常不杀线程，下一秒继续
                stop.wait(1.0)

        self._reminder_stop = stop
        self._reminder_thread = threading.Thread(
            target=loop, name="pika-reminder", daemon=True)
        self._reminder_thread.start()

    def _reminder_sink(self):
        """提醒输出 → 控制器（走 UI 线程），静音/stale/去重逻辑自动生效。"""
        pet = self

        class _Sink:
            def send(self, title, body, level="info", source="reminder"):
                n = Notification(title=title, body=body, level=level,
                                 source=source, ttl=12.0)
                try:
                    pet.root.after(0, lambda: pet._controller.handle(n))
                except Exception:
                    pass  # root 已销毁（退出中）：丢弃

            last_send_ok = True

        return _Sink()

    def _stop_reminder(self):
        if self._reminder_stop is not None:
            self._reminder_stop.set()
        if self._reminder_thread is not None:
            self._reminder_thread.join(timeout=2.0)
        self._reminder_thread = None

    # ---- 总线连接 ----
    def _connect(self, port: int, subscribe_only: bool):
        external = False
        if not subscribe_only:
            try:
                info = bus.fetch_health(port=port, timeout=0.5,
                                        negotiate=False)
                external = info.get("ok") is True
            except Exception:
                external = False
        if subscribe_only or external:
            # 已有独立总线在跑，订阅它
            self.sse = bus.SSEClient(
                port=port, on_event=self._on_bus_msg,
                on_error=lambda e: None)
            self.sse.start()
        else:
            # 内嵌总线：端口被非 pika 服务占用时由内核原子分配随机端口，
            # 并把实际端口写入 runtime/port（外部软件据此连接）
            fell_back = False
            try:
                self.server = bus.BusServer(port=port).start()
            except OSError:
                self.server = bus.BusServer(port=0).start()
                fell_back = True
            runtime_dir = Path(__file__).resolve().parent.parent / "runtime"
            runtime_dir.mkdir(exist_ok=True)
            (runtime_dir / "port").write_text(str(self.server.port),
                                              encoding="utf-8")
            if fell_back:
                print(f"pika-pet 总线端口 {self.server.port} "
                      f"(默认 {port} 被占用，已回退)", flush=True)
            self.sse = bus.SSEClient(
                port=self.server.port, on_event=self._on_bus_msg,
                on_error=lambda e: None)
            self.sse.start()

    def _on_bus_msg(self, n: Notification):
        # SSE 回调在 daemon 线程，桥接到 tk 主线程
        self.root.after(0, lambda: self._controller.handle(n))

    # ---- 控制器回调 ----
    def _bubble_show(self, n: Notification):
        # 悬浮标记的重置由 PetController.handle 在替换气泡时负责
        self._status_close()
        self.bubble.show(n)

    def _bubble_hide(self):
        self.bubble.close()

    def _bubble_click(self):
        # 状态气泡不经过控制器，直接关；通知气泡走 dismiss（清 hover/计时）
        if self.bubble.kind == "status":
            self._status_close()
        else:
            self._controller.dismiss()

    def _tick_ui(self):
        try:
            self._controller.tick()
        except Exception:
            pass
        try:
            self._tick_job = self.root.after(500, self._tick_ui)
        except Exception:
            pass  # root 已销毁

    # ---- 贴图 ----
    def _load_asset(self):
        if self._load_turn_assets():
            return
        path = ASSET
        if not path.exists():
            self._draw_fallback()
            return
        self._static_pil = None
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            px = im.load()
            changed = False
            for y in range(im.size[1]):
                for x in range(im.size[0]):
                    r, g, b, a = px[x, y]
                    if a < 255:
                        lum = 0.299 * r + 0.587 * g + 0.114 * b
                        if a < 32 or lum < 150:
                            px[x, y] = (255, 0, 255, 255)
                            changed = True
                        else:
                            px[x, y] = (r, g, b, 255)
            self._static_pil = im  # 缓存底图，缩放时 NEAREST 重渲染
        except Exception:
            self._static_pil = None
        if self._static_pil is None:
            try:
                self.photo = tk.PhotoImage(file=str(path))
            except Exception:
                self.photo = None
        self._rebuild_static_photo()

    def _rebuild_static_photo(self):
        """按 self.scale 用 NEAREST 重渲染静态贴图。"""
        from PIL import Image, ImageTk
        root = getattr(self, "_static_pil", None)
        if root is None:
            return
        scale = self.scale or 1.0
        im = root
        if scale != 1.0:
            im = im.resize((max(1, round(root.size[0] * scale)),
                            max(1, round(root.size[1] * scale))), Image.NEAREST)
        self.photo = ImageTk.PhotoImage(im)
        self.canvas_w = self.photo.width()
        self.canvas_h = self.photo.height()
        try:
            self.canvas.configure(width=self.canvas_w, height=self.canvas_h)
        except Exception:
            pass
        if self._img_id is not None:
            try:
                self.canvas.delete(self._img_id)
            except Exception:
                pass
        self._img_id = self.canvas.create_image(
            self.canvas_w // 2, self.canvas_h // 2, image=self.photo)

    def _load_turn_assets(self, directory=None) -> bool:
        """加载转身帧资产；成功时以第 0 帧（正面）作为基础贴图。

        帧资产以身体对称轴为画面中心（换边时身体重合、只有尾巴换边），
        画布按帧的实际尺寸加宽，窗口跟着变宽，贴图视觉尺寸不变。
        底帧以 PIL RGBA 缓存（保留 alpha 与透明品红），缩放时用 NEAREST
        重渲染，像素风不糊。"""
        paths = turn_frame_paths(directory or TURN_DIR)
        if paths is None:
            return False
        left_paths, right_paths = paths
        try:
            from PIL import Image
            self._turn_left_pil = [Image.open(str(p)).convert("RGBA")
                                   for p in left_paths]
            self._turn_right_pil = [Image.open(str(p)).convert("RGBA")
                                    for p in right_paths]
        except Exception:
            self._turn_left_pil = []
            self._turn_right_pil = []
            return False
        self._turn_real = self._load_real_indices(
            Path(directory or TURN_DIR), len(self._turn_left_pil))
        return self._rebuild_turn_photos()

    def _rebuild_turn_photos(self, recenter: bool = False):
        """按 self.scale 用 NEAREST 重渲染转身帧，并更新画布/窗口尺寸。"""
        pils = getattr(self, "_turn_left_pil", None)
        if not pils:
            return False
        from PIL import Image, ImageTk
        scale = self.scale or 1.0

        def build(pils):
            out = []
            for im in pils:
                if scale != 1.0:
                    nw = max(1, round(im.size[0] * scale))
                    nh = max(1, round(im.size[1] * scale))
                    im = im.resize((nw, nh), Image.NEAREST)
                out.append(ImageTk.PhotoImage(im))
            return out

        # 记录旧视觉中心，重建后把窗口挪回同一屏幕位置
        old_cx = self.root.winfo_rootx() + self.canvas_w // 2
        old_cy = self.root.winfo_rooty() + self.canvas_h // 2

        self._turn_left = build(self._turn_left_pil)
        self._turn_right = build(self._turn_right_pil)
        pw = max(p.width() for p in self._turn_left + self._turn_right)
        ph = max(p.height() for p in self._turn_left + self._turn_right)
        self.canvas_w, self.canvas_h = pw + 8, ph + 6
        try:
            self.canvas.configure(width=self.canvas_w, height=self.canvas_h)
        except Exception:
            pass
        if self._img_id is not None:
            try:
                self.canvas.delete(self._img_id)
            except Exception:
                pass
        self._img_id = self.canvas.create_image(
            self.canvas_w // 2, self.canvas_h // 2, image=self._turn_left[0])
        self._last_turn_key = None
        if recenter and self.canvas_w and self.canvas_h:
            self._recenter(old_cx, old_cy)
        return True

    def _recenter(self, cx, cy):
        """把窗口中心挪到屏幕坐标 (cx, cy)，并钳回屏幕内。"""
        try:
            x = cx - self.canvas_w // 2
            y = cy - self.canvas_h // 2
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(4, min(x, sw - self.canvas_w - 4))
            y = max(4, min(y, sh - self.canvas_h - 4))
            self.root.geometry(f"+{int(x)}+{int(y)}")
        except tk.TclError:
            pass

    def set_scale(self, scale: float, recenter: bool = True):
        """调整桌宠整体缩放并重渲染（像素风 NEAREST 不糊）。"""
        scale = max(0.4, min(scale, 3.0))
        if abs(scale - self.scale) < 1e-6:
            return
        self.scale = scale
        if getattr(self, "_turn_left_pil", None):
            self._rebuild_turn_photos(recenter=recenter)
        elif getattr(self, "_static_pil", None) is not None:
            old_cx = self.root.winfo_rootx() + self.canvas_w // 2
            old_cy = self.root.winfo_rooty() + self.canvas_h // 2
            self._rebuild_static_photo()
            if recenter:
                self._recenter(old_cx, old_cy)
        # 桌宠尺寸变了：气泡跟着重定位（仍贴脑袋）
        if getattr(self, "bubble", None) is not None:
            self.bubble.reposition()
        self._save_state("scale")

    def _save_state(self, *fields):
        """把指定字段落盘（拖动结束时存位置、缩放/静音变化时存对应项）。"""
        mapping = {
            "scale": lambda: self.scale,
            "bubble_scale": lambda: self.bubble_scale,
            "muted": lambda: self._controller.muted,
            "x": lambda: self.root.winfo_x(),
            "y": lambda: self.root.winfo_y(),
        }
        pet_state.save_state(**{f: mapping[f]() for f in fields if f in mapping})




    def _load_real_indices(self, directory: Path, count: int):
        """从 manifest 读补帧下标，返回真实关键帧下标集合。

        解析失败或数量对不上时返回 None（不吸附，行为与旧资产一致）。"""
        try:
            data = json.loads((directory / "manifest.json").read_text(
                encoding="utf-8"))
            blends = data["blend_indices"]
            if data.get("count") != count:
                return None
            if not isinstance(blends, list) or any(
                    not isinstance(i, int) for i in blends):
                return None
            return sorted(set(range(count)) - set(blends))
        except Exception:
            return None

    # ---- 鼠标跟随 ----
    def _tick_turn(self):
        try:
            if self._turn_left and self.root.state() == "normal":
                pos = get_cursor_pos()
                if pos is not None:
                    self._turn_step(*pos)
        except Exception:
            pass
        try:
            self._turn_job = self.root.after(TURN_TICK_MS, self._tick_turn)
        except Exception:
            pass  # root 已销毁

    def _turn_step(self, mx: float, my: float):
        """按全局光标位置更新一帧朝向（独立出来便于注入坐标做测试）。

        姿态收敛（鼠标停下）时吸附到最近的真实关键帧：混合补帧只在运动中
        充当动态模糊，静止画面永远清晰。"""
        px = self.root.winfo_rootx() + self.canvas_w // 2
        py = self.root.winfo_rooty() + self.canvas_h // 2
        pose, direction = self._director.update(mx, my, px, py)
        photos = self._turn_right if direction > 0 else self._turn_left
        real = self._turn_real if self._director.settled else None
        idx = frame_index(pose, len(photos), real)
        key = (id(photos), idx)
        if key != self._last_turn_key:
            self.canvas.itemconfig(self._img_id, image=photos[idx])
            self._last_turn_key = key

    def _draw_fallback(self):
        c = self.canvas
        c.create_oval(30, 22, 140, 118, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_oval(45, 78, 125, 152, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_polygon(40, 34, 52, 6, 70, 34, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_polygon(44, 32, 54, 14, 64, 32, fill="#2E2E2E")
        c.create_polygon(100, 34, 118, 6, 130, 34, fill=YELLOW, outline="#3B2F2F", width=2)
        c.create_polygon(106, 32, 116, 14, 126, 32, fill="#2E2E2E")
        c.create_oval(58, 60, 74, 76, fill="#2E2E2E")
        c.create_oval(96, 60, 112, 76, fill="#2E2E2E")
        c.create_oval(63, 64, 68, 69, fill="white")
        c.create_oval(101, 64, 106, 69, fill="white")
        c.create_oval(44, 82, 66, 102, fill="#FF7B7B")
        c.create_oval(104, 82, 126, 102, fill="#FF7B7B")
        c.create_arc(74, 72, 96, 94, start=0, extent=180, style=tk.ARC,
                     outline="#3B2F2F", width=2)

    # ---- 拖动 ----
    def _start_press(self, e):
        self._press = (e.x_root, e.y_root)
        self._moved = False

    def _on_move(self, e):
        if self._press:
            self._moved = True
            dx = e.x_root - self._press[0]
            dy = e.y_root - self._press[1]
            self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")
            self._press = (e.x_root, e.y_root)
            # 皮卡丘挪动了，气泡/状态菜单跟着走，贴着脑袋
            self.bubble.reposition()
            self.menu.reposition(self.menu._anchor_x + dx, self.menu._anchor_y + dy)

    def _on_release(self, e):
        if self._moved:
            self._save_state("x", "y")   # 拖完才落盘，拖动过程不写
        self._press = None

    def _on_wheel(self, e):
        """滚轮缩放桌宠：上滚放大，下滚缩小，像素风 NEAREST 不糊。"""
        step = 0.15 if e.delta > 0 else -0.15
        self.set_scale(self.scale + step)

    # ---- 悬浮状态气泡 ----
    def _pet_enter(self, e):
        if not self.bubble.visible:
            self._status_show()

    def _pet_leave(self, e):
        self._status_close()

    def _status_show(self):
        # 通知气泡显示中：先 dismiss 它（走控制器正常收尾），再弹状态气泡，
        # 避免通知的 ttl 计时器回头误关状态气泡
        if self.bubble.visible and self.bubble.kind == "notice":
            self._controller.dismiss()
        if self._status_visible:
            return
        self._status_visible = True
        notif = Notification(title="皮卡丘", body=self._controller.status_text(),
                             level="info", source="pika", ttl=0)
        self.bubble.show(notif, kind="status")

    def _status_close(self):
        self._status_visible = False
        # 只关状态气泡，别误伤正在显示的通知气泡
        if self.bubble.visible and self.bubble.kind == "status":
            self.bubble.close()

    # ---- 右键菜单 ----
    def _menu(self, e):
        self.menu.set_items([
            ("显示状态", self._status_show),
            ("立即提醒休息一次", self._manual_remind),
            ("静音开关", self._toggle_mute),
            ("放大桌宠", lambda: self._zoom_pet(0.25)),
            ("缩小桌宠", lambda: self._zoom_pet(-0.25)),
            ("放大气泡", lambda: self._zoom_bubble(0.25)),
            ("缩小气泡", lambda: self._zoom_bubble(-0.25)),
            ("隐藏到角落", self._hide),
            ("关于", self._about),
            ("退出", self._quit),
        ])
        self.menu.popup(e.x_root, e.y_root)

    def _zoom_pet(self, step):
        self.set_scale(self.scale + step)

    def _zoom_bubble(self, step):
        self.bubble_scale = max(0.5, min(self.bubble_scale + step, 2.5))
        # 立即把当前气泡按新尺寸重弹，用户能立刻看到效果
        if self.bubble.visible:
            self.bubble.show(self.bubble._last_notif,
                             kind=self.bubble.kind)
        self._save_state("bubble_scale")

    def _manual_remind(self):
        n = Notification(title="该休息一下了",
                         body="手动提醒：站起来走两步，看看窗外。",
                         level="warn", source="reminder", ttl=12)
        self._controller.handle(n)

    def _toggle_mute(self):
        muted = self._controller.toggle_mute()
        self._save_state("muted")
        n = Notification(title="🔇 已静音" if muted else "🔊 已取消静音",
                         body="静音期间消息只记录、不弹气泡。" if muted else "新消息会正常弹出。",
                         level="info", source="pika", ttl=6)
        self._controller.handle(n)

    def _about(self):
        from . import __version__
        n = Notification(title=f"皮卡丘 {__version__}",
                         body="本地通知总线 + 桌宠 + 健康提醒。\n"
                              "右键桌宠查看更多操作；外部软件通过总线 POST 消息即可弹气泡。",
                         level="info", source="pika", ttl=12)
        self._controller.handle(n)

    # ---- 隐藏到角落 ----
    def _hide(self):
        self.bubble.close()
        self.root.withdraw()
        tab = tk.Toplevel(self.root)
        tab.overrideredirect(True)
        tab.attributes("-topmost", True)
        try:
            tab.attributes("-transparentcolor", BG)
        except tk.TclError:
            pass
        tab.configure(bg=BG)
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        tab.geometry(f"36x44+{w - 44}+{h - 92}")
        tk.Label(tab, text="⚡", font=("Segoe UI Emoji", 20),
                 bg=BG, fg=YELLOW, cursor="hand2").pack()
        tab.bind("<Button-1>", lambda e: self._show())
        self._tab_win = tab

    def _show(self):
        self.bubble.close()
        if self._tab_win is not None:
            try:
                self._tab_win.destroy()
            except Exception:
                pass
        self._tab_win = None
        self.root.deiconify()
        self.root.lift()

    # ---- 退出 ----
    def _quit(self):
        for attr in ("_tick_job", "_turn_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
            setattr(self, attr, None)
        if self.sse is not None:
            self.sse.stop()
        if self.server is not None:
            self.server.stop()
        self._stop_reminder()
        try:
            self._save_state("x", "y")   # 退出前存最后位置
        except Exception:
            pass
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="pika-pet", description="皮卡丘桌宠")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--subscribe-only", action="store_true",
                        help="只订阅已有总线，不在本进程开端口")
    parser.add_argument("--no-reminder", action="store_true",
                        help="不启动内嵌健康提醒")
    args = parser.parse_args(argv)
    if tk is None:
        print("本环境没有 tkinter，无法启动桌宠 GUI", file=sys.stderr)
        return 1
    _make_dpi_aware()
    pet = PikaPet(port=args.port, subscribe_only=args.subscribe_only,
                  with_reminder=not args.no_reminder)
    pet.mainloop()
    return 0


_DPI_SET = False


def _make_dpi_aware():
    """进程级 DPI awareness：让 Tk 的 'tk scaling' 与系统真实 DPI 一致。

    不设置时 Windows 会按 96dpi 裁切/模糊，或 tk scaling 与系统 DPI 不一致
    导致 fonts.measure（逻辑像素）和实际渲染（物理像素）错位——词长测量与
    卡片宽度对不上。必须在任何 Tk 窗口创建之前调用（进程级、一次生效、
    幂等：重复调用不报错）。"""
    global _DPI_SET
    if _DPI_SET:
        return
    _DPI_SET = True
    import ctypes as _c
    try:
        _c.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            _c.windll.user32.SetProcessDPIAware()
        except Exception:
            pass



if __name__ == "__main__":
    raise SystemExit(main())
