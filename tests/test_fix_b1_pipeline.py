"""P2-7 / P2-8 / P2-9 / P2-10 回归（修订清单 2026-10-23 批次 1，数据管道）。

P2-7：decrypt manifest 必须记录**解密前**的 (size, mtime_ns)。解密窗口内
源库被写且大小不变时，写解密后 mtime 会让下次错误命中旧产物、新增消息永久
缺失；此时必须 pop 条目让下次强制重解，旧条目（无 mtime_ns）回退秒级。

P2-8：备份复用必须先校验（截断 JPEG 不进 _IMG_CACHE、继续下一个候选）；
备份写入走 `.part` + os.replace，中断不留半成品/`.part`。

P2-9：账号根目录从 discover 的 (wxid, db_storage) 推导，不依赖 cache/；
attach/video/file 全缺时 backup_all 必须 ok=False + 明确 error。

P2-10：取消粒度到单库批次/逐文件；cancelled 不计失败、条数恒等于任务数；
备份取消后不再启动 wxgf 批处理。

全部使用合成临时目录与占位符（wxid_test），不读取真实聊天库。
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from tests._base import IsolatedRootCase

ACCOUNT = "wxid_test"
REL = os.path.join("contact", "contact.db")
JPEG_OK = b"\xff\xd8\xff" + b"\x00" * 100 + b"\xff\xd9"
JPEG_TRUNCATED = b"\xff\xd8\xff" + b"\x00" * 100      # 缺 FFD9：半截下载/解密


class _DecryptHarness(IsolatedRootCase):
    """decrypt_dir 夹具：合成源库 + 假密钥 + 可注入的假 decrypt_parallel。"""

    def setUp(self):
        super().setUp()
        from siwx import extract, keystore, pool
        self.extract, self.pool = extract, pool
        self._ks_orig = keystore.load
        keystore.load = lambda: {"aa" * 16: {"key": "ab" * 32,
                                             "strategy": "test", "updated": 0}}
        self._orig = (extract.parse_key, extract.verify_enc_key,
                      extract.decrypt_parallel)
        extract.parse_key = lambda k: b"\x00" * 32
        extract.verify_enc_key = lambda kb, p1: True
        self.db = self.tmp / "xwechat_files" / ACCOUNT / "db_storage"
        (self.db / "contact").mkdir(parents=True)
        (self.db / "contact" / "contact.db").write_bytes(b"x" * 8192)
        self.out = self.tmp / "out" / ACCOUNT

    def tearDown(self):
        from siwx import keystore
        (self.extract.parse_key, self.extract.verify_enc_key,
         self.extract.decrypt_parallel) = self._orig
        keystore.load = self._ks_orig
        super().tearDown()

    def _entry(self, size=8192):
        from siwx.sqlcipher import DbEntry
        return DbEntry(REL, self.db / REL, size, "aa" * 16, b"\x00" * 4096)

    def _install_fake(self, mutate=None):
        calls = []

        def fake(tasks, workers=None, on_done=None, should_cancel=None):
            calls.append({"n": len(tasks), "cancel": should_cancel})
            res = []
            for rel, src, dst, key_hex in tasks:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(b"DECRYPTED")
                if mutate is not None:
                    mutate(Path(src))
                r = (rel, 2, "ok", "")
                res.append(r)
                if on_done:
                    on_done(r)
            return res

        self.extract.decrypt_parallel = fake
        return calls

    def _decrypt(self, **kw):
        kw.setdefault("entries", [self._entry()])
        kw.setdefault("use_cache", True)
        return self.extract.decrypt_dir(str(self.db), str(self.out),
                                        log=lambda m: None, **kw)


class TestManifestMtimeGuard(_DecryptHarness):
    """P2-7：manifest 记录解密前的 stat，解密窗口内变化则不写条目。"""

    def test_source_changed_during_decrypt_is_not_cached(self):
        def bump(src):
            st = src.stat()
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

        calls = self._install_fake(mutate=bump)
        rep = self._decrypt()
        self.assertEqual(rep["ok"], 1)
        self.assertNotIn(REL, self.pool.load_manifest(self.out),
                         "解密期间源库变化的条目绝不能写进 manifest")

        rep2 = self._decrypt()   # 下一次必须重解，而不是命中旧产物
        self.assertEqual(rep2["cached"], 0)
        self.assertEqual(rep2["ok"], 1)
        self.assertEqual([c["n"] for c in calls], [1, 1])

    def test_unchanged_source_writes_manifest_and_hits_cache(self):
        calls = self._install_fake()
        rep = self._decrypt()
        self.assertEqual(rep["ok"], 1)
        entry = self.pool.load_manifest(self.out)[REL]
        self.assertEqual(entry["size"], 8192)
        self.assertEqual(entry["pages"], 2)
        self.assertEqual(entry["mtime_ns"],
                         int(self._entry().path.stat().st_mtime_ns))

        rep2 = self._decrypt()
        self.assertEqual(rep2["cached"], 1)
        self.assertEqual(rep2["ok"], 0)
        self.assertEqual(calls[-1]["n"], 0, "缓存命中时不应再派发解密任务")

    def test_legacy_manifest_without_mtime_ns_still_hits(self):
        e = self._entry()
        (self.out / "contact").mkdir(parents=True)
        (self.out / REL).write_bytes(b"OLD")
        self.pool.save_manifest(self.out, {
            REL: {"size": 8192, "mtime": int(e.path.stat().st_mtime),
                  "pages": 1}})
        calls = self._install_fake()
        rep = self._decrypt()
        self.assertEqual(rep["cached"], 1, "旧条目应回退秒级 mtime，不清空缓存")
        self.assertEqual(calls[0]["n"], 0)


class TestDecryptDirCancel(_DecryptHarness):
    """P2-10：decrypt_dir 透传取消探针并统计 cancelled。"""

    def test_passes_probe_and_reports_cancelled(self):
        seen = {}

        def fake(tasks, workers=None, on_done=None, should_cancel=None):
            seen["probe"] = should_cancel
            return [(t[0], 0, "cancelled", "用户取消") for t in tasks]

        self.extract.decrypt_parallel = fake
        rep = self._decrypt(should_cancel=lambda: True)
        self.assertIsNotNone(seen["probe"])
        self.assertTrue(seen["probe"]())
        self.assertEqual(rep["cancelled"], 1)
        self.assertTrue(rep["is_cancelled"])
        self.assertEqual(rep["failed"], 0, "取消不得计为失败")
        self.assertNotIn(REL, self.pool.load_manifest(self.out))

    def test_default_call_does_not_pass_probe(self):
        """旧调用（不传 should_cancel）必须保持三参数 monkeypatch 可用。"""
        def fake(tasks, workers=None, on_done=None):
            return [(t[0], 2, "ok", "") for t in tasks]

        self.extract.decrypt_parallel = fake
        rep = self._decrypt()
        self.assertEqual(rep["ok"], 1)
        self.assertEqual(rep["cancelled"], 0)
        self.assertFalse(rep["is_cancelled"])


class TestPoolCancel(IsolatedRootCase):
    """P2-10：串行路径逐任务检查，取消后结果条数仍等于任务数。"""

    def _tasks(self):
        return [(f"db{i}.db", "src", "dst", "00") for i in range(3)]

    def test_serial_cancel_after_first_task(self):
        from siwx import pool
        done = []

        def fake_worker(t):
            done.append(t[0])
            return (t[0], 1, "ok", "")

        with mock.patch.object(pool, "_worker", fake_worker):
            res = pool.decrypt_parallel(self._tasks(), workers=1,
                                        should_cancel=lambda: len(done) >= 1)
        self.assertEqual(len(done), 1)
        self.assertEqual(len(res), 3, "结果条数必须等于任务数")
        self.assertEqual([r[0] for r in res],
                         [t[0] for t in self._tasks()])
        self.assertEqual([r[2] for r in res], ["ok", "cancelled", "cancelled"])
        self.assertEqual(res[1][3], "用户取消")

    def test_no_probe_keeps_old_behaviour(self):
        from siwx import pool

        def fake_worker(t):
            return (t[0], 3, "ok", "")

        with mock.patch.object(pool, "_worker", fake_worker):
            res = pool.decrypt_parallel(self._tasks(), workers=1)
        self.assertEqual([r[2] for r in res], ["ok", "ok", "ok"])


class TestBackupReuseValidation(IsolatedRootCase):
    """P2-8：备份复用必须先校验，坏文件不返回也不进缓存。"""

    def setUp(self):
        super().setUp()
        from siwx import media
        self.media = media
        media._IMG_CACHE.clear()
        media._BACKUP_INDEX.clear()
        self.acc = self.tmp / "out" / ACCOUNT
        self.img_dir = (self.acc / "media_backup" / "images" / "chatmd5"
                        / "2026-01" / "Img")
        self.img_dir.mkdir(parents=True)
        self.md5 = "a" * 32

    def tearDown(self):
        self.media._IMG_CACHE.clear()
        self.media._BACKUP_INDEX.clear()
        super().tearDown()

    def test_truncated_backup_file_not_reused_or_cached(self):
        (self.img_dir / f"{self.md5}.jpeg").write_bytes(JPEG_TRUNCATED)
        body, info = self.media.get_image(ACCOUNT, self.md5, self.acc)
        self.assertIsNone(body)
        self.assertEqual(info, "备份产物不完整")
        self.assertEqual(len(self.media._IMG_CACHE), 0,
                         "坏备份产物绝不能进 _IMG_CACHE")

    def test_bad_candidate_falls_through_to_next(self):
        (self.img_dir / f"{self.md5}.jpeg").write_bytes(JPEG_TRUNCATED)
        (self.img_dir / f"{self.md5}_h.jpeg").write_bytes(JPEG_OK)
        body, ctype = self.media.get_image(ACCOUNT, self.md5, self.acc)
        self.assertEqual(body, JPEG_OK, "坏候选应继续下一个备份候选")
        self.assertEqual(ctype, "image/jpeg")

    def test_complete_backup_file_reused_and_cached(self):
        (self.img_dir / f"{self.md5}.jpeg").write_bytes(JPEG_OK)
        body, ctype = self.media.get_image(ACCOUNT, self.md5, self.acc)
        self.assertEqual(body, JPEG_OK)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(len(self.media._IMG_CACHE), 1)
        body2, _ct2 = self.media.get_image(ACCOUNT, self.md5, self.acc)
        self.assertEqual(body2, JPEG_OK)


class TestAtomicBackupWrite(IsolatedRootCase):
    """P2-8：`.part` + os.replace；失败清理，不留半成品。"""

    def test_atomic_write_bytes_leaves_no_part(self):
        from siwx import media_backup
        dst = self.tmp / "a" / "b.jpeg"
        dst.parent.mkdir(parents=True)
        media_backup._atomic_write_bytes(dst, b"data")
        self.assertEqual(dst.read_bytes(), b"data")
        self.assertEqual(list(dst.parent.glob("*.part")), [])

    def test_failed_copy_leaves_no_half_file_or_part(self):
        from siwx import media_backup
        root = self.tmp / "xwechat_files" / ACCOUNT
        vdir = root / "msg" / "video" / "2026-01"
        vdir.mkdir(parents=True)
        (vdir / "x.mp4").write_bytes(b"video-bytes")

        def boom(src, dst):
            Path(dst).write_bytes(b"half")     # 模拟复制到一半
            raise OSError("disk full")

        with mock.patch("siwx.media_backup._account_root", return_value=root), \
             mock.patch("siwx.media_backup.shutil.copyfile", side_effect=boom):
            stats = media_backup.backup_videos(ACCOUNT, self.tmp / "out_videos",
                                               log=None)
        self.assertEqual(stats["failed"], 1)
        out = self.tmp / "out_videos" / "2026-01"
        self.assertEqual(list(out.glob("x.mp4")), [], "不能留下目标半成品")
        self.assertEqual(list(out.glob("*.part")), [], "不能留下 .part")


class TestAccountRootWithoutCache(IsolatedRootCase):
    """P2-9：账号根目录不依赖 cache/；媒体目录缺失必须显式失败。"""

    def _make_root(self):
        root = self.tmp / "xwechat_files" / ACCOUNT
        root.mkdir(parents=True)
        return root

    def _patch_discover(self, root):
        return mock.patch("siwx.discover.find_wechat_data_dirs",
                          return_value=[(ACCOUNT, str(root / "db_storage"))])

    def test_finds_attach_without_cache_dir(self):
        from siwx import media, media_backup
        root = self._make_root()
        attach = root / "msg" / "attach" / "chatmd5" / "2026-01" / "Img"
        attach.mkdir(parents=True)
        (attach / "a.dat").write_bytes(b"opaque")
        self.assertFalse((root / "cache").exists())

        with self._patch_discover(root), \
             mock.patch("siwx.media._decrypt_any",
                        return_value=(JPEG_OK, "image/jpeg")), \
             mock.patch("siwx.media_backup.platform.system", return_value="Linux"):
            stats = media_backup.backup_images(ACCOUNT, self.tmp / "out", log=None)
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["ok_viewable"], 1)

    def test_all_media_dirs_missing_reports_failure(self):
        from siwx import media_backup
        root = self._make_root()
        with self._patch_discover(root), \
             mock.patch("siwx.media_backup.platform.system", return_value="Linux"):
            report = media_backup.backup_all(ACCOUNT, self.tmp / "out_all", log=None)
        self.assertFalse(report["ok"])
        self.assertIn("未找到媒体目录", report["error"])
        self.assertEqual(report["media_dirs"],
                         {"attach": False, "video": False, "file": False})

    def test_missing_account_root_reports_failure(self):
        from siwx import media_backup
        with mock.patch("siwx.discover.find_wechat_data_dirs", return_value=[]), \
             mock.patch("siwx.media._wechat_cache_roots", return_value=[]):
            report = media_backup.backup_all(ACCOUNT, self.tmp / "out_all", log=None)
        self.assertFalse(report["ok"])
        self.assertIn("未找到账号数据目录", report["error"])

    def test_partial_media_dirs_ok_with_flags(self):
        from siwx import media_backup
        root = self._make_root()
        attach = root / "msg" / "attach" / "chatmd5" / "2026-01" / "Img"
        attach.mkdir(parents=True)
        (attach / "a.dat").write_bytes(b"opaque")
        with self._patch_discover(root), \
             mock.patch("siwx.media._decrypt_any",
                        return_value=(JPEG_OK, "image/jpeg")), \
             mock.patch("siwx.media_backup.platform.system", return_value="Linux"):
            report = media_backup.backup_all(ACCOUNT, self.tmp / "out_all", log=None)
        self.assertTrue(report["ok"])
        self.assertEqual(report["media_dirs"],
                         {"attach": True, "video": False, "file": False})
        self.assertEqual(report["images"]["ok_viewable"], 1)
        self.assertEqual(report["videos"]["total"], 0)


class TestBackupJobOkFlag(IsolatedRootCase):
    """P2-9：report.ok=False 时任务必须失败并带 error，不再报"备份完成"。"""

    def test_job_marks_failure_when_report_not_ok(self):
        from siwx import server
        old = dict(server._backup_job)
        logs = []
        try:
            server._backup_job.update({"running": True, "done": False, "ok": True,
                                       "logs": [], "report": None, "error": None})
            with mock.patch("siwx.media_backup.backup_all",
                            return_value={"ok": False,
                                          "error": "未找到媒体目录（msg/attach、"
                                                   "msg/video、msg/file 均不存在）"}):
                server._run_media_backup(ACCOUNT, self.tmp / "out", logs.append)
            self.assertFalse(server._backup_job["ok"])
            self.assertIn("未找到媒体目录", server._backup_job["error"])
            self.assertTrue(any("备份失败" in m for m in logs), logs)
            self.assertTrue(server._backup_job["done"])
        finally:
            server._backup_job.clear()
            server._backup_job.update(old)


class TestBackupCancelStopsWxgf(IsolatedRootCase):
    """P2-10：备份取消后不再启动 wxgf 批处理；剩余原文按留档计数。"""

    def test_cancel_before_wxgf_batch(self):
        from siwx import media_backup
        root = self.tmp / "xwechat_files" / ACCOUNT
        attach = root / "msg" / "attach" / "chatmd5" / "2026-01"
        attach.mkdir(parents=True)
        (attach / "a.dat").write_bytes(b"a")
        (attach / "b.dat").write_bytes(b"b")
        state = {"n": 0}

        def probe():
            state["n"] += 1
            return state["n"] > 1      # 第一张图之后取消

        with mock.patch("siwx.media_backup._account_root", return_value=root), \
             mock.patch("siwx.media._decrypt_any",
                        return_value=(b"wxgf" + b"\x00" * 32, "image/wxgf")), \
             mock.patch("siwx.media_backup._convert_pending_wxgf") as conv:
            stats = media_backup.backup_images(ACCOUNT, self.tmp / "out",
                                               log=None, should_cancel=probe)
        conv.assert_not_called()
        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["ok_wxgf_preserved"], 1)

    def test_windows_convert_cancel_preserves_remaining(self):
        from siwx import media_backup
        pending = [(Path("a.wxgf"), Path("a.heic")),
                   (Path("b.wxgf"), Path("b.heic"))]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}
        with mock.patch("siwx.media_backup.platform.system", return_value="Linux"), \
             mock.patch("siwx.media.convert_wxgf") as conv:
            media_backup._convert_pending_wxgf(pending, stats, None,
                                               should_cancel=lambda: True)
        conv.assert_not_called()
        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["ok_wxgf_preserved"], 2)


class TestWxgfBatchCancel(IsolatedRootCase):
    """P2-10：macOS 批量转码只在批次之间检查，取消返回部分结果。"""

    def test_stops_between_batches(self):
        from siwx import media_wxgf_macos
        pairs = [(Path(f"f{i}.wxgf"), Path(f"f{i}.heic")) for i in range(3)]
        calls = []
        state = {"n": 0}

        def fake_run(pid, chunk, log=None, timeout=0):
            calls.append(len(chunk))
            return {str(a): {"ok": False, "reason": "synthetic"} for a, _ in chunk}

        def probe():
            state["n"] += 1
            return state["n"] > 1

        with mock.patch.object(media_wxgf_macos, "find_wechat_pid",
                               return_value=1234), \
             mock.patch.object(media_wxgf_macos, "_run_lldb_batch", fake_run):
            res = media_wxgf_macos.convert_wxgf_batch(
                pairs, log=lambda *_a: None, batch_size=1, should_cancel=probe)
        self.assertEqual(len(calls), 1, "取消后不得再进入下一个批次")
        self.assertEqual(len(res), 1, "只返回已完成批次的部分结果")


if __name__ == "__main__":
    unittest.main()
