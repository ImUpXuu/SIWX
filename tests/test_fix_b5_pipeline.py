"""P3 批次 5 回归（修订清单 2026-10-23）：管道与导出的低风险缺陷。

P3-1  图片端点兜底：get_image 之外的意外异常返回 404 JSON 而非 500。
P3-6  进程池中断保留已完成结果；extract 边收边写 manifest；清理陈旧 `.part`。
P3-7  head_image.db 损坏时 collect_avatars 降级为空映射，不拖垮整会话导出。
P3-8  CSV 不截断；HTML/SQLite/XLSX 截断计数按格式写入 manifest。
P3-9  JSON→MD/HTML 先写 `.part` 再原子替换，失败不留半成品。
P3-10 /api/export/open 按平台选择 open -R / open / xdg-open / explorer。
P3-11 /api/export/render 超上限返回 413，不整包读入。
P3-12 stats.signature 并入 contact.db（改备注后缓存失效）。
P3-13 解码缓存增加字节预算，超限按 LRU 淘汰且计数不为负。
P3-20 account_dir 拒绝 "." / ".." 与解析到输出根本身的输入。

全部使用合成临时数据与占位符（wxid_test*），不读取真实聊天库。
"""
import csv
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._base import IsolatedRootCase

ACCOUNT = "wxid_test"
CHAT = "wxid_test_friend"


# ── 合成导出账号 ────────────────────────────────────────────────

def _msg_table(chat: str) -> str:
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def _make_shard(path: Path, chat: str, texts) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    t = _msg_table(chat)
    conn.execute(f"""CREATE TABLE [{t}] (
        local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
        create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
        message_content BLOB, packed_info_data BLOB)""")
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
    for i, txt in enumerate(texts):
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (i + 1, 1000 + i, 1, 1_700_000_000 + i, 0, 1,
                      txt.encode("utf-8"), None))
    conn.commit()
    conn.close()
    return path


def make_account(root: Path, texts, account: str = ACCOUNT,
                 chat: str = CHAT) -> Path:
    """最小可导出的解密产物目录（分组/联系人/会话库齐全）。"""
    acc = root / "output" / account
    _make_shard(acc / "message" / "message_0.db", chat, texts)
    (acc / "contact").mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(acc / "contact" / "contact.db")
    c.execute("CREATE TABLE contact (username TEXT, remark TEXT, "
              "nick_name TEXT, alias TEXT)")
    c.execute("INSERT INTO contact VALUES (?,?,?,?)", (chat, "联系人B", "", ""))
    c.commit()
    c.close()
    (acc / "session").mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(acc / "session" / "session.db")
    s.execute("CREATE TABLE SessionTable (username TEXT, summary TEXT, "
              "sort_timestamp INTEGER)")
    s.execute("INSERT INTO SessionTable VALUES (?,?,?)", (chat, "预览", 1))
    s.commit()
    s.close()
    return acc


def _export(acc: Path, fmt, root: Path, **kw):
    from siwx import exporter
    kw.setdefault("want_messages", True)
    return exporter.run_export(
        acc, ACCOUNT, CHAT, "测试会话名", fmt,
        want_media=False, want_avatars=False,
        export_root=root, pack="none", progress=lambda p, m: None, **kw)


# ── P3-1 图片端点兜底 ───────────────────────────────────────────

