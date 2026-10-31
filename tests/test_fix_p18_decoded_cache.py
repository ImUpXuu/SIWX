"""P1-8 回归：超预算分片降级缓存，搜索不再每次全量重解码。

修复前：解码缓存按「12 分片 + 256MB 字节预算」淘汰，几十万条消息的
单分片会话估算必超预算 → 条目写入后连自己也被淘汰（缓存恒为空）→
每次搜索/翻页都重复全量 zstd 解码 + 全量 lower() 分配 + 线性扫描。
修复后：
- 超预算分片降级为 _TextOnlyShard：只缓存文本列（packed 置 None，
  packed 通常占解码结果大头）+ 预折叠小写文本列（lower 在解码时做
  一次，而不是每次搜索每行分配一个新串；小写列的内存增量在降级路径
  可接受——省掉 packed 后余量充足）；
- 命中行翻页后按页补取 packed（_refetch_packed），富化结果与全文
  缓存路径逐字段一致；
- 缓存签名带结构版本号 _DECODED_CACHE_VERSION（4 元组 → 5 元组，
  天然不同键），升级后旧结构条目不会被新代码误读。

全部使用合成临时库与占位符（wxid_test / wxid_friend），不读取真实聊天库。
"""
import hashlib
import sqlite3
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote as urlquote

from tests._base import IsolatedRootCase

from siwx import api_chat

ACCOUNT = "wxid_test"
CHAT = "wxid_friend"
TABLE = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()

_TABLE_SCHEMA = """CREATE TABLE [{t}] (
    local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
    create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
    message_content BLOB, packed_info_data BLOB)"""


