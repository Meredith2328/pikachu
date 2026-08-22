# -*- coding: utf-8 -*-
"""ZCode Stop 钩子脚本：stdin 解析、转录摘录、会话 ID 提取、端到端装配。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import zcode_hook  # noqa: E402


class TestCollapse(unittest.TestCase):
    def test_flattens_whitespace(self):
        self.assertEqual(zcode_hook.collapse("a \n\t b   c"), "a b c")

    def test_truncates_with_ellipsis(self):
        out = zcode_hook.collapse("x" * 200, limit=50)
        self.assertEqual(len(out), 51)
        self.assertTrue(out.endswith("…"))

    def test_none_safe(self):
        self.assertEqual(zcode_hook.collapse(""), "")


class TestTextFromContent(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(zcode_hook.text_from_content("hi"), "hi")

    def test_block_list_picks_text_only(self):
        content = [
            {"type": "thinking", "text": "内部思考"},
            {"type": "text", "text": "第一段"},
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "第二段"},
        ]
        self.assertEqual(zcode_hook.text_from_content(content), "第一段\n第二段")

    def test_weird_shapes_return_empty(self):
        self.assertEqual(zcode_hook.text_from_content(None), "")
        self.assertEqual(zcode_hook.text_from_content([1, 2]), "")


class TestLastAssistantText(unittest.TestCase):
    def test_reads_last_assistant_text_line(self):
        lines = [
            {"type": "user", "message": {"content": "问题"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "中间结论"}]}},
            {"type": "other"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "最终总结"}]}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False, encoding="utf-8") as f:
            for obj in lines:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            path = f.name
        try:
            self.assertEqual(zcode_hook.last_assistant_text(path), "最终总结")
        finally:
            Path(path).unlink()

    def test_missing_file_returns_empty(self):
        self.assertEqual(zcode_hook.last_assistant_text("Z:/不存在.jsonl"), "")

    def test_torn_tail_line_does_not_crash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False, encoding="utf-8") as f:
            f.write('{"type":"assistant","message":{"content":[{"type":"text",'
                    '"text":"好"}]}}\n')
            f.write('{"type":"assistant","mess')  # 模拟写了一半的尾行
            path = f.name
        try:
            self.assertEqual(zcode_hook.last_assistant_text(path), "好")
        finally:
            Path(path).unlink()


class TestExtractSessionId(unittest.TestCase):
    def test_payload_key_wins(self):
        env = {"CLAUDE_SESSION_ID": "env-id"}
        self.assertEqual(
            zcode_hook.extract_session_id({"session_id": "abc"}, env), "abc")

    def test_env_fallback(self):
        env = {"CLAUDE_SESSION_ID": "env-id"}
        self.assertEqual(zcode_hook.extract_session_id({}, env), "env-id")

    def test_missing_everywhere(self):
        self.assertEqual(zcode_hook.extract_session_id({}, {}), "")


class TestExtractSnippet(unittest.TestCase):
    def test_payload_text_key_wins_over_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "t.jsonl"
            tp.write_text('{"type":"assistant","message":{"content":'
                          '"来自转录"}}\n', encoding="utf-8")
            payload = {"response": "直接字段", "transcript_path": str(tp)}
            self.assertEqual(zcode_hook.extract_snippet(payload), "直接字段")

    def test_transcript_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "t.jsonl"
            tp.write_text('{"type":"assistant","message":{"content":'
                          '"来自转录"}}\n', encoding="utf-8")
            self.assertEqual(
                zcode_hook.extract_snippet({"transcript_path": str(tp)}),
                "来自转录")

    def test_nothing_available(self):
        self.assertEqual(zcode_hook.extract_snippet({}), "")


class TestSessionTitle(unittest.TestCase):
    def _make_db(self, path, rows):
        import sqlite3
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
        con.executemany("INSERT INTO session VALUES (?, ?)", rows)
        con.commit()
        con.close()

    def test_looks_up_title_by_session_id(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "db.sqlite"
            self._make_db(db, [("sess_abc", "皮卡丘桌宠模块化")])
            self.assertEqual(
                zcode_hook.session_title_from_db("sess_abc", db),
                "皮卡丘桌宠模块化")

    def test_missing_id_or_row_or_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "db.sqlite"
            self._make_db(db, [("sess_abc", "有标题")])
            self.assertEqual(zcode_hook.session_title_from_db("", db), "")
            self.assertEqual(
                zcode_hook.session_title_from_db("sess_不存在", db), "")
        self.assertEqual(
            zcode_hook.session_title_from_db("sess_abc", Path(td) / "无.sqlite"),
            "")


class TestResolveTitle(unittest.TestCase):
    def test_payload_title_key_wins(self):
        payload = {"session_id": "sess_x",
                   "title": "手动起的标题"}
        with mock.patch.object(zcode_hook, "session_title_from_db",
                               lambda sid: "库里的标题"):
            self.assertEqual(zcode_hook.resolve_title(payload), "手动起的标题")

    def test_falls_back_to_db_title(self):
        payload = {"session_id": "sess_x"}
        with mock.patch.object(zcode_hook, "session_title_from_db",
                               lambda sid: "库里的标题"):
            self.assertEqual(zcode_hook.resolve_title(payload), "库里的标题")

    def test_falls_back_to_first_user_text_then_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "t.jsonl"
            tp.write_text(
                '{"type":"user","message":{"content":"帮我实现桌宠跟随鼠标"}}\n'
                '{"type":"assistant","message":{"content":"好的"}}\n',
                encoding="utf-8")
            payload = {"transcript_path": str(tp)}
            self.assertEqual(zcode_hook.resolve_title(payload),
                             "帮我实现桌宠跟随鼠标")
            payload2 = {"cwd": "D:\\_Project\\pikachu"}
            self.assertEqual(zcode_hook.resolve_title(payload2), "pikachu")

    def test_last_resort_short_id(self):
        payload = {"session_id": "sess_abcdef12-9999"}
        with mock.patch.object(zcode_hook, "session_title_from_db",
                               lambda sid: ""):
            self.assertEqual(zcode_hook.resolve_title(payload), "sess_abc")


class TestMainAssembly(unittest.TestCase):
    def _run_main(self, stdin_text, environ=None):
        sent = []
        with mock.patch.object(zcode_hook, "send_notification",
                               lambda n: sent.append(n) or {"ok": True}), \
             mock.patch.object(zcode_hook, "session_title_from_db",
                               lambda sid: ""), \
             mock.patch.object(sys, "stdin", None), \
             mock.patch("sys.stdin") as fake_stdin:
            fake_stdin.isatty.return_value = False
            fake_stdin.read.return_value = stdin_text
            code = zcode_hook.main(["--event", "Stop"])
        return code, sent

    def test_full_pipeline_with_sid_and_snippet(self):
        payload = {"session_id": "abcdef12-3456",
                   "last_response": "  完成 了\n\n 三件事  "}
        code, sent = self._run_main(json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].title, "会话完成 · abcdef12")
        self.assertEqual(sent[0].body, "完成 了 三件事")
        self.assertEqual(sent[0].source, "zcode")
        self.assertEqual(sent[0].level, "success")  # 与 codex 事件统一

    def test_garbage_stdin_still_exits_zero_without_send(self):
        code, sent = self._run_main("not json at all")
        self.assertEqual(code, 0)
        # 解析不出会话信息时也发一条兜底气泡（标题无 ID，正文提示）
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].title, "会话完成")

    def test_empty_body_falls_back_to_hint(self):
        code, sent = self._run_main(json.dumps({"session_id": "sid-123"}))
        self.assertEqual(code, 0)
        self.assertEqual(sent[0].body, "（未读取到进展内容）")


if __name__ == "__main__":
    unittest.main()
