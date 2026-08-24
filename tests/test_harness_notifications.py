# -*- coding: utf-8 -*-
"""harness 通知路由：扇出、配置兜底、渠道失败隔离、钩子永不失败。

这一层跑在 Codex / ZCode 的钩子链路上，所以两条铁律要钉住：
1. event 入口永远返回 0（宿主 agent 拿它判断要不要报错）；
2. 但绝不静默——每种失败都要留下 WARNING，否则"配了钩子却没弹泡"没法查。
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pikapet import harness_notifications as hn
from tests.helpers import isolated_home


class _Args:
    def __init__(self, **kw):
        self.harness = "codex"
        self.event = "stop"
        self.show = False
        self.channels = None
        self.content = None
        self.feishu_webhook = None
        for k, v in kw.items():
            setattr(self, k, v)


class HarnessTestCase(unittest.TestCase):
    def setUp(self):
        self._home = isolated_home()
        self.home = self._home.__enter__()

    def tearDown(self):
        self._home.__exit__(None, None, None)

    def write_config(self, data):
        hn.config_path().write_text(json.dumps(data, ensure_ascii=False),
                                    encoding="utf-8")


class TestConfigFallbacks(HarnessTestCase):
    """老配置缺段是升级时的常态，不能因此把钩子打断。"""

    def test_creates_default_when_missing(self):
        cfg = hn.load()
        self.assertEqual(cfg["version"], 1)
        self.assertTrue(hn.config_path().exists())

    def test_broken_json_falls_back_and_warns(self):
        hn.config_path().write_text("{这不是 json", encoding="utf-8")
        with self.assertLogs("pikachu.harness", level="WARNING") as cm:
            cfg = hn.load()
        self.assertEqual(cfg["harnesses"], hn.DEFAULT["harnesses"])
        self.assertTrue(any("读取失败" in m for m in cm.output))

    def test_non_object_config_falls_back(self):
        hn.config_path().write_text("[1,2,3]", encoding="utf-8")
        with self.assertLogs("pikachu.harness", level="WARNING"):
            cfg = hn.load()
        self.assertEqual(cfg["version"], 1)

    def test_missing_harness_uses_default_rule(self):
        """配置里只有 codex.stop，取 zcode.stop 不能 KeyError。"""
        self.write_config({"version": 1, "channels": {},
                           "harnesses": {"codex": {"stop": {"channels": []}}}})
        rule = hn._rule(hn.load(), "zcode", "stop")
        self.assertEqual(rule["channels"],
                         hn.DEFAULT["harnesses"]["zcode"]["stop"]["channels"])

    def test_missing_event_uses_default_rule(self):
        self.write_config({"version": 1, "channels": {},
                           "harnesses": {"codex": {"stop": {"channels": []}}}})
        rule = hn._rule(hn.load(), "codex", "approval")
        self.assertEqual(rule["content"], "full")

    def test_missing_channel_section_uses_default(self):
        self.write_config({"version": 1, "channels": {}, "harnesses": {}})
        self.assertTrue(hn._channel(hn.load(), "pika").get("enabled"))


class TestFeishuWebhookValidation(HarnessTestCase):
    """webhook 来自本地配置文件，但配置可能被别的工具改或整份拷来。
    不收窄的话，一个内网/元数据地址就能把 agent 的回答内容送出去。"""

    GOOD = "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"

    def test_accepts_official_endpoint(self):
        self.assertEqual(hn.validate_feishu_webhook(self.GOOD), self.GOOD)

    def test_accepts_larksuite(self):
        url = "https://open.larksuite.com/open-apis/bot/v2/hook/x"
        self.assertEqual(hn.validate_feishu_webhook(url), url)

    def test_rejects_http(self):
        with self.assertRaises(hn.WebhookError):
            hn.validate_feishu_webhook(
                "http://open.feishu.cn/open-apis/bot/v2/hook/x")

    def test_rejects_other_host(self):
        for bad in ("https://169.254.169.254/open-apis/bot/v2/hook/x",
                    "https://127.0.0.1/open-apis/bot/v2/hook/x",
                    "https://evil.example.com/open-apis/bot/v2/hook/x"):
            with self.subTest(url=bad):
                with self.assertRaises(hn.WebhookError):
                    hn.validate_feishu_webhook(bad)

    def test_rejects_custom_port(self):
        with self.assertRaises(hn.WebhookError):
            hn.validate_feishu_webhook(
                "https://open.feishu.cn:8080/open-apis/bot/v2/hook/x")

    def test_rejects_wrong_path(self):
        with self.assertRaises(hn.WebhookError):
            hn.validate_feishu_webhook("https://open.feishu.cn/admin/secrets")

    def test_rejects_empty(self):
        with self.assertRaises(hn.WebhookError):
            hn.validate_feishu_webhook("")


class TestText(HarnessTestCase):
    def test_stop_uses_last_assistant_message(self):
        title, body = hn._text({"last_assistant_message": "改完了"},
                               "codex", "stop", "summary")
        self.assertIn("CODEX", title)
        self.assertEqual(body, "改完了")

    def test_stop_without_message_has_placeholder(self):
        _, body = hn._text({}, "zcode", "stop", "summary")
        self.assertEqual(body, "回复完成")

    def test_summary_is_shorter_than_full(self):
        payload = {"last_assistant_message": "很长的回答" * 200}
        short = hn._text(payload, "codex", "stop", "summary")[1]
        long = hn._text(payload, "codex", "stop", "full")[1]
        self.assertLess(len(short), len(long))
        self.assertLessEqual(len(short), hn.SUMMARY_LEN + 1)

    def test_approval_mentions_tool_and_reason(self):
        title, body = hn._text(
            {"tool_name": "shell", "reason": "要删文件"},
            "codex", "approval", "full")
        self.assertIn("需要确认", title)
        self.assertIn("shell", body)
        self.assertIn("要删文件", body)

    def test_approval_reason_from_tool_input(self):
        _, body = hn._text(
            {"toolName": "apply_patch",
             "toolInput": {"description": "改三个文件"}},
            "zcode", "approval", "full")
        self.assertIn("apply_patch", body)
        self.assertIn("改三个文件", body)


class TestDispatch(HarnessTestCase):
    def test_pika_channel_sends_notification(self):
        self.write_config({"version": 1,
                           "channels": {"pika": {"enabled": True}},
                           "harnesses": {"codex": {"stop": {
                               "channels": ["pika"]}}}})
        with mock.patch.object(hn.bus, "send_notification") as send:
            got = hn.dispatch("codex", "stop", {"last_assistant_message": "x"})
        self.assertEqual(got, ["pika"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args[0][0].source, "codex")

    def test_pika_disabled_is_skipped(self):
        self.write_config({"version": 1,
                           "channels": {"pika": {"enabled": False}},
                           "harnesses": {"codex": {"stop": {
                               "channels": ["pika"]}}}})
        with mock.patch.object(hn.bus, "send_notification") as send:
            with self.assertLogs("pikachu.harness", level="WARNING"):
                got = hn.dispatch("codex", "stop", {})
        self.assertEqual(got, [])
        send.assert_not_called()

    def test_approval_uses_warn_level(self):
        self.write_config({"version": 1,
                           "channels": {"pika": {"enabled": True}},
                           "harnesses": {"codex": {"approval": {
                               "channels": ["pika"]}}}})
        with mock.patch.object(hn.bus, "send_notification") as send:
            hn.dispatch("codex", "approval", {"tool_name": "shell"})
        self.assertEqual(send.call_args[0][0].level, "warn")

    def test_feishu_failure_does_not_block_pika(self):
        """飞书挂了不该连气泡也没有。"""
        self.write_config({"version": 1,
                           "channels": {"pika": {"enabled": True},
                                        "feishu": {"enabled": True,
                                                   "webhook": TestFeishuWebhookValidation.GOOD}},
                           "harnesses": {"codex": {"stop": {
                               "channels": ["pika", "feishu"]}}}})
        with mock.patch.object(hn.bus, "send_notification") as send, \
                mock.patch.object(hn, "_send_feishu",
                                  side_effect=RuntimeError("飞书超时")):
            with self.assertLogs("pikachu.harness", level="WARNING") as cm:
                got = hn.dispatch("codex", "stop", {})
        self.assertEqual(got, ["pika"])          # 气泡照样送到
        self.assertEqual(send.call_count, 1)
        self.assertTrue(any("飞书" in m for m in cm.output))

    def test_pika_failure_does_not_block_feishu(self):
        self.write_config({"version": 1,
                           "channels": {"pika": {"enabled": True},
                                        "feishu": {"enabled": True,
                                                   "webhook": TestFeishuWebhookValidation.GOOD}},
                           "harnesses": {"codex": {"stop": {
                               "channels": ["pika", "feishu"]}}}})
        with mock.patch.object(hn.bus, "send_notification",
                               side_effect=OSError("总线没起")), \
                mock.patch.object(hn, "_send_feishu") as feishu:
            with self.assertLogs("pikachu.harness", level="WARNING"):
                got = hn.dispatch("codex", "stop", {})
        self.assertEqual(got, ["feishu"])
        self.assertEqual(feishu.call_count, 1)

    def test_feishu_enabled_without_webhook_warns(self):
        self.write_config({"version": 1,
                           "channels": {"feishu": {"enabled": True,
                                                   "webhook": ""}},
                           "harnesses": {"codex": {"stop": {
                               "channels": ["feishu"]}}}})
        with self.assertLogs("pikachu.harness", level="WARNING") as cm:
            got = hn.dispatch("codex", "stop", {})
        self.assertEqual(got, [])
        self.assertTrue(any("webhook" in m for m in cm.output))

    def test_email_channel_says_not_implemented(self):
        """邮件还没实现——要说出来，不能假装发了。"""
        self.write_config({"version": 1,
                           "channels": {"email": {"enabled": True}},
                           "harnesses": {"codex": {"stop": {
                               "channels": ["email"]}}}})
        with self.assertLogs("pikachu.harness", level="WARNING") as cm:
            hn.dispatch("codex", "stop", {})
        self.assertTrue(any("尚未实现" in m for m in cm.output))


class TestEventMainNeverFails(HarnessTestCase):
    """钩子入口永远返回 0：宿主 agent 拿它判断要不要报错。"""

    def _run(self, stdin_text):
        orig = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with mock.patch.object(hn.bus, "send_notification"):
                return hn.event_main(_Args())
        finally:
            sys.stdin = orig

    def test_valid_payload(self):
        self.assertEqual(
            self._run(json.dumps({"last_assistant_message": "ok"})), 0)

    def test_empty_stdin(self):
        self.assertEqual(self._run(""), 0)

    def test_malformed_json_returns_zero_and_warns(self):
        with self.assertLogs("pikachu.harness", level="WARNING") as cm:
            rc = self._run("这不是 JSON")
        self.assertEqual(rc, 0)
        self.assertTrue(any("不是合法 JSON" in m for m in cm.output))

    def test_json_array_payload_returns_zero(self):
        with self.assertLogs("pikachu.harness", level="WARNING"):
            self.assertEqual(self._run("[1,2,3]"), 0)

    def test_dispatch_crash_still_returns_zero(self):
        orig = sys.stdin
        sys.stdin = io.StringIO("{}")
        try:
            with mock.patch.object(hn, "dispatch",
                                   side_effect=RuntimeError("炸了")):
                with self.assertLogs("pikachu.harness", level="WARNING"):
                    rc = hn.event_main(_Args())
        finally:
            sys.stdin = orig
        self.assertEqual(rc, 0)


class TestConfigureMain(HarnessTestCase):
    def test_show_prints_config(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hn.configure_main(_Args(show=True))
        self.assertEqual(rc, 0)
        self.assertIn("harnesses", out.getvalue())

    def test_updates_channels_and_content(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            rc = hn.configure_main(_Args(channels="pika", content="full"))
        self.assertEqual(rc, 0)
        rule = hn._rule(hn.load(), "codex", "stop")
        self.assertEqual(rule["channels"], ["pika"])
        self.assertEqual(rule["content"], "full")

    def test_rejects_unknown_channel(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = hn.configure_main(_Args(channels="pika,短信"))
        self.assertEqual(rc, 2)
        self.assertIn("未知渠道", err.getvalue())

    def test_rejects_bad_webhook_at_configure_time(self):
        """配置阶段就拦下不合规 webhook，别等事件来了才在日志里报。"""
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = hn.configure_main(
                _Args(feishu_webhook="http://127.0.0.1/open-apis/bot/x"))
        self.assertEqual(rc, 2)
        self.assertIn("webhook", err.getvalue())

    def test_accepts_good_webhook_and_enables_channel(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            rc = hn.configure_main(
                _Args(feishu_webhook=TestFeishuWebhookValidation.GOOD))
        self.assertEqual(rc, 0)
        feishu = hn._channel(hn.load(), "feishu")
        self.assertTrue(feishu["enabled"])
        self.assertEqual(feishu["webhook"],
                         TestFeishuWebhookValidation.GOOD)

    def test_configure_works_on_legacy_config_missing_sections(self):
        self.write_config({"version": 1})
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            rc = hn.configure_main(_Args(harness="zcode", event="approval",
                                         channels="pika"))
        self.assertEqual(rc, 0)
        self.assertEqual(hn._rule(hn.load(), "zcode", "approval")["channels"],
                         ["pika"])


if __name__ == "__main__":
    unittest.main()
