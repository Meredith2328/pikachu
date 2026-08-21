# -*- coding: utf-8 -*-
"""Windows 键盘/鼠标空闲检测（GetLastInputInfo）。仅本模块依赖 Win32。
"""
import ctypes
import sys

LASTINPUTINFO = None


def _build_struct():
    global LASTINPUTINFO
    if LASTINPUTINFO is None:
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        LASTINPUTINFO = _LASTINPUTINFO
    return LASTINPUTINFO


def _compute_idle_seconds(tick64: int, dw_time: int) -> float:
    """由 GetTickCount64 与 GetLastInputInfo 的 dwTime 计算空闲秒数。

    dwTime 是 32 位毫秒（49.7 天回绕），tick64 不回绕。把 tick64 截成
    32 位再按无符号回绕差计算，避免系统运行超 49.7 天后空闲时间算错。
    """
    tick32 = tick64 & 0xFFFFFFFF
    elapsed = (tick32 - dw_time) & 0xFFFFFFFF
    return elapsed / 1000.0


def get_idle_seconds() -> float:
    """自上次键盘/鼠标输入以来的秒数。非 Windows 或调用失败返回 0.0。"""
    if sys.platform != "win32":
        return 0.0
    try:
        info = _build_struct()()
        info.cbSize = ctypes.sizeof(_build_struct())
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick64 = ctypes.windll.kernel32.GetTickCount64()
        return _compute_idle_seconds(tick64, info.dwTime)
    except Exception:
        return 0.0


class WinIdleSource:
    """ActivitySource 的 Windows 实现：空闲 = 键盘/鼠标没动。"""

    def idle_minutes(self, now: float) -> float:
        return get_idle_seconds() / 60.0
