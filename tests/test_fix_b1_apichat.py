"""P2-2 / P2-4 回归。

P2-2：复合 49（转账/红包/引用等，local_type = subtype << 16 | 49）在
普通分页、搜索/筛选、build_messages（MCP）三条路径必须输出同一类型标签。
修复前普通分页与筛选路径先 `local_type & 0xFFFF`，高 16 位 subtype 被丢掉，
`_fmt` 收到 49 只可能是 [链接]。

P2-4：扫描时打不开的分片不得把不完整索引写进 `_SHARD_INDEX`，否则在目录
签名变化前该分片里的会话会一直"消失"；下一次请求必须能自愈。

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

# 微信 4.x/5.0 packed_info 复合 localType：完整值 = subtype << 16 | 基本类型 49
L_TYPE_TRANSFER = 8589934592049       # [转账]
L_TYPE_RED_PACKET = 8594229559345     # [红包]
L_TYPE_QUOTE_49 = 244813135921        # 外层 49、refermsg 为内层 57 的引用

_TABLE_SCHEMA = """CREATE TABLE [{t}] (
    local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
    create_time INTEGER, origin_source INTEGER, real_sender_id INTEGER,
    message_content BLOB, packed_info_data BLOB)"""


def _msg_table(chat: str) -> str:
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def _make_shard(path: Path, chat: str, rows=(), start_ts=1_700_000_000) -> Path:
    """最小分片库：Msg_ 表 + Name2Id；rows 为 (local_type, 文本 XML) 列表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = _msg_table(chat)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_TABLE_SCHEMA.format(t=table))
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)",
                     (chat,))
        for i, (ltype, content) in enumerate(rows):
            conn.execute(f"INSERT INTO [{table}] VALUES (?,?,?,?,?,?,?,?)",
                         (i + 1, 1000 + i, ltype, start_ts + i, 0, None,
                          content.encode("utf-8"), None))
        conn.commit()
    finally:
        conn.close()
    return path


def _make_account(root: Path, rows, chat: str = CHAT, account: str = ACCOUNT):
    acc = root / "output" / account
    _make_shard(acc / "message" / "message_0.db", chat, rows)
    (acc / "contact").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(acc / "contact" / "contact.db")
    try:
        conn.execute("CREATE TABLE contact (username TEXT, remark TEXT, "
                     "nick_name TEXT, alias TEXT)")
        conn.execute("INSERT INTO contact VALUES (?,?,?,?)",
                     (chat, "测试联系人", "", ""))
        conn.commit()
    finally:
        conn.close()
    return acc, account, chat


_TRANSFER_XML = ('<msg><appmsg><title>测试转账</title>'
                 '<wcpayinfo><paysubtype>1</paysubtype></wcpayinfo>'
                 '</appmsg></msg>')
_RED_PACKET_XML = ('<msg><appmsg><title>测试红包</title>'
                   '<wcpayinfo><paysubtype>1</paysubtype>'
                   '<receiveuin>0</receiveuin></wcpayinfo></appmsg></msg>')
_QUOTE_XML = ('<appmsg type="57"><title>回复文本</title><type>57</type>'
              '<refermsg><displayname>联系人C</displayname>'
              '<content>被引用的原话</content>'
              '<createtime>1700000000</createtime></refermsg></appmsg>')

_COMPOSITE_ROWS = [
    (L_TYPE_TRANSFER, _TRANSFER_XML),
    (L_TYPE_RED_PACKET, _RED_PACKET_XML),
    (L_TYPE_QUOTE_49, _QUOTE_XML),
]
_TEXT_BY_ID = {
    1: "[转账] 测试转账",
    2: "[红包] 测试红包",
    3: "回复文本",
}


class TestComposite49Labels(IsolatedRootCase):
    """同一批复合消息在普通分页、筛选/搜索、build_messages 三处标签一致。"""

    def setUp(self):
        super().setUp()
        self.acc, self.account, self.chat = _make_account(
            self.tmp, _COMPOSITE_ROWS)
        from siwx.server import app
        self.client = app.test_client()

    def _messages(self, extra: str = "") -> dict:
        resp = self.client.get(
            f"/api/chat/messages?account={self.account}&chat={self.chat}"
            f"&limit=20{extra}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _assert_labels(self, msgs) -> dict:
        by_id = {m["id"]: m for m in msgs}
        self.assertEqual(len(by_id), len(_TEXT_BY_ID))
        for mid, want in _TEXT_BY_ID.items():
            self.assertEqual(by_id[mid]["text"], want)
            # 前端分支仍按低 16 位基本类型走
            self.assertEqual(by_id[mid]["type"], 49)
        # 引用复合照样解析出 quote，而不是退化成链接文本
        self.assertEqual(by_id[3]["kind"], "quote")
        self.assertIsNotNone(by_id[3]["quote"])
        self.assertEqual(by_id[3]["quote"]["displayname"], "联系人C")
        return by_id

    def test_plain_pagination_keeps_composite_label(self):
        # 修复前：local_type 在分页路径被 & 0xFFFF，[转账]/[红包] → [链接]
        self._assert_labels(self._messages()["messages"])

    def test_kind_filter_keeps_composite_label(self):
        # 修复前：_decoded_rows 已截断 + 筛选用 t not in type_set 直接漏掉
        data = self._messages("&kind=link")
        self.assertEqual(data["total"], 3)
        self._assert_labels(data["messages"])

    def test_keyword_search_keeps_composite_label(self):
        data = self._messages("&q=" + urlquote("测试转账"))
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["messages"][0]["text"], "[转账] 测试转账")

    def test_build_messages_keeps_composite_label(self):
        # MCP / 导出共用入口：必须与聊天页同标签
        self._assert_labels(api_chat.build_messages(
            self.acc, self.chat, account=self.account))


class TestFailedShardNotCached(IsolatedRootCase):
    """坏分片：第一次索引可返回（尽量可用），但不得入缓存；下次自愈。"""

    def test_transient_failure_is_not_cached_then_recovers(self):
        acc = Path(self.tmp) / "output" / ACCOUNT
        msg_dir = acc / "message"
        good_chat = CHAT
        bad_chat = "wxid_test2"
        _make_shard(msg_dir / "message_0.db", good_chat)
        bad_db = _make_shard(msg_dir / "message_1.db", bad_chat)

        real_connect = sqlite3.connect
        tries = {"n": 0}

        def flaky(path, *args, **kwargs):
            if Path(path) == bad_db and tries["n"] == 0:
                tries["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(sqlite3, "connect", flaky), \
                mock.patch.object(api_chat._logger, "warn") as warn:
            first = api_chat.shard_index(msg_dir)

        self.assertIn(_msg_table(good_chat), first)
        self.assertNotIn(_msg_table(bad_chat), first)
        self.assertNotIn(bad_db, [p for dbs in first.values() for p in dbs])
        self.assertNotIn(str(msg_dir), api_chat._SHARD_INDEX,
                         "存在失败分片时不得写进程内缓存")
        logged = " ".join(str(a) for call in warn.call_args_list
                          for a in call.args)
        self.assertIn("failed=1", logged, "日志必须带失败分片计数")
        self.assertIn("OperationalError", logged, "日志必须带异常类型")

        # 下一次请求（瞬时故障已消失）应重建完整索引并缓存
        second = api_chat.shard_index(msg_dir)
        self.assertIn(_msg_table(bad_chat), second)
        self.assertEqual(api_chat.shards_for(acc, bad_chat), [bad_db])
        self.assertIs(api_chat.shard_index(msg_dir), second,
                      "全部分片成功后应命中缓存（同一对象）")


if __name__ == "__main__":
    unittest.main()