class TestImageEndpointFallback(IsolatedRootCase):
    """/api/chat/media/image 的意外异常必须落到 404 JSON，而不是 500。"""

    def setUp(self):
        super().setUp()
        from siwx import paths
        (paths.out_root() / ACCOUNT).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _client():
        from siwx.server import app
        return app.test_client()

    def test_get_image_exception_returns_404(self):
        from siwx import media
        with mock.patch.object(media, "get_image",
                               side_effect=RuntimeError("boom")):
            r = self._client().get(
                f"/api/chat/media/image?account={ACCOUNT}&md5={'a' * 32}")
        self.assertEqual(r.status_code, 404)
        body = r.get_json()
        self.assertEqual(body["reason"], "decrypt_failed")
        self.assertIn("RuntimeError", body["error"])

    def test_path_validation_exception_returns_404(self):
        from siwx import api_chat
        with mock.patch.object(api_chat.validate, "account_dir",
                               side_effect=RuntimeError("boom")):
            r = self._client().get(
                f"/api/chat/media/image?account={ACCOUNT}&md5={'a' * 32}")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "decrypt_failed")

    def test_success_response_unchanged(self):
        from siwx import media
        with mock.patch.object(media, "get_image",
                               return_value=(b"\xff\xd8\xff", "image/jpeg")):
            r = self._client().get(
                f"/api/chat/media/image?account={ACCOUNT}&md5={'a' * 32}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.data, b"\xff\xd8\xff")


# ── P3-6 进程池中断 + manifest + 陈旧 .part ─────────────────────

class _FakeBreachedPool:
    """产出第一条结果后模拟池管道崩溃。"""

    def __init__(self, n):
        self.n = n
        self.terminated = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def terminate(self):
        self.terminated = True

    def imap_unordered(self, func, tasks):
        yield (tasks[0][0], 3, "ok", "")
        raise RuntimeError("pipe broken")


class TestPoolInterrupt(IsolatedRootCase):
    """P3-6：池中断保留已收集结果，剩余任务补 failed 占位。"""

    def test_completed_kept_and_remaining_marked_failed(self):
        from siwx import pool
        tasks = [(f"db{i}.db", "src", "dst", "00") for i in range(3)]
        done = []
        with mock.patch("multiprocessing.Pool", _FakeBreachedPool):
            res = pool.decrypt_parallel(tasks, workers=2, on_done=done.append)
        self.assertEqual(len(res), 3, "结果条数必须等于任务数")
        self.assertEqual(res[0], ("db0.db", 3, "ok", ""))
        self.assertEqual([r[2] for r in res[1:]], ["failed", "failed"])
        self.assertEqual(res[1][3], "池中断: RuntimeError")
        self.assertEqual([r[0] for r in done], ["db0.db"],
                         "已完成结果必须在中断前交给 on_done")

    def test_interrupt_placeholders_are_not_cancelled(self):
        from siwx import pool
        tasks = [(f"db{i}.db", "src", "dst", "00") for i in range(2)]
        with mock.patch("multiprocessing.Pool", _FakeBreachedPool):
            res = pool.decrypt_parallel(tasks, workers=2,
                                        should_cancel=lambda: False)
        self.assertEqual([r[2] for r in res], ["ok", "failed"])
        self.assertEqual([r[0] for r in res], ["db0.db", "db1.db"])


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
        self.rel = os.path.join("contact", "contact.db")
        self.db = self.tmp / "xwechat_files" / ACCOUNT / "db_storage"
        (self.db / "contact").mkdir(parents=True)
        (self.db / self.rel).write_bytes(b"x" * 8192)
        self.out = self.tmp / "out" / ACCOUNT

    def tearDown(self):
        from siwx import keystore
        (self.extract.parse_key, self.extract.verify_enc_key,
         self.extract.decrypt_parallel) = self._orig
        keystore.load = self._ks_orig
        super().tearDown()

    def _entry(self):
        from siwx.sqlcipher import DbEntry
        return DbEntry(self.rel, self.db / self.rel, 8192, "aa" * 16,
                       b"\x00" * 4096)

    def _decrypt(self):
        return self.extract.decrypt_dir(str(self.db), str(self.out),
                                        entries=[self._entry()],
                                        log=lambda m: None)


class TestExtractManifestOnInterrupt(_DecryptHarness):
    """P3-6：池/调度异常上抛前，已完成条目已落盘。"""

    def test_done_entries_persisted_when_pool_raises(self):
        def fake(tasks, workers=None, on_done=None, should_cancel=None):
            dst = Path(tasks[0][2])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"DECRYPTED")
            on_done((tasks[0][0], 2, "ok", ""))
            raise RuntimeError("池中断")

        self.extract.decrypt_parallel = fake
        with self.assertRaises(RuntimeError):
            self._decrypt()
        manifest = self.pool.load_manifest(self.out)
        self.assertIn(self.rel, manifest, "已完成条目必须已写入 manifest")
        self.assertEqual(manifest[self.rel]["pages"], 2)

    def test_normal_completion_not_double_counted(self):
        def fake(tasks, workers=None, on_done=None, should_cancel=None):
            res = []
            for t in tasks:
                dst = Path(t[2])
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b"DECRYPTED")
                r = (t[0], 2, "ok", "")
                res.append(r)
                on_done(r)
            return res

        self.extract.decrypt_parallel = fake
        rep = self._decrypt()
        self.assertEqual(rep["ok"], 1)
        self.assertEqual(len(rep["files"]), 1, "on_done 与最终遍历不得重复计数")
        self.assertIn(self.rel, self.pool.load_manifest(self.out))

    def test_pool_interrupt_keeps_done_entry_in_manifest(self):
        """imap_unordered 产出 1 条后抛异常：ok 保留、剩余 failed、manifest 有 ok 条目。"""
        from siwx.sqlcipher import DbEntry
        rel2 = os.path.join("contact", "contact_2.db")
        (self.db / rel2).write_bytes(b"y" * 8192)
        entries = [self._entry(),
                   DbEntry(rel2, self.db / rel2, 8192, "bb" * 16,
                           b"\x00" * 4096)]
        with mock.patch("multiprocessing.Pool", _FakeBreachedPool):
            rep = self.extract.decrypt_dir(str(self.db), str(self.out),
                                           entries=entries, workers=2,
                                           log=lambda m: None)
        self.assertEqual((rep["ok"], rep["failed"], rep["cancelled"]),
                         (1, 1, 0))
        manifest = self.pool.load_manifest(self.out)
        self.assertIn(self.rel, manifest, "已完成条目必须保留在 manifest")
        self.assertNotIn(rel2, manifest, "未完成条目不得写入 manifest")


