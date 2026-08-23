# -*- coding: utf-8 -*-
"""pika.paths 的单元测试：目录解析、非法名拒绝、原子写、旧目录迁移。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pika import paths


class PathsTestCase(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get(paths.HOME_ENV)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(paths.HOME_ENV, None)
        else:
            os.environ[paths.HOME_ENV] = self._prev


class TestBaseDir(PathsTestCase):
    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ[paths.HOME_ENV] = td
            self.assertEqual(paths.base_dir(), Path(td))

    def test_default_is_not_inside_source_tree(self):
        """运行时数据绝不能再落在源码目录里（装到 site-packages 会写包目录）。"""
        os.environ.pop(paths.HOME_ENV, None)
        source_root = Path(paths.__file__).resolve().parent.parent
        self.assertNotIn(source_root, paths.base_dir().resolve().parents)

    def test_create_makes_dir(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            os.environ[paths.HOME_ENV] = str(target)
            self.assertTrue(paths.base_dir(create=True).is_dir())

    def test_create_on_file_path_raises(self):
        """路径被普通文件占着要报错，不静默换地方——换了地方等于 token
        与总线不同源，表现是所有投递 403。"""
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            os.environ[paths.HOME_ENV] = str(blocker)
            with self.assertRaises(paths.RuntimeDirError):
                paths.base_dir(create=True)

    def test_windows_default_uses_localappdata(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows")
        os.environ.pop(paths.HOME_ENV, None)
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            self.skipTest("环境缺 LOCALAPPDATA")
        self.assertEqual(paths.base_dir(), Path(local) / paths.APP_DIR_NAME)


class TestRuntimePath(PathsTestCase):
    def test_rejects_path_separators(self):
        """只接受单段文件名：拼接外部输入时不给路径穿越留口子。"""
        for bad in ("../token", "a/b", "a\\b", "", ".", ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.runtime_path(bad)

    def test_named_files_live_in_base_dir(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ[paths.HOME_ENV] = td
            for fn in (paths.token_file, paths.port_file,
                       paths.pet_state_file, paths.log_file,
                       paths.hook_log_file):
                self.assertEqual(fn().parent, Path(td))

    def test_files_have_distinct_names(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ[paths.HOME_ENV] = td
            names = {paths.token_file().name, paths.port_file().name,
                     paths.pet_state_file().name, paths.log_file().name,
                     paths.hook_log_file().name}
            self.assertEqual(len(names), 5)


class TestWriteTextAtomic(PathsTestCase):
    def test_writes_and_replaces(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "nested" / "f.txt"
            paths.write_text_atomic(target, "第一版")
            paths.write_text_atomic(target, "第二版")
            self.assertEqual(target.read_text(encoding="utf-8"), "第二版")

    def test_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "f.txt"
            paths.write_text_atomic(target, "x")
            self.assertEqual([p.name for p in Path(td).iterdir()], ["f.txt"])

    def test_failure_propagates(self):
        """写不进去要抛：调用方（如 token）需要据此报错而不是假装成功。"""
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(OSError):
                paths.write_text_atomic(blocker / "f.txt", "x")


class TestMigrateLegacy(PathsTestCase):
    def test_migrates_token_and_state(self):
        legacy = paths.legacy_runtime_dir()
        with tempfile.TemporaryDirectory() as td:
            os.environ[paths.HOME_ENV] = td
            if not legacy.is_dir():
                self.assertEqual(paths.migrate_legacy(), [])
                return
            moved = paths.migrate_legacy()
            for name in moved:
                self.assertTrue((Path(td) / name).is_file())

    def test_does_not_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ[paths.HOME_ENV] = td
            target = Path(td) / paths.TOKEN_NAME
            paths.write_text_atomic(target, "已有的 token")
            paths.migrate_legacy()
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "已有的 token")

    def test_absent_legacy_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ[paths.HOME_ENV] = td
            missing = Path(td) / "没有这个目录"
            orig = paths.legacy_runtime_dir
            paths.legacy_runtime_dir = lambda: missing
            try:
                self.assertEqual(paths.migrate_legacy(), [])
            finally:
                paths.legacy_runtime_dir = orig


if __name__ == "__main__":
    unittest.main()
