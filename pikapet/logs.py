# -*- coding: utf-8 -*-
"""统一日志：给"吞掉的异常"一个去处。

项目里大量 `except Exception: pass` 是有意的防御——桌宠不该因为一次贴图
重绘失败就崩掉。问题在于它们**静默**：真出 bug 时没有任何线索。本模块
提供两样东西：

- `get_logger(name)`：拿一个 `pikachu.*` 命名空间下的 logger；
- `swallow(logger, 动作描述)`：上下文管理器，替代 `except Exception: pass`。
  异常照旧不向外传播，但会带完整 traceback 记一条日志。

日志默认只往 stderr 走，级别由环境变量 `PIKACHU_LOG_LEVEL` 控制（默认
WARNING）。长驻进程（桌宠 / 独立总线）额外调 `configure(file_path=...)`
挂一个滚动文件 handler，方便事后翻查。

高频路径（如 30fps 的跟随渲染 tick）用 `swallow(..., once=True)`：同一个
动作第一次失败按原级别记录，后续降到 DEBUG，既不刷屏也不会真的静默。
"""
import contextlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

ROOT_NAME = "pikachu"
LEVEL_ENV = "PIKACHU_LOG_LEVEL"
DEFAULT_LEVEL = "WARNING"

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 已经 log 过一次的 (logger 名, 动作) —— once=True 的降级依据
_seen_once = set()


class LogLevelError(ValueError):
    """PIKACHU_LOG_LEVEL 取值非法。"""


def resolve_level(raw=None) -> int:
    """把级别名解析成 logging 的数值级别。

    非法取值直接报错，不静默退回默认值——环境变量拼错时应该立刻知道，
    而不是以为调高了日志级别却什么都没变。
    """
    if raw is None:
        raw = os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL
    if isinstance(raw, int):
        return raw
    name = str(raw).strip().upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        raise LogLevelError(
            f"{LEVEL_ENV} 取值非法：{raw!r}；可选 "
            "DEBUG/INFO/WARNING/ERROR/CRITICAL")
    return level


def get_logger(name: str = None) -> logging.Logger:
    """取 `pikachu.<name>` 下的 logger（name 为空时取根）。"""
    if not name:
        return logging.getLogger(ROOT_NAME)
    if name.startswith(ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def configure(level=None, file_path=None, stream=None) -> logging.Logger:
    """配置 pikachu 日志根。可重复调用（幂等，不会叠加同类 handler）。

    - level: 级别名或数值；None 时读 PIKACHU_LOG_LEVEL，默认 WARNING；
    - file_path: 给出时挂一个滚动文件 handler（同一路径不重复挂）；
      目录建不出来或文件打不开会抛异常——日志落不了盘属于要知道的问题。
    - stream: stderr 的替代流（测试用）。
    """
    root = get_logger()
    root.setLevel(resolve_level(level))
    # 不向 logging 的全局根传播：宿主程序（钩子调用方等）的配置与我们无关
    root.propagate = False
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    target_stream = stream if stream is not None else sys.stderr
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, RotatingFileHandler)
               and h.stream is target_stream
               for h in root.handlers):
        sh = logging.StreamHandler(target_stream)
        sh.setFormatter(formatter)
        root.addHandler(sh)

    if file_path is not None:
        path = os.path.abspath(str(file_path))
        already = any(isinstance(h, RotatingFileHandler)
                      and os.path.abspath(h.baseFilename) == path
                      for h in root.handlers)
        if not already:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES,
                                     backupCount=LOG_BACKUPS,
                                     encoding="utf-8")
            fh.setFormatter(formatter)
            root.addHandler(fh)
    return root


@contextlib.contextmanager
def swallow(logger: logging.Logger, action: str,
            level: int = logging.WARNING, once: bool = False):
    """执行一段"失败也要继续"的代码，异常记日志但不向外传播。

    这是 `except Exception: pass` 的替代品：行为一致（调用方不受影响），
    但留下带 traceback 的记录。

    once=True 时同一 (logger, action) 只在首次按 level 记录，之后降到
    DEBUG——供 30fps 渲染 tick 这类高频路径使用，避免刷满日志。

    注意：KeyboardInterrupt / SystemExit 属于 BaseException，不在捕获范围，
    Ctrl+C 照常中断。
    """
    try:
        yield
    except Exception:
        key = (logger.name, action)
        if once and key in _seen_once:
            eff = logging.DEBUG
        else:
            eff = level
            if once:
                _seen_once.add(key)
        logger.log(eff, "%s 失败（已忽略，继续运行）", action, exc_info=True)


def reset_for_tests():
    """清掉 handler 与 once 记忆（仅测试用，避免用例间互相影响）。"""
    root = get_logger()
    for h in list(root.handlers):
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    root.setLevel(logging.NOTSET)
    _seen_once.clear()
