# -*- coding: utf-8 -*-
"""运行时数据目录：token / 端口文件 / 桌宠状态 / 日志的落脚点。

此前这些文件写在源码目录旁的 `runtime/`（`__file__` 往上两级），有三个
问题：装到 site-packages 后会试图往包目录写文件；两份 checkout 各有一套
token，互相连不上；源码目录被运行时产物污染。

现在统一放 `%LOCALAPPDATA%\\pikachu\\`（Windows）或 `~/.local/share/pikachu`
（其他平台），可用 `PIKACHU_HOME` 覆盖（测试与多实例并存都靠它）。

**不做静默 fallback**：目录建不出来、文件写不进去一律抛异常。token 写不了
盘却继续跑，会让发送方与总线各持一份内存 token，表现为"POST 全部 403"——
这种"看起来在跑其实全断了"的状态比直接报错难查得多。
"""
import os
import sys
from pathlib import Path

HOME_ENV = "PIKACHU_HOME"
# 设为 1 时跳过旧 runtime/ 迁移。测试用：隔离目录里不该把仓库真实的
# token 搬进来（子进程自己会跑一次迁移，patch 模块变量管不到它们）。
NO_MIGRATE_ENV = "PIKACHU_NO_MIGRATE"
APP_DIR_NAME = "pikachu"

TOKEN_NAME = "token"
PORT_NAME = "port"
PET_STATE_NAME = "pet_state.json"
LOG_NAME = "pikachu.log"
HOOK_LOG_NAME = "hook_stdin.log"


class RuntimeDirError(RuntimeError):
    """运行时目录不可用（建不出来 / 不是目录 / 没有写权限）。"""


def _default_base() -> Path:
    """平台默认的应用数据目录（不创建，只计算路径）。"""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / APP_DIR_NAME
        # LOCALAPPDATA 缺失是异常环境（精简容器等），退到用户目录下的
        # 隐藏目录。这不是"静默降级"：路径仍然确定、仍在用户可写区域，
        # 且 home() 本身失败会抛异常。
        return Path.home() / ".pikachu"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def base_dir(create: bool = False) -> Path:
    """运行时根目录。PIKACHU_HOME 优先，否则用平台默认位置。

    create=True 时确保目录存在且可写，任何问题都抛 RuntimeDirError。
    """
    override = os.environ.get(HOME_ENV)
    path = Path(override).expanduser() if override else _default_base()
    if not create:
        return path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeDirError(
            f"无法创建运行时目录 {path}：{e}；"
            f"可设置 {HOME_ENV} 指向一个可写目录") from e
    if not path.is_dir():
        raise RuntimeDirError(f"运行时路径 {path} 不是目录")
    if not os.access(path, os.W_OK):
        raise RuntimeDirError(
            f"运行时目录 {path} 没有写权限；"
            f"可设置 {HOME_ENV} 指向一个可写目录")
    return path


def runtime_path(name: str, create_dir: bool = False) -> Path:
    """运行时目录下某个文件的路径。name 只能是单段文件名。"""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"运行时文件名非法：{name!r}")
    return base_dir(create=create_dir) / name


def token_file(create_dir: bool = False) -> Path:
    return runtime_path(TOKEN_NAME, create_dir)


def port_file(create_dir: bool = False) -> Path:
    return runtime_path(PORT_NAME, create_dir)


def pet_state_file(create_dir: bool = False) -> Path:
    return runtime_path(PET_STATE_NAME, create_dir)


def log_file(create_dir: bool = False) -> Path:
    return runtime_path(LOG_NAME, create_dir)


def hook_log_file(create_dir: bool = False) -> Path:
    return runtime_path(HOOK_LOG_NAME, create_dir)


def write_text_atomic(path: Path, text: str) -> None:
    """原子写文本：先写同目录临时文件再 os.replace 换上去。

    写一半被中断也不会留下截断的 token/端口文件。失败直接抛（调用方
    决定是报错退出还是提示用户），不静默。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def legacy_runtime_dir() -> Path:
    """旧版本的 `<仓库>/runtime` 目录（供一次性迁移读取）。"""
    return Path(__file__).resolve().parent.parent / "runtime"


def migrate_legacy(names=(TOKEN_NAME, PET_STATE_NAME)) -> list:
    """把旧 runtime/ 里的 token 与桌宠状态搬到新目录，返回搬动的文件名。

    只搬"有长期价值"的（token 决定能否互通、pet_state 是用户偏好）；
    port 是每次启动重写的、日志是历史垃圾，不搬。已存在的不覆盖。
    失败直接抛——迁移悄悄失败会让用户以为偏好丢了。
    """
    legacy = legacy_runtime_dir()
    if not legacy.is_dir():
        return []
    moved = []
    target_dir = base_dir(create=True)
    for name in names:
        src = legacy / name
        dst = target_dir / name
        if not src.is_file() or dst.exists():
            continue
        write_text_atomic(dst, src.read_text(encoding="utf-8"))
        moved.append(name)
    return moved


_migrated_once = False


def migrate_legacy_once() -> list:
    """进程内只跑一次的迁移，供"任何要读 token / 状态的地方"前置调用。

    必须发生在**第一次读 token 之前**：否则新目录里会先懒生成一个新
    token，而 migrate 不覆盖已存在的文件，结果是新装的副本与仍在跑的
    旧桌宠各持一个 token，所有投递 403。

    这里不抛异常：调用点是 token 读取这种基础路径，迁移不成也应让程序
    继续走到各自更具体的报错（写不了 token / 403）。
    """
    global _migrated_once
    if _migrated_once:
        return []
    _migrated_once = True
    if os.environ.get(NO_MIGRATE_ENV) == "1":
        return []
    try:
        return migrate_legacy()
    except (OSError, RuntimeDirError):
        return []
