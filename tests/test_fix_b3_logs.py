"""P2-19 / P2-16 后端回归（修订清单 2026-10-23 批次 3，日志与取消守卫）。

P2-19：日志页"粗略模式"此前不隐藏 DEBUG——过滤串 `"] [DEBUG]"` 与两轨
格式都不匹配：
- 文件轨是 `YYYY-mm-dd HH:MM:SS[,ms] [DEBUG] siwx: [模块] 正文`；
- 结构化轨是 `[ts, "DEBUG", module, msg]`，级别在元组第 2 项。
本文件钉住 `_log_item_level` 的级别判定、`_merged_logs` 的粗略/详细口径，
以及"同一进程内切换后立即生效（不重启）"。

P2-16（后端部分）：`POST /api/job/cancel` 可带 `{"mode": ...}`。全局 job
槽是共享的，模式不匹配时必须 409 且不置位取消事件，避免导出页的取消
误停设置页的转换任务。

全部使用占位符与合成日志文本，不联网、不读取真实聊天库。
"""
import time
import unittest
from unittest import mock

from tests._base import IsolatedRootCase


TS = int(time.time() * 1000) - 60_000


class TestLogItemLevel(unittest.TestCase):
    """`_log_item_level`：结构化轨取元组，文件轨解析前缀，其余空串。"""

    def setUp(self):
        from siwx import server
        self.server = server

    def test_structured_level_from_tuple(self):
        self.assertEqual(
            self.server._log_item_level([TS, "DEBUG", "api", "x"]), "DEBUG")
        self.assertEqual(
            self.server._log_item_level([TS, "info", "api", "x"]), "INFO")

    def test_file_track_level_parsed(self):
        line = "2026-10-23 12:00:00 [DEBUG] siwx: [api] x"
        self.assertEqual(self.server._log_item_level([TS, line]), "DEBUG")

    def test_file_track_level_with_millis(self):
        line = "2026-10-23 12:00:00,123 [WARN] siwx: [api] x"
        self.assertEqual(self.server._log_item_level([TS, line]), "WARN")

    def test_unparseable_text_returns_empty(self):
        self.assertEqual(
            self.server._log_item_level([TS, "没有任何级别前缀的文本"]), "")
        self.assertEqual(self.server._log_item_level([]), "")

    def test_mcp_text_not_misdetected(self):
        # MCP 轨条目带 "[MCP] " 前缀，文件轨正则不命中 → 空串（不过滤）
        line = "[MCP] 2026-10-23 12:00:00 [DEBUG] mcp-text-marker"
        self.assertEqual(self.server._log_item_level([TS, line]), "")


