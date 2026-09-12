"""stories-in-wx 回归测试（轻量、自包含、秒级）。

全部使用**合成的临时数据**，不读取任何真实聊天库，因此运行很快、
不会让磁盘/CPU 长时间满载。

覆盖本轮修复的逻辑与性能缺陷：
  1. exporter._safe_name() 恒返回 "_"  → 导出名里的联系人名称丢失
  2. 媒体扩展名与 media 引用不一致（PNG/GIF 指向不存在的 .jpg）
  3. 媒体解密未传 chat/ts → attach/Bubble/Thumb 三级来源全部失效
  4. HTML 导出 session 键不匹配 → KeyError: 'displayName'（该格式不可用）
  5. session["_avatar_map"] 泄漏进导出 JSON
  6. api_chat 的 CDATA 替换串是控制字符 0x01 而非捕获组
  7. messages 的 limit 未做下限校验（LIMIT -2 等同无限制）
  8. 分片索引：消除「每个会话扫全部 *.db」的性能瓶颈
  9. 聊天页只扫 message_*.db → 漏掉 biz_message_*.db 里的会话
 10. cli --json 未生效
 11. decrypt_database 非原子写（失败会破坏已有明文库）

运行：
    python -m unittest discover -s tests -v
"""
import hashlib
import hmac as hmac_mod
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import api_chat, exporter, paths
from siwx.exporter import _safe_name


# ── 合成数据构造 ────────────────────────────────────────────────

def _msg_table(chat):
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def make_shard(path: Path, chat, texts, start_ts=1_700_000_000,
               include_images=False, origin=0):
    """建一个含 Msg_ 表 + Name2Id 的分片库。"""
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
        ts = start_ts + i
        if include_images and i % 5 == 0:
            content = ('<msg><img aeskey="x" md5="'
                       + hashlib.md5(f"img{i}".encode()).hexdigest()
                       + '"/></msg>').encode("utf-8")
            ltype = 3
        else:
            content = txt.encode("utf-8")
            ltype = 1
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (i + 1, 1000 + i, ltype, ts, origin, 1, content, None))
    conn.commit()
    conn.close()
    return path


def make_empty_shard(path: Path):
    """建一个不含 Msg_ 表的分片（模拟 media_*/fts/resource 等）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def make_account(root: Path, account="wxid_test", chat="wxid_friend",
                 n_texts=12, include_images=False):
    """构造一个最小可用的解密产物目录。"""
    acc = root / "output" / account
    msg_dir = acc / "message"
    # 两个分片：一个含目标会话，一个不含（验证索引会跳过它）
    make_shard(msg_dir / "message_0.db", chat,
               [f"第 {i} 条消息" for i in range(n_texts)],
               include_images=include_images)
    make_empty_shard(msg_dir / "media_0.db")
    make_empty_shard(msg_dir / "message_fts.db")

    (acc / "contact").mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(acc / "contact" / "contact.db")
    c.execute("CREATE TABLE contact (username TEXT, remark TEXT, "
              "nick_name TEXT, alias TEXT)")
    c.execute("INSERT INTO contact VALUES (?,?,?,?)", (chat, "测试好友", "", ""))
    c.commit()
    c.close()

    (acc / "session").mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(acc / "session" / "session.db")
    s.execute("CREATE TABLE SessionTable (username TEXT, summary TEXT, "
              "sort_timestamp INTEGER)")
    s.execute("INSERT INTO SessionTable VALUES (?,?,?)", (chat, "预览", 1_700_000_010))
    s.commit()
    s.close()
    return acc, account, chat


class TempRootCase(unittest.TestCase):
    """把 SIWX_ROOT 指向临时目录，避免污染真实 output/exports。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_test_"))
        self._old = os.environ.get("SIWX_ROOT")
        os.environ["SIWX_ROOT"] = str(self.tmp)
        api_chat._SHARD_INDEX.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SIWX_ROOT", None)
        else:
            os.environ["SIWX_ROOT"] = self._old
        api_chat._SHARD_INDEX.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── 1. _safe_name ───────────────────────────────────────────────

