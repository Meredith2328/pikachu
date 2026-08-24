# -*- coding: utf-8 -*-
"""把 ZCode 的 Stop 钩子接到皮卡丘通知路由（幂等安装）。"""
import json
import os
import sys
from pathlib import Path


def default_config_path() -> Path:
    configured = os.environ.get("ZCODE_CONFIG_PATH")
    if configured:
        return Path(configured)
    return Path.home() / ".zcode" / "cli" / "config.json"


def windowless_python(executable: str = None) -> str:
    """同目录下的 pythonw.exe，拿不到就退回给定解释器。

    钩子每轮都会被调起。用带控制台的 python.exe 会让 Windows 每次新开一个
    控制台窗口——而 Windows Terminal 的 closeOnExit 默认是 graceful，进程
    非零退出就把标签页留在屏幕上，几轮下来就攒一屏空白终端（我们在 Codex
    的 notify 分发器上正是踩了这个坑）。pythonw.exe 没有控制台，不会弹窗。
    """
    exe = Path(executable or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _hook_script() -> Path:
    return Path(__file__).resolve().parent.parent / "tools" / "zcode_hook.py"


def _is_pikachu_stop_handler(handler: dict) -> bool:
    args = handler.get("args", [])
    return (isinstance(args, list) and
            (any(str(value).lower().endswith("zcode_hook.py") for value in args)
             or "pikapet.harness_notifications" in args))


def _desired_handler(python: str, script: Path) -> dict:
    return {"type": "process", "command": windowless_python(python),
            "args": ["-m", "pikapet.harness_notifications", "event",
                     "--harness", "zcode", "--event", "stop"],
            "timeoutMs": 5000, "statusMessage": "通知皮卡丘"}


def update_config(config_path: Path, python: str, script: Path) -> bool:
    """Merge exactly one Pikachu Stop hook, preserving every other setting."""
    data = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{config_path} has a non-object hooks setting")
    hooks["enabled"] = True
    events = hooks.setdefault("events", {})
    if not isinstance(events, dict):
        raise ValueError(f"{config_path} has a non-object hooks.events setting")
    previous = events.get("Stop", [])
    if not isinstance(previous, list):
        raise ValueError(f"{config_path} has a non-array Stop hook list")
    retained = []
    for group in previous:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            retained.append(group)
            continue
        handlers = [handler for handler in group["hooks"]
                    if not isinstance(handler, dict) or not _is_pikachu_stop_handler(handler)]
        if handlers:
            clone = dict(group)
            clone["hooks"] = handlers
            retained.append(clone)
    retained.append({"hooks": [_desired_handler(python, script)]})
    changed = retained != previous
    if changed:
        events["Stop"] = retained
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    return changed


def inspect(config_path: Path) -> dict:
    has_stop = False
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            groups = data.get("hooks", {}).get("events", {}).get("Stop", [])
            has_stop = any(isinstance(handler, dict) and _is_pikachu_stop_handler(handler)
                           for group in groups if isinstance(group, dict)
                           for handler in group.get("hooks", [])
                           if isinstance(group.get("hooks"), list))
        except (json.JSONDecodeError, AttributeError):
            pass
    return {"zcode_config": str(config_path), "stop_hook": has_stop}


def run_setup(args) -> int:
    path = (Path(args.zcode_config).expanduser() if args.zcode_config
            else default_config_path())
    if args.check:
        status = inspect(path)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["stop_hook"] else 1
    changed = update_config(path, sys.executable, _hook_script())
    print("ZCode → 皮卡丘通知已配置：Stop（每条消息完整结束时一次）。")
    if changed:
        print("已保留其他 ZCode hooks、插件和 MCP 设置；重启或新开会话后生效。")
    return 0
