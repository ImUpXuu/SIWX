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
import contextlib
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
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 宿主回归测试必须与"用户装了什么插件"解耦：插件可以（合法地）改写会话列表、
# 消息形状与导出产物，若参与本套件会让宿主行为断言变得不确定。
# 故在导入 siwx 之前强制零插件模式；插件自身的测试见 tests/test_plugins.py。
os.environ["SIWX_NO_PLUGINS"] = "1"

from siwx import api_chat, exporter, media, paths
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
    """把 SIWX_ROOT 指向临时目录，避免污染真实 output/exports。

    同时强制"零插件"状态：本套件断言的是宿主默认行为，若 `unittest discover`
    先跑了 tests/test_plugins.py，模块级 registry 单例里会残留合成插件的 hook
    （会话过滤器会剔掉公众号、渲染器会改写 kind），必须在此清空。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_test_"))
        self._old = os.environ.get("SIWX_ROOT")
        os.environ["SIWX_ROOT"] = str(self.tmp)
        os.environ["SIWX_NO_PLUGINS"] = "1"
        self._clear_plugins()
        api_chat._SHARD_INDEX.clear()
        api_chat._CONTACT_CACHE.clear()
        api_chat._SESSION_CACHE.clear()
        paths._PATH_CACHE.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SIWX_ROOT", None)
        else:
            os.environ["SIWX_ROOT"] = self._old
        os.environ["SIWX_NO_PLUGINS"] = "1"
        self._clear_plugins()
        api_chat._SHARD_INDEX.clear()
        api_chat._CONTACT_CACHE.clear()
        api_chat._SESSION_CACHE.clear()
        paths._PATH_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_plugins() -> None:
        """清空模块级 registry（插件测试可能留下合成 hook）。"""
        try:
            from siwx.plugins.registry import registry
        except Exception:
            return
        for ns in vars(registry).values():
            if hasattr(ns, "items") and isinstance(ns.items, list):
                ns.items = []
        registry.metas = {}
        registry.report = None
        registry._loaded = True       # 已加载但为空 == 零插件


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

class TestSessionsApi(TempRootCase):

    def test_filters_ghost_sessions_and_marks_official_accounts(self):
        acc, account, chat = make_account(self.tmp, n_texts=3)
        sdb = acc / "session" / "session.db"
        conn = sqlite3.connect(sdb)
        conn.executemany("INSERT INTO SessionTable VALUES (?,?,?)", [
            ("gh_live", "公众号摘要", 1_700_000_100),
            ("gh_empty", "", 0),
            ("brandsessionholder", "聚合入口", 1_700_000_200),
            ("@placeholder_foldgroup", "占位入口", 1_700_000_201),
        ])
        conn.commit(); conn.close()
        cdb = acc / "contact" / "contact.db"
        conn = sqlite3.connect(cdb)
        conn.execute("INSERT INTO contact VALUES (?,?,?,?)",
                     ("gh_live", "公众号A", "", ""))
        conn.commit(); conn.close()

        from siwx.server import app
        data = app.test_client().get(f"/api/chat/sessions?account={account}").get_json()
        usernames = {s["username"]: s for s in data["sessions"]}
        self.assertIn(chat, usernames)
        self.assertIn("gh_live", usernames)
        self.assertTrue(usernames["gh_live"]["is_official"])
        self.assertEqual(usernames["gh_live"]["kind"], "official")
        self.assertEqual(usernames["gh_live"]["display"], "公众号A")
        self.assertNotIn("gh_empty", usernames)
        self.assertNotIn("brandsessionholder", usernames)
        self.assertNotIn("@placeholder_foldgroup", usernames)

    def test_sessions_api_uses_cache_after_first_call(self):
        acc, account, _chat = make_account(self.tmp, n_texts=3)
        from siwx.server import app
        client = app.test_client()
        self.assertEqual(client.get(f"/api/chat/sessions?account={account}").status_code, 200)

        opened = []
        real_connect = sqlite3.connect
        def spy(path, *a, **k):
            opened.append(Path(path).name)
            return real_connect(path, *a, **k)
        sqlite3.connect = spy
        try:
            r = client.get(f"/api/chat/sessions?account={account}")
        finally:
            sqlite3.connect = real_connect
        self.assertEqual(r.status_code, 200)
        self.assertEqual(opened, [], "会话列表缓存命中时不应再打开 contact/session 数据库")


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

    def test_multi_format_export_writes_all_files(self):
        """fmt 传列表时应一次写出多种格式，统计与清单口径不翻倍。"""
        res = self._export(["json", "html"])
        files = [Path(f) for f in res["files"]]
        self.assertEqual([f.suffix for f in files], [".json", ".html"])
        for f in files:
            self.assertTrue(f.is_file(), f"缺产物: {f}")
        # written_count = 两种格式之和；message_count 仍是会话真实条数
        self.assertEqual(res["message_count"], 24)
        manifest = json.loads(
            (Path(res["export_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 2)
        # 解析统计按消息数计（12 条），不随格式数翻倍
        self.assertEqual(sum(manifest["parse"]["type_counts"].values()), 12)

    def test_multi_format_via_run_export_multi(self):
        """run_export_multi 透传格式列表。"""
        acc, account, chat = self.acc, self.account, self.chat
        res = exporter.run_export_multi(
            acc, account, [{"chat": chat, "display": self.display}],
            fmt=["json", "txt"], want_media=False, want_avatars=False,
            export_root=self.tmp / "multi_fmt", pack="folder")
        self.assertEqual(res["ok_count"], 1)
        sess = res["sessions"][0]
        self.assertEqual(len(sess["files"]), 2)
        self.assertTrue(all(Path(f).is_file() for f in sess["files"]))


# ── 6. 媒体：扩展名 + chat/ts 透传 ──────────────────────────────

class TestVoiceMedia(TempRootCase):

    def test_parse_voice_meta_and_read_silk_blob(self):
        from siwx import voice
        acc, account, chat = make_account(self.tmp, n_texts=1)
        db = acc / "message" / "media_0.db"
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE unrelated")
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
        conn.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER, "
                     "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT)")
        raw = b"\x02#!SILK_V3\x00\x01voice"
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                     (1, 1700000000, 9, 123456789, raw, "0"))
        conn.commit(); conn.close()
        meta = voice.parse_voice_meta('<msg><voicemsg voicelength="2429" length="3990" voiceformat="4" /></msg>')
        self.assertEqual(meta["durationMs"], 2429)
        body, info = voice.get_voice(acc, chat=chat, local_id=9, svr_id=123456789, ts=1700000000)
        self.assertEqual(body, b"#!SILK_V3\x00\x01voice")
        self.assertEqual(info["silkOffset"], 1)

    def test_voice_api_serves_silk(self):
        acc, account, chat = make_account(self.tmp, n_texts=1)
        db = acc / "message" / "media_0.db"
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE unrelated")
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
        conn.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER, "
                     "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT)")
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                     (1, 1700000000, 9, 123456789, b"\x02#!SILK_V3abc", "0"))
        conn.commit(); conn.close()
        from siwx.server import app
        r = app.test_client().get(
            f"/api/chat/media/voice?account={account}&chat={chat}&local_id=9&svr_id=123456789&ts=1700000000")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"#!SILK_V3abc")
        self.assertEqual(r.headers.get("X-SIWX-Voice-Format"), "silk")

    def test_pcm_to_wav_uses_stdlib_container(self):
        from siwx import voice
        wav = voice.pcm_to_wav(b"\x00\x00\x01\x00", sample_rate=24000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])
        self.assertGreater(len(wav), 44)

    def test_transcode_prefers_pilk_backend(self):
        from siwx import voice
        seen = []
        old_pilk = voice._decode_silk_to_pcm_with_pilk
        old_cmd = voice._decode_silk_to_pcm_with_command
        try:
            voice._decode_silk_to_pcm_with_pilk = lambda data: (seen.append("pilk") or b"\x00\x00", "", "pilk")
            voice._decode_silk_to_pcm_with_command = lambda data, rate: (seen.append("cmd") or b"\x01\x00", "", "cmd")
            body, meta = voice.transcode_voice(b"#!SILK_V3abc", "wav")
        finally:
            voice._decode_silk_to_pcm_with_pilk = old_pilk
            voice._decode_silk_to_pcm_with_command = old_cmd
        self.assertEqual(seen, ["pilk"])
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertEqual(meta["engine"], "pilk")

    def test_bundled_decoder_path_is_available_as_fallback(self):
        from siwx import voice
        vendor = self.tmp / "siwx" / "vendor" / "silk-decoder" / "windows"
        vendor.mkdir(parents=True, exist_ok=True)
        exe = vendor / ("silk_v3_decoder.exe" if os.name == "nt" else "silk_v3_decoder")
        exe.write_bytes(b"fake")
        old_roots = voice._resource_roots
        try:
            voice._resource_roots = lambda: [self.tmp / "siwx"]
            candidates = voice._decoder_candidates()
        finally:
            voice._resource_roots = old_roots
        self.assertTrue(candidates)
        self.assertEqual(candidates[0][1], [str(exe)])
        self.assertTrue(candidates[0][0].startswith("bundled:"))

    def test_voice_api_transcodes_wav_when_decoder_available(self):
        acc, account, chat = make_account(self.tmp, n_texts=1)
        db = acc / "message" / "media_0.db"
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE unrelated")
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
        conn.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER, "
                     "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT)")
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                     (1, 1700000000, 9, 123456789, b"\x02#!SILK_V3abc", "0"))
        conn.commit(); conn.close()
        from siwx import voice
        from siwx.server import app
        old = voice.transcode_voice
        try:
            voice.transcode_voice = lambda data, target="wav": (
                b"RIFFxxxxWAVEfmt ", {"format": "wav", "mimetype": "audio/wav", "ext": "wav", "engine": "fake"})
            r = app.test_client().get(
                f"/api/chat/media/voice?account={account}&chat={chat}&local_id=9&svr_id=123456789&ts=1700000000&format=wav")
        finally:
            voice.transcode_voice = old
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/wav")
        self.assertEqual(r.headers.get("X-SIWX-Voice-Format"), "wav")
        self.assertEqual(r.headers.get("X-SIWX-Voice-Transcoder"), "fake")


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
            out, reason = ex._try_decrypt(str(self.acc), self.account, self.chat,
                                          "a" * 32, None, 1, 1_700_000_000,
                                          dest / "0000_aaaaaaaaaaaa.jpg")
            self.assertIsNotNone(out)
            self.assertEqual(reason, "")
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

    def _add_voice_message(self, local_id=99, svr_id=123456789, ts=1_700_000_099):
        t = _msg_table(self.chat)
        conn = sqlite3.connect(self.acc / "message" / "message_0.db")
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (local_id, svr_id, 34, ts, 0, 1,
                      b'<msg><voicemsg voicelength="1000" length="12" /></msg>', None))
        conn.commit(); conn.close()

        db = self.acc / "message" / "media_0.db"
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE unrelated")
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (self.chat,))
        conn.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER, "
                     "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT)")
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                     (1, ts, local_id, svr_id, b"\x02#!SILK_V3abc", "0"))
        conn.commit(); conn.close()

    def test_export_includes_transcoded_voice_media(self):
        from siwx import exporter as ex
        self._add_voice_message()
        old = ex.voice.transcode_voice
        try:
            ex.voice.transcode_voice = lambda data, target="wav": (
                b"RIFFxxxxWAVEfmt ", {"format": "wav", "mimetype": "audio/wav", "ext": "wav", "engine": "fake"})
            res = ex.run_export(self.acc, self.account, self.chat, "测试好友",
                                fmt="html", want_media=False, want_voice=True,
                                want_avatars=False,
                                export_root=self.tmp / "exports", pack="none")
        finally:
            ex.voice.transcode_voice = old
        html = Path(res["file"]).read_text(encoding="utf-8")
        self.assertEqual(res["voice_count"], 1)
        self.assertTrue((Path(res["file"]).parent / "media" / "voice_0000_99.wav").is_file())
        self.assertIn("voice_0000_99.wav", html)

    def test_all_formats_can_reference_exported_voice(self):
        from siwx import exporter as ex
        self._add_voice_message(local_id=77, svr_id=777, ts=1_700_000_077)
        old = ex.voice.transcode_voice
        try:
            ex.voice.transcode_voice = lambda data, target="wav": (
                b"RIFFxxxxWAVEfmt ", {"format": "wav", "mimetype": "audio/wav", "ext": "wav", "engine": "fake"})
            for fmt in ("json", "html", "txt", "csv", "markdown", "toml", "sqlite", "xlsx"):
                with self.subTest(fmt=fmt):
                    res = ex.run_export(self.acc, self.account, self.chat, "测试好友",
                                        fmt=fmt, want_media=False, want_voice=True,
                                        want_avatars=False, export_root=self.tmp / "voice_formats",
                                        folder_name=f"voice_{fmt}", pack="none")
                    out_file = Path(res["file"])
                    voice_file = out_file.parent / "media" / "voice_0000_77.wav"
                    self.assertTrue(voice_file.is_file())
                    self.assertEqual(res["voice_count"], 1)
                    if fmt == "sqlite":
                        conn = sqlite3.connect(out_file)
                        vals = [r[0] for r in conn.execute("SELECT mediaFile FROM messages WHERE mediaFile IS NOT NULL")]
                        conn.close()
                        self.assertIn("media/voice_0000_77.wav", vals)
                    elif fmt == "xlsx":
                        from openpyxl import load_workbook
                        wb = load_workbook(out_file, read_only=True)
                        vals = [row[-1] for row in wb.active.iter_rows(values_only=True)]
                        self.assertIn("media/voice_0000_77.wav", vals)
                    else:
                        self.assertIn("voice_0000_77.wav", out_file.read_text(encoding="utf-8"))
        finally:
            ex.voice.transcode_voice = old


# ── 7. messages 的 limit 下限 ───────────────────────────────────

class TestAvatarApi(TempRootCase):

    def test_owner_avatar_falls_back_to_clean_wxid(self):
        """输出目录名可能带 _数字后缀，但头像库里本人是原始 wxid。"""
        acc, account, _chat = make_account(self.tmp, account="wxid_owner_1234", n_texts=1)
        (acc / "head_image").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(acc / "head_image" / "head_image.db")
        conn.execute("CREATE TABLE head_image (username TEXT PRIMARY KEY, md5 TEXT, image_buffer BLOB, update_time INTEGER)")
        conn.execute("INSERT INTO head_image VALUES (?,?,?,?)",
                     ("wxid_owner", "m", b"JPEGDATA", 1))
        conn.commit(); conn.close()
        from siwx.server import app
        r = app.test_client().get("/api/chat/avatar?account=wxid_owner_1234&username=wxid_owner_1234")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"JPEGDATA")


class TestSettingsAutoSync(TempRootCase):

    def test_auto_sync_settings_roundtrip(self):
        from siwx.server import app
        c = app.test_client()
        r = c.post("/api/settings/auto-sync", json={"enabled": True, "interval_minutes": 5})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["enabled"])
        self.assertEqual(r.get_json()["interval_minutes"], 5)
        r2 = c.get("/api/settings/auto-sync")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["interval_minutes"], 5)

    def test_auto_sync_interval_is_clamped(self):
        from siwx.server import app
        r = app.test_client().post("/api/settings/auto-sync", json={"enabled": True, "interval_minutes": 99999})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["interval_minutes"], 1440)


class TestStatsApi(TempRootCase):
    """聊天统计：跨分片聚合、类型分布、时间维度、缓存与过滤。"""

    @staticmethod
    def _make_stats_account(root: Path, account="wxid_stats"):
        """构造含多分片、多类型、多发送者的统计样本。"""
        acc = root / "output" / account
        msg_dir = acc / "message"
        msg_dir.mkdir(parents=True, exist_ok=True)
        # 固定基准时间（本地时区正午）：任何时区下这 6 条消息的本地日期都是
        # 2023-11-15。此前硬编码 epoch（=2023-11-15 06:13 +08:00），在 UTC 的
        # CI 上跨到 11-14/11-15 两天，日期过滤用例把 6 条过滤成了 1 条。
        from datetime import datetime
        base = int(datetime(2023, 11, 15, 12, 0, 0).timestamp())

        def shard(name, chat, rows):
            conn = sqlite3.connect(msg_dir / name)
            t = _msg_table(chat)
            conn.execute(f"""CREATE TABLE [{t}] (
                local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
                create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
                message_content BLOB, packed_info_data BLOB)""")
            for i, (ltype, ts, sid) in enumerate(rows):
                conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                             (i + 1, 100 + i, ltype, ts, 0, sid, b"x", None))
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
            conn.commit()
            conn.close()

        # 分片 0：文本 + 图片，发送者 1
        shard("message_0.db", "wxid_a",
              [(1, base, 1), (1, base + 3600, 1), (3, base + 7200, 1)])
        # 分片 1：表情 + 语音，发送者 2
        shard("message_1.db", "wxid_b",
              [(47, base + 100, 2), (34, base + 200, 2)])
        # 分片 2：系统消息 + 一年前的文本
        shard("message_2.db", "wxid_c",
              [(10000, base + 300, 0), (1, base - 400 * 86400, 1)])

        # 三个不同会话 → chat_count 应为 3；联系人库用于验证排行显示昵称。
        (acc / "contact").mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(acc / "contact" / "contact.db")
        c.execute("CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, "
                  "alias TEXT, verify_flag INTEGER)")
        c.executemany("INSERT INTO contact VALUES (?,?,?,?,?)", [
            ("wxid_a", "好友A", "", "", 0),
            ("wxid_b", "", "好友B", "", 0),
            ("wxid_c", "", "好友C", "", 0),
            ("gh_news", "", "公众号", "news_alias", 1053),
            ("group@chatroom", "", "群聊", "", 0),
        ])
        c.commit()
        c.close()
        return acc, account

    def test_overview_totals_and_types(self):
        self._make_stats_account(self.tmp)
        from siwx.server import app
        r = app.test_client().get("/api/stats/overview?account=wxid_stats")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["total"], 7)
        self.assertEqual(d["chat_count"], 3)
        self.assertEqual(d["shards"], 3)
        # 类型分组：文本 3、图片 1、表情 1、语音 1、系统 1（合计 7）
        groups = {g["label"]: g["count"] for g in d["type_groups"]}
        self.assertEqual(groups.get("文本"), 3)
        self.assertEqual(groups.get("图片"), 1)
        self.assertEqual(groups.get("表情"), 1)
        self.assertEqual(groups.get("语音"), 1)
        self.assertEqual(groups.get("系统"), 1)
        # 分组之和必须等于总量，否则图表会缺数据
        self.assertEqual(sum(groups.values()), d["total"])

    def test_hour_and_weekday_histograms(self):
        self._make_stats_account(self.tmp)
        from siwx.server import app
        d = app.test_client().get("/api/stats/overview?account=wxid_stats").get_json()
        self.assertEqual(len(d["by_hour"]), 24)
        self.assertEqual(len(d["by_weekday"]), 7)
        # 直方图总量应等于消息总数（每条消息恰好落进一个小格）
        self.assertEqual(sum(d["by_hour"]), d["total"])
        self.assertEqual(sum(d["by_weekday"]), d["total"])

    def test_month_series_spans_multiple_months(self):
        self._make_stats_account(self.tmp)
        from siwx.server import app
        d = app.test_client().get("/api/stats/overview?account=wxid_stats").get_json()
        # 样本含一年前的消息 → 至少两个不同月份
        self.assertGreaterEqual(len(d["by_month"]), 2)
        self.assertEqual(sum(m["count"] for m in d["by_month"]), d["total"])

    def test_top_senders_only_private_and_resolves_nickname(self):
        acc, _account = self._make_stats_account(self.tmp)
        # 群聊与公众号给更多消息，若未过滤会排到第一。
        msg_dir = acc / "message"
        def add_shard(name, chat, n):
            conn = sqlite3.connect(msg_dir / name)
            t = _msg_table(chat)
            conn.execute(f"""CREATE TABLE [{t}] (
                local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
                create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
                message_content BLOB, packed_info_data BLOB)""")
            for i in range(n):
                conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                             (i + 1, 200 + i, 1, 1_700_000_000 + i, 0, 1, b"x", None))
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
            conn.commit()
            conn.close()
        add_shard("message_3.db", "group@chatroom", 20)
        add_shard("message_4.db", "gh_news", 30)

        from siwx import stats
        stats.clear_cache()
        from siwx.server import app
        d = app.test_client().get("/api/stats/overview?account=wxid_stats&refresh=1").get_json()
        top = {s["wxid"]: s for s in d["top_senders"]}
        self.assertNotIn("group@chatroom", top)
        self.assertNotIn("gh_news", top)
        # 排行按私聊会话聚合，且展示联系人备注/昵称。
        self.assertEqual(top["wxid_a"]["count"], 3)
        self.assertEqual(top["wxid_a"]["name"], "好友A")
        self.assertEqual(top["wxid_b"]["name"], "好友B")

    def test_date_filter_narrows_all_statistics(self):
        self._make_stats_account(self.tmp)
        from siwx.server import app
        c = app.test_client()
        full = c.get("/api/stats/overview?account=wxid_stats").get_json()
        filtered = c.get(
            "/api/stats/overview?account=wxid_stats&start=2023-11-15&end=2023-11-15"
        ).get_json()
        # 样本里 6 条在 2023-11-15，1 条在 400 天前；过滤应影响整页指标。
        self.assertEqual(full["total"], 7)
        self.assertEqual(filtered["total"], 6)
        self.assertEqual(sum(m["count"] for m in filtered["by_month"]), 6)
        self.assertEqual(sum(filtered["by_hour"]), 6)
        self.assertEqual(sum(filtered["by_weekday"]), 6)
        groups = {g["label"]: g["count"] for g in filtered["type_groups"]}
        self.assertEqual(groups.get("文本"), 2)
        top = {s["wxid"]: s["count"] for s in filtered["top_senders"]}
        # wxid_c 在当天只有 1 条系统消息；一年前那条文本不应混进范围内。
        self.assertEqual(top.get("wxid_c"), 1)

    def test_cache_hit_after_first_compute(self):
        acc, account = self._make_stats_account(self.tmp)
        from siwx import stats
        stats.clear_cache()
        stats.compute_stats(account)
        self.assertTrue((acc / ".siwx_stats.json").is_file())
        sig = stats.signature(account)
        self.assertIsNotNone(sig)
        # 磁盘缓存可被读取（内容与签名匹配）
        cached = stats._load_disk_cache(account, sig)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["total"], 7)

    def test_cache_invalidated_when_shard_changes(self):
        acc, account = self._make_stats_account(self.tmp)
        from siwx import stats
        stats.clear_cache()
        stats.compute_stats(account)
        old_sig = stats.signature(account)
        # 追加一个分片 → 签名必须变化，否则统计会永久停在旧结果
        conn = sqlite3.connect(acc / "message" / "message_9.db")
        t = _msg_table("wxid_new")
        conn.execute(f"""CREATE TABLE [{t}] (
            local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
            create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
            message_content BLOB, packed_info_data BLOB)""")
        conn.execute(f"INSERT INTO [{t}] VALUES (1,1,1,1700000500,0,1,?,NULL)", (b"x",))
        conn.commit()
        conn.close()
        new_sig = stats.signature(account)
        self.assertNotEqual(old_sig, new_sig)
        self.assertIsNone(stats._load_disk_cache(account, new_sig))
        self.assertEqual(stats.compute_stats(account)["total"], 8)

    def test_accounts_endpoint_lists_only_decrypted(self):
        self._make_stats_account(self.tmp)
        (self.tmp / "output" / "wxid_no_msg").mkdir(parents=True, exist_ok=True)
        from siwx.server import app
        d = app.test_client().get("/api/stats/accounts").get_json()
        names = [a["wxid"] for a in d["accounts"]]
        self.assertIn("wxid_stats", names)
        self.assertNotIn("wxid_no_msg", names)

    def test_overview_requires_account(self):
        from siwx.server import app
        self.assertEqual(app.test_client().get("/api/stats/overview").status_code, 400)

    def test_overview_unknown_account_is_404(self):
        from siwx.server import app
        r = app.test_client().get("/api/stats/overview?account=wxid_missing")
        self.assertEqual(r.status_code, 404)

    def test_refresh_endpoint_recomputes(self):
        self._make_stats_account(self.tmp)
        from siwx.server import app
        r = app.test_client().post("/api/stats/refresh", json={"account": "wxid_stats"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["total"], 7)

    def test_empty_account_returns_zeroes(self):
        acc = self.tmp / "output" / "wxid_empty"
        (acc / "message").mkdir(parents=True, exist_ok=True)
        from siwx.server import app
        d = app.test_client().get("/api/stats/overview?account=wxid_empty").get_json()
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["chat_count"], 0)
        self.assertEqual(d["type_groups"], [])


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


class TestChatTimelineAndStats(TempRootCase):

    def test_timeline_and_conversation_stats(self):
        _acc, account, chat = make_account(self.tmp, n_texts=6)
        from siwx.server import app
        c = app.test_client()
        tl = c.get(f"/api/chat/timeline?account={account}&chat={chat}")
        self.assertEqual(tl.status_code, 200)
        self.assertEqual(tl.get_json()["total"], 6)
        self.assertTrue(tl.get_json()["months"])
        month = tl.get_json()["months"][0]["month"]
        days = c.get(f"/api/chat/timeline?account={account}&chat={chat}&month={month}")
        self.assertEqual(days.status_code, 200)
        self.assertEqual(days.get_json()["total"], 6)
        self.assertTrue(days.get_json()["days"])

        st = c.get(f"/api/chat/stats?account={account}&chat={chat}")
        self.assertEqual(st.status_code, 200)
        d = st.get_json()
        self.assertEqual(d["total"], 6)
        self.assertEqual(d["types"][0]["label"], "文本消息")
        self.assertGreaterEqual(d["active_days"], 1)


class TestManualWechatPaths(TempRootCase):

    def test_validate_endpoint_persists_manual_path(self):
        db_dir = self.tmp / "custom" / "wxid_manual" / "db_storage"
        db_dir.mkdir(parents=True)
        from siwx.server import app
        from siwx.discover import load_manual_data_dirs, manual_paths_file
        r = app.test_client().post("/api/discover/validate", json={"path": str(db_dir)})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["saved"])
        self.assertTrue(manual_paths_file().is_file())
        self.assertIn(("wxid_manual", str(db_dir.resolve())), load_manual_data_dirs())

    def test_resolves_account_subdir_and_database_file(self):
        from siwx.discover import resolve_db_paths
        db_dir = self.tmp / "xwechat_files" / "wxid_one" / "db_storage"
        message = db_dir / "message"
        message.mkdir(parents=True)
        db_file = message / "message_0.db"
        db_file.write_bytes(b"")
        expected = [{"wxid": "wxid_one", "db_dir": str(db_dir.resolve())}]
        self.assertEqual(resolve_db_paths(str(db_dir.parent)), expected)
        self.assertEqual(resolve_db_paths(str(message)), expected)
        self.assertEqual(resolve_db_paths(str(db_file)), expected)

    def test_xwechat_root_finds_multiple_accounts(self):
        from siwx.discover import validate_db_path
        root = self.tmp / "xwechat_files"
        for wxid in ("wxid_a", "wxid_b"):
            (root / wxid / "db_storage").mkdir(parents=True)
        d = validate_db_path(str(root))
        self.assertTrue(d["ok"])
        self.assertEqual(d["account_count"], 2)
        self.assertEqual({a["wxid"] for a in d["accounts"]}, {"wxid_a", "wxid_b"})


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

    def test_decrypted_body_equals_plaintext(self):
        """整库解密的正文必须与原始明文**逐字节一致**（CBC 链式 XOR 回归）。

        覆盖 page1（salt 特例，4000 字节密文）与后续页（4016 字节密文）两种长度。
        现有用例只断言了大小与头部，此处补齐内容校验 —— 这是 strxor 改写
        （由大整数 XOR 换成 C 实现）最直接的回归防线。
        """
        from siwx.sqlcipher import decrypt_database, PAGE_SZ, RESERVE_SZ, SALT_SZ
        dst = self.tmp / "out" / "body.db"
        decrypt_database(self.src, dst, self.enc_key)
        body_len = PAGE_SZ - RESERVE_SZ
        plain1 = bytes((i * 3) & 0xFF for i in range(body_len - SALT_SZ))
        plain2 = bytes((i * 5) & 0xFF for i in range(body_len))
        zeros = b"\x00" * RESERVE_SZ
        expect = b"SQLite format 3\x00" + plain1 + zeros + plain2 + zeros
        self.assertEqual(dst.read_bytes(), expect)

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

class TestLogsApi(unittest.TestCase):
    """/api/logs 条目是**混合形状**的：

    旧/文件日志 → [ts_ms, text]；结构化日志（siwx.logger）→ [ts_ms, level, module, text]。
    因此断言必须走统一的取文本逻辑，不能假定固定长度（插件加载等会在启动期
    就写入结构化日志，使结构化条目非空）。
    """

    @staticmethod
    def _texts(data):
        items = data.get("logs", []) if isinstance(data, dict) else (data or [])
        return [str(it[3] if len(it) >= 4 else it[1]) for it in items]

    def test_api_logs_includes_file_logger_messages(self):
        """日志页不能只看内存 ring；普通 logger 写入的文件日志也要显示。"""
        from siwx import server
        marker = f"unit-log-marker-{int(time.time() * 1000)}"
        server._siwx_logger.info(marker)
        for h in server._siwx_logger.handlers:
            try:
                h.flush()
            except Exception:
                pass
        data = server.app.test_client().get("/api/logs").get_json()
        self.assertTrue(any(marker in m for m in self._texts(data)),
                        "文件日志没有出现在 /api/logs")

    def test_404_is_not_logged_as_uncaught_error(self):
        from siwx import server
        before = len(server.app.test_client().get("/api/logs").get_json().get("logs", []))
        r = server.app.test_client().get("/__definitely_missing__")
        self.assertEqual(r.status_code, 404)
        data = server.app.test_client().get("/api/logs").get_json()
        lines = self._texts(data)
        self.assertFalse(any("__definitely_missing__" in m or "404 Not Found" in m
                             for m in lines[-20:]))
        self.assertGreaterEqual(len(lines), before)

    def test_task_exception_is_persisted_to_file_logs(self):
        from siwx import server
        marker = "unit-task-failure-marker"
        try:
            raise RuntimeError(marker)
        except Exception as e:
            server._siwx_logger.exception("任务执行失败: %s", e)
            server._flush_logs()
        data = server.app.test_client().get("/api/logs").get_json()
        self.assertTrue(any(marker in m for m in self._texts(data)),
                        "任务异常没有落盘到 /api/logs")

    def test_all_log_items_are_renderable(self):
        """混合形状下的健壮性：每条日志都能取出文本，且前端可安全渲染。"""
        from siwx import server, logger as _log
        _log.info("plugin", "unit-mixed-shape-marker")
        items = server.app.test_client().get("/api/logs").get_json()["logs"]
        self.assertTrue(items, "日志不应为空")
        texts = self._texts(items)
        self.assertTrue(all(isinstance(t, str) and t for t in texts),
                        "存在无法取文本的日志条目")
        self.assertTrue(any("unit-mixed-shape-marker" in t for t in texts))


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

class TestVersionSource(unittest.TestCase):

    def test_current_version_comes_from_package_init(self):
        from siwx import __version__
        from siwx.auto_update import current_version
        self.assertEqual(current_version(), __version__)
        self.assertEqual(__version__, "5.0.8")

    def test_release_metadata_matches_package_version(self):
        """version.json 与 README 徽章的版本号必须跟 __version__ 一致。

        发版清单只钉了 `siwx/__init__.py` 与测试里的字面量，version.json
        （对外公布的更新清单）与 README 徽章不在其中——曾出现 __version__
        已升到 5.0.7、README 徽章也写 5.0.7，而 version.json 仍停在 5.0.6
        的三处不一致（以 version.json 为准的更新链会读到旧版本）。
        """
        from siwx import __version__
        import re as _re
        meta = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], __version__,
                         "version.json 的 version 与 siwx.__version__ 不一致")
        badge = _re.search(r"badge/version-([0-9]+\.[0-9]+\.[0-9]+)-",
                           (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIsNotNone(badge, "README 未找到版本徽章")
        self.assertEqual(badge.group(1), __version__,
                         "README 徽章版本与 siwx.__version__ 不一致")

    def test_remote_version_uses_newest_source_and_bypasses_cache(self):
        from siwx import auto_update

        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req)
            version = "5.0.0" if "gh.1s.fan" in req.full_url else "5.0.1"
            return io.BytesIO(json.dumps({"version": version}).encode("utf-8"))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            remote = auto_update.fetch_remote_version()

        self.assertEqual(remote["version"], "5.0.1")
        self.assertEqual(len(seen), len(auto_update.VERSION_URLS))
        for req in seen:
            self.assertIn("_siwx_update=", req.full_url)
            self.assertEqual(req.get_header("Cache-control"), "no-cache")
            self.assertEqual(req.get_header("Pragma"), "no-cache")

    def test_update_check_response_is_not_cached(self):
        from flask import Flask
        from siwx import api_update

        app = Flask(__name__)
        app.register_blueprint(api_update.bp)
        remote = {"version": "5.0.1", "notes": "update"}
        with mock.patch.object(api_update, "has_update",
                               return_value=(True, remote, "5.0.0")):
            response = app.test_client().get("/api/update/check")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["has_update"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Pragma"], "no-cache")


class TestAutoUpdateInstall(unittest.TestCase):
    """自动更新安装环节：跨盘安全、带版本号文件名、失败可回滚。

    背景：os.replace 不能跨盘（WinError 17），且旧逻辑硬编码替换
    stories-in-wx.exe——用户目录里往往是按版本号命名的多个 exe，
    根本没有这个名字的文件。
    """

    @staticmethod
    def _write(dir_path: Path, name: str, content: bytes) -> Path:
        p = dir_path / name
        p.write_bytes(content)
        return p

    def _patch_common(self, auto_update, paths, app_dir, running):
        return [
            mock.patch.object(paths, "app_root", return_value=app_dir),
            mock.patch.object(auto_update.sys, "executable", str(running)),
            mock.patch.object(auto_update, "_schedule_exit"),
        ]

    def test_target_exe_name_versioned_bumps_version(self):
        from siwx.auto_update import _target_exe_name
        self.assertEqual(
            _target_exe_name("stories-in-wx-v5.0.4-windows-x64.exe",
                             "5.0.4", "5.0.5"),
            "stories-in-wx-v5.0.5-windows-x64.exe")

    def test_target_exe_name_plain_name_kept_inplace(self):
        from siwx.auto_update import _target_exe_name
        self.assertEqual(
            _target_exe_name("stories-in-wx.exe", "5.0.4", "5.0.5"),
            "stories-in-wx.exe")

    def test_target_exe_name_ignores_unrelated_versions(self):
        from siwx.auto_update import _target_exe_name
        # 文件名里的版本号与当前版本不符（用户改过名）→ 就地替换
        self.assertEqual(
            _target_exe_name("stories-in-wx-v0.4.0-windows-x64.exe",
                             "5.0.4", "5.0.5"),
            "stories-in-wx-v0.4.0-windows-x64.exe")
        # 版本号子串不能误伤（v5.0.4 ≠ v5.0.45）
        self.assertEqual(
            _target_exe_name("stories-in-wx-v5.0.45.exe", "5.0.4", "5.0.5"),
            "stories-in-wx-v5.0.45.exe")

    def test_install_copy_verifies_content(self):
        from siwx import auto_update
        with tempfile.TemporaryDirectory() as td:
            src = self._write(Path(td), "src.exe", b"A" * 4096)
            dst = Path(td) / "sub" / "dst.exe"
            dst.parent.mkdir()
            # 临时目录模拟不了真跨盘，但 copyfile 路径与盘符布局无关
            auto_update._install_copy(src, dst)
            self.assertEqual(dst.read_bytes(), b"A" * 4096)

    def test_install_copy_bad_sha_leaves_no_residue(self):
        from siwx import auto_update
        with tempfile.TemporaryDirectory() as td:
            src = self._write(Path(td), "src.exe", b"DATA")
            dst = Path(td) / "dst.exe"
            with self.assertRaises(RuntimeError):
                auto_update._install_copy(src, dst, expected_sha="0" * 64)
            self.assertFalse(dst.exists())

    def test_replace_windows_exe_writes_versioned_target(self):
        """用户实际场景：目录里全是带版本号的 exe，正在运行 v5.0.4。"""
        from siwx import auto_update, paths
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            running = self._write(app_dir, "stories-in-wx-v5.0.4-windows-x64.exe", b"OLD")
            new_exe = self._write(app_dir, "downloaded.exe", b"NEW")
            launched = []
            patches = self._patch_common(auto_update, paths, app_dir, running)
            patches += [
                mock.patch.object(auto_update, "current_version",
                                  return_value="5.0.4"),
                mock.patch.object(auto_update.subprocess, "Popen",
                                  side_effect=lambda cmd, **kw: launched.append(cmd)),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                result = auto_update._replace_windows_exe(new_exe, "5.0.5")

            self.assertTrue(result["ok"], result)
            target = app_dir / "stories-in-wx-v5.0.5-windows-x64.exe"
            self.assertEqual(target.read_bytes(), b"NEW")
            # 正在运行的旧 exe 不被触碰
            self.assertEqual(running.read_bytes(), b"OLD")
            self.assertEqual(launched[0][0], str(target))

    def test_replace_windows_exe_inplace_success_keeps_backup(self):
        from siwx import auto_update, paths
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            running = self._write(app_dir, "stories-in-wx.exe", b"OLD")
            new_exe = self._write(app_dir, "downloaded.exe", b"NEW")
            good_sha = hashlib.sha256(b"NEW").hexdigest()
            launched = []
            patches = self._patch_common(auto_update, paths, app_dir, running)
            patches += [
                mock.patch.object(auto_update, "current_version",
                                  return_value="5.0.5"),
                mock.patch.object(auto_update.subprocess, "Popen",
                                  side_effect=lambda cmd, **kw: launched.append(cmd)),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                result = auto_update._replace_windows_exe(new_exe, "5.0.6", good_sha)

            self.assertTrue(result["ok"], result)
            self.assertEqual((app_dir / "stories-in-wx.exe").read_bytes(), b"NEW")
            self.assertTrue((app_dir / "stories-in-wx.backup.exe").exists())
            self.assertEqual(launched[0][0], str(app_dir / "stories-in-wx.exe"))

    def test_replace_windows_exe_inplace_rollback_on_failure(self):
        """复制失败必须把改名出去的旧 exe 改回来，不能把程序变砖。"""
        from siwx import auto_update, paths
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            running = self._write(app_dir, "stories-in-wx.exe", b"OLD")
            new_exe = self._write(app_dir, "downloaded.exe", b"NEW")
            bad_sha = hashlib.sha256(b"NOT-THIS").hexdigest()
            patches = self._patch_common(auto_update, paths, app_dir, running)
            patches += [
                mock.patch.object(auto_update, "current_version",
                                  return_value="5.0.5"),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                result = auto_update._replace_windows_exe(new_exe, "5.0.6", bad_sha)

            self.assertFalse(result["ok"])
            self.assertEqual(running.read_bytes(), b"OLD")
            self.assertFalse((app_dir / "stories-in-wx.backup.exe").exists())
            self.assertFalse((app_dir / "stories-in-wx.exe.new").exists())


class TestEnvInfo(unittest.TestCase):
    """环境信息采集（供 bug 报告粘贴）：字段齐全、用户名打码、不污染 stdout。"""

    def test_collect_has_fields_required_by_issue_template(self):
        from siwx import __version__
        from siwx import env_info

        info = env_info.collect(quiet=True)
        # 这几个字段对应 issue 模板里要求用户填写的内容
        for key in ("siwx 版本", "运行模式", "操作系统", "系统版本",
                    "系统架构", "Python", "数据目录", "密钥库"):
            self.assertIn(key, info, f"缺少字段: {key}")
        self.assertEqual(info["siwx 版本"], __version__)
        self.assertIn(info["运行模式"], ("打包产物", "源码运行"))
        # 所有值都必须是可直接粘贴的字符串
        for key, value in info.items():
            self.assertIsInstance(value, str, f"{key} 不是字符串")

    def test_mask_path_hides_username_on_all_platforms(self):
        from siwx.env_info import mask_path

        cases = [
            (r"C:\Users\alice\AppData\Local\stories-in-wx", "alice"),
            ("/Users/bob/Library/Application Support/stories-in-wx", "bob"),
            ("/home/carol/.local/share/stories-in-wx", "carol"),
        ]
        for raw, user in cases:
            masked = mask_path(raw)
            self.assertNotIn(user, masked, f"用户名未打码: {masked}")
            self.assertIn("<user>", masked)

    def test_format_text_masks_current_user(self):
        from siwx import env_info

        text = env_info.format_text(quiet=True)
        self.assertIn("### 环境信息", text)
        self.assertIn(f"- siwx 版本: {env_info.__version__}", text)
        user = Path.home().name
        if user and user not in ("root",):
            self.assertNotIn(f"\\Users\\{user}", text, "Windows 用户名未打码")
            self.assertNotIn(f"/Users/{user}", text, "macOS 用户名未打码")
            self.assertNotIn(f"/home/{user}", text, "Linux 用户名未打码")

    def test_collect_quiet_writes_nothing_to_stdout(self):
        from siwx import env_info

        buf = io.StringIO()
        with redirect_stdout(buf):
            info = env_info.collect(quiet=True)
        self.assertEqual(buf.getvalue(), "", "quiet=True 仍向 stdout 输出了内容")
        self.assertTrue(info)

    def test_settings_env_api(self):
        from flask import Flask
        from siwx import __version__
        from siwx import api_settings

        app = Flask(__name__)
        app.register_blueprint(api_settings.bp)
        response = app.test_client().get("/api/settings/env")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("info", data)
        self.assertIn("text", data)
        self.assertEqual(data["info"]["siwx 版本"], __version__)
        self.assertIn("### 环境信息", data["text"])
        # 接口返回的文本同样必须打码
        user = Path.home().name
        if user and user not in ("root",):
            self.assertNotIn(f"\\Users\\{user}", data["text"])
            self.assertNotIn(f"/Users/{user}", data["text"])

    def test_cli_doctor_prints_paste_block(self):
        import argparse
        from siwx import cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.cmd_doctor(argparse.Namespace())

        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("### 环境信息", out)
        self.assertIn("- siwx 版本:", out)
        self.assertIn("可直接粘贴到 GitHub issue", out)


class TestContributionTemplates(unittest.TestCase):
    """贡献规范化文件存在且 YAML 合法（GitHub 表单格式错误会直接不显示）。"""

    def _parse(self, path: Path):
        try:
            import yaml
        except ImportError:
            self.skipTest("未安装 pyyaml，跳过表单格式校验")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_issue_templates_present_and_valid(self):
        tpl_dir = ROOT / ".github" / "ISSUE_TEMPLATE"
        self.assertTrue(tpl_dir.is_dir(), "缺少 .github/ISSUE_TEMPLATE 目录")

        expected = {"bug_report.yml", "feature_request.yml", "question.yml"}
        found = {p.name for p in tpl_dir.glob("*.yml")}
        self.assertTrue(expected.issubset(found), f"缺少模板: {expected - found}")

        for name in sorted(expected):
            data = self._parse(tpl_dir / name)
            for key in ("name", "description", "body"):
                self.assertIn(key, data, f"{name} 缺少 {key}")
            ids = [b.get("id") for b in data["body"] if b.get("id")]
            self.assertEqual(len(ids), len(set(ids)), f"{name} 存在重复 id")
            for block in data["body"]:
                self.assertIn(block.get("type"),
                              ("markdown", "input", "textarea", "dropdown",
                               "checkboxes"),
                              f"{name} 含不支持的字段类型")

    def test_bug_report_requires_version_os_arch(self):
        """用户明确要求：bug 反馈必须填 siwx 版本、系统版本、架构。"""
        data = self._parse(ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml")
        required = {b["id"] for b in data["body"]
                    if (b.get("validations") or {}).get("required")}
        for field in ("siwx_version", "os_version", "arch", "os",
                      "wechat_version", "run_mode", "env_info"):
            self.assertIn(field, required, f"bug 模板未强制要求 {field}")

    def test_bug_report_arch_is_dropdown(self):
        data = self._parse(ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml")
        arch = next(b for b in data["body"] if b.get("id") == "arch")
        self.assertEqual(arch["type"], "dropdown")
        options = " ".join(arch["attributes"]["options"]).lower()
        self.assertIn("x64", options)
        self.assertIn("arm64", options)

    def test_blank_issues_disabled(self):
        data = self._parse(ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml")
        self.assertTrue(data.get("blank_issues_enabled") is False,
                        "空白 issue 应被禁用，强制走模板")

    def test_pr_template_and_contributing_present(self):
        self.assertTrue((ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file())
        self.assertTrue((ROOT / "CONTRIBUTING.md").is_file())


class TestCryptoIntact(unittest.TestCase):

    def test_verify_enc_key_byte_layout(self):
        from siwx import sqlcipher as sc
        self.assertEqual(sc.PAGE_SZ - sc.RESERVE_SZ + sc.IV_SZ - sc.SALT_SZ, 4016)

    def test_handwritten_cbc_matches_stdlib(self):
        """手写 CBC 链式 XOR（现用 pycryptodome 的 C 实现 strxor）对齐标准库。"""
        from Crypto.Cipher import AES
        from Crypto.Util import strxor
        from siwx.sqlcipher import PAGE_SZ, RESERVE_SZ, IV_SZ
        key, iv = bytes(range(32)), bytes(range(16, 32))
        for ct_len in (PAGE_SZ - RESERVE_SZ - IV_SZ, PAGE_SZ - RESERVE_SZ):
            pt = bytes((i * 7 + 3) & 0xFF for i in range(ct_len))
            ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pt)
            std = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
            raw = AES.new(key, AES.MODE_ECB).decrypt(ct)
            mine = strxor.strxor(iv + ct[:len(ct) - 16], raw)
            self.assertEqual(mine, std, f"ct_len={ct_len}")

    def test_decrypt_uses_c_strxor_not_bigint(self):
        """源码层面确认页 CBC 的链式 XOR 走 C 实现 ``strxor``，而非大整数转换。

        大整数 ``from_bytes``/``to_bytes`` 曾占单库解密耗时约 39%（改动前）。
        docstring 里会提到旧写法做说明，因此先用 ``ast`` 剥掉 docstring，
        只在**真正的代码**里断言。
        """
        import ast
        src = Path(__file__).resolve().parent.parent / "siwx" / "sqlcipher.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
                    and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)          # 丢弃 docstring
        code = ast.unparse(tree)
        self.assertIn("strxor", code, "页 CBC 未使用 strxor")
        self.assertNotIn("from_bytes", code, "仍在用大整数 from_bytes")
        self.assertNotIn("to_bytes", code, "仍在用大整数 to_bytes")


# ── 数据安全修复：临时文件唯一性（D-2）─────────────────────────

class TestTempFileUniqueness(unittest.TestCase):
    """`_read_page1` / `decrypt_database` 的临时文件必须并发唯一。

    修复前用 f"siwx_p1_{os.getpid()}.tmp"：Flask 以 threaded=True 运行，
    `/api/status` 每次请求都会走 `collect_db_files` -> `_read_page1`，
    「微信占用中」时同进程多线程会撞名互相覆盖，读到对方的 page1，
    进而把 salt 张冠李戴写进密钥库。
    """

    def test_read_page1_concurrent_no_crosstalk(self):
        """并发读取多个文件，每个都必须拿到自己的 page1。"""
        import threading
        import builtins
        from siwx.sqlcipher import PAGE_SZ, _read_page1

        tmp = Path(tempfile.mkdtemp(prefix="siwx_p1_"))
        try:
            n = 8
            targets = []
            for i in range(n):
                p = tmp / f"db_{i}" / "message.db"
                p.parent.mkdir(parents=True, exist_ok=True)
                marker = 0x10 + i
                p.write_bytes(bytes([marker]) * 16
                              + bytes((j + marker) % 256 for j in range(PAGE_SZ - 16)))
                targets.append((p, marker))

            # 强制走「复制到临时文件」的回退分支
            real_open = builtins.open
            locked = {str(p) for p, _ in targets}

            def flaky_open(file, *a, **kw):
                if str(file) in locked:
                    raise OSError(13, "simulated lock")
                return real_open(file, *a, **kw)

            results, errors = {}, []
            lock = threading.Lock()
            barrier = threading.Barrier(n)

            def worker(path, marker):
                barrier.wait()
                try:
                    pg = _read_page1(path)
                    with lock:
                        results[str(path)] = pg[:16] if pg else None
                except Exception as e:      # noqa: BLE001
                    with lock:
                        errors.append(repr(e))

            builtins.open = flaky_open
            try:
                ts = [threading.Thread(target=worker, args=t) for t in targets]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
            finally:
                builtins.open = real_open

            self.assertEqual(errors, [], f"并发异常: {errors}")
            for path, marker in targets:
                self.assertEqual(
                    results.get(str(path)), bytes([marker]) * 16,
                    f"{path.parent.name} 读到的 page1 不匹配（并发串扰）")

            residue = list(Path(tempfile.gettempdir()).glob("siwx_p1_*.tmp"))
            self.assertEqual(residue, [], f"临时文件残留: {residue}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_temp_names_are_unique_across_calls(self):
        """mkstemp 生成的临时名必须唯一（旧实现恒为同一个名）。"""
        names = []
        for _ in range(20):
            fd, name = tempfile.mkstemp(prefix="siwx_p1_", suffix=".tmp")
            os.close(fd)
            names.append(name)
            Path(name).unlink(missing_ok=True)
        self.assertEqual(len(names), len(set(names)))

    def test_source_uses_mkstemp(self):
        """源码层面确认临时文件名不再是「固定前缀 + PID」的拼接。

        注意：注释里会引用旧实现的名字做说明，因此只看**代码行**
        （去掉注释与空行）是否还存在 `gettempdir() / f"siwx_..._{os.getpid()}"`。
        """
        src = Path(__file__).resolve().parent.parent / "siwx" / "sqlcipher.py"
        code_lines = []
        for line in src.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            code_lines.append(stripped)
        code = "\n".join(code_lines)
        self.assertNotIn("siwx_p1_{os.getpid()}", code,
                         "仍在使用 PID 拼接的临时文件名")
        self.assertNotIn("siwx_db_{os.getpid()}", code,
                         "仍在使用 PID 拼接的临时文件名")
        self.assertIn("mkstemp", code)


# ── 数据安全修复：同名账号冲突检测（方案 C）────────────────────

class TestAccountConflicts(unittest.TestCase):

    def test_detects_duplicate_wxid(self):
        from siwx.discover import find_account_conflicts
        dirs = [
            ("wxid_a", r"C:\x\wxid_a\db_storage"),
            ("wxid_a", r"D:\x\wxid_a\db_storage"),
            ("wxid_b", r"C:\x\wxid_b\db_storage"),
        ]
        conflicts = find_account_conflicts(dirs)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["wxid"], "wxid_a")
        self.assertEqual(len(conflicts[0]["dirs"]), 2)

    def test_no_conflict_returns_empty(self):
        from siwx.discover import find_account_conflicts
        dirs = [("wxid_a", r"C:\x\a\db_storage"),
                ("wxid_b", r"C:\x\b\db_storage")]
        self.assertEqual(find_account_conflicts(dirs), [])

    def test_empty_input(self):
        from siwx.discover import find_account_conflicts
        self.assertEqual(find_account_conflicts([]), [])

    def test_three_copies(self):
        from siwx.discover import find_account_conflicts
        dirs = [("w", "1"), ("w", "2"), ("w", "3")]
        c = find_account_conflicts(dirs)
        self.assertEqual(len(c), 1)
        self.assertEqual(len(c[0]["dirs"]), 3)

    def test_status_api_exposes_conflicts(self):
        """/api/status 必须返回 conflicts 字段（前端据此提示）。"""
        from siwx import server
        client = server.app.test_client()
        resp = client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("conflicts", resp.get_json())


# ── 数据安全修复：manifest 来源保护（方案 B）──────────────────

class TestManifestSourceGuard(TempRootCase):
    """同名账号共用 output/<wxid>/ 时，来源变更不得静默覆盖已存在产物。

    核心兼容约束：旧 manifest 无 @source（升级自 v5.0.x）必须放行，
    行为与旧版完全一致。
    """

    def setUp(self):
        super().setUp()
        from siwx import extract, keystore
        from siwx import pool
        self.extract, self.pool = extract, pool
        self._bench = []
        # 密钥依赖必须自给自足：此前用的是开发者本机真实密钥库，干净环境
        # （CI）里 0 密钥，5 个用例全在 _resolve_key 处因"无密钥"跳过。
        # 显式种入 salt→key，配合下方 mock 的 parse_key/verify_enc_key，
        # 让 _resolve_key 在任何机器上都必定命中。
        store = keystore.load()
        keystore.insert(store, "aa" * 16, "ab" * 32, "test")
        keystore.save(store)
        self._orig = (extract.parse_key, extract.verify_enc_key,
                      extract.decrypt_parallel)
        extract.parse_key = lambda k: b"\x00" * 32
        extract.verify_enc_key = lambda kb, p1: True

        def fake_parallel(tasks, workers=None, on_done=None):
            res = []
            for rel, src, dst, key_hex in tasks:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(b"DECRYPTED")
                r = (rel, 2, "ok", "")
                res.append(r)
                if on_done:
                    on_done(r)
            return res

        extract.decrypt_parallel = fake_parallel
        self._bench.append(fake_parallel)

    def tearDown(self):
        (self.extract.parse_key, self.extract.verify_enc_key,
         self.extract.decrypt_parallel) = self._orig
        super().tearDown()

    def _entry(self, rel, path, size=8192):
        from siwx.sqlcipher import DbEntry
        return DbEntry(rel, Path(path), size, "aa" * 16, b"\x00" * 4096)

    def _make_dirs(self):
        c = self.tmp / "c" / "wxid_t" / "db_storage"
        d = self.tmp / "d" / "wxid_t" / "db_storage"
        for base in (c, d):
            (base / "contact").mkdir(parents=True, exist_ok=True)
            (base / "contact" / "contact.db").write_bytes(b"x" * 8192)
        return c, d

    def test_upgrade_without_source_field_is_allowed(self):
        """升级场景：旧 manifest 无 @source -> 放行（关键兼容性保证）。"""
        c, d = self._make_dirs()
        out = self.tmp / "out" / "wxid_t"
        (out / "contact").mkdir(parents=True, exist_ok=True)
        (out / "contact" / "contact.db").write_bytes(b"LEGACY")

        # 模拟 v5.0.2 的 manifest：只有业务键，没有 @source
        self.pool.save_manifest(out, {
            "contact\\contact.db": {"size": 1, "mtime": 1, "pages": 1,
                                    "key": "ab" * 32}})

        entries = [self._entry("contact\\contact.db", d / "contact" / "contact.db")]
        rep = self.extract.decrypt_dir(str(d), str(out), log=lambda m: None,
                                      entries=entries, use_cache=True)
        self.assertEqual(rep["conflicts"], 0, "升级用户不应被拦截")
        self.assertEqual(rep["ok"], 1)

    def test_source_change_blocks_overwrite(self):
        """来源变更 + 产物存在 -> 跳过，内容不变。"""
        c, d = self._make_dirs()
        out = self.tmp / "out" / "wxid_t"
        (out / "contact").mkdir(parents=True, exist_ok=True)
        target = out / "contact" / "contact.db"
        target.write_bytes(b"PROTECT-ME")

        self.pool.save_manifest(out, {
            self.extract.SOURCE_FIELD: str(c.resolve()).casefold()})

        entries = [self._entry("contact\\contact.db", d / "contact" / "contact.db")]
        rep = self.extract.decrypt_dir(str(d), str(out), log=lambda m: None,
                                      entries=entries, use_cache=True)
        self.assertEqual(rep["conflicts"], 1)
        self.assertEqual(target.read_bytes(), b"PROTECT-ME",
                         "其他副本的产物被覆盖了")

    def test_same_source_allows_decrypt(self):
        """来源一致 -> 正常解密。"""
        c, d = self._make_dirs()
        out = self.tmp / "out" / "wxid_t"
        (out / "contact").mkdir(parents=True, exist_ok=True)
        (out / "contact" / "contact.db").write_bytes(b"OLD")

        self.pool.save_manifest(out, {
            self.extract.SOURCE_FIELD: str(d.resolve()).casefold()})

        entries = [self._entry("contact\\contact.db", d / "contact" / "contact.db")]
        rep = self.extract.decrypt_dir(str(d), str(out), log=lambda m: None,
                                      entries=entries, use_cache=True)
        self.assertEqual(rep["conflicts"], 0)
        self.assertEqual(rep["ok"], 1)

    def test_writes_source_when_absent(self):
        """无历史来源且成功解密后，必须写入 @source。"""
        c, d = self._make_dirs()
        out = self.tmp / "out" / "wxid_t"
        (out / "contact").mkdir(parents=True, exist_ok=True)

        entries = [self._entry("contact\\contact.db", d / "contact" / "contact.db")]
        self.extract.decrypt_dir(str(d), str(out), log=lambda m: None,
                                entries=entries, use_cache=True)
        m = self.pool.load_manifest(out)
        self.assertEqual(m.get(self.extract.SOURCE_FIELD),
                         str(d.resolve()).casefold())

    def test_no_conflict_when_no_existing_artifact(self):
        """来源变更但无产物 -> 不拦截。"""
        c, d = self._make_dirs()
        out = self.tmp / "out" / "wxid_t"
        (out / "contact").mkdir(parents=True, exist_ok=True)   # 目录在，文件不在
        self.pool.save_manifest(out, {
            self.extract.SOURCE_FIELD: str(c.resolve()).casefold()})

        entries = [self._entry("contact\\contact.db", d / "contact" / "contact.db")]
        rep = self.extract.decrypt_dir(str(d), str(out), log=lambda m: None,
                                      entries=entries, use_cache=True)
        self.assertEqual(rep["conflicts"], 0)
        self.assertEqual(rep["ok"], 1)

    def test_source_field_coexists_with_business_keys(self):
        """@source 与业务键混存不互相干扰。"""
        out = self.tmp / "out" / "wxid_t"
        out.mkdir(parents=True)
        self.pool.save_manifest(out, {
            "contact\\contact.db": {"size": 1, "mtime": 1, "pages": 1,
                                    "key": "ab" * 32},
            self.extract.SOURCE_FIELD: "d:\\x"})
        m = self.pool.load_manifest(out)
        self.assertEqual(len(m), 2)
        self.assertIn("contact\\contact.db", m)
        self.assertEqual(m.get(self.extract.SOURCE_FIELD), "d:\\x")

    def test_report_includes_conflicts_count(self):
        """report 必须带 conflicts 字段（供上层/日志展示）。"""
        c, d = self._make_dirs()
        out = self.tmp / "out" / "wxid_t"
        (out / "contact").mkdir(parents=True, exist_ok=True)
        rep = self.extract.decrypt_dir(str(d), str(out), log=lambda m: None,
                                      entries=[], use_cache=True)
        self.assertIn("conflicts", rep)


class TestDisclaimerSync(unittest.TestCase):
    """免责声明双源同步：README 与控制台弹层（ui/pages/disclaimer.html）必须一致。

    免责条款改写时两处必须同步更新，避免「文档说一套、应用里另一套」。
    若调整哨兵条款的措辞，请同步修改本测试。
    """

    README = ROOT / "README.md"
    UI_DISCLAIMER = ROOT / "siwx" / "ui" / "pages" / "disclaimer.html"

    def test_ui_disclaimer_file_exists(self):
        self.assertTrue(self.UI_DISCLAIMER.is_file(),
                        "缺少 siwx/ui/pages/disclaimer.html（控制台免责弹层全文）")

    def test_key_clauses_present_in_both_sources(self):
        readme = self.README.read_text(encoding="utf-8")
        ui = self.UI_DISCLAIMER.read_text(encoding="utf-8")
        for phrase in (
            "技术研究与个人数据管理工具",
            "数据权属合法",
            "取得必要授权",
            "账号被平台限制或封禁",
            "世界多数国家和地区",
            "AS IS",
            "明文或不完全加密",
            "接入 AI 客户端前自行评估",
            "本声明不修改、不限制 AGPL-3.0 已授予的权利",
            "商用支持需另行授权",
            "可分割性与更新",
        ):
            self.assertIn(phrase, readme, f"README 免责声明缺少关键条款：{phrase}")
            self.assertIn(phrase, ui, f"应用内免责声明缺少关键条款：{phrase}")

    def test_consent_gate_wired_into_shell(self):
        """免责声明接线：向导第 1 步承担首启确认（含 ack 状态），设置页承担常驻全文入口。"""
        app_js = (ROOT / "siwx" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("DISCLAIMER_VERSION", app_js, "app.js 缺少免责条款版本常量")
        self.assertIn("siwx-disclaimer-ack", app_js, "app.js 未接入确认状态（localStorage）")
        self.assertIn("/pages/disclaimer.html", app_js, "app.js 未加载免责声明全文")
        onboarding_js = (ROOT / "siwx" / "ui" / "onboarding.js").read_text(encoding="utf-8")
        self.assertIn("/pages/disclaimer.html", onboarding_js, "首启向导缺少免责声明步骤")
        self.assertIn("siwx-disclaimer-ack", onboarding_js, "首启向导未写入声明确认状态")
        settings_html = (ROOT / "siwx" / "ui" / "pages" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('id="s-view-disclaimer"', settings_html, "设置页缺少免责声明查看入口")


# ── 12. 同秒消息分页游标（create_time + local_id 组合游标）──────────

class TestSameSecondPagination(TempRootCase):
    """向上翻页原来只按 `create_time < before` 取数：翻页边界落在同一秒的
    一批消息中间时，剩余同秒消息会被永久跳过。升级为 (create_time,
    local_id) 组合游标后必须能全部翻出；只传 before 时保持旧语义。"""

    def _make_same_second_account(self):
        acc, account, chat = make_account(self.tmp, n_texts=0)
        db = acc / "message" / "message_0.db"
        t = _msg_table(chat)
        conn = sqlite3.connect(db)
        # ts=100 同秒 4 条（id 1~4，新→旧写入），ts=50 两条（id 5~6）
        for lid, ts in [(1, 100), (2, 100), (3, 100), (4, 100), (5, 50), (6, 50)]:
            content = f"消息 {lid}".encode("utf-8")
            conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                         (lid, 1000 + lid, 1, ts, 0, 1, content, None))
        conn.commit()
        conn.close()
        api_chat._SHARD_INDEX.clear()
        return account, chat

    def test_same_second_messages_are_not_skipped(self):
        from siwx.server import app
        account, chat = self._make_same_second_account()
        client = app.test_client()
        base = f"/api/chat/messages?account={account}&chat={chat}&limit=3"

        page1 = client.get(base).get_json()
        self.assertEqual([m["id"] for m in page1["messages"]], [2, 3, 4])
        self.assertTrue(page1["has_more"])

        cursor = page1["messages"][0]
        url = (f"{base}&before={cursor['ts']}&before_id={cursor['id']}")
        page2 = client.get(url).get_json()
        # 修复前：before=100 只按 `ts < 100` 取 → id=1（同秒）永久丢失
        self.assertEqual(sorted(m["id"] for m in page2["messages"]), [1, 5, 6])
        self.assertFalse(page2["has_more"])

    def test_legacy_before_only_cursor_still_works(self):
        from siwx.server import app
        account, chat = self._make_same_second_account()
        client = app.test_client()
        page = client.get(
            f"/api/chat/messages?account={account}&chat={chat}&limit=10&before=100"
        ).get_json()
        self.assertEqual(sorted(m["id"] for m in page["messages"]), [5, 6])


# ── 13. HTML 导出 </script> / <!-- 注入 ─────────────────────────

class TestScriptSafeJson(unittest.TestCase):
    """消息或会话名含 </script> 会提前闭合 window.CHAT_DATA 的 script
    标签，导出的自包含网页损坏；含 <!-- 则令 script 进入双转义状态、
    模板写出的闭标签被吞。两者都必须转义。"""

    def test_closing_script_is_escaped(self):
        from siwx.html_template import render_html
        data = self._chat_data("</script><script>alert(1)</script>")
        html = render_html(data)
        blob = html.split("window.CHAT_DATA = ", 1)[1]
        # script 体里不允许再出现原始的 </script>（闭标签只有模板自己写的）
        body = blob[:blob.rindex(";</script>")]
        self.assertNotIn("</script>", body)
        import json
        self.assertEqual(json.loads(blob[:blob.index(";</script>")])
                         ["meta"]["sessionName"],
                         "</script><script>alert(1)</script>")

    def test_html_comment_is_escaped(self):
        # 只转义 </ 不转 <!-- 等于没修：<!-- 叠加 <script> 会令 HTML
        # 解析器吞掉模板写出的闭标签
        from siwx.html_template import render_html
        import json
        data = self._chat_data("<!--")
        html = render_html(data)
        blob = html.split("window.CHAT_DATA = ", 1)[1]
        self.assertEqual(json.loads(blob[:blob.index(";</script>")])
                         ["meta"]["sessionName"], "<!--")

    @staticmethod
    def _chat_data(name):
        from siwx.html_template import build_chat_data
        msgs = [{
            "createTime": 1, "senderUsername": "a", "senderDisplayName": "A",
            "localType": 1, "content": name, "rawContent": name, "isSend": 0,
        }]
        session = {"wxid": "room", "displayName": name, "isGroup": False,
                   "firstTimestamp": 1, "lastTimestamp": 1, "ownerId": "o",
                   "messageCount": 1}
        return build_chat_data(session, msgs, {})


# ── 14. pack="zip" 后 file 字段死链 ─────────────────────────────

class TestZipPackFileDeadLink(TempRootCase):
    """run_export 打包 zip 后删除整个导出目录，但返回值 file 仍指向
    目录内已删除的文件。「每会话一个 ZIP」（pack="each" → 每会话
    run_export(pack="zip")）模式下前端拿它渲染"下载文件"链接，点击
    必然 404。打包后 file 必须置空。"""

    def test_zip_pack_file_is_none_and_zip_exists(self):
        acc, account, chat = make_account(self.tmp)
        from siwx.exporter import run_export
        res = run_export(acc, account, chat, "测试好友", "json",
                         export_root=self.tmp / "exports", pack="zip")
        self.assertIsNone(res["file"], "zip 打包删除目录后 file 不应再指向死路径")
        self.assertTrue(Path(res["zip"]).is_file())
        # 不打包时 file 正常返回
        res2 = run_export(acc, account, chat, "测试好友", "json",
                          export_root=self.tmp / "exports", pack="none")
        self.assertTrue(Path(res2["file"]).is_file())


# ── 15. is_me 判定的账号目录名解析 ──────────────────────────────

class TestOwnerBase(TempRootCase):
    """is_me 依赖"从账号目录名 wxid_xxx_<uin> 推断本人原始 wxid"。
    原实现 split("_6")[0] 赌 uin 以 6 开头、且是任意子串匹配：
    uin 不以 6 开头时 is_me 全灭；wxid 本体含 6 开头段（如
    wxid_6abc_6409）时直接得到 "wxid"。也不能改用 media.clean_wxid()
    ——它对 wxid_a_b_1234 会切错，且语义被 MMKV 密钥派生依赖。"""

    def test_owner_base_matrix(self):
        from siwx.api_chat import owner_base
        cases = [
            ("wxid_abc_6409", "wxid_abc"),      # 常规：uin 以 6 开头
            ("wxid_abc_123456", "wxid_abc"),    # uin 不以 6 开头（旧实现切错）
            ("wxid_a_b_1234", "wxid_a_b"),      # wxid 本体含下划线
            ("wxid_6abc_6409", "wxid_6abc"),    # wxid 本体含 6 开头段
            ("my_custom_id", "my_custom_id"),   # 自定义账号 ID（无数字后缀）
            ("wxid_abc", "wxid_abc"),           # 无 uin 后缀
            ("", ""),
            (None, ""),
        ]
        for inp, want in cases:
            self.assertEqual(owner_base(inp), want, f"owner_base({inp!r})")

    def test_is_me_uses_owner_base(self):
        # uin 不以 6 开头的账号目录，自己发的消息 is_me / isSend 必须正确
        acc, account, chat = make_account(self.tmp, account="wxid_me_123456",
                                          chat="wxid_friend")
        db = acc / "message" / "message_0.db"
        t = _msg_table(chat)
        conn = sqlite3.connect(db)
        content = "wxid_me_123456:\n自己发的消息".encode("utf-8")
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (999, 1999, 1, 1_700_000_500, 1, None, content, None))
        conn.commit()
        conn.close()
        api_chat._SHARD_INDEX.clear()
        from siwx.export_stream import message_stream
        msgs = list(message_stream(acc, chat, account=account))
        mine = [m for m in msgs if m["localId"] == 999]
        self.assertTrue(mine and mine[0]["isSend"] == 1,
                        "uin 不以 6 开头时 is_me 判定失败")


# ── 16. 引用消息（外层 49、内层 57）──────────────────────────────

_QUOTE_49_XML = (
    '<appmsg type="57"><title><![CDATA[这是回复文本]]></title><type>57</type>'
    '<refermsg><displayname><![CDATA[张三]]></displayname>'
    '<content><![CDATA[被引用的原话]]></content>'
    '<createtime>1700000000</createtime></refermsg></appmsg>')
_LINK_49_XML = ('<appmsg type="5"><title>文章标题</title>'
                '<url>https://example.com/a</url><des>摘要</des></appmsg>')


class TestQuoteInType49(TempRootCase):
    """微信 5.0 库里引用消息外层 local_type=49、引用信息在内层 <refermsg>；
    原实现只在外层 57 时解析 → quote 恒为 null（实测 0/31437 条）。修复后
    49 + refermsg 必须走引用分支：quote 有值、link 为空、content 为纯回复
    文本；真链接消息（49 无 refermsg）不受影响。"""

    def _make(self):
        # 每个用例独立的子目录，避免 make_shard 重复建表冲突
        acc, account, chat = make_account(self.tmp / self._testMethodName,
                                          n_texts=0)
        db = acc / "message" / "message_0.db"
        t = _msg_table(chat)
        conn = sqlite3.connect(db)
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (1, 1001, 49, 1_700_000_100, 0, None,
                      _QUOTE_49_XML.encode("utf-8"), None))
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (2, 1002, 49, 1_700_000_200, 0, None,
                      _LINK_49_XML.encode("utf-8"), None))
        conn.commit()
        conn.close()
        api_chat._SHARD_INDEX.clear()
        return acc, account, chat

    def test_type49_with_refermsg_yields_quote(self):
        from siwx.export_stream import message_stream
        acc, account, chat = self._make()
        msgs = list(message_stream(acc, chat, account=account))
        q = next(m for m in msgs if m["localId"] == 1)
        self.assertIsNotNone(q["quote"])
        self.assertEqual(q["quote"]["displayname"], "张三")
        self.assertEqual(q["quote"]["content"], "被引用的原话")
        self.assertEqual(q["quote"]["ts"], 1700000000)
        self.assertIsNone(q["link"])
        self.assertEqual(q["content"], "这是回复文本")

    def test_type49_link_still_works(self):
        from siwx.export_stream import message_stream
        acc, account, chat = self._make()
        msgs = list(message_stream(acc, chat, account=account))
        lk = next(m for m in msgs if m["localId"] == 2)
        self.assertIsNone(lk["quote"])
        self.assertIsNotNone(lk["link"])
        self.assertEqual(lk["link"]["title"], "文章标题")
        self.assertEqual(lk["content"], "[链接] 文章标题")

    def test_build_messages_quote_for_web(self):
        acc, account, chat = self._make()
        data = api_chat.build_messages(acc, chat, account=account)
        q = next(m for m in data if m["localId"] == 1)
        self.assertIsNotNone(q["quote"])
        self.assertEqual(q["quote"]["displayname"], "张三")
        self.assertEqual(q["kind"], "quote")

    def test_html_renderer_shows_quote_not_undefined(self):
        from siwx.html_template import build_chat_data, render_html
        from siwx.export_stream import message_stream
        acc, account, chat = self._make()
        msgs = list(message_stream(acc, chat, account=account))
        session = {"wxid": chat, "displayName": "测试", "isGroup": False,
                   "firstTimestamp": 1, "lastTimestamp": 2, "ownerId": "o",
                   "messageCount": len(msgs)}
        html = render_html(build_chat_data(session, msgs, {}))
        self.assertIn('class="msg-quote"', html)
        self.assertIn("张三", html)
        self.assertIn("这是回复文本", html)
        blob = html.split("window.CHAT_DATA = ", 1)[1]
        self.assertNotIn("undefined", blob.split(";</script>")[0])


# ── 17. 媒体回填类型门禁（跨分片 local_id 撞号）──────────────────

class TestMediaAttachTypeGate(unittest.TestCase):
    """媒体映射以 (md5, localId, ts) 组合键为索引（语音为 (\x00voice, localId, ts)），
    多分片会话的 local_id 独立编号会撞号：文本/链接/系统消息不能被挂上属于
    其它消息的 mediaFile（实测 885 条）；跨分片同 localId 的图片之间由 md5
    区分（真实库 19210 个撞号键 / 350 会话）；图片槽位与语音槽位互斥。
    所有回填点必须走 _attach_media 门禁。"""

    MEDIA_MAP = {
        ("abc123", 5, 100): "media/0001_abc.jpg",
        ("def456", 5, 200): "media/0002_def.jpg",       # 同 localId 不同 md5
        ("\x00voice", 7, 300): "media/voice_0003_7.wav",
    }

    def _attach(self, msg):
        from siwx.exporter import _attach_media
        msg = dict(msg)
        _attach_media(msg, self.MEDIA_MAP)
        return msg.get("mediaFile")

    def test_gate_matrix(self):
        cases = [
            ({"localId": 5, "localType": 1, "md5": "abc123", "createTime": 100}, None),      # 文本撞号 → 不挂
            ({"localId": 5, "localType": 49, "md5": "abc123", "createTime": 100}, None),     # 链接撞号 → 不挂
            ({"localId": 5, "localType": 10000, "md5": "abc123", "createTime": 100}, None),  # 系统消息 → 不挂
            ({"localId": 5, "localType": 3, "md5": "abc123", "createTime": 100}, "media/0001_abc.jpg"),
            ({"localId": 5, "localType": 47, "md5": "abc123", "createTime": 100}, "media/0001_abc.jpg"),
            # 组合键根治跨分片互挂：同 localId、不同 md5 → 各挂各的图
            ({"localId": 5, "localType": 3, "md5": "def456", "createTime": 200}, "media/0002_def.jpg"),
            ({"localId": 7, "localType": 34, "createTime": 300}, "media/voice_0003_7.wav"),
            ({"localId": 7, "localType": 3, "createTime": 300}, None),      # 图片消息撞语音键 → 不挂
            ({"localId": 9, "localType": 3, "md5": "abc123", "createTime": 100}, None),      # 键不存在 → 不挂
            ({"localId": 5, "localType": 3, "md5": "abc123"}, None),        # ts 缺失 → 键不匹配 → 不挂
        ]
        for msg, want in cases:
            self.assertEqual(self._attach(msg), want, msg)

    def test_export_pipeline_logging_present(self):
        """报告二.1：导出管线失败不得静默——分片/媒体失败与条数对账必须有日志，
        且回填点全部走类型门禁、不允许裸 get。"""
        stream_src = (ROOT / "siwx" / "export_stream.py").read_text(encoding="utf-8")
        self.assertIn("分片打开失败", stream_src)
        exporter_src = (ROOT / "siwx" / "exporter.py").read_text(encoding="utf-8")
        self.assertIn("处理失败", exporter_src)
        self.assertIn("条数对账不一致", exporter_src)
        self.assertNotIn('msg["mediaFile"] = media_map.get', exporter_src)
        self.assertEqual(exporter_src.count("_attach_media(msg, media_map)"), 5)


# ── 18. _fmt 剥离 CDATA ─────────────────────────────────────────

class TestFmtCdataStrip(unittest.TestCase):
    """type 49 截取 <title> 时不剥 CDATA → content 出现 <![CDATA[...]]> 原文
    （实测某会话 835 条链接消息中 45 条含 CDATA）；引用消息（外层 49）的
    content 也不应带 "[链接]" 前缀。"""

    def test_link_title_cdata_stripped(self):
        from siwx.api_chat import _fmt
        self.assertEqual(
            _fmt(49, "<appmsg><title><![CDATA[标题A]]></title></appmsg>"),
            "[链接] 标题A")

    def test_quote_title_cdata_stripped(self):
        from siwx.api_chat import _fmt
        self.assertEqual(
            _fmt(57, "<msg><title><![CDATA[回复B]]></title></msg>"), "回复B")

    def test_refermsg_49_content_is_plain_title(self):
        from siwx.api_chat import _fmt
        self.assertEqual(_fmt(49, _QUOTE_49_XML), "这是回复文本")


# ── 19. 日志脱敏（键值对账号标识 + get_logs 默认脱敏）────────────

class TestLogDesensitize(unittest.TestCase):
    """报告二.3：脱敏此前只在 export_logs 生效；wxid/gh 之外的自定义微信号
    （如 wxalias_xxx）不在任何规则内。现在 get_logs 默认脱敏、日志页统一
    脱敏、新增键值对账号标识规则。"""

    def test_custom_account_kv_masked(self):
        from siwx.logger import desensitize_msg
        out = desensitize_msg(
            "[msg] 查询消息: account=wxalias_abc12345, chat=wxid_secret999")
        self.assertNotIn("wxalias_abc12345", out)
        self.assertIn("account=wxal***", out)
        self.assertNotIn("wxid_secret999", out)
        self.assertIn("wxid_***", out)

    def test_desensitize_idempotent(self):
        from siwx.logger import desensitize_msg
        once = desensitize_msg("account=wxalias_abc12345 key=" + "ab" * 32)
        twice = desensitize_msg(once)
        self.assertEqual(once, twice)

    def test_get_logs_desensitized_by_default(self):
        from siwx import logger as _logger
        _logger.rough("test", "chat=wxid_secret999 account=wxalias_abc12345")
        entries = _logger.get_logs(limit=10)
        text = " ".join(e[3] for e in entries)
        self.assertNotIn("wxid_secret999", text)
        self.assertNotIn("wxalias_abc12345", text)

    def test_log_page_and_settings_wired(self):
        server_src = (ROOT / "siwx" / "server.py").read_text(encoding="utf-8")
        self.assertIn("_desensitize_item", server_src)
        # 级别切换的双通道同步已抽到 siwx/loglevel.py（审计 §2.3 持久化改造）
        self.assertIn("_loglevel.apply", server_src)
        loglevel_src = (ROOT / "siwx" / "loglevel.py").read_text(encoding="utf-8")
        self.assertIn('logging.getLogger("siwx").setLevel', loglevel_src)


# ── 20. 日志级别切换：开关只控展示，文件通道恒 DEBUG ────────────

class TestLogLevelSwitchAffectsFileLog(unittest.TestCase):
    """P0 留存改造后的语义：detailed 埋点始终写 siwx.log（文件通道恒
    DEBUG），"先开 Debug 再复现"的范式废弃——偶发问题事后可在文件里找到
    当时记录。开关只控制环形缓冲/UI 展示（logger.get_level）。"""

    def test_file_channel_stays_debug_in_both_modes(self):
        import logging as stdlib_logging
        from siwx.server import app
        c = app.test_client()
        c.post("/api/logs/settings", json={"level": "detailed"})
        self.assertEqual(stdlib_logging.getLogger("siwx").level,
                         stdlib_logging.DEBUG)
        c.post("/api/logs/settings", json={"level": "rough"})
        # 关键回归点：ROUGH 模式文件通道仍是 DEBUG（此前这里断言 INFO，
        # P0 后该断言反向——若回到 INFO，detailed 落盘会被整体掐掉）
        self.assertEqual(stdlib_logging.getLogger("siwx").level,
                         stdlib_logging.DEBUG)

    def test_switch_still_controls_ring_level(self):
        from siwx import logger as _logger
        from siwx.server import app
        c = app.test_client()
        c.post("/api/logs/settings", json={"level": "detailed"})
        self.assertEqual(_logger.get_level(), _logger.LogLevel.DETAILED)
        c.post("/api/logs/settings", json={"level": "rough"})
        self.assertEqual(_logger.get_level(), _logger.LogLevel.ROUGH)


# ── 21. 引用解析修复（审计 v2.1 D1/D2/D3/D7/D8）────────────────

class TestParseReferHardening(unittest.TestCase):
    """_parse_refer 健壮性：D1 容忍属性、D7 空 content 不崩溃、
    D8 实体转义还原、D2/D3 精确类型开关。"""

    def test_d7_empty_content_no_crash(self):
        """refermsg 无 <content> / content 为空：此前对 None 做 re.search
        抛 TypeError（真实库 182 条，整会话导出失败 / 聊天页 500）。"""
        from siwx.api_chat import _parse_refer, parse_quote_or_link
        xml = ('<appmsg><title>回复</title><refermsg>'
               '<displayname>张三</displayname><createtime>1</createtime>'
               '</refermsg></appmsg>')
        q = _parse_refer(xml)
        self.assertIsNotNone(q)
        self.assertEqual(q["content"], "")
        # 空 content 且全空 text 也不崩溃
        self.assertIsNone(_parse_refer(""))
        self.assertIsNone(parse_quote_or_link(49, "<other/>")[0])

    def test_d1_refermsg_with_attributes(self):
        """<refermsg type="3"> 带属性形态：引用关系不再静默丢失。"""
        from siwx.api_chat import has_refermsg, _parse_refer
        xml = ('<appmsg><title>回复</title>'
               '<refermsg type="3" svrid="123">'
               '<displayname>张三</displayname>'
               '<content>&lt;msg&gt;&lt;img aeskey="k"/&gt;&lt;/msg&gt;</content>'
               '<createtime>1700000000</createtime></refermsg></appmsg>')
        self.assertTrue(has_refermsg(xml))
        q = _parse_refer(xml)
        self.assertIsNotNone(q)
        self.assertEqual(q["displayname"], "张三")
        self.assertEqual(q["content"], "[图片]")   # type=3 精确开关
        self.assertEqual(q["ts"], 1700000000)

    def test_d8_entity_escaped_nested_xml(self):
        """嵌套 XML 以 HTML 实体存储：quote.content 不再是 &lt;title&gt; 字面量
        （真实库 7358 条 / 23% 的"引用乱码"根因）。"""
        from siwx.api_chat import _parse_refer
        inner = ('&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;被引用的链接标题'
                 '&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;')
        xml = ('<appmsg><title>回复</title><refermsg>'
               '<displayname>张三</displayname>'
               f'<content>{inner}</content>'
               '<createtime>1</createtime></refermsg></appmsg>')
        q = _parse_refer(xml)
        self.assertEqual(q["content"], "被引用的链接标题")

    def test_d3_video_refer_label(self):
        """引用视频：不再把嵌套 XML 原文直出，按 type=43 给 [视频]。"""
        from siwx.api_chat import _parse_refer
        xml = ('<refermsg><displayname>李四</displayname>'
               '<content>&lt;msg&gt;&lt;videomsg aeskey="v"/&gt;&lt;/msg&gt;</content>'
               '<type>43</type></refermsg>')
        q = _parse_refer(xml)
        self.assertEqual(q["content"], "[视频]")

    def test_d2_no_loose_regex_misjudge(self):
        """datatype="3" 等子串不再误判为图片：refermsg type 全等才给 [图片]。"""
        from siwx.api_chat import _parse_refer
        inner = ('&lt;recordinfo&gt;&lt;dataitem datatype="3"&gt;x'
                 '&lt;/dataitem&gt;&lt;/recordinfo&gt;')
        xml = ('<refermsg><displayname>群</displayname>'
               f'<content>{inner}</content>'
               '<type>19</type></refermsg>')
        q = _parse_refer(xml)
        self.assertEqual(q["content"], "[聊天记录]")

    def test_export_stream_no_bare_refermsg_check(self):
        """export_stream 不再使用 "<refermsg>" 裸包含判断（D1 三处同步）。"""
        es_src = (ROOT / "siwx" / "export_stream.py").read_text(encoding="utf-8")
        self.assertNotIn('if "<refermsg>" in text', es_src)


# ── 22. 合并转发热解析（审计 D5）───────────────────────────────

class TestRecordinfoParsing(unittest.TestCase):
    """appmsg type=19 合并转发：逐条 dataitem 热解析，不再整包丢弃
    （真实库 2252 条 / 1206 条 datadesc 丢弃）。"""

    XML = ('<appmsg type="19"><title>张三和李四的聊天记录</title><recordinfo>'
           '<dataitem datatype="1" dataid="1"><sourcename>张三</sourcename>'
           '<sourcetime>1700000001</sourcetime>'
           '<datadesc><![CDATA[你好]]></datadesc></dataitem>'
           '<dataitem datatype="2" dataid="2"><sourcename>李四</sourcename>'
           '<sourcetime>1700000002</sourcetime>'
           '<datadesc><![CDATA[[图片]]></datadesc></dataitem>'
           '<dataitem datatype="1" dataid="3"><sourcename>张三</sourcename>'
           '<sourcetime>1700000001</sourcetime>'
           '<datadesc><![CDATA[你好]]></datadesc></dataitem>'
           '</recordinfo></appmsg>')

    def test_parse_recordinfo_items(self):
        from siwx.api_chat import _parse_recordinfo
        rec = _parse_recordinfo(self.XML)
        self.assertEqual(rec["title"], "张三和李四的聊天记录")
        # 非相邻的合法重复（同图连发两次）不再被全局去重折叠（微信原样显示）
        self.assertEqual(rec["count"], 3)
        self.assertEqual(rec["items"][0]["sender"], "张三")
        self.assertEqual(rec["items"][0]["text"], "你好")
        self.assertEqual(rec["items"][0]["time"], "")   # epoch sourcetime → 走 ts
        self.assertEqual(rec["items"][0]["ts"], 1700000001)

    def test_parse_recordinfo_adjacent_dedup(self):
        """仅紧邻的完全重复折叠（防解析瑕疵双计）。"""
        from siwx.api_chat import _parse_recordinfo
        xml = ('<appmsg type="19"><title>t</title><recordinfo>'
               '<dataitem datatype="1"><sourcename>a</sourcename>'
               '<sourcetime>1</sourcetime><datadesc><![CDATA[x]]></datadesc></dataitem>'
               '<dataitem datatype="1"><sourcename>a</sourcename>'
               '<sourcetime>1</sourcetime><datadesc><![CDATA[x]]></datadesc></dataitem>'
               '</recordinfo></appmsg>')
        self.assertEqual(_parse_recordinfo(xml)["count"], 1)

    def test_parse_recordinfo_sourcetime_string(self):
        """sourcetime 为 "YYYY-MM-DD HH:MM" 字符串（服务器端形态）不再丢时间。"""
        from siwx.api_chat import _parse_recordinfo
        xml = ('<appmsg type="19"><title>t</title><recordinfo>'
               '<dataitem datatype="8" dataid="x"><sourcename>张三</sourcename>'
               '<sourcetime>2025-12-20 19:47</sourcetime>'
               '<datatitle>资料.pdf</datatitle></dataitem>'
               '</recordinfo></appmsg>')
        it = _parse_recordinfo(xml)["items"][0]
        self.assertEqual(it["ts"], 0)
        self.assertEqual(it["time"], "2025-12-20 19:47")
        self.assertEqual(it["text"], "资料.pdf")

    def test_parse_recordinfo_server_side_des(self):
        """服务器端合并记录（<type>19</type> 无 dataitem）→ des 逐行拆子消息。"""
        from siwx.api_chat import _parse_recordinfo, _is_recordinfo, parse_quote_or_link
        xml = ('<?xml version="1.0"?>\n<msg>\n<appmsg appid="" sdkver="0">\n'
               '\t<title>张三和李四的聊天记录</title>\n'
               '\t<des>张三: 你好呀\n李四: [图片]\n没有冒号的行</des>\n'
               '\t<type>19</type>\n</appmsg></msg>')
        self.assertTrue(_is_recordinfo(xml))
        quote, link, record, _ch = parse_quote_or_link(49, xml)
        self.assertIsNone(link)
        self.assertIsNotNone(record)
        self.assertEqual(record["title"], "张三和李四的聊天记录")
        self.assertEqual([(i["sender"], i["text"]) for i in record["items"]],
                         [("张三", "你好呀"), ("李四", "[图片]"), ("", "没有冒号的行")])

    def test_fmt_49_recordinfo_summary(self):
        from siwx.api_chat import _fmt
        out = _fmt(49, self.XML)
        self.assertTrue(out.startswith("[聊天记录] 张三和李四的聊天记录"))
        self.assertIn("张三: 你好", out)
        self.assertIn("3 条", out)

    def test_fmt_49_file_label(self):
        """带 fileext/totallen 的 49 是文件消息，content 前缀不再误标 [链接]。"""
        from siwx.api_chat import _fmt
        xml = ('<appmsg appid="" sdkver="0"><title>新建文档.docx</title>'
               '<totallen>14260</totallen><fileext>docx</fileext></appmsg>')
        out = _fmt(49, xml)
        self.assertTrue(out.startswith("[文件]"), out)

    def test_fmt_49_transfer_composite_label(self):
        """packed_info 复合 localType（转账 8589934592049）不再标成 [链接]。"""
        from siwx.api_chat import _fmt
        out = _fmt(8589934592049,
                   '<appmsg><title><![CDATA[转账给张三]]></title></appmsg>')
        self.assertEqual(out, "[转账] 转账给张三")

    def test_parse_quote_or_link_record(self):
        from siwx.api_chat import parse_quote_or_link
        quote, link, record, _ch = parse_quote_or_link(49, self.XML)
        self.assertIsNone(quote)
        self.assertIsNone(link)
        self.assertIsNotNone(record)


# ── 23. S6/S1：密钥不落盘 + 更新链 fail-closed ─────────────────

class TestManifestKeyStrip(unittest.TestCase):
    """S6：缓存清单不再持久化 SQLCipher 明文密钥，旧文件加载即剥离。"""

    def test_sanitize_manifest_strips_key(self):
        from siwx.pool import sanitize_manifest, manifest_has_keys
        m = {"message_0.db": {"size": 1, "mtime": 2, "pages": 3, "key": "ab" * 32},
             "@source": "d:/x"}
        clean = sanitize_manifest(m)
        self.assertNotIn("key", clean["message_0.db"])
        self.assertEqual(clean["@source"], m["@source"])
        self.assertFalse(manifest_has_keys(clean))
        self.assertTrue(manifest_has_keys(m))

    def test_save_load_roundtrip_no_key(self):
        import tempfile
        from pathlib import Path
        from siwx.pool import load_manifest, save_manifest, manifest_has_keys
        with tempfile.TemporaryDirectory() as td:
            save_manifest(Path(td), {"a.db": {"size": 1, "key": "cd" * 32}})
            m = load_manifest(Path(td))
            self.assertFalse(manifest_has_keys(m))

    def test_extract_manifest_no_key_written(self):
        src = (ROOT / "siwx" / "extract.py").read_text(encoding="utf-8")
        self.assertNotIn('"pages": pages, "key": key_hex', src)


class TestUpdateChainHardening(unittest.TestCase):
    """S1：更新校验 fail-closed、下载/哈希域名白名单。"""

    def test_verify_sha256_empty_expected_fails_closed(self):
        from siwx.auto_update import _verify_sha256
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "x.bin"
            f.write_bytes(b"hello")
            self.assertFalse(_verify_sha256(f, ""))          # 空期望 → 拒绝
            self.assertFalse(_verify_sha256(f, "0" * 64))    # 哈希不匹配 → 拒绝

    def test_asset_url_host_whitelist(self):
        from siwx.auto_update import _asset_urls
        remote = {"version": "9.9.9",
                  "assets": {"windows": "https://evil.example.com/x.exe"}}
        urls = _asset_urls(remote, "windows")
        self.assertEqual(len(urls), 1)  # 非白名单域名被剔除，只剩 GitHub 兜底
        self.assertIn("github.com", urls[0])

    def test_sha_url_host_whitelist(self):
        from siwx.auto_update import _get_asset_sha
        self.assertEqual(_get_asset_sha(
            {"version": "9.9.9", "sha256": "https://evil.example.com/SUMS"},
            "windows"), "")


# ── 24. S2：Host 头校验（DNS rebinding 防护）───────────────────

class TestHostGuard(unittest.TestCase):
    def test_localhost_allowed(self):
        from siwx.server import app
        c = app.test_client()
        r = c.get("/api/update/current", headers={"Host": "127.0.0.1:8787"})
        self.assertEqual(r.status_code, 200)

    def test_evil_host_rejected(self):
        from siwx.server import app
        c = app.test_client()
        r = c.get("/api/update/current", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 403)


# ── 25. P0：detailed 留存语义（始终落盘，开关只控展示）────────────

class TestDetailedRetention(unittest.TestCase):
    """P0 回归：detailed 不再随 ROUGH 开关整体丢弃。

    - ROUGH 模式：detailed 进 _FILE_LOG（导出可见）但进不了 siwx.log 文件
      ——文件通道由 loglevel.apply 恒置 DEBUG 保证，此处不重复断言；
    - ring=False：逐条高频埋点只绕环形缓冲，不绕文件缓冲。"""

    def test_rough_mode_detailed_still_in_file_log(self):
        from siwx import logger as _logger
        _logger.set_level(_logger.LogLevel.ROUGH)
        before = len(_logger._FILE_LOG)
        _logger.detailed("test", "P0 留存断言标记 abc123")
        self.assertEqual(len(_logger._FILE_LOG), before + 1,
                         "ROUGH 模式 detailed 也必须进 _FILE_LOG（留存语义）")

    def test_ring_false_skips_ring_but_keeps_file_log(self):
        from siwx import logger as _logger
        _logger.set_level(_logger.LogLevel.DETAILED)
        ring_before = len(_logger._LOG_RING)
        file_before = len(_logger._FILE_LOG)
        _logger.detailed("test", "ring=False 断言标记", ring=False)
        self.assertEqual(len(_logger._LOG_RING), ring_before,
                         "ring=False 不得进环形缓冲")
        self.assertEqual(len(_logger._FILE_LOG), file_before + 1,
                         "ring=False 必须仍进文件缓冲")


# ── 26. P1：导出链路修复（server_id 透传 / HTML 真流式）──────────

class TestExportServerIdPassthrough(TempRootCase):
    """P1-1 回归：_shard_iter 此前解包出 server_id 却丢弃，
    message_stream 重组 row 时写死 None——导出 platformMessageId 恒空串、
    语音导出 svr_id 恒 0（voice.get_voice 少一条命中路径）。"""

    def test_platform_message_id_not_empty(self):
        acc, account, chat = make_account(self.tmp, n_texts=5)
        from siwx.export_stream import message_stream
        msgs = list(message_stream(acc, chat, account=account))
        self.assertEqual(len(msgs), 5)
        for m in msgs:
            self.assertTrue(m["platformMessageId"],
                            "platformMessageId 不应再恒为空串")


class TestHtmlStreamingExport(TempRootCase):
    """P1-2 回归：HTML 导出改真流式后，产出与全量 build_chat_data 路线
    数据等价（CHAT_DATA JSON 可解析、消息数一致、正文完整）。"""

    def test_streamed_html_data_matches(self):
        import json as _json
        import re as _re
        acc, account, chat = make_account(self.tmp, n_texts=8)
        from siwx.exporter import run_export
        res = run_export(acc, account, chat, "测试会话", "html",
                         want_media=False, want_avatars=False,
                         export_root=self.tmp / "exports", pack="folder")
        html = Path(res["file"]).read_text(encoding="utf-8")
        # 真流式产出的 CHAT_DATA 仍是合法 JSON（meta/members/messages 完整）
        blob = html.split("window.CHAT_DATA = ", 1)[1].split(";</script>", 1)[0]
        data = _json.loads(blob)
        self.assertEqual(data["meta"]["messageCount"], 8)
        self.assertEqual(len(data["messages"]), 8)
        self.assertEqual(data["messages"][0]["content"], "第 0 条消息")
        # JS 渲染器依赖的头部/尾部结构未被流式改写破坏
        self.assertIn("const MSG_COUNT = 8;", html)
        self.assertIn("</html>", html)


# ── 26b. 微信小黄脸内嵌 + 名片/位置/通话渲染 ─────────────────────

class TestWxFaces(unittest.TestCase):
    """wx_faces：官方表情名称表与素材一致性；文本扫描；按需 dataURI。"""

    def test_assets_complete(self):
        import base64 as _b64
        from siwx import wx_faces
        faces = wx_faces.load_faces()
        self.assertEqual(len(faces), len(wx_faces.NAMES))
        for name, b64 in faces.items():
            raw = _b64.b64decode(b64)
            self.assertTrue(raw.startswith(b"\x89PNG"), name)
        # 抽查核心名称在表里
        for n in ("[微笑]", "[破涕为笑]", "[旺柴]", "[吃瓜]", "[裂开]", "[666]"):
            self.assertIn(n, faces)

    def test_find_used(self):
        from siwx import wx_faces
        self.assertEqual(wx_faces.find_used("你好[微笑][破涕为笑]"),
                         {"[微笑]", "[破涕为笑]"})
        # 未知 [xxx] 不是表情
        self.assertEqual(wx_faces.find_used("[不存在的表情]"), set())
        # 引用块里的表情也被收集
        used = wx_faces.used_from_message(
            {"content": "[旺柴]", "quote": {"content": "[吃瓜]"}})
        self.assertEqual(used, {"[旺柴]", "[吃瓜]"})

    def test_datauris_filters_unknown(self):
        from siwx import wx_faces
        out = wx_faces.datauris(["[微笑]", "[不存在的]"])
        self.assertEqual(set(out), {"[微笑]"})
        self.assertTrue(out["[微笑]"].startswith("data:image/png;base64,"))


class TestHtmlFaceInjection(unittest.TestCase):
    """HTML 导出：[表情名] → 官方表情图（按需 base64 注入 window.WX_FACES）。"""

    def test_used_faces_embedded_only(self):
        from siwx.html_template import render_html
        data = self._chat_data("早[微笑]晚[破涕为笑]")
        html = render_html(data)
        self.assertIn("window.WX_FACES", html)
        # 只嵌入用到的 2 张
        self.assertEqual(html.count("data:image/png;base64,"), 2)
        # 渲染器把 [表情名] 替换为 wx-face 图片（静态契约）
        self.assertIn('class="wx-face"', html)
        self.assertNotIn('alt="[微笑]"', html)  # alt 由 JS 运行时生成

    def test_no_faces_no_injection(self):
        from siwx.html_template import render_html
        data = self._chat_data("普通文本消息")
        html = render_html(data)
        # 渲染器注释里含 WX_FACES 字样，这里断言的是注入语句本身
        self.assertNotIn("window.WX_FACES =", html)

    def test_stream_tail_injects_faces(self):
        import io
        from siwx.html_template import stream_html_tail
        from siwx import wx_faces
        buf = io.StringIO()
        stream_html_tail(buf, 3, "会话",
                         faces=wx_faces.datauris({"[微笑]"}))
        out = buf.getvalue()
        self.assertIn("window.WX_FACES =", out)
        self.assertIn("data:image/png;base64,", out)
        # faces 为空时不注入空对象
        buf2 = io.StringIO()
        stream_html_tail(buf2, 0, "会话")
        self.assertNotIn("window.WX_FACES =", buf2.getvalue())

    def test_renderer_branches_present(self):
        """渲染器静态契约：名片(42)/位置(48)/通话(50) 分支与地图跳转。"""
        from siwx.html_template import get_template
        R = get_template().renderer
        self.assertIn("t === 42", R)
        self.assertIn("xmlVal(raw, 'nickname')", R)
        self.assertIn("t === 48", R)
        self.assertIn("xmlAttr(raw, 'location', 'x')", R)
        # 跳转用 URI API marker（旧 poi 接口已废弃 HTTP 501）；无坐标回退 search
        self.assertIn("apis.map.qq.com/uri/v1/marker", R)
        self.assertIn("apis.map.qq.com/uri/v1/search", R)
        self.assertIn("t === 50", R)
        self.assertIn("xmlVal(raw, 'calltype')", R)
        self.assertIn("xmlVal(raw, 'duration')", R)
        # 表情替换应用于正文/引用/系统消息
        self.assertIn("fmtText(sc)", R)
        self.assertIn("fmtText(msg.quote.content)", R)
        self.assertIn("return fmtText(content);", R)
        # 本轮新增：合并转发/转文字/CDN 回退/撤回文案/文件大小/红包收窄
        self.assertIn("msg.record) return renderRecord", R)
        self.assertIn("voiceTrans(raw)", R)
        self.assertIn("xmlAttr(raw, 'emoji', 'cdnurl')", R)
        self.assertIn("xmlVal(sc, 'replacemsg')", R)
        self.assertIn("xmlVal(raw, 'totallen')", R)
        self.assertIn("raw.indexOf('<wcpayinfo')", R)
        # 类型名映射补全（此前 50/42/48 裸露为 "类型50"）
        self.assertIn("42:'名片'", R)
        self.assertIn("48:'位置'", R)
        self.assertIn("50:'通话'", R)
        self.assertIn("10002:'撤回'", R)
        # 文件卡图标（无 url 的文件消息不再挂 🔗）；记录卡子消息时间；esc 转义引号
        self.assertIn("finfo ? '📄' : '🔗'", R)
        self.assertIn("recordItemTime", R)
        self.assertIn(".replace(/'/g, '&#39;')", R)

    @staticmethod
    def _chat_data(content):
        from siwx.html_template import build_chat_data
        msgs = [{
            "createTime": 1, "senderUsername": "a", "senderDisplayName": "A",
            "localType": 1, "content": content, "rawContent": "", "isSend": 0,
        }]
        session = {"wxid": "room", "displayName": "会话", "isGroup": False,
                   "firstTimestamp": 1, "lastTimestamp": 1, "ownerId": "o",
                   "messageCount": 1}
        return build_chat_data(session, msgs, {})


class TestHtmlFacesEndToEnd(TempRootCase):
    """全链路：分片里带 [表情名] 的真实导出，WX_FACES 只含用到的表情。"""

    def test_export_embeds_used_faces(self):
        acc = self.tmp / "output" / "wxid_test"
        msg_dir = acc / "message"
        make_shard(msg_dir / "message_0.db", "wxid_friend",
                   ["带表情[微笑]", "再一个[旺柴]"])
        make_empty_shard(msg_dir / "media_0.db")
        make_empty_shard(msg_dir / "message_fts.db")
        self._make_contact_session(acc)
        from siwx.exporter import run_export
        res = run_export(acc, "wxid_test", "wxid_friend", "测试会话", "html",
                         want_media=False, want_avatars=False,
                         export_root=self.tmp / "exports", pack="folder")
        html = Path(res["file"]).read_text(encoding="utf-8")
        self.assertIn("window.WX_FACES =", html)
        self.assertEqual(html.count("data:image/png;base64,"), 2)
        self.assertIn('"[微笑]"', html)
        self.assertIn('"[旺柴]"', html)
        # WX_FACES 对象本身合法 JSON
        blob = html.split("window.WX_FACES = ", 1)[1].split(";</script>", 1)[0]
        data = json.loads(blob)
        self.assertEqual(set(data), {"[微笑]", "[旺柴]"})

    @staticmethod
    def _make_contact_session(acc):
        (acc / "contact").mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(acc / "contact" / "contact.db")
        c.execute("CREATE TABLE contact (username TEXT, remark TEXT, "
                  "nick_name TEXT, alias TEXT)")
        c.execute("INSERT INTO contact VALUES (?,?,?,?)",
                  ("wxid_friend", "测试好友", "", ""))
        c.commit()
        c.close()
        (acc / "session").mkdir(parents=True, exist_ok=True)
        s = sqlite3.connect(acc / "session" / "session.db")
        s.execute("CREATE TABLE SessionTable (username TEXT, summary TEXT, "
                  "sort_timestamp INTEGER)")
        s.execute("INSERT INTO SessionTable VALUES (?,?,?)",
                  ("wxid_friend", "预览", 1_700_000_010))
        s.commit()
        s.close()


# ── 26b+. 位置静态缩略图（wx_maps） ─────────────────────────────

LOCATION_RAW = ('<location poiname="腾讯滨海大厦" label="深圳市南山区科技园" '
                'x="22.540503" y="113.934428" scale="16"/>')


class TestWxMaps(unittest.TestCase):
    """wx_maps：瓦片键计算、位置消息解析、尽力下载与缓存/离线降级。"""

    def setUp(self):
        from siwx import wx_maps
        wx_maps.reset()

    def test_tile_key_known_values(self):
        from siwx import wx_maps
        self.assertEqual(wx_maps.tile_key(22.540503, 113.934428),
                         "15/26754/14277")
        self.assertEqual(wx_maps.tile_key(39.9042, 116.4074), "15/26979/12416")
        # 越界坐标收敛到合法瓦片范围
        k = wx_maps.tile_key(-85.2, 200.0)
        z, x, y = k.split("/")
        self.assertTrue(0 <= int(x) <= 32767 and 0 <= int(y) <= 32767)

    def test_used_from_message(self):
        from siwx import wx_maps
        self.assertEqual(wx_maps.used_from_message(
            {"localType": 48, "rawContent": LOCATION_RAW}),
            {"15/26754/14277"})
        # entry 形态（localType 缺省时回退 type 字段）
        self.assertEqual(wx_maps.used_from_message(
            {"type": 48, "rawContent": LOCATION_RAW}), {"15/26754/14277"})
        # 非 48 / 无坐标 / 坐标非法 → 空集
        self.assertEqual(wx_maps.used_from_message(
            {"localType": 1, "rawContent": LOCATION_RAW}), set())
        self.assertEqual(wx_maps.used_from_message(
            {"localType": 48, "rawContent": "<msg/>"}), set())
        self.assertEqual(wx_maps.used_from_message(
            {"localType": 48, "rawContent":
             '<location x="999" y="999"/>'}), set())

    def test_datauris_cache_and_offline(self):
        import base64 as _b64
        from siwx import wx_maps
        calls = []
        orig = wx_maps._fetch_tile
        wx_maps._fetch_tile = lambda key: (calls.append(key) or b"\x89PNGfake")
        try:
            out = wx_maps.datauris({"15/26754/14277", "15/26979/12416"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(out["15/26754/14277"],
                             "data:image/png;base64," +
                             _b64.b64encode(b"\x89PNGfake").decode())
            # 命中缓存后重复调用不再下载
            wx_maps.datauris({"15/26754/14277"})
            self.assertEqual(len(calls), 2)
        finally:
            wx_maps._fetch_tile = orig

    def test_datauris_failure_sticky_offline(self):
        from siwx import wx_maps
        calls = []
        orig = wx_maps._fetch_tile
        def boom(key):
            calls.append(key)
            raise OSError("offline")
        wx_maps._fetch_tile = boom
        try:
            self.assertEqual(wx_maps.datauris({"15/26754/14277"}), {})
            # 首次失败后本进程内不再尝试（多会话导出不逐个付超时）
            self.assertEqual(wx_maps.datauris({"15/26979/12416"}), {})
            self.assertEqual(len(calls), 1)
        finally:
            wx_maps._fetch_tile = orig
        wx_maps.reset()
        wx_maps._fetch_tile = lambda key: b"\x89PNGfake"
        try:
            out = wx_maps.datauris({"15/26754/14277"})
            self.assertEqual(set(out), {"15/26754/14277"})
        finally:
            wx_maps._fetch_tile = orig


class TestHtmlMapInjection(unittest.TestCase):
    """HTML 注入契约：WX_MAPS 仅在有瓦片时注入；渲染器含缩略图分支。"""

    def test_stream_tail_injects_maps(self):
        import io
        from siwx.html_template import stream_html_tail
        buf = io.StringIO()
        stream_html_tail(buf, 1, "会话",
                         maps={"15/26754/14277": "data:image/png;base64,AA"})
        self.assertIn("window.WX_MAPS =", buf.getvalue())
        buf2 = io.StringIO()
        stream_html_tail(buf2, 0, "会话")
        self.assertNotIn("window.WX_MAPS =", buf2.getvalue())

    def test_render_html_injects_maps_for_location(self):
        from siwx.html_template import render_html
        from siwx import wx_maps
        wx_maps.reset()
        orig = wx_maps._fetch_tile
        wx_maps._fetch_tile = lambda key: b"\x89PNGfake"
        try:
            data = TestHtmlFaceInjection._chat_data("x")
            data["messages"][0]["type"] = 48
            data["messages"][0]["rawContent"] = LOCATION_RAW
            html = render_html(data)
            self.assertIn("window.WX_MAPS =", html)
            self.assertIn('"15/26754/14277"', html)
        finally:
            wx_maps._fetch_tile = orig
            wx_maps.reset()

    def test_renderer_map_contract(self):
        from siwx.html_template import get_template
        tpl = get_template()
        self.assertIn("window.WX_MAPS", tpl.renderer)
        self.assertIn("function tileKey", tpl.renderer)
        self.assertIn('class="wx-map"', tpl.renderer)
        self.assertIn("wx-card-mapleft", tpl.renderer)
        self.assertIn(".wx-map{", tpl.head)


class TestHtmlMapsEndToEnd(TempRootCase):
    """全链路：真实导出 → 位置消息命中的瓦片注入 WX_MAPS；离线回退文字卡。"""

    def _make_account_with_location(self):
        acc = self.tmp / "output" / "wxid_test"
        msg_dir = acc / "message"
        make_shard(msg_dir / "message_0.db", "wxid_friend",
                   ["到达附近了[微笑]"])
        conn = sqlite3.connect(msg_dir / "message_0.db")
        t = _msg_table("wxid_friend")
        conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                     (2, 1001, 48, 1_700_000_001, 0, 1,
                      LOCATION_RAW.encode("utf-8"), None))
        conn.commit()
        conn.close()
        make_empty_shard(msg_dir / "media_0.db")
        make_empty_shard(msg_dir / "message_fts.db")
        TestHtmlFacesEndToEnd._make_contact_session(acc)
        return acc

    def _run_export(self, acc):
        from siwx.exporter import run_export
        return run_export(acc, "wxid_test", "wxid_friend", "测试会话", "html",
                          want_media=False, want_avatars=False,
                          export_root=self.tmp / "exports", pack="folder")

    def setUp(self):
        super().setUp()
        from siwx import wx_maps
        wx_maps.reset()

    def test_export_embeds_location_tile(self):
        acc = self._make_account_with_location()
        from siwx import wx_maps
        orig = wx_maps._fetch_tile
        wx_maps._fetch_tile = lambda key: b"\x89PNGfake"
        try:
            res = self._run_export(acc)
        finally:
            wx_maps._fetch_tile = orig
        html = Path(res["file"]).read_text(encoding="utf-8")
        self.assertIn("window.WX_MAPS =", html)
        self.assertIn('"15/26754/14277"', html)
        # 渲染器分支与表情注入并存
        self.assertIn("window.WX_FACES =", html)
        self.assertIn('"[微笑]"', html)

    def test_export_offline_keeps_text_card(self):
        acc = self._make_account_with_location()
        from siwx import wx_maps
        orig = wx_maps._fetch_tile
        wx_maps._fetch_tile = lambda key: (_ for _ in ()).throw(OSError("x"))
        try:
            res = self._run_export(acc)
        finally:
            wx_maps._fetch_tile = orig
        html = Path(res["file"]).read_text(encoding="utf-8")
        self.assertNotIn("window.WX_MAPS =", html)
        # 坐标与跳转链接仍随 rawContent 在，渲染器回退文字卡 + marker 链接
        self.assertIn("22.540503", html)
        self.assertIn("apis.map.qq.com/uri/v1/marker", html)


# ── 26d. 本人身份判定（微信4.x 设备后缀账号名）+ 正向翻页 ────────

class TestSelfIdentity(TempRootCase):
    """is_me 判定：账号目录名带十六进制设备后缀（wxid_xxx_d29b）时，
    本人消息（发送者为不带后缀的原始 wxid）不得被错判为对方。
    实测案例：743 条本人文本落左侧、15 条通话错落右侧。"""

    def _make_hex_account(self, contacts):
        """账号目录名 wxid_abc_d29b；contacts 是放进 contact 表的 username 列表。"""
        acc = self.tmp / "output" / "wxid_abc_d29b"
        if acc.exists():
            shutil.rmtree(acc)
        msg_dir = acc / "message"
        msg_dir.mkdir(parents=True, exist_ok=True)
        (acc / "contact").mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(acc / "contact" / "contact.db")
        c.execute("CREATE TABLE contact (username TEXT, remark TEXT, "
                  "nick_name TEXT, alias TEXT)")
        for u in contacts:
            c.execute("INSERT INTO contact VALUES (?,?,?,?)", (u, u, u, ""))
        c.commit(); c.close()
        # Name2Id: 1=peer, 2=原始 wxid, 3=设备后缀变体；三条消息各指一个
        chat = "wxid_peer"
        t = _msg_table(chat)
        conn = sqlite3.connect(msg_dir / "message_0.db")
        conn.execute(f"""CREATE TABLE [{t}] (
            local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
            create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
            message_content BLOB, packed_info_data BLOB)""")
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        for i, u in enumerate((chat, "wxid_abc", "wxid_abc_d29b"), start=1):
            conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (?,?)",
                         (i, u))
        for i, (ts, rsid) in enumerate([(1_700_000_001, 2),   # 本人（原始 wxid）
                                        (1_700_000_002, 1),   # 对方
                                        (1_700_000_003, 3),   # 本人（后缀变体）
                                        (1_700_000_004, 2)]):  # 本人（原始 wxid）
            conn.execute(f"INSERT INTO [{t}] VALUES (?,?,?,?,?,?,?,?)",
                         (i + 1, 1000 + i, 1, ts, 0, rsid,
                          f"消息{i}".encode("utf-8"), None))
        conn.commit(); conn.close()
        return acc, chat

    def test_message_stream_marks_self_correctly(self):
        acc, chat = self._make_hex_account(["wxid_abc", "wxid_peer"])
        from siwx.export_stream import message_stream
        msgs = list(message_stream(acc, chat, None, None,
                                   "wxid_abc_d29b", None))
        self.assertEqual([(m["isSend"], m["senderUsername"]) for m in msgs],
                         [(1, "wxid_abc"),      # 原始 wxid → 本人
                          (0, "wxid_peer"),     # 对方不受影响
                          (1, "wxid_abc"),      # 设备后缀变体 → 本人，归一到原始 wxid
                          (1, "wxid_abc")])
        # 归一后本人名字可解析
        self.assertEqual(msgs[2]["senderDisplayName"], "wxid_abc")

    def test_self_ids_gating(self):
        """剥十六进制后缀必须有联系人佐证：目录名与 base 都存在 → 不剥。"""
        from siwx.api_chat import self_ids_for
        acc, _chat = self._make_hex_account(["wxid_abc", "wxid_peer"])
        preferred, ids = self_ids_for(acc, "wxid_abc_d29b")
        self.assertEqual(preferred, "wxid_abc")
        self.assertEqual(set(ids), {"wxid_abc", "wxid_abc_d29b"})
        # 完整目录名也是联系人（罕见但可能是真实 wxid）→ 不剥离
        acc2, _ = self._make_hex_account(["wxid_abc", "wxid_abc_d29b"])
        preferred2, ids2 = self_ids_for(acc2, "wxid_abc_d29b")
        self.assertEqual(preferred2, "wxid_abc_d29b")
        self.assertEqual(set(ids2), {"wxid_abc_d29b"})
        # 联系人表里谁都不存在 → 维持旧行为（不剥离）
        acc3, _ = self._make_hex_account([])
        preferred3, ids3 = self_ids_for(acc3, "wxid_abc_d29b")
        self.assertEqual(preferred3, "wxid_abc_d29b")
        self.assertEqual(set(ids3), {"wxid_abc_d29b"})

    def test_web_chat_page_renders_record(self):
        """网页端聊天页与导出 HTML 同口径：合并转发逐条展开，不再单行预览。"""
        src = (ROOT / "siwx" / "ui" / "pages" / "chat.js").read_text(encoding="utf-8")
        self.assertIn("m.record", src)
        self.assertIn("m-record-title", src)
        self.assertIn("m-record-item", src)
        self.assertIn("m-record-count", src)

    def test_messages_api_forward_paging_and_owner(self):
        """/api/chat/messages：after 正向翻页、has_newer、owner 字段。"""
        acc, account, chat = make_account(self.tmp, n_texts=6)
        from siwx.server import app
        client = app.test_client()
        base = (f"/api/chat/messages?account={account}&chat={chat}")
        r1 = client.get(f"{base}&limit=2").get_json()
        self.assertEqual(len(r1["messages"]), 2)          # 最新的 2 条
        self.assertFalse(r1["has_newer"])                 # 打开会话=最新窗口
        self.assertEqual(r1["owner"], account)            # 本人 wxid（无后缀场景）
        # 反向翻页带游标 → 窗口之后还有消息
        before_ts = r1["messages"][0]["ts"]
        r2 = client.get(f"{base}&limit=2&before={before_ts}").get_json()
        self.assertTrue(r2["has_newer"])
        # 正向从 r2 页尾继续 → 回到 r1 的内容；6 条消息 limit 2 时
        # r3 已是最后一页，has_more=False
        last = r2["messages"][-1]
        r3 = client.get(f"{base}&limit=2&after={last['ts']}"
                        f"&after_id={last['id']}").get_json()
        self.assertEqual([m["id"] for m in r3["messages"]],
                         [m["id"] for m in r1["messages"]])
        self.assertFalse(r3["has_more"])
        # 越过最后一页 → 空页且 has_more=False
        last3 = r3["messages"][-1]
        r4 = client.get(f"{base}&limit=2&after={last3['ts']}"
                        f"&after_id={last3['id']}").get_json()
        self.assertEqual(r4["messages"], [])
        self.assertFalse(r4["has_more"])


# ── 26c. HTML 模板可替换框架 + 合并转发/转文字渲染 ───────────────

class TestHtmlTemplateFramework(TempRootCase):
    """模板包解析：内置 default 可用、用户目录同名覆盖、缺失报错。"""

    def test_builtin_default_loads(self):
        from siwx.html_template import get_template, list_templates
        tpl = get_template("default")
        self.assertIn("<!DOCTYPE html>", tpl.head)
        self.assertIn("renderContent", tpl.renderer)
        names = [t["name"] for t in list_templates()]
        self.assertIn("default", names)
        self.assertTrue([t for t in list_templates() if t["name"] == "default"]
                        [0]["builtin"])

    def test_user_template_overrides_builtin(self):
        # TempRootCase 把 SIWX_ROOT 指向临时目录 → 用户模板根 <tmp>/templates
        root = self.tmp / "templates" / "mytpl"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(
            json.dumps({"name": "mytpl", "label": "我的模板"}, ensure_ascii=False),
            encoding="utf-8")
        (root / "head.html").write_text("<!DOCTYPE html><title>mytpl</title>",
                                        encoding="utf-8")
        (root / "renderer.js").write_text("// custom", encoding="utf-8")
        from siwx.html_template import get_template
        tpl = get_template("mytpl")
        self.assertEqual(tpl.label, "我的模板")
        self.assertIn("mytpl", tpl.head)
        # list 中标记为非内置
        from siwx.html_template import list_templates
        me = [t for t in list_templates() if t["name"] == "mytpl"][0]
        self.assertFalse(me["builtin"])

    def test_missing_template_raises(self):
        from siwx.html_template import get_template
        with self.assertRaises(RuntimeError):
            get_template("no_such_template")

    def test_stream_head_tail_use_template(self):
        import io
        from siwx.html_template import stream_html_head, stream_html_tail
        session = {"wxid": "c", "displayName": "会话", "isGroup": False,
                   "firstTimestamp": 0, "lastTimestamp": 0, "ownerId": "o",
                   "messageCount": 0}
        buf = io.StringIO()
        stream_html_head(buf, session, [], {}, template="default")
        stream_html_tail(buf, 0, "会话", template="default")
        self.assertIn("<!DOCTYPE html>", buf.getvalue())
        self.assertIn("MSG_COUNT = 0", buf.getvalue())

    def test_export_with_unknown_template_fails_clean(self):
        acc, account, chat = make_account(self.tmp, n_texts=2)
        from siwx.exporter import run_export
        with self.assertRaises(RuntimeError):
            run_export(acc, account, chat, "测试会话", "html",
                       want_media=False, want_avatars=False,
                       export_root=self.tmp / "exports", pack="folder",
                       template="no_such_template")

    def test_templates_api_endpoint(self):
        from siwx.server import app
        client = app.test_client()
        r = client.get("/api/export/templates")
        self.assertEqual(r.status_code, 200)
        names = [t["name"] for t in r.get_json()["templates"]]
        self.assertIn("default", names)


class TestHtmlRenderFixes(unittest.TestCase):
    """不足清单修复：record 透传与嵌套渲染。"""

    def test_msg_entry_passes_record(self):
        from siwx.html_template import _msg_entry
        rec = {"title": "甲和乙的聊天记录", "count": 2,
               "items": [{"sender": "甲", "ts": 1, "text": "你好"}]}
        m = {"createTime": 1, "senderUsername": "a", "senderDisplayName": "A",
             "localType": 49, "content": "[聊天记录] 甲和乙的聊天记录",
             "rawContent": "<recordinfo/>", "isSend": 0, "record": rec}
        self.assertEqual(_msg_entry(m)["record"], rec)

    def test_render_record_html(self):
        from siwx.html_template import render_html
        rec = {"title": "甲和乙的聊天记录", "count": 2,
               "items": [{"sender": "甲", "ts": 1, "text": "你好[微笑]"},
                         {"sender": "乙", "ts": 2, "text": "[图片]"}]}
        msgs = [{"createTime": 1, "senderUsername": "a", "senderDisplayName": "A",
                 "localType": 49, "content": "[聊天记录] 甲和乙的聊天记录",
                 "rawContent": "<recordinfo/>", "isSend": 0, "record": rec}]
        session = {"wxid": "room", "displayName": "会话", "isGroup": False,
                   "firstTimestamp": 1, "lastTimestamp": 1, "ownerId": "o",
                   "messageCount": 1}
        html = render_html(self._chat_data(msgs))
        # record 数据完整进入 CHAT_DATA（渲染由 JS 运行时完成）
        self.assertIn('"record"', html)
        self.assertIn("甲和乙的聊天记录", html)
        # 渲染器含 record 展开逻辑与条数脚注模板
        self.assertIn('class="msg-record"', html)
        self.assertIn("rec.count || items.length", html)

    @staticmethod
    def _chat_data(msgs):
        from siwx.html_template import build_chat_data
        session = {"wxid": "room", "displayName": "会话", "isGroup": False,
                   "firstTimestamp": 1, "lastTimestamp": 1, "ownerId": "o",
                   "messageCount": len(msgs)}
        return build_chat_data(session, msgs, {})


# ── 27. P2：fallback_labels 精确判定（前缀误报修复）────────────────

class TestFallbackExactMatch(unittest.TestCase):
    """P2-1 回归：合法链接卡 "[链接] 标题"、合并转发 "[聊天记录] 标题（N 条…）"
    曾被 startswith 前缀判定误计为兜底文案；修复后只有恰等于兜底标签原文
    （解析真失败）或 "[类型N]" 才计数。"""

    def _observe(self, content, t=49):
        from siwx.exporter import _ExportStats
        st = _ExportStats()
        st.observe({"localType": t, "content": content, "localId": 1,
                    "createTime": 0, "rawContent": ""})
        return st.fallback_labels

    def test_prefixed_success_not_fallback(self):
        self.assertEqual(self._observe("[链接] 文章标题"), 0)
        self.assertEqual(self._observe("[聊天记录] 群聊的聊天记录（3 条：a: b）"), 0)
        self.assertEqual(self._observe("[转账] 请收款"), 0)

    def test_exact_label_is_fallback(self):
        self.assertEqual(self._observe("[链接]"), 1)
        self.assertEqual(self._observe("[引用]", t=57), 1)

    def test_unknown_type_pattern_is_fallback(self):
        self.assertEqual(self._observe("[类型4321]", t=4321), 1)


class TestWxgfSubprocessAndIntegrity(unittest.TestCase):
    """wxgf 转码子进程隔离 + 解密产物完整性（2026-10-06 家族群占位符排查）。

    背景：VoipEngine.dll 在长驻进程内随机 access violation（crash.log
    2026-10-05 21:51 整进程崩溃），旧实现把转码失败的原始 wxgf 以 200 +
    image/wxgf 透传且写入缓存 → 浏览器解码失败显示"原图未下载"、重试无效。
    """

    def test_finalize_wxgf_failure_returns_none(self):
        """转码失败必须返回 None，绝不透传 wxgf 字节。"""
        from siwx import media
        orig = media.convert_wxgf
        media.convert_wxgf = lambda data: None
        try:
            self.assertIsNone(media._finalize(b"wxgf\x13\x00", "wxgf", "image/wxgf"))
        finally:
            media.convert_wxgf = orig

    def test_finalize_wxgf_success(self):
        from siwx import media
        orig = media.convert_wxgf
        media.convert_wxgf = lambda data: b"\xff\xd8\xff\xd9"
        try:
            body, ext, ctype = media._finalize(b"wxgf\x13\x00", "wxgf", "image/wxgf")
            self.assertEqual((body, ext, ctype), (b"\xff\xd8\xff\xd9", "jpeg", "image/jpeg"))
        finally:
            media.convert_wxgf = orig

    def test_plausible_image_jpeg_tail(self):
        from siwx import media
        self.assertTrue(media._plausible_image(b"\xff\xd8\xff" + b"\x00" * 100 + b"\xff\xd9"))
        # 转码产物尾部带填充（实测 DLL 在 FFD9 后附 26B）仍应通过
        padded = b"\xff\xd8\xff" + b"\x00" * 100 + b"\xff\xd9" + b"\x00" * 26
        self.assertTrue(media._plausible_image(padded))
        # 半截文件（白熊图 _h.dat 形态：头好尾坏）必须拒绝
        self.assertFalse(media._plausible_image(b"\xff\xd8\xff" + b"\x00" * 100))

    def test_plausible_image_png_gif(self):
        from siwx import media
        self.assertTrue(media._plausible_image(b"\x89PNG" + b"\x00" * 50 + b"IEND" + b"\x00" * 4))
        self.assertFalse(media._plausible_image(b"\x89PNG" + b"\x00" * 64))
        self.assertTrue(media._plausible_image(b"GIF89a" + b"\x00" * 20 + b"\x3b"))
        self.assertFalse(media._plausible_image(b"GIF89a" + b"\x00" * 20))

    def test_worker_only_accepts_converted_output(self):
        """worker 的有效签名表不含 wxgf：wxgf→wxgf 视为失败，杜绝透传回归。"""
        from siwx import media
        sigs_line = [l for l in media._WXGF_WORKER.splitlines()
                     if l.startswith("_SIGS")][0]
        self.assertNotIn("wxgf", sigs_line)


class TestAuditFixes20261006(TempRootCase):
    """2026-10-06 审计复核结论的回归：信任边界、失败契约、导出页渲染、媒体定位。"""

    @staticmethod
    def _client():
        from siwx.server import app
        return app.test_client()

    # ── 信任边界 ──────────────────────────────────────────────

    def test_validate_account_rejects_traversal(self):
        from siwx import validate
        # 正则本身允许 "."（账号名里有它），穿越必须靠 resolve()+relative_to 兜住
        self.assertTrue(validate.valid_account(".."))
        self.assertIsNone(validate.account_dir(".."))
        self.assertIsNone(validate.account_dir("../x"))
        for bad in ("a/b", "a\\b", "", None, 123, "C:/Windows"):
            self.assertFalse(validate.valid_account(bad), bad)
            self.assertIsNone(validate.account_dir(bad), bad)
        self.assertTrue(validate.valid_account("wxid_abc_1234"))
        self.assertTrue(validate.valid_account("wxalias_example_01"))

    def test_settings_clear_rejects_path_wxid(self):
        """POST /api/settings/clear 的 wxid 此前裸拼路径，可 rmtree 任意目录。"""
        from siwx.server import app
        r = app.test_client().post("/api/settings/clear",
                                   json={"kind": "output", "wxid": "C:/Windows"})
        self.assertEqual(r.status_code, 400)

    def test_run_rejects_out_dir_outside_output_root(self):
        from siwx.server import app
        r = app.test_client().post("/api/run", json={
            "mode": "export", "out_dir": str(self.tmp / "elsewhere")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("out_dir", r.get_json()["error"])

    def test_run_rejects_unrecognized_db_dir(self):
        from siwx.server import app
        r = app.test_client().post("/api/run", json={
            "mode": "auto", "db_dir": str(self.tmp / "not-a-wechat-dir")})
        self.assertEqual(r.status_code, 400)

    def test_sns_emoji_rejects_non_cdn_url(self):
        """表情 url 来自好友评论 XML，属于 GET 参数，必须过 CDN 白名单。"""
        import json as _json
        from siwx.server import app
        spec = _json.dumps({"url": "http://169.254.169.254/latest/meta-data/"})
        r = app.test_client().get("/api/sns/emoji", query_string={"emoji": spec})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json().get("reason"), "not-cdn")

    def test_artifact_origin_pinned_to_repo_path(self):
        from siwx import auto_update
        self.assertTrue(auto_update._manifest_url_allowed(
            "https://github.com/ImUpXuu/SIWX/releases/download/v5.0.6/x.exe"))
        # 只校验 host 时 github.com/attacker/... 也会放行 —— 必须拒绝
        self.assertFalse(auto_update._manifest_url_allowed(
            "https://github.com/attacker/SIWX/releases/download/v1/evil.exe"))
        self.assertFalse(auto_update._manifest_url_allowed("https://evil.com/x.exe"))
        self.assertFalse(auto_update._manifest_url_allowed(""))

    def test_version_field_must_be_semver(self):
        """version 会拼进文件名/安装目标/正则替换模板，必须严格校验。"""
        from siwx import auto_update
        self.assertTrue(auto_update.valid_version("5.0.7"))
        self.assertTrue(auto_update.valid_version("5.0.7-rc1"))
        for bad in ("9.9.9/../../../../Startup/evil", "5.0", "", None, "v5.0.7", ".."):
            self.assertFalse(auto_update.valid_version(bad), bad)

    def test_cdn_fetch_verifies_tls(self):
        """生产媒体链路默认必须校验证书（此前 check_hostname=False + CERT_NONE）。"""
        src = (ROOT / "siwx" / "sns_cdn.py").read_text(encoding="utf-8")
        self.assertNotIn("verify_mode = ssl.CERT_NONE", src)
        self.assertNotIn("ctx.check_hostname = False", src)
        self.assertIn("ctx = ssl.create_default_context()", src)

    # ── 失败契约 ──────────────────────────────────────────────

    def test_job_exposes_error_field(self):
        """前端四处（export/settings/onboarding）判 job.error，此前该字段从不返回。"""
        from siwx import server
        with server._lock:
            server._job.update({"error": "RuntimeError: boom", "ok": False,
                                "done": True, "running": False, "logs": [], "report": None})
        try:
            body = server.app.test_client().get("/api/job").get_json()
            self.assertEqual(body["error"], "RuntimeError: boom")
        finally:
            with server._lock:
                server._job.update({"error": None, "ok": False, "done": False,
                                    "running": False, "logs": [], "report": None})

    def test_start_job_poll_survives_transient_failure(self):
        """common.js 的 startJob：单次 fetch 失败不得终止轮询（旧实现 return 掉）。"""
        src = (ROOT / "siwx" / "ui" / "common.js").read_text(encoding="utf-8")
        self.assertIn("failStreak", src)
        self.assertIn("与后端失去联系", src)

    def test_messages_rejects_non_numeric_cursor(self):
        """裸 int() 会把客户端参数错误变成 500 + 完整 traceback 回显。"""
        acc, account, chat = make_account(self.tmp, n_texts=1)
        c = self._client()
        for qs in ("before=abc", "before_id=abc", "after=abc", "after_id=abc", "limit=abc"):
            r = c.get(f"/api/chat/messages?account={account}&chat={chat}&{qs}")
            self.assertEqual(r.status_code, 400, qs)
            self.assertIn("参数无效", r.get_json()["error"])
        # 合法参数仍然 200
        self.assertEqual(c.get(
            f"/api/chat/messages?account={account}&chat={chat}&limit=10").status_code, 200)

    def test_logs_limit_non_numeric_400(self):
        self.assertEqual(self._client().get("/api/logs?limit=abc").status_code, 400)

    def test_logs_export_shares_log_page_sources(self):
        """日志导出与日志页同源（此前导出只有进程内 _FILE_LOG，重启即空）。"""
        from siwx import server, logger as _logger
        _logger.rough("test", "导出同源校验标记 XYZ")
        text = self._client().get("/api/logs/export").get_data(as_text=True)
        self.assertIn("XYZ", text)
        self.assertIn("] [", text)

    def test_sns_export_empty_is_not_a_failure(self):
        """"没有符合条件的动态"是空结果，不是失败（旧实现 ok=False → 红错态）。"""
        from siwx import sns_export
        old = sns_export._iter_feeds
        try:
            sns_export._iter_feeds = lambda *a, **k: iter(())
            res = sns_export.run_sns_export(self.tmp / "sns.db", "wxalias_example_01", fmt="json")
        finally:
            sns_export._iter_feeds = old
        self.assertTrue(res["ok"])
        self.assertTrue(res["empty"])
        self.assertEqual(res["count"], 0)

    # ── 数据正确性 ────────────────────────────────────────────

    def test_chat_count_dedupes_shards(self):
        """一个会话的表散在多个分片时，会话数只能算 1（旧实现 2.39× 虚高）。"""
        acc, account, chat = make_account(self.tmp, n_texts=3)
        make_shard(acc / "message" / "message_1.db", chat, ["另一个分片的同会话消息"],
                   start_ts=1_700_000_100)
        from siwx import stats
        raw = stats.compute_stats(account, force=True)
        self.assertEqual(raw["chat_count"], 1)
        self.assertEqual(stats.summarize(raw)["chat_count"], 1)

    def test_stats_range_filter_scopes_chat_count(self):
        """会话数必须跟随日期筛选（旧实现取全量值，无视区间）。"""
        from siwx import stats
        acc, account, chat = make_account(self.tmp, n_texts=3)
        # 另一个会话，时间戳落在 2020 年（默认夹具在 2023-11）
        make_shard(acc / "message" / "message_1.db", "wxid_other", ["别的时间段"],
                   start_ts=1_600_000_000)
        raw = stats.compute_stats(account, force=True)
        self.assertEqual(raw["chat_count"], 2)
        only_2023 = stats.summarize(raw, start="2023-01-01", end="2023-12-31")
        self.assertEqual(only_2023["chat_count"], 1)
        only_2020 = stats.summarize(raw, start="2020-01-01", end="2020-12-31")
        self.assertEqual(only_2020["chat_count"], 1)

    def test_voice_does_not_leak_across_sessions(self):
        """不同会话共享同一 local_id 时，裸 local_id=? 会捞到别的会话的语音。

        构造：目标语音在 media_0.db，media_1.db 里有一条 chat_name_id 属于别的
        会话、但 local_id 相同的记录。旧实现先在 media_1.db 用裸 local_id 命中
        并直接返回 → 跨会话音频泄漏；修复后必须继续走到 media_0.db。
        """
        from siwx import voice
        acc, account, chat = make_account(self.tmp, n_texts=1)
        msg_dir = acc / "message"

        decoy = msg_dir / "media_1.db"
        conn = sqlite3.connect(decoy)
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (chat,))
        conn.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER, "
                     "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT)")
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                     (2, 1_700_000_000, 9, 0, b"\x02#!SILK_V3WRONG", "0"))
        conn.commit(); conn.close()

        conn = sqlite3.connect(msg_dir / "media_0.db")
        conn.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER, "
                     "local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT)")
        conn.execute("INSERT INTO VoiceInfo VALUES (?,?,?,?,?,?)",
                     (1, 1_700_000_000, 9, 0, b"\x02#!SILK_V3RIGHT", "0"))
        conn.commit(); conn.close()

        data, _info = voice.get_voice(acc, chat=chat, local_id=9, svr_id=0, ts=0)
        self.assertEqual(data, b"#!SILK_V3RIGHT")

    def test_extract_md5_ignores_longer_attributes(self):
        """originsourcemd5 等属性名里含 md5=，必须有左边界。"""
        from siwx import media
        real, decoy = "b" * 32, "a" * 32
        self.assertEqual(
            media.extract_md5_from_xml(f'<msg originsourcemd5="{decoy}" md5="{real}"/>'),
            real)
        self.assertIsNone(media.extract_md5_from_xml(
            f'<msg originsourcemd5="{decoy}"/>'))
        self.assertIsNone(media.extract_md5_from_xml(f'<msg androidmd5="{decoy}"/>'))

    def test_bubble_paths_match_exact_local_id_and_ts(self):
        """<id>*.dat 会命中 91_… 等同目录兄弟文件，必须带下划线边界与 ts。"""
        from siwx import media
        root = self.tmp / "cache"
        target = hashlib.md5(b"wxid_friend").hexdigest()
        d = root / "2025-01" / "Message" / target / "Bubble"
        d.mkdir(parents=True, exist_ok=True)
        (d / "9_1700000000_b.dat").write_bytes(b"x")
        (d / "91_1700000000_b.dat").write_bytes(b"y")
        old_find = media._wechat_cache_roots
        media._CACHE_ROOTS_MEMO.clear()
        try:
            media._wechat_cache_roots = lambda _wxid: [root]
            got = media.bubble_paths("wxid_friend", "wxid_friend", 9, 1_700_000_000)
        finally:
            media._wechat_cache_roots = old_find
            media._CACHE_ROOTS_MEMO.clear()
        self.assertEqual([p.name for p in got], ["9_1700000000_b.dat"])

    def test_save_key_cache_uses_unique_temp_name(self):
        """固定 media_key.tmp 在多进程导出下会互相踩（写坏即丢全部派生密钥）。"""
        from siwx import media
        src = (ROOT / "siwx" / "media.py").read_text(encoding="utf-8")
        self.assertIn("mkstemp", src)
        self.assertNotIn('p.with_suffix(".tmp")', src)

    def test_index_and_type_filter_regressions(self):
        """导出页类型的类型筛选与 href scheme 白名单（A-7 / A-4）。"""
        src = (ROOT / "siwx" / "templates" / "default" / "renderer.js").read_text(encoding="utf-8")
        # activeTypes 必须与 m.type（数值）同型，否则 Set.has 恒假、列表被清空
        self.assertIn("activeTypes.has(Number(t.dataset.type))", src)
        self.assertIn("Number(this.dataset.type)", src)
        # 链接卡 href 必须过 scheme 白名单
        self.assertIn("function safeUrl(", src)
        self.assertIn("var href = safeUrl(url);", src)

    def test_decode_content_degrades_visibly(self):
        from siwx import api_chat
        out = api_chat._decode_content(b"\xff\xfeabc")
        self.assertTrue(out)                      # 不再整条置空
        self.assertIn("abc", out)
        self.assertNotEqual(api_chat._decode_content(b"\x28\xb5\x2f\xfdbroken"), "")

    def test_xlsx_writer_is_streaming(self):
        from siwx import exporter
        src = (ROOT / "siwx" / "exporter.py").read_text(encoding="utf-8")
        self.assertIn("Workbook(write_only=True)", src)


# ── 21. None 解引用加固（手改配置/异常库值）─────────────────────

class TestNullConfigHardening(TempRootCase):
    """手改 mcp_config.json 把 "tools" 写成 null 时，`.get("tools", {})`
    拿到的是 None（键存在值为 null，默认值不生效）→ 每一次 MCP 工具调用
    与 /api/mcp/info 都会 AttributeError。加固后必须正常降级。"""

    def _write_null_tools_config(self):
        import json as _json
        from siwx import mcp_server
        p = mcp_server.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"tools": None}), encoding="utf-8")

    def test_tool_enabled_with_null_tools(self):
        from siwx import mcp_server
        self._write_null_tools_config()
        self.assertTrue(mcp_server.tool_enabled("get_status"))

    def test_mcp_info_endpoint_with_null_tools(self):
        from siwx.server import app
        self._write_null_tools_config()
        c = app.test_client()
        r = c.get("/api/mcp/info")
        self.assertEqual(r.status_code, 200)
        self.assertIn("tools", r.get_json())


# ── 22. macOS 支持（kvcomm 密钥发现 + 图片导出扩展名）───────────

class TestFindKvcommCodesMacOS(unittest.TestCase):
    """find_kvcomm_codes() 此前只扫 Windows 路径
    (C:/Users/*/AppData/...)，macOS 上恒返回空列表 ->
    candidate_keys() 恒无候选 -> 账号级媒体密钥永远推导不出来 ->
    所有聊天图片/朋友圈图片解密失败（不只是原图未下载的那部分）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_test_kvcomm_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_macos_container_kvcomm_discovered(self):
        from siwx import media
        kv = (self.tmp / "Library" / "Containers" / "com.tencent.xinWeChat" /
              "Data" / "Documents" / "app_data" / "net" / "kvcomm")
        kv.mkdir(parents=True)
        (kv / "key_3377726147_4066647381_1_1_1_3600_input.statistic").write_bytes(b"")
        # 非数字 code 的同名兄弟文件不应被误当成 code 命中
        (kv / "key_reportnow_1_2_3_4_5_input.statistic").write_bytes(b"")

        with mock.patch("siwx.media.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media.Path.home", return_value=self.tmp):
            codes = media.find_kvcomm_codes()
        self.assertEqual(codes, [3377726147])

    def test_no_container_dir_returns_empty_not_raises(self):
        from siwx import media
        with mock.patch("siwx.media.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media.Path.home", return_value=self.tmp):
            self.assertEqual(media.find_kvcomm_codes(), [])

    def test_windows_path_unaffected(self):
        """非 Darwin 平台不应触碰 macOS 专属逻辑（回归保护，防止条件写反）。"""
        from siwx import media
        with mock.patch("siwx.media.platform.system", return_value="Windows"):
            self.assertEqual(media.find_kvcomm_codes(), [])


class TestMediaExportExtensionFromInfo(TempRootCase):
    """_try_decrypt() 原实现只认 info 里有没有 "png"/"gif" 子串，其余
    （wxgf 转码失败的原样留档、heic）一律落 else 写成 .jpg——文件名是
    .jpg，字节却是别的格式，导出结果里是一份打不开的损坏文件。"""

    def setUp(self):
        super().setUp()
        self.acc, self.account, self.chat = make_account(self.tmp, n_texts=6)

    def _run_with_fake_image(self, body: bytes, ctype: str):
        from siwx import exporter as ex

        def fake_get_image(account, md5, acc_dir, **kw):
            return body, ctype

        old = ex.media.get_image
        ex.media.get_image = fake_get_image
        try:
            dest = self.tmp / "media_out"
            dest.mkdir(parents=True, exist_ok=True)
            return ex._try_decrypt(str(self.acc), self.account, self.chat,
                                   "a" * 32, None, 1, 1_700_000_000,
                                   dest / "0000_aaaaaaaaaaaa.jpg")
        finally:
            ex.media.get_image = old

    def test_heic_not_mislabeled_as_jpg(self):
        out, reason = self._run_with_fake_image(b"\x00" * 32, "image/heic")
        self.assertIsNotNone(out)
        self.assertEqual(reason, "")
        self.assertEqual(out.suffix, ".heic")

    def test_wxgf_not_mislabeled_as_jpg(self):
        out, reason = self._run_with_fake_image(b"wxgf" + b"\x00" * 28, "image/wxgf")
        self.assertIsNotNone(out)
        self.assertEqual(out.suffix, ".wxgf")

    def test_jpeg_still_works(self):
        out, reason = self._run_with_fake_image(b"\xff\xd8\xff" + b"\x00" * 29, "image/jpeg")
        self.assertIsNotNone(out)
        self.assertEqual(out.suffix, ".jpeg")


class TestMediaBackup(unittest.TestCase):
    """新增 siwx/media_backup.py —— 直接枚举 msg/attach、msg/video 下磁盘上
    实际存在的文件逐个解密/复制，不要求能定位到具体消息。用于"删除微信
    本地数据前先确认媒体已安全导出"：保证的是文件级 100% 覆盖，不依赖
    exporter.py 那条以消息为中心、可能受跨分片 local_id 撞号等边界情况
    影响而漏导个别文件的路径。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_test_mediabackup_"))
        self.wxid = "wxid_test"
        self.account_root = self.tmp / "xwechat_files" / self.wxid
        (self.account_root / "cache").mkdir(parents=True)
        self.attach_root = (self.account_root / "msg" / "attach" / "chatmd5"
                            / "2026-01" / "Img")
        self.attach_root.mkdir(parents=True)
        self.video_root = self.account_root / "msg" / "video" / "2026-01"
        self.video_root.mkdir(parents=True)
        self.file_root = self.account_root / "msg" / "file" / "2026-01"
        self.file_root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_roots(self):
        return mock.patch("siwx.media._wechat_cache_roots",
                          return_value=[self.account_root / "cache"])

    def test_images_classified_by_content_type(self):
        """可预览(jpeg) / wxgf 转码失败后原样保留 / 真失败，三种结果各自
        正确归类，wxgf 不被误标成打不开的假 jpg。转码本身走哪条平台分支
        不是这个用例关心的（见 TestMediaBackupWxgfConversion），这里只
        验证转码失败时"原样保留 wxgf"这条兜底路径。"""
        from siwx import media_backup
        (self.attach_root / "a.dat").write_bytes(b"fake-jpeg-bytes")
        (self.attach_root / "b.dat").write_bytes(b"fake-wxgf-bytes")
        (self.attach_root / "c.dat").write_bytes(b"fake-fail-bytes")

        def fake_decrypt_any(data, wxid):
            if data == b"fake-jpeg-bytes":
                return b"\xff\xd8\xff" + b"\x00" * 10, "image/jpeg"
            if data == b"fake-wxgf-bytes":
                return b"wxgf" + b"\x00" * 10, "image/wxgf"
            return None, None

        with self._patch_roots(), \
             mock.patch("siwx.media._decrypt_any", side_effect=fake_decrypt_any), \
             mock.patch("siwx.media_backup.platform.system", return_value="Linux"), \
             mock.patch("siwx.media.convert_wxgf", return_value=None):
            stats = media_backup.backup_images(self.wxid, self.tmp / "out_images",
                                               log=None)

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["ok_viewable"], 1)
        self.assertEqual(stats["ok_wxgf_preserved"], 1)
        self.assertEqual(stats["failed"], 1)
        out_dir = self.tmp / "out_images" / "chatmd5" / "2026-01" / "Img"
        self.assertTrue((out_dir / "a.jpeg").is_file())
        self.assertTrue((out_dir / "b.wxgf").is_file())
        self.assertFalse((out_dir / "c.jpeg").exists())
        self.assertFalse((out_dir / "c.wxgf").exists())

    def test_videos_copied_without_decryption(self):
        """视频实测未加密（ISO Media/MP4 容器），原样复制即可，不经过
        media._decrypt_any。"""
        from siwx import media_backup
        raw = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20
        (self.video_root / "x.mp4").write_bytes(raw)

        with self._patch_roots():
            stats = media_backup.backup_videos(self.wxid, self.tmp / "out_videos",
                                               log=None)

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["failed"], 0)
        out_file = self.tmp / "out_videos" / "2026-01" / "x.mp4"
        self.assertTrue(out_file.is_file())
        self.assertEqual(out_file.read_bytes(), raw)

    def test_rerun_on_readonly_source_video_does_not_fail(self):
        """实测踩坑：微信落盘的源视频文件本身是只读的（-r--r--r--）。旧实现
        用 shutil.copy2() 连权限位也一起拷到目标文件——第一次备份后目标
        也变只读，第二次运行（新消息到达后重新备份）对同名目标文件再次
        copy2() 等于覆盖写一个只读文件，当场 PermissionError（实测 1926
        个视频里 1913 个"已备份过的"全部这样假性失败，数据其实一直都在）。
        模拟"已存在只读目标 + 内容不变"的重复运行场景，必须直接跳过，
        不碰权限位也不报错。"""
        from siwx import media_backup
        raw = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20
        src = self.video_root / "x.mp4"
        src.write_bytes(raw)
        src.chmod(0o444)  # 源文件只读，与微信实测落盘权限一致
        dst = self.tmp / "out_videos" / "2026-01" / "x.mp4"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(raw)
        dst.chmod(0o444)  # 模拟第一次备份后、被 copy2() 带只读的旧产物

        with self._patch_roots():
            stats = media_backup.backup_videos(self.wxid, self.tmp / "out_videos",
                                               log=None)

        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(dst.read_bytes(), raw)

    def test_backup_all_combines_images_and_videos(self):
        from siwx import media_backup
        (self.attach_root / "a.dat").write_bytes(b"fake-jpeg-bytes")
        (self.video_root / "x.mp4").write_bytes(b"videobytes")
        (self.file_root / "report.pdf").write_bytes(b"%PDF-1.7 fake pdf bytes")

        with self._patch_roots(), \
             mock.patch("siwx.media._decrypt_any",
                        return_value=(b"\xff\xd8\xff" + b"\x00" * 10, "image/jpeg")):
            report = media_backup.backup_all(self.wxid, self.tmp / "out_all", log=None)

        self.assertEqual(report["images"]["ok_viewable"], 1)
        self.assertEqual(report["videos"]["ok"], 1)
        self.assertEqual(report["files"]["ok"], 1)

    def test_files_copied_without_decryption_keeps_original_name(self):
        """msg/file 下的文件消息附件实测同样未加密、原文件名/扩展名原样
        保留在磁盘上（PDF/APK/PNG 头部签名完好），直接复制即可，不经过
        media._decrypt_any。"""
        from siwx import media_backup
        raw = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 20
        (self.file_root / "导师管理系统使用手册(1).pdf").write_bytes(raw)

        with self._patch_roots():
            stats = media_backup.backup_files(self.wxid, self.tmp / "out_files",
                                              log=None)

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["failed"], 0)
        out_file = self.tmp / "out_files" / "2026-01" / "导师管理系统使用手册(1).pdf"
        self.assertTrue(out_file.is_file())
        self.assertEqual(out_file.read_bytes(), raw)

    def test_files_skips_ds_store(self):
        from siwx import media_backup
        (self.file_root / ".DS_Store").write_bytes(b"junk")
        (self.file_root / "real.apk").write_bytes(b"PK\x03\x04" + b"\x00" * 10)

        with self._patch_roots():
            stats = media_backup.backup_files(self.wxid, self.tmp / "out_files",
                                              log=None)

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["ok"], 1)
        self.assertFalse((self.tmp / "out_files" / "2026-01" / ".DS_Store").exists())

    def test_files_rerun_on_readonly_source_does_not_fail(self):
        """同一只读权限位坑（见 backup_videos 的回归用例）：msg/file 下的
        源文件也是只读的，重新运行备份时必须直接跳过已存在且内容不变的
        目标文件，不碰权限位也不报错。"""
        from siwx import media_backup
        raw = b"PK\x03\x04" + b"\x00" * 20
        src = self.file_root / "app.apk"
        src.write_bytes(raw)
        src.chmod(0o444)
        dst = self.tmp / "out_files" / "2026-01" / "app.apk"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(raw)
        dst.chmod(0o444)

        with self._patch_roots():
            stats = media_backup.backup_files(self.wxid, self.tmp / "out_files",
                                              log=None)

        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(dst.read_bytes(), raw)

    def test_no_account_dir_returns_empty_stats_not_raises(self):
        from siwx import media_backup
        with mock.patch("siwx.media._wechat_cache_roots", return_value=[]):
            stats = media_backup.backup_images("wxid_nonexistent",
                                               self.tmp / "out_none", log=None)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["failed"], 0)


class TestMediaBackupWxgfConversion(unittest.TestCase):
    """backup_images() 对解密出来的 wxgf 按平台分流转码——macOS 走
    media_wxgf_macos 的批量接口（一次 attach 处理一批，不逐张开销）；
    其它平台走 media.convert_wxgf() 的逐张调用（Windows VoipEngine.dll
    句柄内部已缓存，逐张调用开销不大）。两条路径都要能在转码失败时
    干净回退到"原样保留 wxgf"，不让整个备份中断。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_test_wxgfconv_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_macos_path_uses_batch_api_and_cleans_up_source(self):
        from siwx import media_backup
        wxgf = self.tmp / "a.wxgf"
        wxgf.write_bytes(b"fake-wxgf")
        heic = wxgf.with_suffix(".heic")
        pending = [(wxgf, heic)]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}

        def fake_batch(items, log=None):
            for src, dst in items:
                dst.write_bytes(b"HEIC-BYTES")
            return {str(wxgf): {"ok": True, "bytes": 10}}

        with mock.patch("siwx.media_backup.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media_wxgf_macos.convert_wxgf_batch", side_effect=fake_batch):
            media_backup._convert_pending_wxgf(pending, stats, log=None)

        self.assertEqual(stats["ok_viewable"], 1)
        self.assertEqual(stats["ok_wxgf_preserved"], 0)
        self.assertFalse(wxgf.exists())  # 转码成功后原始 wxgf 应被清理
        self.assertTrue(heic.is_file())

    def test_macos_path_preserves_wxgf_when_batch_reports_failure(self):
        from siwx import media_backup
        wxgf = self.tmp / "b.wxgf"
        wxgf.write_bytes(b"fake-wxgf")
        pending = [(wxgf, wxgf.with_suffix(".heic"))]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}

        with mock.patch("siwx.media_backup.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media_wxgf_macos.convert_wxgf_batch",
                        return_value={str(wxgf): {"ok": False, "reason": "no_wechat_process"}}):
            media_backup._convert_pending_wxgf(pending, stats, log=None)

        self.assertEqual(stats["ok_viewable"], 0)
        self.assertEqual(stats["ok_wxgf_preserved"], 1)
        self.assertTrue(wxgf.is_file())  # 转码失败，原始 wxgf 必须还在

    def test_non_macos_path_uses_convert_wxgf_per_file(self):
        from siwx import media_backup
        wxgf = self.tmp / "c.wxgf"
        wxgf.write_bytes(b"fake-wxgf")
        pending = [(wxgf, wxgf.with_suffix(".heic"))]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}
        jpeg_bytes = b"\xff\xd8\xff" + b"\x00" * 10

        with mock.patch("siwx.media_backup.platform.system", return_value="Windows"), \
             mock.patch("siwx.media.convert_wxgf", return_value=jpeg_bytes):
            media_backup._convert_pending_wxgf(pending, stats, log=None)

        self.assertEqual(stats["ok_viewable"], 1)
        self.assertEqual(stats["ok_wxgf_preserved"], 0)
        self.assertFalse(wxgf.exists())
        self.assertTrue(wxgf.with_suffix(".jpeg").is_file())

    def test_non_macos_path_preserves_wxgf_when_convert_returns_none(self):
        from siwx import media_backup
        wxgf = self.tmp / "d.wxgf"
        wxgf.write_bytes(b"fake-wxgf")
        pending = [(wxgf, wxgf.with_suffix(".heic"))]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}

        with mock.patch("siwx.media_backup.platform.system", return_value="Windows"), \
             mock.patch("siwx.media.convert_wxgf", return_value=None):
            media_backup._convert_pending_wxgf(pending, stats, log=None)

        self.assertEqual(stats["ok_viewable"], 0)
        self.assertEqual(stats["ok_wxgf_preserved"], 1)
        self.assertTrue(wxgf.is_file())

    def test_log_none_does_not_raise(self):
        """log=None 是本模块其余函数的既有约定（"不要日志"），这里不能因为
        内部要给 convert_wxgf_batch 传 log 就炸出 TypeError。"""
        from siwx import media_backup
        wxgf = self.tmp / "e.wxgf"
        wxgf.write_bytes(b"fake-wxgf")
        pending = [(wxgf, wxgf.with_suffix(".heic"))]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}
        with mock.patch("siwx.media_backup.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media_wxgf_macos.convert_wxgf_batch", return_value={}):
            media_backup._convert_pending_wxgf(pending, stats, log=None)  # 不应抛异常
        self.assertEqual(stats["ok_wxgf_preserved"], 1)


class TestGetImageReusesBackup(unittest.TestCase):
    """get_image() 优先复用 media_backup.py 已经转码好的结果，不现场重新
    解密+转码。批量导出/浏览很多会话时，同一张图可能被多条消息引用到；
    全量备份往往已经批量转过一遍、结果就在磁盘上，现场再走一遍
    attach/hardlink/Bubble 解析 + （wxgf 的话）活的微信进程 LLDB 转码，
    既慢得多，微信没开时还直接没法看——有现成备份就应该优先用。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="siwx_test_getimage_backup_"))
        self.acc_out = self.tmp / "wxid_test"
        self.backup_images = self.acc_out / "media_backup" / "images" / "chatmd5" / "2026-01" / "Img"
        self.backup_images.mkdir(parents=True)
        media._BACKUP_INDEX.clear()  # 每个用例独立，避免索引缓存串台

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        media._BACKUP_INDEX.clear()
        media._IMG_CACHE.clear()

    def test_viewable_backup_hit_skips_live_decrypt_entirely(self):
        md5 = "a" * 32
        (self.backup_images / f"{md5}_M.jpeg").write_bytes(
            b"\xff\xd8\xff" + b"\x00" * 10)

        with mock.patch("siwx.media.attach_paths") as m_attach, \
             mock.patch("siwx.media._decrypt_any") as m_decrypt:
            body, ctype = media.get_image("wxid_test", md5, self.acc_out)

        self.assertEqual(ctype, "image/jpeg")
        self.assertTrue(body.startswith(b"\xff\xd8\xff"))
        m_attach.assert_not_called()
        m_decrypt.assert_not_called()

    def test_heic_backup_hit_converts_via_sips_for_browser_compat(self):
        """第一版实现直接把 .heic 原样返回，浏览器 <img> 里 HEIC 原生支持
        不可靠（Safari 能显示，Chrome/Firefox 普遍不行）——必须跟现场
        转码路径一样，补一次 sips 转 JPEG。"""
        md5 = "b" * 32
        (self.backup_images / f"{md5}_M.heic").write_bytes(b"HEIC-BYTES")
        jpeg_bytes = b"\xff\xd8\xff" + b"\x00" * 10

        with mock.patch("siwx.media.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media_wxgf_macos._heic_to_jpeg", return_value=jpeg_bytes) as m_sips:
            body, ctype = media.get_image("wxid_test", md5, self.acc_out)

        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(body, jpeg_bytes)
        m_sips.assert_called_once()

    def test_heic_backup_hit_falls_back_to_raw_heic_when_sips_fails(self):
        md5 = "c" * 32
        (self.backup_images / f"{md5}_M.heic").write_bytes(b"HEIC-BYTES")

        with mock.patch("siwx.media.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media_wxgf_macos._heic_to_jpeg", return_value=None):
            body, ctype = media.get_image("wxid_test", md5, self.acc_out)

        self.assertEqual(ctype, "image/heic")
        self.assertEqual(body, b"HEIC-BYTES")

    def test_preserved_wxgf_in_backup_does_not_short_circuit(self):
        """备份里的 .wxgf 是"转码失败时的兜底留档"，不算已有可预览结果——
        不能直接原样返回给浏览器，必须落到下面走一遍正常的现场解析路径。"""
        md5 = "d" * 32
        (self.backup_images / f"{md5}_M.wxgf").write_bytes(b"wxgf" + b"\x00" * 10)

        with mock.patch("siwx.media.attach_paths", return_value=[]) as m_attach:
            body, info = media.get_image("wxid_test", md5, self.acc_out, chat="any")

        m_attach.assert_called_once()  # 确实落到了现场解析路径
        self.assertIsNone(body)  # 没有真实 attach 数据，自然找不到，返回失败是预期的

    def test_backup_index_is_cached_across_calls(self):
        md5 = "e" * 32
        (self.backup_images / f"{md5}_M.jpeg").write_bytes(
            b"\xff\xd8\xff" + b"\x00" * 10)
        media.get_image("wxid_test", md5, self.acc_out)
        backup_root = self.acc_out / "media_backup" / "images"
        self.assertIn(str(backup_root), media._BACKUP_INDEX)
        idx_obj_id = id(media._BACKUP_INDEX[str(backup_root)])
        media._IMG_CACHE.clear()  # 清掉图片缓存，逼第二次调用重新走查找
        media.get_image("wxid_test", md5, self.acc_out)
        self.assertEqual(id(media._BACKUP_INDEX[str(backup_root)]), idx_obj_id)

    def test_no_backup_dir_falls_through_to_live_path(self):
        """账号从没跑过 media_backup 全量备份时，目录都不存在——必须正常
        落到现场解析路径，不能因为目录缺失就报错。"""
        acc_out = self.tmp / "wxid_never_backed_up"
        with mock.patch("siwx.media.attach_paths", return_value=[]) as m_attach:
            media.get_image("wxid_never_backed_up", "f" * 32, acc_out, chat="any")
        m_attach.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
