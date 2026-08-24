# -*- coding: utf-8 -*-
"""Codex notify 分发器：把同一个事件同时转给多个消费者。

Codex 的 `notify` 在 config.toml 里只有一个槽位。本机那个槽已经被
computer-use 占了（`codex-computer-use.exe turn-ended`），直接改成皮卡丘
就会把 computer-use 弄坏。所以这里做一层分发：

    Codex ──notify──> 本脚本 ──┬─> 原来的 computer-use（原样透传参数）
                               └─> 皮卡丘适配器（弹气泡）

设计约束与 zcode 钩子一致：**绝不阻塞、绝不拖累 Codex**。
- 无论下游成败，一律 exit 0；
- 先转发 computer-use（它是原本就在的功能，优先级更高），再通知皮卡丘；
- 皮卡丘那边失败只记日志，不影响 computer-use 的结果。

Codex 传参形式是 `notify_program <事件JSON>`（也可能追加 notify 数组里
配置的额外参数），本脚本把收到的 argv 原样传给 computer-use，只额外把
事件 JSON 喂给皮卡丘适配器。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pikapet.logs import configure as configure_logging  # noqa: E402
from pikapet.logs import get_logger, swallow             # noqa: E402
from pikapet import paths                                # noqa: E402

log = get_logger("codex.notify_dispatch")

# 原来占着 notify 槽的程序。用环境变量覆盖便于换机器/换版本。
DEFAULT_DOWNSTREAM = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" /
    "runtimes" / "cua_node" / "cd454f7c85348168" / "bin" / "node_modules" /
    "@oai" / "sky" / "bin" / "windows" / "codex-computer-use.exe"
)
DOWNSTREAM_ENV = "PIKACHU_CODEX_NOTIFY_DOWNSTREAM"
# 下游程序在 notify 数组里带的固定参数（本机是 "turn-ended"）
DOWNSTREAM_ARGS_ENV = "PIKACHU_CODEX_NOTIFY_DOWNSTREAM_ARGS"

TIMEOUT_SEC = 20


def _no_window_kwargs():
    """Windows 下让子进程不弹控制台窗口的 subprocess 参数。

    computer-use 是个控制台程序（subsystem:console）。从 pythonw 这种无
    控制台的父进程启动它时，Windows 会**新开一个控制台窗口**；而 Windows
    Terminal 的 closeOnExit 默认是 graceful，子进程非零退出就把那个标签页
    留在屏幕上。于是每收到一次 notify 就攒下一个空白终端标签。

    CREATE_NO_WINDOW 让它在后台无窗口运行——它本来也不需要被看见。
    """
    if sys.platform != "win32":
        return {}
    # 0x08000000 = CREATE_NO_WINDOW
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def downstream_command():
    """原 notify 程序的路径；返回 None 表示没有下游要转发。"""
    override = os.environ.get(DOWNSTREAM_ENV)
    if override is not None:
        override = override.strip()
        if not override:
            return None          # 显式设为空：只通知皮卡丘，不转发
        return Path(override)
    return DEFAULT_DOWNSTREAM if DEFAULT_DOWNSTREAM.is_file() else None


def forward_downstream(argv):
    """把原始参数转给 computer-use。它是原有功能，优先跑、失败要留痕。

    安全性说明（这里确实在 spawn 子进程，值得写清楚）：
    - **不经过 shell**：subprocess.run 传的是 argv 列表、shell=False，
      事件 JSON 里就算带 `&&`、`|`、`%PATH%` 也只是一个普通字符串参数，
      不会被解释成命令；
    - **可执行文件不来自事件内容**：只能是内置的 computer-use 路径，或
      本机环境变量显式指定的路径，且必须先通过 is_file() 检查。Codex 传来
      的 argv 只作为「参数」附在后面，永远不会变成被执行的程序；
    - 下游本来就是 Codex 自己要调的程序，我们只是把同一份参数原样递过去，
      没有放大它的权限。
    """
    exe = downstream_command()
    if exe is None:
        log.debug("没有配置下游 notify 程序，跳过转发")
        return
    if not exe.is_file():
        log.warning("下游 notify 程序不存在，本次事件没转发：%s", exe)
        return
    extra = [a for a in
             (os.environ.get(DOWNSTREAM_ARGS_ENV) or "turn-ended").split()
             if a]
    with swallow(log, f"转发 notify 事件给 {exe.name}"):
        subprocess.run([str(exe), *extra, *argv], timeout=TIMEOUT_SEC,
                       capture_output=True, shell=False,
                       **_no_window_kwargs())


def notify_pet(argv):
    """把事件 JSON 交给皮卡丘适配器（内部会解析并 POST 到总线）。"""
    from pikapet.adapters.codex import main as codex_main
    payload = argv[0] if argv else ""
    with swallow(log, "通知皮卡丘"):
        codex_main(["event", payload])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    with swallow(log, "初始化日志"):
        configure_logging(file_path=paths.log_file(create_dir=True))
    # 先转发原有功能，再通知桌宠：桌宠是附加的，不能挤掉 computer-use
    forward_downstream(argv)
    notify_pet(argv)
    return 0     # 永远成功：notify 程序绝不能让 Codex 卡住或报错


if __name__ == "__main__":
    raise SystemExit(main())
