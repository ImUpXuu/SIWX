"""P2-21 MCP 6 个聊天工具 + /api/job/cancel 端点级测试（批次 4）。

工具返回 JSON 字符串，形状（字段名/计数/scope）此前无任何测试钉住；
取消端点的 200/409 与 cancelled 透传同样只在前端假数据上验证过。

全部使用 tests.fixtures 的合成账号（make_account，落 SIWX_ROOT/output，
即 paths.out_root()）与 IsolatedRootCase 临时根，不联网、不读真实库。
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from tests._base import IsolatedRootCase

N_TEXTS = 6
KEYWORD = "第 2 条消息"          # 合成消息正文，仅一条命中


def make_account(*args, **kwargs):
    """复用 test_regressions 的合成产物构造器（延迟导入，避免收集期副作用）。"""
    from tests.test_regressions import make_account as impl
    return impl(*args, **kwargs)


class _McpCase(IsolatedRootCase):
    def setUp(self):
        super().setUp()
        from siwx import mcp_server
        self.M = mcp_server
        self.acc, self.account, self.chat = make_account(
            self.tmp, n_texts=N_TEXTS)

    def call(self, fn, args):
        """工具统一返回 JSON 字符串。"""
        out = fn(args)
        self.assertIsInstance(out, str)
        return json.loads(out)


class TestMcpChatTools(_McpCase):

    def test_get_status(self):
        data = self.call(self.M.tool_get_status, {})
        self.assertIn("wechat_running", data)
        self.assertIsInstance(data["wechat_running"], bool)
        self.assertIn(self.account, [a["wxid"] for a in data["accounts"]])
        info = next(a for a in data["accounts"] if a["wxid"] == self.account)
        self.assertGreaterEqual(info["message_dbs"], 1)
        self.assertIn("keystore_salts", data)

    def test_list_accounts(self):
        data = self.call(self.M.tool_list_accounts, {})
        self.assertEqual(data["accounts"], [self.account])

    def test_list_sessions(self):
        data = self.call(self.M.tool_list_sessions, {"account": self.account})
        self.assertEqual(data["account"], self.account)
        self.assertGreaterEqual(data["total"], 1)
        session = data["sessions"][0]
        self.assertEqual(session["username"], self.chat)
        for key in ("username", "display", "is_group", "preview", "last_time"):
            self.assertIn(key, session)
        self.assertFalse(session["is_group"])

    def test_get_messages(self):
        data = self.call(self.M.tool_get_messages,
                         {"account": self.account, "chat": self.chat})
        self.assertEqual(data["chat"], self.chat)
        self.assertEqual(data["total"], N_TEXTS)
        self.assertTrue(data["messages"])
        for msg in data["messages"]:
            self.assertEqual(set(msg), {"ts", "sender", "is_me", "type",
                                        "content"})
            self.assertIsInstance(msg["ts"], int)
            self.assertIsInstance(msg["is_me"], bool)
            self.assertTrue(msg["content"])
        # limit 生效且取最新 N 条（时间正序）
        page = self.call(self.M.tool_get_messages,
                         {"account": self.account, "chat": self.chat,
                          "limit": 2})
        self.assertEqual(len(page["messages"]), 2)
        self.assertEqual(page["messages"][-1]["content"],
                         data["messages"][-1]["content"])

    def test_search_messages_in_chat(self):
        data = self.call(self.M.tool_search_messages,
                         {"account": self.account, "chat": self.chat,
                          "keyword": KEYWORD})
        self.assertEqual(data["scope"], self.chat)
        self.assertEqual(data["scanned"], N_TEXTS)
        self.assertEqual(len(data["matches"]), 1)
        self.assertIn(KEYWORD, data["matches"][0]["content"])
        self.assertEqual(data["matches"][0]["chat"], self.chat)

    def test_search_messages_all_chats(self):
        data = self.call(self.M.tool_search_messages,
                         {"account": self.account, "keyword": KEYWORD})
        self.assertEqual(data["scope"], "全部会话")
        self.assertGreaterEqual(data["scanned"], N_TEXTS)
        self.assertEqual(len(data["matches"]), 1)
        self.assertIn(KEYWORD, data["matches"][0]["content"])
        # 全库命中也要能反查到所属会话（md5 反查表）
        self.assertEqual(data["matches"][0]["chat"], self.chat)

    def test_search_messages_no_hit(self):
        data = self.call(self.M.tool_search_messages,
                         {"account": self.account, "chat": self.chat,
                          "keyword": "不存在的关键词-zzz"})
        self.assertEqual(data["matches"], [])

    def test_export_chat_json_without_media(self):
        data = self.call(self.M.tool_export_chat,
                         {"account": self.account, "chat": self.chat,
                          "format": "json", "media": False})
        self.assertEqual(data["format"], "json")
        self.assertEqual(data["message_count"], N_TEXTS)
        self.assertEqual(data["media_count"], 0)
        f = Path(data["file"])
        self.assertTrue(f.is_file(), f)
        self.assertFalse((Path(data["export_dir"]) / "media").exists(),
                         "media=False 不应产出媒体目录")
        exported = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(len(exported["messages"]), N_TEXTS)
        self.assertEqual(exported["session"]["wxid"], self.chat)
        self.assertTrue(any(KEYWORD in (m.get("content") or "")
                            for m in exported["messages"]))


class TestJobCancelEndpoint(IsolatedRootCase):
    """取消语义：200 且 cancelling=true；无任务 409；report.cancelled 透传。"""

    def setUp(self):
        super().setUp()
        from siwx import server
        self.server = server
        self.client = server.app.test_client()
        self._saved_job = dict(server._job)
        self._saved_event = server._cancel_event.is_set()
        server._cancel_event.clear()
        p = mock.patch.object(server, "_log")   # 不往日志轨写测试内容
        self._log_mock = p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.server._job.clear()
        self.server._job.update(self._saved_job)
        if self._saved_event:
            self.server._cancel_event.set()
        else:
            self.server._cancel_event.clear()
        super().tearDown()

    def _set_job(self, running, mode=None, report=None):
        self.server._job.update({
            "running": running, "mode": mode, "done": not running,
            "ok": False, "logs": [], "report": report, "error": None,
            "progress": {"pct": 0, "msg": ""},
        })

    def test_no_running_job_returns_409(self):
        self._set_job(False)
        r = self.client.post("/api/job/cancel", json={})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()["cancelling"])
        self.assertFalse(self.server._cancel_event.is_set())

    def test_running_job_accepts_cancel_and_report_passthrough(self):
        self._set_job(True, mode="export")
        r = self.client.post("/api/job/cancel", json={"mode": "export"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["cancelling"])
        self.assertTrue(self.server._cancel_event.is_set())

        # 任务线程收尾后写 cancelled 报告：GET /api/job 必须原样透传
        with self.server._lock:
            self.server._job.update({
                "running": False, "done": True, "ok": True,
                "report": {"cancelled": True, "exported": 1},
            })
        job = self.client.get("/api/job").get_json()
        self.assertIsNotNone(job["report"])
        self.assertIs(job["report"]["cancelled"], True)
        self.assertEqual(job["report"]["exported"], 1)


if __name__ == "__main__":
    unittest.main()