class TestStalePartCleanup(IsolatedRootCase):
    """P3-6：解密前清理同目标的陈旧 `.part`，新文件不碰。"""

    @staticmethod
    def _make_encrypted_db(path: Path):
        from Crypto.Cipher import AES
        from siwx.sqlcipher import PAGE_SZ, RESERVE_SZ, SALT_SZ
        key = bytes(range(32))
        salt = bytes(range(16, 32))
        body_len = PAGE_SZ - RESERVE_SZ

        def encrypt_page(plain: bytes, pageno: int, is_first: bool) -> bytes:
            mac_salt = bytes(b ^ 0x3A for b in salt)
            mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
            iv = bytes((pageno * 7 + i) & 0xFF for i in range(16))
            ct = AES.new(key, AES.MODE_CBC, iv).encrypt(plain)
            page = (salt + ct + iv) if is_first else (ct + iv)
            mac_input = page[len(salt):] if is_first else page
            mac = hmac.new(mac_key, mac_input, hashlib.sha512)
            mac.update(struct.pack("<I", pageno))
            return page + mac.digest()

        page1 = encrypt_page(bytes(body_len - SALT_SZ), 1, True)
        page2 = encrypt_page(bytes(body_len), 2, False)
        path.write_bytes(page1 + page2)
        return key

    def test_old_part_removed_new_part_kept(self):
        from siwx.sqlcipher import decrypt_database
        src = self.tmp / "src.db"
        key = self._make_encrypted_db(src)
        dst = self.tmp / "out" / "dst.db"
        dst.parent.mkdir(parents=True, exist_ok=True)
        old = dst.parent / f"{dst.name}.old.part"
        old.write_bytes(b"stale-half-decrypt")
        two_hours_ago = time.time() - 2 * 3600
        os.utime(old, (two_hours_ago, two_hours_ago))
        fresh = dst.parent / f"{dst.name}.fresh.part"
        fresh.write_bytes(b"in-flight")

        self.assertEqual(decrypt_database(src, dst, key), 2)
        self.assertFalse(old.exists(), "超过 1 小时的陈旧 .part 必须清理")
        self.assertTrue(fresh.exists(), "刚创建/并发中的 .part 不能删")
        self.assertTrue(dst.is_file())


# ── P3-7 头像阶段 sqlite3.Error ─────────────────────────────────

class TestCollectAvatarsCorruptDb(IsolatedRootCase):
    def test_corrupt_head_image_db_returns_empty(self):
        from siwx import exporter
        acc = self.tmp / "output" / ACCOUNT
        (acc / "head_image").mkdir(parents=True)
        (acc / "head_image" / "head_image.db").write_bytes(
            b"this is not a sqlite database" * 64)
        dest = self.tmp / "avatars"
        dest.mkdir()
        mapping = exporter.collect_avatars(acc, [CHAT, "wxid_test_other"], dest)
        self.assertEqual(mapping, {})


# ── P3-8 截断阈值与 manifest 计数 ───────────────────────────────