def _make_db(path: Path, rows, start_ts=1_700_000_000) -> Path:
    """最小分片库；rows 为 (local_id, local_type, 文本, packed) 列表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_TABLE_SCHEMA.format(t=TABLE))
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)",
                     (CHAT,))
        for lid, ltype, text, packed in rows:
            conn.execute(f"INSERT INTO [{TABLE}] VALUES (?,?,?,?,?,?,?,?)",
                         (lid, 1000 + lid, ltype, start_ts + lid, 0, None,
                          text.encode("utf-8"), packed))
        conn.commit()
    finally:
        conn.close()
    return path


def _clear_decoded():
    with api_chat._DECODED_CACHE_LOCK:
        api_chat._DECODED_CACHE.clear()
        api_chat._DECODED_CACHE_SIZES.clear()
        api_chat._DECODED_CACHE_BYTES = 0


class TestDegradedShardCache(IsolatedRootCase):
    """_decoded_shard 的降级判定、零重解码与缓存结构。"""

    def setUp(self):
        super().setUp()
        _clear_decoded()

    def tearDown(self):
        _clear_decoded()
        super().tearDown()

    def test_oversized_shard_degrades_and_second_lookup_no_redecode(self):
        # 文本很小、packed 很大（贴近真实：packed 是解码结果的大头）
        rows = [(i, 1, f"消息内容{i}", b"P" * 4000) for i in range(1, 9)]
        db = _make_db(Path(self.tmp) / "output" / ACCOUNT
                      / "message" / "message_0.db", rows)
        real_decode = api_chat._decode_content
        calls = {"n": 0}

        def counting(content):
            calls["n"] += 1
            return real_decode(content)

        # 预算取值：全文估算 ≈ 8 × (4000 + 文本/开销) ≈ 35KB > 20KB，
        # 降级条目（文本列 + 小写列，无 packed）≈ 4KB < 20KB
        with mock.patch.object(api_chat, "_DECODED_CACHE_MAX_BYTES", 20_000), \
                mock.patch.object(api_chat, "_decode_content", counting):
            mode, rows1, lows = api_chat._decoded_shard(db, TABLE)
            self.assertEqual(mode, "textonly")
            self.assertEqual(calls["n"], 8, "首次必须逐行解码一次")
            self.assertEqual(len(rows1), 8)
            self.assertTrue(all(r[7] is None for r in rows1),
                            "降级条目不缓存 packed 字节")
            self.assertEqual([r[6] for r in rows1],
                             [f"消息内容{i}" for i in range(1, 9)])
            self.assertEqual(len(lows), len(rows1))
            for r, low in zip(rows1, lows):
                self.assertEqual(low, (r[6] or "").lower())
            with api_chat._DECODED_CACHE_LOCK:
                entries = list(api_chat._DECODED_CACHE.values())
            self.assertEqual(len(entries), 1)
            self.assertIsInstance(entries[0], api_chat._TextOnlyShard)

            # 第二次读取：命中降级缓存，零重复解码，返回同一缓存条目
            mode2, rows2, lows2 = api_chat._decoded_shard(db, TABLE)
            self.assertEqual(mode2, "textonly")
            self.assertEqual(calls["n"], 8, "第二次不得重复解码")
            self.assertIs(rows2, rows1, "应命中同一缓存对象")
            self.assertIs(lows2, lows)

    def test_within_budget_shard_stays_full(self):
        rows = [(i, 1, f"内容{i}", b"P" * 10) for i in range(1, 4)]
        db = _make_db(Path(self.tmp) / "output" / ACCOUNT
                      / "message" / "message_0.db", rows)
        mode, cached, lows = api_chat._decoded_shard(db, TABLE)
        self.assertEqual(mode, "full")
        self.assertIsNone(lows, "预算内分片不做小写列的额外内存开销")
        self.assertTrue(all(r[7] == b"P" * 10 for r in cached),
                        "预算内分片 packed 留在缓存行上")

    def test_cache_signature_carries_structure_version(self):
        db = _make_db(Path(self.tmp) / "output" / ACCOUNT
                      / "message" / "message_0.db",
                      [(1, 1, "hi", b"P")])
        api_chat._decoded_shard(db, TABLE)
        with api_chat._DECODED_CACHE_LOCK:
            sig = next(iter(api_chat._DECODED_CACHE))
        self.assertEqual(len(sig), 5, "签名应为 (库, mtime, size, 表, 版本) 5 元组")
        self.assertEqual(sig[-1], api_chat._DECODED_CACHE_VERSION,
                         "结构版本必须拼进签名，升级后旧条目不可误读")

    def test_refetch_packed_returns_original_blobs(self):
        packed = b"PK" * 50
        db = _make_db(Path(self.tmp) / "output" / ACCOUNT
                      / "message" / "message_0.db",
                      [(1, 1, "文本", packed), (3, 1, "文本2", None)])
        out = api_chat._refetch_packed(db, TABLE, [1, 3, 999])
        self.assertEqual(out[(str(db), 1)], packed)
        self.assertIsNone(out[(str(db), 3)], "库里本就没有 packed 的行取回 None")
        self.assertNotIn((str(db), 999), out, "不存在的行不编造结果")


class TestDegradedSearchViaApi(IsolatedRootCase):
    """端到端：降级路径的搜索结果与全文缓存路径一致，且第二次搜索零重解码。"""

    def setUp(self):
        super().setUp()
        acc = Path(self.tmp) / "output" / ACCOUNT
        rows = [(i, 1, f"Hello World {i} " + "内容" * 20, b"P" * 6000)
                for i in range(1, 13)]
        _make_db(acc / "message" / "message_0.db", rows)
        (acc / "contact").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(acc / "contact" / "contact.db")
        try:
            conn.execute("CREATE TABLE contact (username TEXT, remark TEXT, "
                         "nick_name TEXT, alias TEXT)")
            conn.execute("INSERT INTO contact VALUES (?,?,?,?)",
                         (CHAT, "测试联系人", "", ""))
            conn.commit()
        finally:
            conn.close()
        from siwx.server import app
        self.client = app.test_client()
        # 大写查询：降级路径靠预存小写列命中，全文路径靠现场 lower()
        self.url = (f"/api/chat/messages?account={ACCOUNT}&chat={CHAT}"
                    f"&q={urlquote('HELLO WORLD 3')}&limit=20")

    def test_degraded_search_matches_full_path_and_no_redecode(self):
        # ① 默认大预算：全文缓存路径，作为逐字段一致的基准
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        json_full = resp.get_json()
        self.assertEqual(json_full["total"], 1)
        with api_chat._DECODED_CACHE_LOCK:
            size_full = sum(api_chat._DECODED_CACHE_SIZES.values())
        _clear_decoded()

        # ② 预算收缩到全文估算的 70%：全文条目必超 → 降级条目
        #    （无 packed）远小于预算，必定可缓存
        budget = max(1, int(size_full * 0.7))
        real_decode = api_chat._decode_content
        calls = {"n": 0}

        def counting(content):
            calls["n"] += 1
            return real_decode(content)

        with mock.patch.object(api_chat, "_DECODED_CACHE_MAX_BYTES", budget), \
                mock.patch.object(api_chat, "_decode_content", counting):
            resp1 = self.client.get(self.url)
            self.assertEqual(resp1.status_code, 200)
            n1 = calls["n"]
            self.assertGreater(n1, 0, "首次搜索应触发全量解码")
            with api_chat._DECODED_CACHE_LOCK:
                entries = list(api_chat._DECODED_CACHE.values())
            self.assertTrue(entries and
                            all(isinstance(e, api_chat._TextOnlyShard)
                                for e in entries),
                            "超预算分片应缓存为降级条目")
            resp2 = self.client.get(self.url)
            self.assertEqual(resp2.status_code, 200)
            self.assertEqual(calls["n"], n1, "第二次搜索不得重复解码")

        # ③ 降级路径结果与全文缓存路径逐字段一致（含按页补取的 packed）
        self.assertEqual(resp1.get_json(), json_full)
        self.assertEqual(resp2.get_json(), json_full)


if __name__ == "__main__":
    unittest.main()
