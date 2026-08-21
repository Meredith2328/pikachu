# -*- coding: utf-8 -*-
"""桌宠随鼠标转身的姿态决策与帧资产加载。

- TurnDirector 是纯逻辑：鼠标相对桌宠的方位角 → 目标姿态 [0,1]，内部做指数
  平滑与方向滞回，保证动作连续不抖；不依赖 tkinter，可独立单测。
- 帧资产在 assets/turn/{left,right}/NN.png，由 tools/build_turn_assets.py 从
  pikachu_turn_v4.mp4 生成（left 为原始朝向，right 为镜像）；资产缺失时跟随
  功能自动停用，桌宠回退到静态贴图。

方位模型：以桌宠为原点、屏幕 y 轴向下。定义方位角 theta = atan2(|dx|, |dy|)：
正上方为 0°，水平为 90°。theta 超过 enter_deg 才"认定"朝向（左/右），回落到
exit_deg 以下才回到正面；已认定朝向后鼠标越过头顶到另一侧，也要再次超过
enter_deg 才改变期望朝向——滞回带防止鼠标在头顶附近抖动时朝向反复横跳。

换边规则：右向帧是左向帧的镜像，同一姿态下身体不重合（尾巴换边），所以
渲染朝向绝不允许在姿态 > 0 时直接切换。期望朝向与当前朝向不一致时，先把
目标姿态压到 0（平滑转回正面），等姿态到达正面帧（左右第 0 帧完全相同）
才真正切换朝向，再朝新目标转身——任何鼠标轨迹都不会出现镜像瞬移。

姿态目标值随 theta 在 [up_deg, level_deg] 区间线性增长，再经一阶惯性环节
（时间常数 tau）逼近，鼠标瞬移时皮卡丘也是连续转过而不是跳帧。
"""
import ctypes
import math
import time
from pathlib import Path


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


class TurnDirector:
    """把鼠标位置决策成 (pose, direction)。pose∈[0,1] 为转身幅度，
    direction: -1 朝左（原始帧）、+1 朝右（镜像帧）、0 正面。

    内部维护两个状态：_want 是鼠标方位加滞回后"期望"的朝向；_dir 是当前
    渲染朝向。二者不一致时目标姿态强制为 0，只有姿态回到正面帧才把 _dir
    切到 _want——保证换边永远发生在左右帧完全相同的正面。"""

    SWITCH_EPS = 0.001  # 姿态低于此值视为已回到正面帧
    SETTLE_EPS = 0.003  # 姿态与目标差低于此值视为已静止（吸附真实关键帧）

    def __init__(self, up_deg: float = 12.0, level_deg: float = 75.0,
                 enter_deg: float = 22.0, exit_deg: float = 14.0,
                 tau: float = 0.09, clock=time.time):
        if level_deg <= up_deg:
            raise ValueError("level_deg 必须大于 up_deg")
        if not exit_deg < enter_deg:
            raise ValueError("exit_deg 必须小于 enter_deg（滞回带）")
        if tau <= 0:
            raise ValueError("tau 必须为正")
        self.up_deg = up_deg
        self.level_deg = level_deg
        self.enter_deg = enter_deg
        self.exit_deg = exit_deg
        self.tau = tau
        self._clock = clock
        self._pose = 0.0
        self._target = 0.0
        self._dir = 0     # 当前渲染朝向
        self._want = 0    # 滞回后的期望朝向
        self._last_t = None

    @property
    def pose(self) -> float:
        return self._pose

    @property
    def settled(self) -> bool:
        """姿态是否已收敛到目标（鼠标停下后为 True，渲染端据此吸附到
        最近的真实关键帧，避免停在混合补帧上发虚）。"""
        return abs(self._pose - self._target) <= self.SETTLE_EPS

    @property
    def direction(self) -> int:
        return self._dir

    @property
    def wanted_direction(self) -> int:
        return self._want

    def reset(self):
        self._pose = 0.0
        self._target = 0.0
        self._dir = 0
        self._want = 0
        self._last_t = None

    def _update_want(self, theta: float, dx: float) -> int:
        if self._want == 0:
            if theta >= self.enter_deg and dx != 0:
                self._want = -1 if dx < 0 else 1
        elif theta <= self.exit_deg:
            self._want = 0
        else:
            # 越过头顶到另一侧且角度足够大：改变期望朝向（滞回带内不变，防抖）
            opposite = (self._want < 0 < dx) or (self._want > 0 > dx)
            if opposite and theta >= self.enter_deg:
                self._want = -1 if dx < 0 else 1
        return self._want

    def update(self, mx: float, my: float, px: float, py: float):
        """喂入鼠标坐标 (mx,my) 与桌宠中心 (px,py)，返回 (pose, direction)。"""
        now = self._clock()
        if self._last_t is None:
            dt = self.tau  # 首帧按一个完整时间常数算，避免第一步过冲
        else:
            dt = max(0.0, now - self._last_t)
        self._last_t = now

        dx = mx - px
        dy = my - py
        if dx == 0 and dy == 0:
            theta = 0.0
        else:
            theta = math.degrees(math.atan2(abs(dx), abs(dy)))

        want = self._update_want(theta, dx)
        if want != self._dir:
            if self._pose <= self.SWITCH_EPS:
                self._dir = want  # 已在正面帧，换边无跳变
        target = 0.0
        if want == self._dir and self._dir != 0:
            span = self.level_deg - self.up_deg
            target = _clamp01((theta - self.up_deg) / span)
        self._target = target

        # 一阶惯性平滑：pose 以时间常数 tau 逼近 target
        k = 1.0 - math.exp(-dt / self.tau)
        self._pose += (target - self._pose) * k
        if abs(target - self._pose) < 0.002:
            self._pose = target
        return self._pose, self._dir


def frame_index(pose: float, count: int, real=None) -> int:
    """姿态 [0,1] → 帧下标；count<1 视为无帧返回 0。

    real 是真实关键帧下标集合（补帧除外）。给出时吸附到最近的真实帧——
    混合补帧只该在运动中充当动态模糊，静止时停在上面会发虚。"""
    if count <= 0:
        return 0
    if count == 1:
        return 0
    idx = int(round(_clamp01(pose) * (count - 1)))
    idx = max(0, min(count - 1, idx))
    if real:
        idx = min(real, key=lambda r: (abs(r - idx), r))
    return idx


def turn_frame_paths(directory: Path):
    """读取帧目录，返回 (left_paths, right_paths)；缺失或两侧数量不一致
    返回 None（调用方据此停用跟随功能）。"""
    directory = Path(directory)
    left_dir = directory / "left"
    right_dir = directory / "right"
    if not (left_dir.is_dir() and right_dir.is_dir()):
        return None
    left = sorted(left_dir.glob("*.png"))
    right = sorted(right_dir.glob("*.png"))
    if not left or len(left) != len(right):
        return None
    if [p.name for p in left] != [p.name for p in right]:
        return None
    return left, right


def get_cursor_pos():
    """全局鼠标坐标（Windows）。失败或非 Windows 返回 None。"""
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
    except Exception:
        pass
    return None