class TestTruncationManifest(IsolatedRootCase):
    LONG = "长" * 9000

    def setUp(self):
        super().setUp()
        self.acc = make_account(self.tmp, [self.LONG, "第二条"])

    def test_csv_keeps_full_content_and_reports_zero(self):
        res = _export(self.acc, "csv", self.tmp / "exports")
        self.assertEqual(res["truncated"], {"csv": 0})
        text = Path(res["file"]).read_text(encoding="utf-8-sig")
        self.assertIn(self.LONG, text, "CSV 不得截断内容")
        rows = list(csv.reader(text.splitlines()))
        self.assertIn(self.LONG, rows[1])

    def test_html_counts_truncation(self):
        res = _export(self.acc, "html", self.tmp / "exports")
        self.assertEqual(res["truncated"], {"html": 1})
        manifest = json.loads(
            (Path(res["export_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["truncated"], {"html": 1})

    def test_sqlite_counts_truncation(self):
        res = _export(self.acc, "sqlite", self.tmp / "exports")
        self.assertEqual(res["truncated"], {"sqlite": 1})
        manifest = json.loads(
            (Path(res["export_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["truncated"], {"sqlite": 1})
        conn = sqlite3.connect(res["file"])
        try:
            raw = conn.execute(
                "SELECT rawContent FROM messages ORDER BY createTime").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(len(raw), 8000)

    def test_json_writes_zero(self):
        res = _export(self.acc, "json", self.tmp / "exports")
        self.assertEqual(res["truncated"], {"json": 0})


# ── P3-9 JSON→MD 原子生成 ───────────────────────────────────────

class TestConvertAtomic(IsolatedRootCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "export"
        sess = self.root / "01_测试会话"
        sess.mkdir(parents=True)
        self.json_path = sess / "测试会话_20261011_120000_000.json"
        self.json_path.write_text(json.dumps({
            "session": {"wxid": CHAT, "displayName": "测试会话", "type": "私聊",
                        "isGroup": False, "ownerId": ACCOUNT,
                        "firstTimestamp": 1, "lastTimestamp": 2,
                        "messageCount": 1},
            "messages": [{"localId": 1, "platformMessageId": "1",
                          "createTime": 1, "localType": 1, "typeName": "文本",
                          "rawContent": "你好", "content": "你好", "isSend": 0,
                          "senderUsername": CHAT, "senderDisplayName": "测试好友"}],
        }, ensure_ascii=False), encoding="utf-8")
        (sess / "manifest.json").write_text(json.dumps(
            {"generator": "stories-in-wx", "parse": {}, "media": {}},
            ensure_ascii=False), encoding="utf-8")
        self.out = sess / "测试会话_20261011_120000_000.md"

    def test_failed_convert_leaves_no_out_and_no_part(self):
        from siwx import json_export_convert as jec

        def boom(session, msgs, out):
            Path(out).write_text("半截内容", encoding="utf-8")
            raise RuntimeError("转换失败")

        with mock.patch.object(jec, "_convert_md", boom):
            r = jec.convert_dir_report(self.root, ["md"])
        self.assertEqual(r["failed"], 1)
        self.assertFalse(self.out.exists(), "失败不得留下半成品 out")
        self.assertFalse((self.out.parent / (self.out.name + ".part")).exists(),
                         "失败必须删除 .part")

    def test_retry_regenerates_after_failure(self):
        from siwx import json_export_convert as jec

        def boom(session, msgs, out):
            Path(out).write_text("半截内容", encoding="utf-8")
            raise RuntimeError("转换失败")

        with mock.patch.object(jec, "_convert_md", boom):
            jec.convert_dir_report(self.root, ["md"])
        r = jec.convert_dir_report(self.root, ["md"])
        self.assertEqual((r["ok"], r["skipped"], r["failed"]), (1, 0, 0))
        self.assertTrue(self.out.is_file())
        self.assertIn("你好", self.out.read_text(encoding="utf-8"))


# ── P3-10 / P3-11 导出端点 ──────────────────────────────────────

class TestExportOpenPlatform(IsolatedRootCase):
    def setUp(self):
        super().setUp()
        from siwx import paths
        self.root = paths.exports_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "demo.txt"
        self.file.write_text("x", encoding="utf-8")
        self.dir = self.root / "sub"
        self.dir.mkdir()

    @staticmethod
    def _client():
        from siwx.server import app
        return app.test_client()

    def _post(self, path):
        r = self._client().post("/api/export/open", json={"path": str(path)})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    def test_darwin_file_uses_open_r(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("subprocess.Popen") as popen:
            self._post(self.file)
        self.assertEqual(popen.call_args[0][0],
                         ["open", "-R", str(self.file.resolve())])

    def test_darwin_dir_uses_open(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("subprocess.Popen") as popen:
            self._post(self.dir)
        self.assertEqual(popen.call_args[0][0], ["open", str(self.dir.resolve())])

    def test_linux_file_opens_parent_dir(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("subprocess.Popen") as popen:
            self._post(self.file)
        self.assertEqual(popen.call_args[0][0],
                         ["xdg-open", str(self.file.resolve().parent)])

    def test_linux_dir_uses_xdg_open(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("subprocess.Popen") as popen:
            self._post(self.dir)
        self.assertEqual(popen.call_args[0][0], ["xdg-open", str(self.dir.resolve())])

    def test_windows_file_uses_explorer_select(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch("os.startfile", create=True) as startfile:
            self._post(self.file)
        self.assertEqual(popen.call_args[0][0],
                         ["explorer", "/select,", str(self.file.resolve())])
        startfile.assert_not_called()

    def test_windows_dir_uses_startfile(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch("os.startfile", create=True) as startfile:
            self._post(self.dir)
        startfile.assert_called_once_with(str(self.dir.resolve()))
        popen.assert_not_called()


class TestRenderSizeLimit(IsolatedRootCase):
    @staticmethod
    def _client():
        from siwx.server import app
        return app.test_client()

    def test_oversize_file_returns_413(self):
        from siwx import api_export, paths
        root = paths.exports_root()
        root.mkdir(parents=True, exist_ok=True)
        big = root / "big.json"
        big.write_text(json.dumps({"session": {}, "messages": []}) + " " * 512,
                       encoding="utf-8")
        with mock.patch.object(api_export, "MAX_PREVIEW_BYTES", 64):
            r = self._client().post("/api/export/render", json={"path": "big.json"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("文件过大", r.get_json()["error"])

    def test_normal_file_preview_unchanged(self):
        from siwx import paths
        root = paths.exports_root()
        root.mkdir(parents=True, exist_ok=True)
        msgs = [{"localId": i, "content": f"第 {i} 条"} for i in range(25)]
        (root / "ok.json").write_text(json.dumps(
            {"session": {"wxid": CHAT}, "messages": msgs},
            ensure_ascii=False), encoding="utf-8")
        r = self._client().post("/api/export/render", json={"path": "ok.json"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["session"]["wxid"], CHAT)
        self.assertEqual(len(body["preview"]), 20)


# ── P3-12 stats 签名并入 contact.db ─────────────────────────────

class TestStatsSignatureContact(IsolatedRootCase):
    def setUp(self):
        super().setUp()
        self.acc = make_account(self.tmp, ["一条消息"])

    def test_signature_changes_when_contact_db_touched(self):
        from siwx import stats
        before = stats.signature(ACCOUNT)
        self.assertIn("contact/contact.db", [x[0] for x in before])
        cdb = self.acc / "contact" / "contact.db"
        st = cdb.stat()
        os.utime(cdb, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        after = stats.signature(ACCOUNT)
        self.assertNotEqual(before, after,
                            "contact.db 变化（改备注）必须让签名失效")

    def test_disk_cache_invalidated_while_messages_unchanged(self):
        from siwx import stats
        sig_old = stats.signature(ACCOUNT)
        stats._save_disk_cache(ACCOUNT, sig_old, {"total": 1})
        self.assertEqual(stats._load_disk_cache(ACCOUNT, sig_old), {"total": 1})

        cdb = self.acc / "contact" / "contact.db"
        st = cdb.stat()
        os.utime(cdb, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000_000))
        sig_new = stats.signature(ACCOUNT)
        self.assertIsNone(stats._load_disk_cache(ACCOUNT, sig_new),
                          "message 分片未变，但 contact.db 变了，磁盘缓存必须失效")

    def test_missing_contact_db_is_not_added(self):
        from siwx import stats
        (self.acc / "contact" / "contact.db").unlink()
        sig = stats.signature(ACCOUNT)
        self.assertNotIn("contact/contact.db", [x[0] for x in sig],
                         "无 contact.db 时保持旧签名口径（缓存兼容）")


# ── P3-13 解码缓存字节预算 ─────────────────────────────────────

def _row(text: str):
    return (1_700_000_000, 1, 1, 1, 0, 1, text, b"", {})


class TestDecodedCacheBudget(IsolatedRootCase):
    def tearDown(self):
        from siwx import api_chat
        with api_chat._DECODED_CACHE_LOCK:
            api_chat._DECODED_CACHE.clear()
            api_chat._DECODED_CACHE_SIZES.clear()
            api_chat._DECODED_CACHE_BYTES = 0
        super().tearDown()

    def _reset(self):
        from siwx import api_chat
        with api_chat._DECODED_CACHE_LOCK:
            api_chat._DECODED_CACHE.clear()
            api_chat._DECODED_CACHE_SIZES.clear()
            api_chat._DECODED_CACHE_BYTES = 0

    def test_estimate_scales_with_text(self):
        from siwx import api_chat
        small = api_chat._estimate_rows_bytes([_row("a" * 10)])
        big = api_chat._estimate_rows_bytes([_row("a" * 5000)])
        self.assertGreater(big, small)
        self.assertGreaterEqual(small, 1)

    def test_budget_evicts_lru_and_counter_stays_non_negative(self):
        from siwx import api_chat
        rows_a = [_row("a" * 300)]
        rows_b = [_row("b" * 300)]
        one = api_chat._estimate_rows_bytes(rows_a)
        budget = one + 10
        with mock.patch.object(api_chat, "_DECODED_CACHE_MAX_BYTES", budget):
            api_chat._decoded_cache_put(("shard_a",), rows_a)
            self.assertIn(("shard_a",), api_chat._DECODED_CACHE)
            api_chat._decoded_cache_put(("shard_b",), rows_b)
        self.assertNotIn(("shard_a",), api_chat._DECODED_CACHE,
                         "超预算必须按 LRU 淘汰旧分片")
        self.assertIn(("shard_b",), api_chat._DECODED_CACHE)
        self.assertGreaterEqual(api_chat._DECODED_CACHE_BYTES, 0)
        self.assertLessEqual(api_chat._DECODED_CACHE_BYTES, budget)

    def test_single_oversize_shard_is_not_kept(self):
        from siwx import api_chat
        with mock.patch.object(api_chat, "_DECODED_CACHE_MAX_BYTES", 100):
            api_chat._decoded_cache_put(("huge",), [_row("x" * 5000)])
        self.assertEqual(api_chat._DECODED_CACHE_BYTES, 0)
        self.assertNotIn(("huge",), api_chat._DECODED_CACHE)

    def test_same_sig_replace_does_not_double_count(self):
        from siwx import api_chat
        rows = [_row("a" * 100)]
        api_chat._decoded_cache_put(("same",), rows)
        first = api_chat._DECODED_CACHE_BYTES
        api_chat._decoded_cache_put(("same",), rows)
        self.assertEqual(api_chat._DECODED_CACHE_BYTES, first,
                         "同签名替换必须先扣旧占用，不能重复累加")
        self.assertEqual(len(api_chat._DECODED_CACHE_SIZES), 1)


# ── P3-20 账号名 "." / ".." ────────────────────────────────────

class TestAccountDotRejected(IsolatedRootCase):
    def setUp(self):
        super().setUp()
        from siwx import paths
        self.root = paths.out_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker = self.root / "keep.txt"
        self.marker.write_text("keep", encoding="utf-8")

    def test_account_dir_rejects_dot_and_dotdot(self):
        from siwx import validate
        self.assertIsNone(validate.account_dir(".", must_exist=False))
        self.assertIsNone(validate.account_dir("."))
        self.assertIsNone(validate.account_dir(".."))
        self.assertTrue(self.marker.is_file(), "拒绝不能触碰输出根内容")

    def test_clear_output_dot_returns_400_and_root_survives(self):
        from siwx.server import app
        r = app.test_client().post(
            "/api/settings/clear", json={"kind": "output", "wxid": "."})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.root.is_dir())
        self.assertTrue(self.marker.is_file(), "out_root 标记文件必须仍在")

    def test_clear_without_wxid_only_removes_subdirs(self):
        from siwx.server import app
        sub = self.root / "wxid_test_old"
        sub.mkdir()
        (sub / "data.txt").write_text("x", encoding="utf-8")
        r = app.test_client().post(
            "/api/settings/clear", json={"kind": "output"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(sub.exists())
        self.assertTrue(self.root.is_dir())
        self.assertTrue(self.marker.is_file(), "无 wxid 分支只清子目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