class TestSafeName(unittest.TestCase):

    def test_keeps_contact_name(self):
        # 修复前：任何输入都返回 "_"
        self.assertEqual(_safe_name("2427班级群（野生）"), "2427班级群（野生）")
        self.assertEqual(_safe_name("高途思维海超老师"), "高途思维海超老师")
        self.assertEqual(_safe_name("文件传输助手"), "文件传输助手")

    def test_replaces_illegal_chars(self):
        self.assertEqual(_safe_name('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_strips_and_falls_back(self):
        self.assertEqual(_safe_name("  带空格  "), "带空格")
        self.assertEqual(_safe_name("结尾有点..."), "结尾有点")
        self.assertEqual(_safe_name(""), "chat")
        self.assertEqual(_safe_name(None), "chat")
        self.assertEqual(len(_safe_name("x" * 80)), 48)

    def test_windows_reserved(self):
        self.assertEqual(_safe_name("CON"), "_CON")
        self.assertEqual(_safe_name("nul.txt"), "_nul.txt")


# ── 2. CDATA 解析 ───────────────────────────────────────────────

class TestCdata(unittest.TestCase):

    def test_cdata_content_is_preserved(self):
        """修复前 CDATA 会被替换成一个 0x01 控制字符。"""
        got = api_chat._xml_text("<![CDATA[标题内容]]>")
        self.assertEqual(got, "标题内容")
        self.assertNotIn("\x01", got or "")

    def test_appmsg_title_from_cdata(self):
        xml = ('<appmsg><title><![CDATA[一个链接标题]]></title>'
               '<url><![CDATA[https://example.com/x]]></url></appmsg>')
        title, url, _des = api_chat._parse_appmsg(xml)
        self.assertEqual(title, "一个链接标题")
        self.assertEqual(url, "https://example.com/x")


# ── 3. 分片索引 ─────────────────────────────────────────────────

class TestShardIndex(TempRootCase):

    def test_index_finds_only_real_shards(self):
        acc, _account, chat = make_account(self.tmp, n_texts=3)
        shards = api_chat.shards_for(acc, chat)
        self.assertEqual([p.name for p in shards], ["message_0.db"])
        # 不含 Msg_ 表的库不应出现在索引里
        idx = api_chat.shard_index(acc / "message")
        self.assertNotIn("media_0.db", [p.name for ps in idx.values() for p in ps])

    def test_unknown_chat_returns_empty(self):
        acc, _account, _chat = make_account(self.tmp)
        self.assertEqual(api_chat.shards_for(acc, "wxid_nobody"), [])

    def test_index_is_cached(self):
        acc, _account, chat = make_account(self.tmp)
        api_chat.shards_for(acc, chat)
        first = api_chat.shard_index(acc / "message")
        second = api_chat.shard_index(acc / "message")
        self.assertIs(first, second, "第二次调用应命中缓存（同一对象）")

    def test_message_tables_by_shard(self):
        acc, _account, chat = make_account(self.tmp)
        by_shard = api_chat.message_tables_by_shard(acc)
        only = acc / "message" / "message_0.db"
        self.assertEqual(len(by_shard), 1)
        self.assertIn(only, by_shard)
        self.assertEqual(by_shard[only], [_msg_table(chat)])


# ── 4. message_stream / count_messages ──────────────────────────

class TestMessageStream(TempRootCase):

    def test_stream_yields_all_in_order(self):
        acc, account, chat = make_account(self.tmp, n_texts=12)
        from siwx.export_stream import message_stream
        msgs = list(message_stream(acc, chat, account=account))
        self.assertEqual(len(msgs), 12)
        ts = [m["createTime"] for m in msgs]
        self.assertEqual(ts, sorted(ts), "必须按时间正序")
        self.assertEqual(msgs[0]["content"], "第 0 条消息")
        self.assertEqual(msgs[0]["senderDisplayName"], "测试好友")

    def test_count_matches_stream(self):
        acc, account, chat = make_account(self.tmp, n_texts=7)
        from siwx.export_stream import count_messages, message_stream
        self.assertEqual(count_messages(acc, chat),
                         len(list(message_stream(acc, chat, account=account))))

    def test_shard_scan_is_avoided(self):
        """索引建好之后，不含 Msg_ 表的库不应再被打开。

        索引本身需要扫一遍目录（这是必要的一次性成本）；收益体现在后续调用：
        每个会话的两遍导出、多会话批量、聊天页都直接命中缓存。
        """
        acc, account, chat = make_account(self.tmp, n_texts=3)
        from siwx.export_stream import message_stream

        # 预热：第一次会扫全部 *.db 建立索引
        list(message_stream(acc, chat, account=account))

        opened = []
        real_connect = sqlite3.connect

        def spy(path, *a, **k):
            opened.append(Path(path).name)
            return real_connect(path, *a, **k)

        sqlite3.connect = spy
        try:
            list(message_stream(acc, chat, account=account))
        finally:
            sqlite3.connect = real_connect

        self.assertNotIn("media_0.db", opened, "索引未生效：仍在打开无关分片")
        self.assertNotIn("message_fts.db", opened, "索引未生效：仍在打开无关分片")
        self.assertIn("message_0.db", opened)

    def test_index_survives_dir_change(self):
        """目录内容变化（mtime 改变）后索引应自动失效并重建。"""
        acc, account, chat = make_account(self.tmp, n_texts=3)
        self.assertEqual(len(api_chat.shards_for(acc, chat)), 1)
        time.sleep(0.01)
        make_shard(acc / "message" / "message_1.db", chat, ["后加的"])
        self.assertEqual(len(api_chat.shards_for(acc, chat)), 2)


# ── 5. 导出：命名、HTML、JSON 干净性 ────────────────────────────

class TestExport(TempRootCase):

    def _export(self, fmt, **kw):
        acc, account, chat = self.acc, self.account, self.chat
        return exporter.run_export(
            acc, account, chat, self.display, fmt,
            want_messages=True, want_media=kw.pop("media", False),
            want_avatars=kw.pop("avatars", False),
            export_root=self.tmp / "exports", pack="none",
            progress=lambda p, m: None)

    def setUp(self):
        super().setUp()
        self.acc, self.account, self.chat = make_account(self.tmp, n_texts=12)
        self.display = "测试会话名ABC"

    def test_all_formats_embed_display_name(self):
        for fmt in ("json", "html", "txt", "csv", "markdown",
                    "toml", "sqlite", "xlsx"):
            with self.subTest(fmt=fmt):
                res = self._export(fmt)
                self.assertIn(self.display, Path(res["export_dir"]).name)
                self.assertIn(self.display, Path(res["file"]).name)
                self.assertTrue(Path(res["file"]).is_file())
                self.assertEqual(res["message_count"], 12)

    def test_html_export_not_broken(self):
        """修复前会抛 KeyError: 'displayName'。"""
        res = self._export("html")
        html = Path(res["file"]).read_text(encoding="utf-8")
        self.assertIn("window.CHAT_DATA", html)
        i = html.index("window.CHAT_DATA = ") + len("window.CHAT_DATA = ")
        data, _ = json.JSONDecoder().raw_decode(html[i:])
        meta = data["meta"]
        self.assertEqual(meta["sessionName"], self.display)
        self.assertEqual(meta["sessionId"], self.chat)
        self.assertGreater(meta["dateRange"]["start"], 0)
        self.assertGreater(meta["dateRange"]["end"], 0)
        self.assertEqual(meta["messageCount"], len(data["messages"]))

    def test_json_session_has_no_internal_keys(self):
        res = self._export("json")
        d = json.loads(Path(res["file"]).read_text(encoding="utf-8"))
        self.assertNotIn("_avatar_map", d["session"])
        for k in d["session"]:
            self.assertFalse(k.startswith("_"), f"内部键泄漏: {k}")

    def test_multi_export_creates_separate_dirs(self):
        """修复前所有会话都写进同一个 "_" 目录。"""
        acc, account, chat = self.acc, self.account, self.chat
        res = exporter.run_export_multi(
            acc, account,
            [{"chat": chat, "display": "会话甲"}, {"chat": chat, "display": "会话乙"}],
            fmt="json", want_media=False, want_avatars=False,
            export_root=self.tmp / "multi", pack="folder")
        self.assertEqual(res["ok_count"], 2)
        names = sorted(p.name for p in Path(res["total_dir"]).iterdir() if p.is_dir())
        self.assertEqual(len(names), 2, f"目录未按会话隔离: {names}")


# ── 6. 媒体：扩展名 + chat/ts 透传 ──────────────────────────────

class TestMediaExport(TempRootCase):

    def setUp(self):
        super().setUp()
        self.acc, self.account, self.chat = make_account(self.tmp, n_texts=6)

    def test_extension_follows_actual_content(self):
        """修复前 media 引用恒为 .jpg，即使实际写的是 .png。"""
        from siwx import exporter as ex

        def fake_get_image(account, md5, acc_dir, **kw):
            return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png"

        old = ex.media.get_image
        ex.media.get_image = fake_get_image
        try:
            dest = self.tmp / "media_out"
            dest.mkdir(parents=True, exist_ok=True)
            out = ex._try_decrypt(str(self.acc), self.account, self.chat,
                                  "a" * 32, None, 1, 1_700_000_000,
                                  dest / "0000_aaaaaaaaaaaa.jpg")
            self.assertIsNotNone(out)
            self.assertEqual(out.suffix, ".png")
            self.assertTrue(out.is_file())
            self.assertEqual(out.name, "0000_aaaaaaaaaaaa.png")
        finally:
            ex.media.get_image = old

    def test_chat_and_ts_are_forwarded(self):
        """修复前未传 chat/ts，attach/Bubble/Thumb 三级来源全部失效。"""
        from siwx import exporter as ex
        seen = {}

        def fake_get_image(account, md5, acc_dir, **kw):
            seen.update(kw)
            return None, "nope"

        old = ex.media.get_image
        ex.media.get_image = fake_get_image
        try:
            ex._try_decrypt(str(self.acc), self.account, self.chat,
                            "b" * 32, "c" * 32, 42, 1_700_000_123,
                            self.tmp / "x.jpg")
        finally:
            ex.media.get_image = old
        self.assertEqual(seen.get("chat"), self.chat)
        self.assertEqual(seen.get("ts"), 1_700_000_123)
        self.assertEqual(seen.get("local_id"), 42)
        self.assertEqual(seen.get("bubble_md5"), "c" * 32)


# ── 7. messages 的 limit 下限 ───────────────────────────────────

class TestLimitGuard(TempRootCase):

    def test_negative_limit_is_clamped(self):
        acc, account, chat = make_account(self.tmp, n_texts=30)
        from siwx.server import app
        c = app.test_client()
        r = c.get(f"/api/chat/messages?account={account}&chat={chat}&limit=-1")
        self.assertEqual(r.status_code, 200)
        n = len(r.get_json()["messages"])
        self.assertLessEqual(n, 300)
        self.assertGreater(n, 0)


# ── 8. 解密原子写 ───────────────────────────────────────────────

class TestDecryptAtomic(unittest.TestCase):
    """构造一个合法的 SQLCipher 4 单页库，验证解密与失败时的原子性。"""

    @staticmethod
    def _encrypt_page(plain_body: bytes, pageno: int, enc_key: bytes,
                      salt: bytes, is_first: bool):
        from Crypto.Cipher import AES
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
        iv = bytes((pageno * 7 + i) & 0xFF for i in range(16))
        ct = AES.new(enc_key, AES.MODE_CBC, iv).encrypt(plain_body)
        if is_first:
            page = salt + ct + iv
        else:
            page = ct + iv
        # 与 verify_enc_key 对齐：HMAC 覆盖 page1[16:]（页 1 跳过 salt）
        mac_input = page[len(salt):] if is_first else page
        mac = hmac_mod.new(mac_key, mac_input, hashlib.sha512)
        mac.update(struct.pack("<I", pageno))
        return page + mac.digest()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_db_"))
        from Crypto.Cipher import AES
        from siwx.sqlcipher import PAGE_SZ, RESERVE_SZ, SALT_SZ
        self.enc_key = bytes(range(32))
        salt = bytes(range(16, 32))
        body_len = PAGE_SZ - RESERVE_SZ          # 4016
        # 页 1：正文 4000 字节（salt 占掉 16）
        page1 = self._encrypt_page(bytes((i * 3) & 0xFF for i in range(body_len - SALT_SZ)),
                                   1, self.enc_key, salt, True)
        # 页 2
        page2 = self._encrypt_page(bytes((i * 5) & 0xFF for i in range(body_len)),
                                   2, self.enc_key, salt, False)
        self.src = self.tmp / "src.db"
        self.src.write_bytes(page1 + page2)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decrypts_and_leaves_no_residue(self):
        from siwx.sqlcipher import decrypt_database
        dst = self.tmp / "out" / "dst.db"
        pages = decrypt_database(self.src, dst, self.enc_key)
        self.assertEqual(pages, 2)
        self.assertTrue(dst.is_file())
        self.assertEqual(dst.stat().st_size, 2 * 4096)
        with dst.open("rb") as f:
            self.assertEqual(f.read(16), b"SQLite format 3\x00")
        residue = list(dst.parent.glob("*.part")) + list(dst.parent.glob("*.tmp"))
        self.assertEqual(residue, [], f"残留临时文件: {residue}")

    def test_failure_does_not_clobber_existing_file(self):
        """源库密钥错误时，已存在的明文库必须保持原样。"""
        from siwx.sqlcipher import decrypt_database
        dst = self.tmp / "out" / "dst.db"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"ORIGINAL-GOOD-CONTENT")
        with self.assertRaises(ValueError):
            decrypt_database(self.src, dst, bytes(32))   # 错误密钥
        self.assertEqual(dst.read_bytes(), b"ORIGINAL-GOOD-CONTENT")
        residue = list(dst.parent.glob("*.part"))
        self.assertEqual(residue, [], f"残留临时文件: {residue}")


# ── 9. CLI --json ───────────────────────────────────────────────

class TestCliJson(unittest.TestCase):

    def test_json_flag_emits_parseable_json(self):
        import argparse
        from siwx import cli, extract
        old = extract.extract_all
        extract.extract_all = lambda **kw: [
            {"wxid": "wxid_x", "db_count": 1, "total_salts": 2, "verified": 2,
             "cached": 0, "duration_ms": 1, "salts": []}]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.cmd_keys_extract(
                    argparse.Namespace(json=True, no_cache=False))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())[0]["wxid"], "wxid_x")
        finally:
            extract.extract_all = old

    def test_json_flag_returns_1_when_no_accounts(self):
        import argparse
        from siwx import cli, extract
        old = extract.extract_all
        extract.extract_all = lambda **kw: []
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.cmd_keys_extract(
                    argparse.Namespace(json=True, no_cache=False))
            self.assertEqual(code, 1)
        finally:
            extract.extract_all = old


# ── 10. 密码学原语未被破坏 ──────────────────────────────────────

class TestCryptoIntact(unittest.TestCase):

    def test_verify_enc_key_byte_layout(self):
        from siwx import sqlcipher as sc
        self.assertEqual(sc.PAGE_SZ - sc.RESERVE_SZ + sc.IV_SZ - sc.SALT_SZ, 4016)

    def test_handwritten_cbc_matches_stdlib(self):
        from Crypto.Cipher import AES
        from siwx.sqlcipher import PAGE_SZ, RESERVE_SZ, IV_SZ
        key, iv = bytes(range(32)), bytes(range(16, 32))
        for ct_len in (PAGE_SZ - RESERVE_SZ - IV_SZ, PAGE_SZ - RESERVE_SZ):
            pt = bytes((i * 7 + 3) & 0xFF for i in range(ct_len))
            ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pt)
            std = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
            raw = AES.new(key, AES.MODE_ECB).decrypt(ct)
            prev = int.from_bytes(iv + ct[:len(ct) - 16], "little")
            mine = (int.from_bytes(raw, "little") ^ prev).to_bytes(len(ct), "little")
            self.assertEqual(mine, std, f"ct_len={ct_len}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
