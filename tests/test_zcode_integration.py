# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

from pikapet import zcode_integration as integration


class TestZcodeIntegration(unittest.TestCase):
    def test_merges_stop_hook_and_preserves_other_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            original = {"plugins": {"enabledPlugins": {"demo": True}},
                        "hooks": {"enabled": False, "events": {
                            "Stop": [{"hooks": [{"type": "process", "command": "py",
                                                   "args": ["old-zcode_hook.py"]}]}],
                            "SessionStart": [{"hooks": [{"type": "process",
                                                           "command": "memory"}]}]}}}
            path.write_text(json.dumps(original), encoding="utf-8")
            script = Path(td) / "tools" / "zcode_hook.py"
            self.assertTrue(integration.update_config(path, sys.executable, script))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(saved["hooks"]["enabled"])
            self.assertIn("SessionStart", saved["hooks"]["events"])
            self.assertEqual(saved["plugins"], original["plugins"])
            handlers = [handler for group in saved["hooks"]["events"]["Stop"]
                        for handler in group["hooks"]]
            self.assertEqual(sum("pikapet.harness_notifications" in handler.get("args", [])
                                 for handler in handlers), 1)
            self.assertFalse(integration.update_config(path, sys.executable, script))