class TestMergedLogsRoughFilter(unittest.TestCase):
    """`_merged_logs` 按级别过滤 DEBUG；切换模式后同一进程内立即生效。"""

    def setUp(self):
        from siwx import logger as log, server
        self.log, self.server = log, server
        self._old_level = log.get_level()
        self.addCleanup(log.set_level, self._old_level)

        self._old_ring = server._LOG_RING
        server._LOG_RING = [[TS + 5000, "ring-info-marker"]]
        self.addCleanup(setattr, server, "_LOG_RING", self._old_ring)

        file_debug = "2026-10-23 12:00:00,123 [DEBUG] siwx: [api] file-dbg-marker"
        file_info = "2026-10-23 12:00:01 [INFO] siwx: [api] file-info-marker"
        mcp_text = "[MCP] 2026-10-23 12:00:02 [DEBUG] mcp-text-marker"
        for target, value in (
            ("_tail_app_log", [[TS, file_debug], [TS + 1000, file_info]]),
            ("_tail_mcp_log", [[TS + 2000, mcp_text]]),
        ):
            p = mock.patch.object(server, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(log, "get_logs", return_value=[
            [TS + 3000, "DEBUG", "api", "struct-dbg-marker"],
            [TS + 4000, "INFO", "api", "struct-info-marker"],
        ])
        p.start()
        self.addCleanup(p.stop)

    def _texts(self):
        return [self.server._log_item_text(it)
                for it in self.server._merged_logs(100)]

    def test_rough_hides_debug_on_both_tracks(self):
        self.log.set_level(self.log.LogLevel.ROUGH)
        texts = self._texts()
        self.assertFalse(any("file-dbg-marker" in t for t in texts),
                         f"文件轨 DEBUG 未被过滤: {texts}")
        self.assertFalse(any("struct-dbg-marker" in t for t in texts),
                         f"结构化轨 DEBUG 未被过滤: {texts}")
        # INFO 不误杀；无法判定级别的 MCP 文本保留（宁多勿缺）
        self.assertTrue(any("file-info-marker" in t for t in texts))
        self.assertTrue(any("struct-info-marker" in t for t in texts))
        self.assertTrue(any("ring-info-marker" in t for t in texts))
        self.assertTrue(any("mcp-text-marker" in t for t in texts))

    def test_detailed_keeps_debug_on_both_tracks(self):
        self.log.set_level(self.log.LogLevel.DETAILED)
        texts = self._texts()
        self.assertTrue(any("file-dbg-marker" in t for t in texts))
        self.assertTrue(any("struct-dbg-marker" in t for t in texts))

    def test_switch_takes_effect_without_restart(self):
        self.log.set_level(self.log.LogLevel.ROUGH)
        rough_texts = self._texts()
        self.log.set_level(self.log.LogLevel.DETAILED)
        detailed_texts = self._texts()
        self.assertFalse(any("struct-dbg-marker" in t for t in rough_texts))
        self.assertTrue(any("struct-dbg-marker" in t for t in detailed_texts))
        self.assertFalse(any("file-dbg-marker" in t for t in rough_texts))
        self.assertTrue(any("file-dbg-marker" in t for t in detailed_texts))


class TestJobCancelModeGuard(IsolatedRootCase):
    """P2-16 后端：取消请求声明的 mode 必须与运行中任务一致。"""

    def setUp(self):
        super().setUp()
        from siwx import server
        self.server = server
        self.client = server.app.test_client()
        self._saved_job = dict(server._job)
        self._saved_event = server._cancel_event.is_set()
        server._cancel_event.clear()
        # 取消成功的分支会写任务日志/文件日志；测试只关心事件与响应
        p = mock.patch.object(server, "_log")
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

    def _set_job(self, mode, running=True):
        self.server._job.update({
            "running": running, "mode": mode, "done": False, "ok": False,
            "logs": [], "report": None, "error": None,
            "progress": {"pct": 0, "msg": ""},
        })

    def test_no_running_job_returns_409(self):
        self._set_job(None, running=False)
        for body in (None, {"mode": "export"}):
            r = self.client.post("/api/job/cancel", json=body)
            self.assertEqual(r.status_code, 409, body)
            self.assertFalse(r.get_json()["cancelling"])
            self.assertFalse(self.server._cancel_event.is_set())

    def test_mismatched_mode_rejected_without_cancel(self):
        self._set_job("export")
        r = self.client.post("/api/job/cancel", json={"mode": "convert"})
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertFalse(body["cancelling"])
        self.assertEqual(body["error"],
                         "当前运行的是 export 任务，不是本页面发起的任务")
        self.assertFalse(self.server._cancel_event.is_set(),
                         "模式不匹配时不得置位取消事件")

    def test_matching_mode_cancels(self):
        self._set_job("export")
        r = self.client.post("/api/job/cancel", json={"mode": "export"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["cancelling"])
        self.assertTrue(self.server._cancel_event.is_set())

    def test_bodyless_cancel_keeps_legacy_behavior(self):
        # 旧客户端/脚本不带 body：不核对模式，保持原行为
        self._set_job("export")
        r = self.client.post("/api/job/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["cancelling"])
        self.assertTrue(self.server._cancel_event.is_set())

    def test_empty_job_mode_does_not_block_cancel(self):
        # 运行中任务没记 mode（旧路径）：无从核对，不拦
        self._set_job("")
        r = self.client.post("/api/job/cancel", json={"mode": "convert"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.server._cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()
