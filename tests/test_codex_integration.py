# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

from pikapet import codex_integration as integration


class TestCodexIntegration(unittest.TestCase):
    def test_update_hooks_preserves_custom_hook_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            hooks = {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command",
                                            "command": "custom-stop"}]}],
                    "PermissionRequest": [{"hooks": [{"type": "command",
                                                         "command": "notify"}]}],
                }
            }
            (home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            self.assertTrue(integration.update_hooks(home, sys.executable))
            saved = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
            commands = [handler["command"] for group in saved["hooks"]["Stop"]
                        for handler in group["hooks"]]
            self.assertIn("custom-stop", commands)
            self.assertEqual(sum("pikapet.harness_notifications event" in c
                                 for c in commands), 1)
            self.assertFalse(integration.update_hooks(home, sys.executable))

    def test_removes_only_legacy_pikachu_previous_notify(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config = ("notify = ['computer.exe', 'turn-ended', "
                      "\"--previous-notify\", "
                      "'[\"pythonw.exe\",\"D:\\\\_Project\\\\pikachu\\\\tools\\\\codex_notify_dispatch.py\"]']\n"
                      "model = 'x'\n")
            (home / "config.toml").write_text(config, encoding="utf-8")
            self.assertTrue(integration.remove_legacy_pikachu_notify(home))
            updated = (home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("previous-notify", updated)
            self.assertIn("computer.exe", updated)


class TestHooksFeatureFlag(unittest.TestCase):
    """hooks 在 Codex 0.147 是实验特性：开关不开，钩子配得再对也不会被调用
    （实测钩子从未执行，日志里一条痕迹都没有）。"""

    def test_detects_disabled_when_no_features_section(self):
        self.assertFalse(integration.hooks_feature_enabled("model = 'x'\n"))

    def test_detects_disabled_when_features_lacks_hooks(self):
        text = "[features]\njs_repl = false\n"
        self.assertFalse(integration.hooks_feature_enabled(text))

    def test_detects_enabled(self):
        text = "[features]\nhooks = true\njs_repl = false\n"
        self.assertTrue(integration.hooks_feature_enabled(text))

    def test_explicit_false_is_disabled(self):
        self.assertFalse(
            integration.hooks_feature_enabled("[features]\nhooks = false\n"))

    def test_hooks_in_another_section_does_not_count(self):
        """别的段里有 hooks = true 不算——只认 [features] 段内的。"""
        text = "[somethingelse]\nhooks = true\n\n[features]\njs_repl = false\n"
        self.assertFalse(integration.hooks_feature_enabled(text))

    def test_enable_adds_to_existing_features_section(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text(
                "model = 'x'\n\n[features]\njs_repl = false\n",
                encoding="utf-8")
            self.assertTrue(integration.enable_hooks_feature(home))
            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertTrue(integration.hooks_feature_enabled(text))
            # 不能把原有设置弄丢
            self.assertIn("js_repl = false", text)
            self.assertIn("model = 'x'", text)

    def test_enable_creates_features_section_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
            self.assertTrue(integration.enable_hooks_feature(home))
            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertTrue(integration.hooks_feature_enabled(text))
            self.assertIn("model = 'x'", text)

    def test_enable_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text("[features]\nhooks = true\n",
                                              encoding="utf-8")
            self.assertFalse(integration.enable_hooks_feature(home))

    def test_enable_works_without_existing_config(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "codex"
            self.assertTrue(integration.enable_hooks_feature(home))
            self.assertTrue(integration.hooks_feature_enabled(
                (home / "config.toml").read_text(encoding="utf-8")))

    def test_inspect_reports_feature_state(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
            self.assertFalse(integration.inspect(home)["hooks_feature_enabled"])
            integration.enable_hooks_feature(home)
            self.assertTrue(integration.inspect(home)["hooks_feature_enabled"])


class TestWindowlessPython(unittest.TestCase):
    def test_prefers_pythonw(self):
        from pikapet.zcode_integration import windowless_python
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "python.exe"
            exe.write_text("", encoding="utf-8")
            (Path(td) / "pythonw.exe").write_text("", encoding="utf-8")
            self.assertTrue(
                windowless_python(str(exe)).lower().endswith("pythonw.exe"))

    def test_falls_back_when_pythonw_absent(self):
        from pikapet.zcode_integration import windowless_python
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "python.exe"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(windowless_python(str(exe)), str(exe))
