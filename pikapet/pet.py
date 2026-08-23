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
import queue
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
except ImportError:
    tk = None  # 无 GUI 环境（CI）下 import 本模块不报错

from . import bus
from . import paths
from . import pet_state
from .bubble import Bubble
from .logs import configure as configure_logging
from .logs import get_logger, swallow
from .menu import PikaMenu
from .pet_core import PetController
from .pixtokens import ASSET, BG, TURN_DIR, TURN_TICK_MS, YELLOW
from .protocol import Notification
from .turn import TurnDirector, frame_index, get_cursor_pos, turn_frame_paths
from .win.idle import get_idle_seconds, WinIdleSource  # noqa: F401

log = get_logger("pet")

DEFAULT_PORT = bus.DEFAULT_PORT

# 半透明像素的判定阈值：alpha 低于 ALPHA_CUT，或亮度低于 LUM_CUT 的
# 半透明像素，都当作"该被挖掉的背景"填成透明品红；其余半透明像素直接
# 压成不透明（Tk 没有逐像素 alpha，只能二选一）
ALPHA_CUT = 32
LUM_CUT = 150
MAGENTA = (255, 0, 255, 255)


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


# 气泡缩放 → 状态气泡的信息量。缩放调的是"显示多少内容"，不只是字号：
# 放大了却只有三行大字很浪费，缩小了塞五条又挤。阈值按气泡缩放档位
# （菜单每次 ±0.25，范围 0.5~2.5）取整，落点稳定、不会在边界抖动。
STATUS_BUDGET = (
    # (缩放下限, 最多几条, 是否带一行摘要)
    (1.6, 6, True),
    (1.3, 5, True),
    (1.0, 4, True),
    (0.8, 3, False),
    (0.0, 2, False),
)


def status_budget(bubble_scale: float):
    """按气泡缩放决定状态气泡显示几条、是否带摘要。返回 (条目数, 带摘要)。"""
    for lo, items, preview in STATUS_BUDGET:
        if bubble_scale >= lo:
            return items, preview
    return STATUS_BUDGET[-1][1], STATUS_BUDGET[-1][2]


