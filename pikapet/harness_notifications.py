# -*- coding: utf-8 -*-
"""Codex / ZCode 生命周期通知的统一路由。

一条事件（某个 agent 的"这轮结束了"或"要你确认"）进来，按配置扇出到若干
渠道：桌宠气泡、飞书群机器人、（预留）邮件。配置落在运行时目录的
`harness_notifications.json`，可用 `pikachu harness configure` 改。

设计约束与其他钩子一致：**绝不阻塞、绝不拖累宿主 agent**。
event 入口无论遇到什么（stdin 不是 JSON、配置被写坏、某个渠道超时）都
返回 0，只把原因记进日志——但绝不静默：每种失败都有对应的 WARNING，
否则"钩子明明配了却没弹泡"就完全没有线索。

配置读取一律走 `_rule()` / `_channel()` 这两个带兜底的取值函数：老版本
配置文件缺 harness / event / channels 段是升级时的常态，直接 `[...]`
下标会抛 KeyError 把钩子打断。
"""
import copy
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import bus, paths
from .adapters.common import collapse
from .logs import get_logger, swallow
from .protocol import Notification

log = get_logger("harness")

HARNESSES = ("codex", "zcode")
EVENTS = ("stop", "approval")

SUMMARY_LEN = 160     # content=summary 时的正文长度
FULL_LEN = 1200       # content=full 时的正文长度
FEISHU_TIMEOUT = 8.0
# 飞书自定义机器人常配"关键词"安全设置；正文带上它才不会被丢掉
FEISHU_KEYWORD = "皮卡丘"
# webhook 只允许指向飞书官方域名。配置文件是本地文件，但它可能被别的工具
# 写、也可能从别处拷来；把"任意 URL"直接喂给 urlopen 等于给了一条打内网
# 和云元数据（169.254.169.254 那类）的通道，所以在这里收窄成白名单。
FEISHU_HOSTS = ("open.feishu.cn", "open.larksuite.com", "open.larkoffice.com")
FEISHU_PATH_PREFIX = "/open-apis/bot/"


class WebhookError(ValueError):
    """webhook 地址不符合要求（协议/域名/路径）。"""


DEFAULT = {
    "version": 1,
    "channels": {
        "pika": {"enabled": True},
        "feishu": {"enabled": False, "webhook": ""},
        "email": {"enabled": False},
    },
    "harnesses": {
        "codex": {
            "stop": {"channels": ["pika", "feishu"], "content": "summary"},
            "approval": {"channels": ["feishu"], "content": "full",
                         "escalate": {"after_minutes": 20,
                                      "channels": ["email"]}},
        },
        "zcode": {
            "stop": {"channels": ["pika", "feishu"], "content": "summary"},
            "approval": {"channels": ["feishu"], "content": "full",
                         "escalate": {"after_minutes": 20,
                                      "channels": ["email"]}},
        },
    },
}


def config_path() -> Path:
    return paths.harness_config_file(create_dir=True)


def load() -> dict:
    """读配置；不存在则写入默认值。

    内容坏了不抛：钩子链路上抛异常等于宿主 agent 那边看到报错。记一条
    WARNING 后退回默认配置——通知照常发出去，比"因为配置坏了就彻底不通知"
    好得多，而且日志里查得到。
    """
    path = config_path()
    if not path.exists():
        with swallow(log, "写入默认 harness 配置"):
            paths.write_text_atomic(
                path, json.dumps(DEFAULT, ensure_ascii=False, indent=2) + "\n")
        return copy.deepcopy(DEFAULT)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("harness 配置 %s 读取失败，本次用默认路由：%s", path, e)
        return copy.deepcopy(DEFAULT)
    if not isinstance(data, dict):
        log.warning("harness 配置 %s 不是 JSON 对象，本次用默认路由", path)
        return copy.deepcopy(DEFAULT)
    return data


