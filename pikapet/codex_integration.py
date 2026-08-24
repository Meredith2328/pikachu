# -*- coding: utf-8 -*-
"""把 Codex 的 Stop 钩子接到皮卡丘通知路由（幂等安装）。

Codex 老的 ``notify`` 回调会为内部小轮次触发（agent 自己套娃时也算一轮），
而官方的 ``Stop`` 钩子才是"用户的一条消息真正处理完"的边界。本模块只管
Codex 那侧需要的那点配置：hooks.json 里的 Stop 处理器，加上 config.toml
的 features.hooks 开关——后者在 0.147 里还是实验特性，不打开钩子根本不会
被调用。
"""
import json
import os
import re
from pathlib import Path


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _hook_command(python: str) -> str:
    return (f'"{python}" -m pikapet.harness_notifications event '
            "--harness codex --event stop")


def _is_pikachu_stop_hook(handler: dict) -> bool:
    command = str(handler.get("commandWindows") or handler.get("command") or "")
    return ("pikapet.adapters.codex event --quiet" in command
            or "pikapet.harness_notifications event" in command
            or "Notify-PikachuOnStop.ps1" in command)


def _desired_handler(python: str) -> dict:
    command = _hook_command(python)
    return {
        "type": "command",
        "command": command,
        "commandWindows": command,
        "async": True,
        "timeout": 10,
    }


def update_hooks(codex_home: Path, python: str) -> bool:
    """Merge exactly one Pikachu Stop handler into Codex hooks.json."""
    path = codex_home / "hooks.json"
    data = {"hooks": {}}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"{path} is not a valid Codex hooks.json object")

    hooks = data.setdefault("hooks", {})
    stop_groups = hooks.setdefault("Stop", [])
    retained = []
    for group in stop_groups:
        if not isinstance(group, dict):
            retained.append(group)
            continue
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            retained.append(group)
            continue
        kept = [handler for handler in handlers
                if not isinstance(handler, dict) or not _is_pikachu_stop_hook(handler)]
        if kept:
            clone = dict(group)
            clone["hooks"] = kept
            retained.append(clone)

    retained.append({"hooks": [_desired_handler(python)]})
    changed = retained != stop_groups
    if changed:
        hooks["Stop"] = retained

    if changed:
        codex_home.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return changed


def remove_legacy_pikachu_notify(codex_home: Path) -> bool:
    """Detach the old sub-turn ``--previous-notify`` Pikachu callback.

    The Codex ``notify`` command itself is left intact for computer-use.  The
    regular expression targets only the extra previous-notify program entry.
    """
    path = codex_home / "config.toml"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    pattern = (r',\s*"--previous-notify"\s*,\s*'
               r"'(?:[^']*codex_notify_dispatch\.py[^']*)'")
    updated, count = re.subn(pattern, "", text)
    if count:
        path.write_text(updated, encoding="utf-8")
    return bool(count)


HOOKS_FEATURE_RE = re.compile(r"^\s*hooks\s*=\s*(true|false)\s*$",
                              re.MULTILINE | re.IGNORECASE)
FEATURES_HEADER_RE = re.compile(r"^\s*\[features\]\s*$", re.MULTILINE)


def hooks_feature_enabled(text: str) -> bool:
    """config.toml 里 [features] 段的 hooks 是否为 true。"""
    match = FEATURES_HEADER_RE.search(text)
    if not match:
        return False
    # 只看 [features] 段到下一个段头之间
    rest = text[match.end():]
    next_section = re.search(r"^\s*\[", rest, re.MULTILINE)
    block = rest[:next_section.start()] if next_section else rest
    found = HOOKS_FEATURE_RE.search(block)
    return bool(found and found.group(1).lower() == "true")


def enable_hooks_feature(codex_home: Path) -> bool:
    """在 config.toml 的 [features] 段打开 hooks，返回是否改动过。

    Codex 0.147 里 hooks 仍是实验特性：这个开关不打开，hooks.json 配得再对
    也不会被调用（实测钩子从来没被执行过，日志里一条痕迹都没有）。
    """
    path = codex_home / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if hooks_feature_enabled(text):
        return False
    match = FEATURES_HEADER_RE.search(text)
    if match:
        # 已有 [features] 段：把 hooks = true 插在段头后面
        insert_at = match.end()
        updated = text[:insert_at] + "\nhooks = true" + text[insert_at:]
    else:
        prefix = text if text.endswith("\n") or not text else text + "\n"
        updated = prefix + "\n[features]\nhooks = true\n"
    codex_home.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return True


def inspect(codex_home: Path) -> dict:
    hooks_path = codex_home / "hooks.json"
    has_stop = False
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            for group in data.get("hooks", {}).get("Stop", []):
                for handler in group.get("hooks", []):
                    if isinstance(handler, dict) and _is_pikachu_stop_hook(handler):
                        has_stop = True
        except (json.JSONDecodeError, AttributeError):
            pass
    config_path = codex_home / "config.toml"
    config_text = (config_path.read_text(encoding="utf-8")
                   if config_path.exists() else "")
    return {"codex_home": str(codex_home), "stop_hook": has_stop,
            # hooks 是实验特性：开关没开，钩子配得再对也不会被调用
            "hooks_feature_enabled": hooks_feature_enabled(config_text),
            "legacy_subturn_notify": "--previous-notify" in config_text}


def run_setup(args) -> int:
    home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    if args.check:
        status = inspect(home)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if (status["stop_hook"] and status["hooks_feature_enabled"]
                     and not status["legacy_subturn_notify"]) else 1

    from .zcode_integration import windowless_python
    python = windowless_python()
    legacy_removed = remove_legacy_pikachu_notify(home)
    hook_changed = update_hooks(home, python)
    feature_changed = enable_hooks_feature(home)
    print("Codex → 皮卡丘通知已配置：Stop（每条消息完整结束时一次）。")
    if legacy_removed:
        print("已移除旧的内部小轮次通知转发。")
    if feature_changed:
        print("已在 config.toml 打开 features.hooks —— Codex 0.147 里 hooks "
              "还是实验特性，不打开这个开关钩子根本不会被调用。")
    if hook_changed or feature_changed:
        print("请在 Codex 输入 /hooks 并信任更新后的本地钩子；之后无需手工改配置。")
    return 0


def register(parent_subparsers) -> None:
    parser = parent_subparsers.add_parser(
        "setup", help="安装或检查 Codex 的整轮完成皮卡丘通知")
    parser.add_argument("--check", action="store_true", help="只检查，不写入配置")
    parser.add_argument("--codex-home", default=None,
                        help="Codex 配置目录（默认 ~/.codex）")
    parser.set_defaults(func=run_setup)
