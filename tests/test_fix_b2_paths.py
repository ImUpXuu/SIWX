"""P2-13 回归（修订清单 2026-10-23 批次 2，安全隐私）：手动路径配置落盘位置。

缺陷：`wechat_paths.json` 写在 `app_root()`（源码运行时=仓库根），内容含真实
绝对路径与账号目录名，且未进 `.gitignore` —— 一次 `git add .` 就可能把用户
隐私路径提交上去。

修法：`data_dir()` 新增最高优先级 `SIWX_DATA_DIR`；`manual_paths_file()`
改指 `data_dir()`；`load_manual_data_dirs()` 首次读取时把旧位置文件原子迁移
到新位置（失败保留旧文件并回退读取）；`.gitignore` 覆盖任意层级同名文件。

全部使用合成临时目录与占位符（wxid_example_01），不读取真实聊天库。
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._base import IsolatedRootCase

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "wxid_example_01"


class TestDataDirOverride(unittest.TestCase):
    """SIWX_DATA_DIR 覆盖 data_dir()：目录不存在也原样生效。"""

    def setUp(self):
        self._old = os.environ.get("SIWX_DATA_DIR")
        self._base = Path(tempfile.mkdtemp(prefix="siwx_example_"))

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SIWX_DATA_DIR", None)
        else:
            os.environ["SIWX_DATA_DIR"] = self._old
        shutil.rmtree(self._base, ignore_errors=True)

    def test_override_wins_even_if_dir_missing(self):
        from siwx import paths
        target = self._base / "data_not_created"
        os.environ["SIWX_DATA_DIR"] = str(target)
        self.assertEqual(paths.data_dir(), target)
        self.assertFalse(target.exists(), "data_dir() 不应替调用方创建目录")

    def test_override_takes_priority_over_platform_default(self):
        from siwx import paths
        os.environ["SIWX_DATA_DIR"] = str(self._base)
        self.assertEqual(paths.data_dir(), self._base)
        self.assertNotEqual(paths.data_dir().name, "stories-in-wx")


class TestManualPathsLocation(IsolatedRootCase):

    def _make_db(self, account: str = ACCOUNT) -> Path:
        db = self.tmp / "custom" / account / "db_storage"
        db.mkdir(parents=True)
        return db

    def test_manual_paths_file_lives_in_data_dir(self):
        from siwx import paths
        from siwx.discover import manual_paths_file
        p = manual_paths_file()
        self.assertEqual(p.parent, paths.data_dir())
        self.assertEqual(p.parent, self.tmp / "data")
        self.assertNotEqual(p.parent, paths.app_root())
        self.assertEqual(p.name, "wechat_paths.json")

    def test_save_does_not_write_into_app_root(self):
        from siwx import paths
        from siwx.discover import (load_manual_data_dirs, manual_paths_file,
                                   save_manual_data_dirs)
        db = self._make_db()
        save_manual_data_dirs([str(db)])
        self.assertTrue(manual_paths_file().is_file())
        self.assertFalse((paths.app_root() / "wechat_paths.json").exists())
        self.assertIn((ACCOUNT, str(db.resolve())), load_manual_data_dirs())

    def test_migrates_legacy_file_once(self):
        from siwx import paths
        from siwx.discover import load_manual_data_dirs, manual_paths_file
        db = self._make_db()
        legacy = paths.app_root() / "wechat_paths.json"
        legacy.write_text(json.dumps({"paths": [str(db)]}), encoding="utf-8")

        loaded = load_manual_data_dirs()      # 首次读取触发迁移

        dest = manual_paths_file()
        self.assertTrue(dest.is_file())
        self.assertFalse(legacy.exists(), "迁移成功后旧文件应被删除")
        self.assertIn((ACCOUNT, str(db.resolve())), loaded)
        self.assertEqual(json.loads(dest.read_text(encoding="utf-8")),
                         {"paths": [str(db)]})
        self.assertEqual(load_manual_data_dirs(), loaded)   # 新位置可重复读取
        self.assertFalse(dest.with_suffix(dest.suffix + ".tmp").exists())

    def test_existing_new_file_wins_and_legacy_kept(self):
        from siwx import paths
        from siwx.discover import load_manual_data_dirs, manual_paths_file
        new_db = self._make_db()
        old_db = self._make_db("wxid_example_02")
        dest = manual_paths_file()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"paths": [str(new_db)]}), encoding="utf-8")
        legacy = paths.app_root() / "wechat_paths.json"
        legacy.write_text(json.dumps({"paths": [str(old_db)]}), encoding="utf-8")

        loaded = load_manual_data_dirs()

        self.assertIn((ACCOUNT, str(new_db.resolve())), loaded)
        self.assertNotIn("wxid_example_02", [w for w, _d in loaded])
        self.assertTrue(legacy.exists(), "新位置已存在时不应动旧文件")

    def test_copy_failure_falls_back_to_legacy(self):
        from siwx import paths
        from siwx.discover import load_manual_data_dirs, manual_paths_file
        db = self._make_db()
        legacy = paths.app_root() / "wechat_paths.json"
        legacy.write_text(json.dumps({"paths": [str(db)]}), encoding="utf-8")
        paths.data_dir().write_bytes(b"")   # 让 mkdir(parents=True) 必然失败

        loaded = load_manual_data_dirs()

        self.assertIn((ACCOUNT, str(db.resolve())), loaded)
        self.assertTrue(legacy.exists(), "复制失败必须保留旧文件")
        self.assertFalse(manual_paths_file().exists())


class TestGitignoreCoversManualPaths(unittest.TestCase):

    def test_gitignore_has_entry(self):
        lines = [ln.strip() for ln in
                 (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn("wechat_paths.json", lines)


if __name__ == "__main__":
    unittest.main()