def _flatten_transparency(path):
    """把 RGBA 贴图压平成"不透明 + 透明品红"两色 alpha。

    用整图运算替代逐像素 Python 循环：原实现对 180×180 尚可，但换大图时
    每次启动都要跑几万次解释器循环。

    亮度判定刻意不走 convert("L")：后者把加权和四舍五入到整数，恰好落在
    阈值上的像素判定会翻转（实测 assets/pikachu.png 里有 lum=149.718 的
    像素被舍成 150，于是该挖掉的背景没被挖掉）。这里改成整数比较：
    0.299r + 0.587g + 0.114b < 150 两边乘 1000，用 bytes 直接算，
    与原浮点实现逐点一致。
    """
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    r, g, b, a = (band.tobytes() for band in im.split())
    dark_cut = LUM_CUT * 1000
    holes = bytes(
        255 if (av < 255 and (av < ALPHA_CUT
                              or 299 * rv + 587 * gv + 114 * bv < dark_cut))
        else 0
        for rv, gv, bv, av in zip(r, g, b, a))
    mask = Image.frombytes("L", im.size, holes)
    flat = im.copy()
    flat.putalpha(255)                      # 其余半透明像素压成不透明
    flat.paste(MAGENTA, (0, 0), mask)       # 挖掉的部分填透明品红
    return flat



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
        # 后台线程（SSE / 提醒）→ 主线程的通知通道：跨线程绝不直接碰 Tk
        self._ui_queue = queue.Queue()
        self._connect(port, subscribe_only)

        # 内嵌健康提醒：后台线程跑调度循环（默认开启，--no-reminder 关闭）。
        # 此前提醒器是独立进程，忘了启动就等于没有（日志里 8/19 后就没跑过）
        self._reminder_stop = None
        self._reminder_thread = None
        self._scheduler = None   # 提醒调度器（供菜单"立即提醒"取文案）
        if with_reminder:
            self._start_reminder()

        # 驱动 controller 的自动隐藏 tick
        self._tick_ui()
        # 驱动鼠标跟随的渲染 tick（~30fps）
        self._tick_turn()

    # ---- 内嵌健康提醒 ----
    def _start_reminder(self):
        """后台线程跑提醒调度循环。配置读 configs/reminder.json（与独立
        runner 同源），坏配置/缺依赖只报错不挡桌宠启动。"""
        try:
            from .reminder import ReminderScheduler
            from .reminder_runner import load_config
            from .win.idle import WinIdleSource
            cfg = load_config()
        except (ImportError, OSError, ValueError) as e:
            # 提醒是可选功能，配置写错不该连桌宠都起不来；但要明确报出来，
            # 否则用户改了 reminder.json 却一直没提醒，完全不知道为什么
            print(f"提醒器未启动（配置或依赖问题）：{e}", file=sys.stderr)
            log.error("提醒器未启动：%s", e, exc_info=True)
            return

        scheduler = ReminderScheduler(
            activity=WinIdleSource(), sink=self._reminder_sink(),
            config=cfg)
        # 存下来：右键菜单的"立即提醒"要用它的文案池，不再写死一句假消息
        self._scheduler = scheduler
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                # 单步异常不杀线程，下一秒继续；高频循环用 once 限流
                with swallow(log, "提醒调度单步", once=True):
                    scheduler.step()
                stop.wait(1.0)

        self._reminder_stop = stop
        self._reminder_thread = threading.Thread(
            target=loop, name="pika-reminder", daemon=True)
        self._reminder_thread.start()

    def _reminder_sink(self):
        """提醒输出 → 线程安全队列 → 主线程 tick 消费进控制器，
        静音/stale/去重链路自动生效（绝不从后台线程碰 Tk）。"""
        pet = self

        class _Sink:
            def send(self, title, body, level="info", source="reminder"):
                n = Notification(title=title, body=body, level=level,
                                 source=source, ttl=12.0)
                try:
                    pet._ui_queue.put_nowait(n)
                except queue.Full:
                    # UI 队列满说明主线程卡住了，这条提醒就丢了——是真问题
                    log.warning("UI 队列已满，丢弃提醒：%s", title)

            last_send_ok = True

        return _Sink()

    def _stop_reminder(self):
        if self._reminder_stop is not None:
            self._reminder_stop.set()
        if self._reminder_thread is not None:
            self._reminder_thread.join(timeout=2.0)
        self._reminder_thread = None
        self._scheduler = None

    # ---- 总线连接 ----
    def _connect(self, port: int, subscribe_only: bool):
        external = False
        if not subscribe_only:
            try:
                info = bus.fetch_health(port=port, timeout=0.5,
                                        negotiate=False)
                external = info.get("ok") is True
            except OSError as e:
                # 连不上是最常见的情况（没有外部总线），DEBUG 即可
                log.debug("探测外部总线失败，改为内嵌：%s", e)
                external = False
        if subscribe_only or external:
            # 已有独立总线在跑，订阅它
            self.sse = bus.SSEClient(
                port=port, on_event=self._on_bus_msg,
                on_error=lambda e: None)
            self.sse.start()
        else:
            # 内嵌总线：端口被非皮卡丘服务占用时由内核原子分配随机端口，
            # 并把实际端口写入运行时 port 文件（外部软件据此连接）
            fell_back = False
            try:
                self.server = bus.BusServer(port=port).start()
            except OSError:
                self.server = bus.BusServer(port=0).start()
                fell_back = True
            # 端口文件写不了要报出来：外部软件（适配器/钩子）靠它找回退
            # 端口，静默失败会表现为"通知莫名发不出去"
            paths.write_text_atomic(paths.port_file(create_dir=True),
                                    str(self.server.port))
            if fell_back:
                msg = (f"pika-pet 总线端口 {self.server.port} "
                       f"(默认 {port} 被占用，已回退)")
                print(msg, flush=True)
                log.warning("%s", msg)
            self.sse = bus.SSEClient(
                port=self.server.port, on_event=self._on_bus_msg,
                on_error=lambda e: None)
            self.sse.start()

    def _on_bus_msg(self, n: Notification):
        # SSE 回调在 daemon 线程：跨线程调 Tk（after/destroy 等）会触发
        # Tcl_AsyncDelete 崩溃，改为投递线程安全队列，由主线程 tick 消费
        try:
            self._ui_queue.put_nowait(n)
        except queue.Full:
            # 队列满：这条丢了，但 SSE 游标不推进（异常冒回 _emit），
            # 重连后会带 after=<旧mid> 重新拉到
            log.warning("UI 队列已满，丢弃通知：%s", n.title)
            raise

    def _drain_ui_queue(self):
        """主线程消费后台线程投递的通知（SSE / 内嵌提醒共用）。"""
        while True:
            try:
                n = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            with swallow(log, "处理通知"):
                self._controller.handle(n)

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
        # 每 500ms 一次的 UI tick：单次失败不能停掉整个循环，否则气泡
        # 再也不会自动消失。用 once 限流，避免坏状态把日志刷满
        with swallow(log, "控制器 tick", once=True):
            self._controller.tick()
        with swallow(log, "消费 UI 队列", once=True):
            self._drain_ui_queue()
        try:
            self._tick_job = self.root.after(500, self._tick_ui)
        except tk.TclError:
            log.debug("root 已销毁，UI tick 结束")

    # ---- 贴图 ----
    def _load_asset(self):
        if self._load_turn_assets():
            return
        path = ASSET
        if not path.exists():
            log.warning("贴图 %s 不存在，改用手绘兜底图", path)
            self._draw_fallback()
            return
        self._static_pil = None
        try:
            self._static_pil = _flatten_transparency(path)
        except ImportError as e:
            # 没装 Pillow：退回 Tk 自带的 PhotoImage（不做透明处理）
            log.info("Pillow 不可用（%s），静态贴图不做透明压平", e)
        except (OSError, ValueError) as e:
            log.warning("贴图 %s 处理失败，改用 Tk 直接加载：%s", path, e)
        if self._static_pil is None:
            try:
                # 显式钉 master：多 Tk 实例（测试/嵌入）时不绑错默认 root
                self.photo = tk.PhotoImage(file=str(path), master=self.root)
            except tk.TclError as e:
                log.error("贴图 %s 无法加载，改用手绘兜底图：%s", path, e)
                self.photo = None
                self._draw_fallback()
                return
        self._rebuild_static_photo()

    def _rebuild_static_photo(self):
        """按 self.scale 用 NEAREST 重渲染静态贴图。"""
        from PIL import Image, ImageTk
        base = getattr(self, "_static_pil", None)
        if base is None:
            return
        scale = self.scale or 1.0
        im = base
        if scale != 1.0:
            im = im.resize((max(1, round(base.size[0] * scale)),
                            max(1, round(base.size[1] * scale))), Image.NEAREST)
        self.photo = ImageTk.PhotoImage(im, master=self.root)
        self.canvas_w = self.photo.width()
        self.canvas_h = self.photo.height()
        self.canvas.configure(width=self.canvas_w, height=self.canvas_h)
        if self._img_id is not None:
            self.canvas.delete(self._img_id)
        self._img_id = self.canvas.create_image(
            self.canvas_w // 2, self.canvas_h // 2, image=self.photo)

    def _load_turn_assets(self, directory=None) -> bool:
        """加载转身帧资产；成功时以第 0 帧（正面）作为基础贴图。

        帧资产以身体对称轴为画面中心（换边时身体重合、只有尾巴换边），
        画布按帧的实际尺寸加宽，窗口跟着变宽，贴图视觉尺寸不变。
        底帧以 PIL RGBA 缓存（保留 alpha 与透明品红），缩放时用 NEAREST
        重渲染，像素风不糊。"""
        frames = turn_frame_paths(directory or TURN_DIR)
        if frames is None:
            log.info("转身帧资产不完整（%s），鼠标跟随停用，回退静态贴图",
                     directory or TURN_DIR)
            return False
        left_paths, right_paths = frames
        try:
            from PIL import Image
            self._turn_left_pil = [Image.open(str(p)).convert("RGBA")
                                   for p in left_paths]
            self._turn_right_pil = [Image.open(str(p)).convert("RGBA")
                                    for p in right_paths]
        except ImportError as e:
            log.info("Pillow 不可用（%s），鼠标跟随停用", e)
            self._turn_left_pil = []
            self._turn_right_pil = []
            return False
        except (OSError, ValueError) as e:
            log.warning("转身帧读取失败，鼠标跟随停用：%s", e)
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
                out.append(ImageTk.PhotoImage(im, master=self.root))
            return out

        # 记录旧视觉中心，重建后把窗口挪回同一屏幕位置
        old_cx = self.root.winfo_rootx() + self.canvas_w // 2
        old_cy = self.root.winfo_rooty() + self.canvas_h // 2

        self._turn_left = build(self._turn_left_pil)
        self._turn_right = build(self._turn_right_pil)
        pw = max(p.width() for p in self._turn_left + self._turn_right)
        ph = max(p.height() for p in self._turn_left + self._turn_right)
        self.canvas_w, self.canvas_h = pw + 8, ph + 6
        self.canvas.configure(width=self.canvas_w, height=self.canvas_h)
        if self._img_id is not None:
            self.canvas.delete(self._img_id)
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
        except tk.TclError as e:
            log.debug("重定位窗口失败（root 可能已销毁）：%s", e)

    def set_scale(self, scale: float, recenter: bool = True):
        """调整桌宠整体缩放并重渲染（像素风 NEAREST 不糊）。"""
        scale = _clamp(scale, *pet_state.SCALE_RANGE)
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

        资产没带 manifest 是正常的（旧资产），返回 None 表示不吸附。
        manifest 存在但内容不对劲则记 WARNING：那是资产生成脚本的问题，
        静默忽略会让"静止时画面发虚"这种现象查不出原因。"""
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            blends = data["blend_indices"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("转身帧 manifest %s 无法解析，静止不吸附关键帧：%s",
                        manifest, e)
            return None
        if data.get("count") != count:
            log.warning("转身帧 manifest 记录 count=%s 与实际帧数 %s 不符，"
                        "静止不吸附关键帧", data.get("count"), count)
            return None
        if not isinstance(blends, list) or any(
                not isinstance(i, int) for i in blends):
            log.warning("转身帧 manifest 的 blend_indices 不是整数数组，"
                        "静止不吸附关键帧")
            return None
        return sorted(set(range(count)) - set(blends))

    # ---- 鼠标跟随 ----
    def _tick_turn(self):
        # 30fps 渲染 tick：失败不能停掉循环（否则皮卡丘从此不再转头），
        # 也不能每帧记一条日志（30 条/秒会瞬间刷满），用 once 限流
        with swallow(log, "鼠标跟随渲染", once=True):
            if self._turn_left and self.root.state() == "normal":
                pos = get_cursor_pos()
                if pos is not None:
                    self._turn_step(*pos)
        try:
            self._turn_job = self.root.after(TURN_TICK_MS, self._tick_turn)
        except tk.TclError:
            log.debug("root 已销毁，跟随 tick 结束")

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
            ax, ay = self.menu.anchor
            self.menu.reposition(ax + dx, ay + dy)

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
        self._draw_status_bubble()

    def refresh_status_bubble(self):
        """按当前缩放重新取内容并重画状态气泡（缩放联动内容量）。"""
        if self._status_visible:
            self._draw_status_bubble()

    def _draw_status_bubble(self):
        """把控制器的状态模型交给气泡渲染。

        条目数与"是否带摘要"由气泡缩放决定：缩放调的是信息量，不只是
        字号——放大到 1.3 以上给 5 条并带一行摘要，缩到 0.8 以下只留
        2 条标题。上限来自 status_model 的去重历史，不够就少显示几条。
        """
        max_items, preview = status_budget(self.bubble_scale)
        model = self._controller.status_model(max_items=max_items,
                                              preview=preview)
        notif = Notification(title="皮卡丘", body="", level="info",
                             source="pika", ttl=0)
        self.bubble.show(notif, kind="status", status=model)

    def _status_close(self):
        self._status_visible = False
        # 只关状态气泡，别误伤正在显示的通知气泡
        if self.bubble.visible and self.bubble.kind == "status":
            self.bubble.close()

    # ---- 右键菜单 ----
    def _menu(self, e):
        # 菜单文案反映当前状态：以前一律显示"静音开关"，看不出此刻是开还是关
        self.menu.set_items([
            ("显示状态", self._status_show),
            ("立即提醒休息一次", self._manual_remind),
            ("取消静音" if self._controller.muted else "静音",
             self._toggle_mute),
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
        self.bubble_scale = _clamp(self.bubble_scale + step,
                                   *pet_state.BUBBLE_SCALE_RANGE)
        # 立即把当前气泡按新尺寸重弹，用户能立刻看到效果
        self.bubble.redraw()
        self._save_state("bubble_scale")

    def _manual_remind(self):
        """立即来一条真实提醒：走调度器的文案池，不是写死的假消息。

        以前这里 hardcode 一句"站起来走两步"，于是菜单里的"立即提醒"和
        真正的定时提醒长得不一样，也测不到文案池。现在从调度器取——没有
        调度器（--no-reminder）时才退回一句固定文案，并说明原因。
        """
        if self._scheduler is not None:
            body = self._scheduler.manual_body()
            title = self._scheduler.config.title
        else:
            title = "该休息一下了"
            body = "站起来走两步，看看窗外。（提醒调度未启用）"
        self._controller.handle(Notification(
            title=title, body=body, level="warn", source="reminder", ttl=12))

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
            except tk.TclError as e:
                log.debug("角标窗口已不存在：%s", e)
        self._tab_win = None
        self.root.deiconify()
        self.root.lift()

    # ---- 退出 ----
    def _quit(self):
        # 退出路径上每一步都要尽力走完（取消定时器 / 停 SSE / 停总线 /
        # 停提醒 / 存位置），任一步失败不能让后面的收尾被跳过
        for attr in ("_tick_job", "_turn_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError as e:
                    log.debug("取消定时任务 %s 失败：%s", attr, e)
            setattr(self, attr, None)
        if self.sse is not None:
            with swallow(log, "停止 SSE 客户端"):
                self.sse.stop()
        if self.server is not None:
            with swallow(log, "停止内嵌总线"):
                self.server.stop()
        with swallow(log, "停止提醒线程"):
            self._stop_reminder()
        with swallow(log, "退出前保存窗口位置"):
            self._save_state("x", "y")
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
    try:
        configure_logging(file_path=paths.log_file(create_dir=True))
    except (OSError, paths.RuntimeDirError) as e:
        print(f"运行时目录不可用：{e}", file=sys.stderr)
        return 1
    moved = paths.migrate_legacy()
    if moved:
        log.info("已从旧 runtime/ 目录迁移：%s", "、".join(moved))
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
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except (AttributeError, OSError) as e:
        # shcore 是 Win8.1+；老系统退到 user32 的进程级开关
        log.debug("SetProcessDpiAwareness 不可用，改用 SetProcessDPIAware：%s", e)
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError) as e:
        # 两个都不行：Tk 会按 96dpi 渲染，字号测量与实际渲染可能错位
        log.warning("无法设置进程 DPI 感知，气泡排版可能错位：%s", e)



if __name__ == "__main__":
    raise SystemExit(main())