def save(data: dict) -> None:
    paths.write_text_atomic(
        config_path(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _rule(config: dict, harness: str, event: str) -> dict:
    """取某个 harness+event 的路由规则，缺哪层都退到默认值。

    老配置文件没有新加的 harness / event 是升级时的常态，不能因此炸掉。
    """
    fallback = DEFAULT["harnesses"].get(harness, {}).get(event, {})
    harnesses = config.get("harnesses")
    if not isinstance(harnesses, dict):
        return copy.deepcopy(fallback)
    events = harnesses.get(harness)
    if not isinstance(events, dict):
        log.debug("配置里没有 harness=%s，用默认规则", harness)
        return copy.deepcopy(fallback)
    rule = events.get(event)
    if not isinstance(rule, dict):
        log.debug("配置里没有 %s.%s，用默认规则", harness, event)
        return copy.deepcopy(fallback)
    return rule


def _channel(config: dict, name: str) -> dict:
    """取某个渠道的设置，缺失时退到默认值。"""
    channels = config.get("channels")
    if not isinstance(channels, dict):
        return copy.deepcopy(DEFAULT["channels"].get(name, {}))
    conf = channels.get(name)
    if not isinstance(conf, dict):
        return copy.deepcopy(DEFAULT["channels"].get(name, {}))
    return conf


def validate_feishu_webhook(url: str) -> str:
    """校验 webhook 只指向飞书官方机器人端点，否则抛 WebhookError。

    收窄到白名单而不是"随便什么 URL 都发"：这个地址来自本地配置文件，
    但配置可能被别的工具改、也可能整份拷贝自别处。不校验的话，一个
    `http://169.254.169.254/...` 就能把 agent 的回答内容送去内网。
    """
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme != "https":
        raise WebhookError(f"webhook 必须是 https，收到 {parsed.scheme or '空'}")
    if parsed.hostname not in FEISHU_HOSTS:
        raise WebhookError(
            f"webhook 域名 {parsed.hostname!r} 不在白名单："
            f"{'、'.join(FEISHU_HOSTS)}")
    if parsed.port not in (None, 443):
        raise WebhookError(f"webhook 不允许自定义端口：{parsed.port}")
    if not parsed.path.startswith(FEISHU_PATH_PREFIX):
        raise WebhookError(
            f"webhook 路径必须以 {FEISHU_PATH_PREFIX} 开头，收到 {parsed.path!r}")
    return parsed.geturl()


def _text(payload: dict, harness: str, event: str, mode: str):
    """把事件负载整理成 (标题, 正文)。"""
    label = harness.upper()
    if event == "approval":
        title = f"{label} 需要确认"
        tool = (payload.get("tool_name") or payload.get("toolName")
                or "工具调用")
        tool_input = (payload.get("tool_input")
                      or payload.get("toolInput") or {})
        reason = payload.get("reason") or ""
        if not reason and isinstance(tool_input, dict):
            reason = tool_input.get("description") or ""
        body = f"工具：{tool}"
        if reason:
            body += f"\n原因：{reason}"
    else:
        title = f"{label} 完成"
        body = (payload.get("last_assistant_message")
                or payload.get("last_response")
                or payload.get("response") or "回复完成")
    limit = FULL_LEN if mode == "full" else SUMMARY_LEN
    return title, collapse(str(body), limit)


def _send_feishu(webhook: str, title: str, body: str) -> None:
    """发飞书自定义机器人。失败抛异常，由调用方 swallow 记账。"""
    url = validate_feishu_webhook(webhook)     # 先校验再请求
    data = json.dumps(
        {"msg_type": "text",
         "content": {"text": f"{FEISHU_KEYWORD}: {title}\n{body}"}},
        ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=FEISHU_TIMEOUT) as response:
        result = json.loads(response.read().decode("utf-8"))
    # 飞书 HTTP 200 也可能是业务失败（关键词不匹配、机器人被停用等）
    if result.get("code") not in (0, None):
        raise RuntimeError(
            f"飞书拒收：{result.get('msg')}（code={result.get('code')}）")


def dispatch(harness: str, event: str, raw: dict) -> list:
    """把一条事件扇出到配置的渠道，返回成功送达的渠道名。

    单个渠道失败不影响其他渠道：飞书挂了不该连气泡也没有。
    """
    config = load()
    payload = dict(raw)
    rule = _rule(config, harness, event)
    mode = rule.get("content", "summary")
    title, body = _text(payload, harness, event, mode)
    channels = rule.get("channels")
    if not isinstance(channels, list):
        channels = DEFAULT["harnesses"][harness][event]["channels"]
    level = "warn" if event == "approval" else "success"

    delivered = []
    if "pika" in channels and _channel(config, "pika").get("enabled", True):
        with swallow(log, "投递气泡通知"):
            bus.send_notification(Notification(
                title=title, body=body, source=harness, level=level))
            delivered.append("pika")
    if "feishu" in channels:
        feishu = _channel(config, "feishu")
        webhook = feishu.get("webhook") or ""
        if not feishu.get("enabled"):
            log.debug("飞书渠道未启用，跳过")
        elif not webhook:
            log.warning("飞书渠道已启用但没配 webhook，本次没发")
        else:
            with swallow(log, "投递飞书通知"):
                _send_feishu(webhook, title, body)
                delivered.append("feishu")
    if "email" in channels and _channel(config, "email").get("enabled"):
        # 邮件渠道尚未实现：明确说出来，不假装发了
        log.warning("邮件渠道尚未实现，%s.%s 的邮件通知被跳过", harness, event)
    if not delivered:
        log.warning("%s.%s 没有任何渠道送达（配置 channels=%s）",
                    harness, event, channels)
    return delivered


def event_main(args) -> int:
    """钩子入口：读 stdin 的事件 JSON 并扇出。**永远返回 0。**

    宿主 agent（Codex / ZCode）拿这个返回码决定要不要报错，所以哪怕
    stdin 是空的、不是 JSON、配置坏了、渠道全挂，也只能记日志。
    """
    payload = {}
    try:
        raw = sys.stdin.read().strip()
    except (OSError, ValueError) as e:
        log.warning("读取 stdin 失败，按空负载处理：%s", e)
        raw = ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
            else:
                log.warning("事件负载不是 JSON 对象（%s），按空负载处理",
                            type(parsed).__name__)
        except ValueError as e:
            log.warning("事件负载不是合法 JSON，按空负载处理：%s", e)
    with swallow(log, f"{args.harness}.{args.event} 事件扇出"):
        dispatch(args.harness, args.event, payload)
    return 0


def configure_main(args) -> int:
    config = load()
    if args.show:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    # 用带兜底的取值拿到规则，再写回配置：老配置缺段时也能改
    harnesses = config.setdefault("harnesses", {})
    events = harnesses.setdefault(args.harness, {})
    rule = dict(_rule(config, args.harness, args.event))
    if args.channels:
        names = [c.strip() for c in args.channels.split(",") if c.strip()]
        unknown = [c for c in names if c not in DEFAULT["channels"]]
        if unknown:
            print(f"未知渠道：{'、'.join(unknown)}；"
                  f"可选 {'、'.join(DEFAULT['channels'])}", file=sys.stderr)
            return 2
        rule["channels"] = names
    if args.content:
        rule["content"] = args.content
    events[args.event] = rule
    if args.feishu_webhook:
        # 配置阶段就把不合规的 webhook 拦下来，别等到事件来了才在日志里报
        try:
            url = validate_feishu_webhook(args.feishu_webhook)
        except WebhookError as e:
            print(f"webhook 不合法：{e}", file=sys.stderr)
            return 2
        config.setdefault("channels", {})["feishu"] = {
            "enabled": True, "webhook": url}
    save(config)
    print(f"已更新 {args.harness}.{args.event}：channels="
          f"{'、'.join(rule.get('channels', []))} content={rule.get('content')}")
    return 0


def add_modes(sub):
    """把 event / configure 两个子命令挂到给定 subparsers 上。

    顶层 `pikachu harness ...` 与模块入口
    `python -m pikapet.harness_notifications ...` 共用这一处定义，
    两边的参数不会各写一遍而走偏。
    """
    e = sub.add_parser("event", help="处理一条生命周期事件（钩子调用）")
    e.add_argument("--harness", choices=HARNESSES, required=True)
    e.add_argument("--event", choices=EVENTS, required=True)
    e.set_defaults(func=event_main)

    c = sub.add_parser("configure", help="改路由规则或查看当前配置")
    c.add_argument("harness", choices=HARNESSES)
    c.add_argument("event", choices=EVENTS)
    c.add_argument("--channels", help="逗号分隔，如 pika,feishu")
    c.add_argument("--content", choices=("summary", "full"))
    c.add_argument("--feishu-webhook", help="设置飞书 webhook 并启用该渠道")
    c.add_argument("--show", action="store_true", help="打印当前完整配置")
    c.set_defaults(func=configure_main)
    return sub


def register(sub):
    """注册 `pikachu harness event|configure` 子命令。"""
    p = sub.add_parser("harness", help="管理 Codex/ZCode 的统一通知路由")
    add_modes(p.add_subparsers(dest="harness_mode", required=True))
    return p


def main(argv=None) -> int:
    """`python -m pikapet.harness_notifications event ...` 的入口。

    钩子配置里写的就是这个模块（不是顶层 pikachu 命令），所以必须自带
    入口——少了它模块被执行时只把函数定义一遍就退出，返回 0 却什么都没做，
    表现为"钩子跑了、日志干净、但气泡不来"。这里直接挂 event/configure，
    不再套一层 harness 前缀（钩子命令行里没有那个词）。
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="pikapet.harness_notifications",
        description="Codex / ZCode 生命周期通知路由")
    add_modes(parser.add_subparsers(dest="mode", required=True))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
