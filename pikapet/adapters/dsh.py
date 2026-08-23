# -*- coding: utf-8 -*-
"""adapter: dsh → 皮卡丘总线的适配器（headless 子任务包装器）。

DSH 是一次性 headless agent（`dsh --profile headless "任务"`），没有钩子
系统，所以本适配器做**包装运行**：把 dsh 包在中间，开始时弹「▶ 开始」
气泡，结束后弹「✅ 完成 + 回答开头一小段」或「❌ 失败 + stderr 尾部」，
同时把 stdout 原样透传——包装器对调用方完全透明，可以无脑替换 dsh。

用法：
  包装模式（推荐，替代直接调 dsh）：
    python -m pikapet.adapters.dsh run "调研X" "任务文本..." --cwd D:\\scratch --timeout 420
    # 超长任务文本（Windows 命令行 ~32K 上限）写文件后用：
    python -m pikapet.adapters.dsh run "调研X" --task-file /tmp/dsh-task.md
    # stdout = dsh 的最终回答；退出码与 dsh 一致；过程中皮卡丘弹两个气泡

  报告模式（手动汇报某个阶段，与 zcode 适配器同构）：
    python -m pikapet.adapters.dsh report "调研X" --stage done --detail "结论：..."

本适配器只依赖 pikapet.bus / pikapet.protocol，对 dsh 无任何代码依赖。

退出码：0 成功；2 参数错误；3 总线不可达（包装模式下仍继续跑 dsh，
只是没气泡）；4 dsh 不存在；否则透传 dsh 退出码；124 超时。
"""
import argparse
import subprocess
import sys
from pathlib import Path

from .. import bus
from ..protocol import Notification
from .common import collapse, stage_level, stage_title

DEFAULT_TIMEOUT = 600      # headless 默认超时（秒）；重任务用 --timeout 调大
SNIPPET_LEN = 160          # 完成气泡里回答摘要长度
ERR_TAIL_LEN = 200         # 失败气泡里 stderr 尾部长度


def _send(notif, port, fatal=False) -> bool:
    try:
        bus.send_notification(notif, port=port)
        return True
    except Exception as e:
        print(f"总线不可达：{e}", file=sys.stderr)
        if fatal:
            raise
        return False


def run_report(args) -> int:
    level = args.level or stage_level(args.stage)
    n = Notification(title=stage_title(args.stage, args.name),
                     body=args.detail or "", level=level,
                     source=args.source, ttl=args.ttl)
    if not _send(n, args.port):
        return 3
    return 0


def run_wrapped(args) -> int:
    """包装运行 dsh：start 气泡 → 跑 → done/error 气泡 + stdout 透传。"""
    if args.task_file:
        try:
            task = Path(args.task_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"任务文件读取失败：{e}", file=sys.stderr)
            return 2
    else:
        task = (args.task or "").strip()
    if not task:
        print("任务文本为空：dsh run 需要位置参数或 --task-file", file=sys.stderr)
        return 2

    cmd = [args.dsh_exe, "--profile", args.profile, task]
    _send(Notification(title=stage_title("start", args.name),
                       body=collapse(task, SNIPPET_LEN), level="info",
                       source=args.source, ttl=args.ttl), args.port)

    try:
        proc = subprocess.run(
            cmd, cwd=args.cwd, timeout=args.timeout,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    except FileNotFoundError:
        _send(Notification(title=stage_title("error", args.name),
                           body=f"找不到可执行文件：{args.dsh_exe}",
                           level="error", source=args.source, ttl=args.ttl),
              args.port)
        return 4
    except subprocess.TimeoutExpired:
        _send(Notification(title=stage_title("error", args.name),
                           body=f"超时（>{args.timeout}s）被终止",
                           level="error", source=args.source, ttl=args.ttl),
              args.port)
        return 124

    if proc.stdout:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()

    if proc.returncode == 0 and proc.stdout.strip():
        body = collapse(proc.stdout, SNIPPET_LEN) or "（空输出）"
        stage = "done"
    else:
        tail = collapse((proc.stderr or "").strip()[-ERR_TAIL_LEN:], ERR_TAIL_LEN)
        body = (f"退出码 {proc.returncode}\n{tail}").strip()
        stage = "error"
    _send(Notification(title=stage_title(stage, args.name),
                       body=body, level=stage_level(stage),
                       source=args.source, ttl=args.ttl),
          args.port)
    return proc.returncode


def add_subcommands(sub):
    """挂 run / report 两个模式。顶层 CLI 与独立入口共用这一处定义。"""
    p_run = sub.add_parser("run", help="包装运行 dsh，start/done/error 全程汇报")
    p_run.add_argument("name", help="任务名称（显示在气泡标题）")
    p_run.add_argument("task", nargs="?", default="",
                       help="任务文本（超长文本用 --task-file）")
    p_run.add_argument("--task-file", default=None,
                       help="从 UTF-8 文件读任务文本（绕开命令行长度上限）")
    p_run.add_argument("--dsh-exe", default="dsh", help="dsh 可执行文件（测试可换桩）")
    p_run.add_argument("--profile", default="headless")
    p_run.add_argument("--cwd", default=None, help="工作目录（= dsh 的 workspace 根）")
    p_run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p_run.add_argument("--source", default="dsh")
    p_run.add_argument("--ttl", type=float, default=20.0)
    p_run.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    p_run.set_defaults(func=run_wrapped)

    p_rp = sub.add_parser("report", help="手动汇报某个阶段（与 zcode 适配器同构）")
    p_rp.add_argument("name", help="任务名称")
    p_rp.add_argument("--stage", choices=("start", "done", "error", "run"),
                      default="done")
    p_rp.add_argument("--detail", default="")
    p_rp.add_argument("--level", choices=Notification.VALID_LEVELS, default=None)
    p_rp.add_argument("--source", default="dsh")
    p_rp.add_argument("--ttl", type=float, default=15.0)
    p_rp.add_argument("--port", type=int, default=bus.DEFAULT_PORT)
    p_rp.set_defaults(func=run_report)


def register(sub):
    """注册 `pikachu dsh run|report` 子命令。"""
    p = sub.add_parser("dsh", help="DSH 子任务包装器（run/report）")
    add_subcommands(p.add_subparsers(dest="mode", required=True))
    return p


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pikapet-adapter-dsh",
        description="DSH headless 子任务 → 皮卡丘通知包装器")
    add_subcommands(parser.add_subparsers(dest="mode", required=True))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
