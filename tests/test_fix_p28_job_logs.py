"""P2-8 回归：任务日志出口（/api/job 与备份 status）必须与 /api/logs 同口径脱敏。

历史缺陷：/api/logs 出口统一过 desensitize_msg，但 /api/job 把 _job["logs"]
原样 jsonify——任务轨里有 out_root 绝对路径、"提取账号 wxid_…"、sns 关键词，
`--trust-lan` 下即局域网可读。入队前的脱敏挡不住所有写入点（异常 handler、
错误分支直接 append 原文），所以出口脱敏是最后防线；缓冲区必须保留原文
（本地文件轨 export desensitize=0 依赖它）。

全部使用合成占位符（wxid_example_01 / alias_abc12345）与临时目录路径，
不出现任何真实账号标识。
"""
import time
import unittest
from unittest import mock

from tests._base import IsolatedRootCase

_WXID = "wxid_example_01"          # 合成占位，见 AGENTS.md 示例数据红线
_ALIAS = "alias_abc12345"          # 合成占位
_KEY64 = "ab" * 32                 # 合成 64 hex，非真实密钥


class TestJobLogDesensitize(IsolatedRootCase):
    """/api/job 出口脱敏：与 /api/logs 同口径，缓冲区保留原文。"""

    def setUp(self):
        super().setUp()
        from siwx import server
        self.server = server
        self.client = server.app.test_client()
        self._saved_job = dict(server._job)
        server._job.update({
            "running": True, "done": False, "ok": False, "mode": "export",
            "logs": [], "report": None, "error": None,
            "export_dir": None, "progress": {"pct": 0, "msg": ""},
        })

    def tearDown(self):
        self.server._job.clear()
        self.server._job.update(self._saved_job)
        super().tearDown()

    def _job_log(self, msg):
        self.server._job["logs"].append([int(time.time() * 1000), msg])

    def test_api_job_masks_wxid_paths_and_keys(self):
        self._job_log(f"提取账号 {_WXID} 完成（account={_ALIAS}）")
        self._job_log(f"输出目录 D:\\siwx_test_out\\{_WXID}\\db_storage 共 25 库")
        self._job_log(f"sns 关键词命中 {_KEY64} 落盘 /tmp/siwx_test_out/media")
        r = self.client.get("/api/job")
        self.assertEqual(r.status_code, 200)
        texts = [str(item[-1]) for item in r.get_json()["logs"]]
        joined = "\n".join(texts)
        self.assertNotIn(_WXID, joined, "wxid 明文不许出现在 /api/job")
        self.assertNotIn(_ALIAS, joined)
        self.assertNotIn(_KEY64, joined, "64 hex 密钥明文不许出现")
        self.assertNotIn("siwx_test_out", joined, "绝对路径只许保留末段")
        self.assertIn("wxid_***", joined)
        self.assertIn("****", joined)
        self.assertIn("db_storage", joined, "路径末段保留（排障可用）")

    def test_api_job_same_gauge_as_api_logs(self):
        """同一条原文，/api/job 与 /api/logs 必须产出相同的脱敏文本。"""
        msg = f"提取账号 {_WXID}，输出 D:\\siwx_test_out\\{_WXID}\\db_storage"
        ts = int(time.time() * 1000)
        self.server._job["logs"].append([ts, msg])
        self.server._LOG_RING.append([ts, msg])
        job_texts = [str(it[1]) for it in self.client.get("/api/job").get_json()["logs"]]
        merged = self.client.get("/api/logs").get_json()["logs"]
        merged_texts = [str(it[1] if len(it) == 2 else it[3]) for it in merged]
        masked = self.server.log.desensitize_msg(msg)
        self.assertIn(masked, job_texts)
        self.assertIn(masked, merged_texts, "/api/logs 也应含同口径脱敏行")
        self.assertNotIn(_WXID, "\n".join(merged_texts))

    def test_api_job_keeps_raw_text_in_buffer(self):
        """出口脱敏不许写回底层缓冲：原文留在任务轨（本地文件轨依赖）。"""
        msg = f"提取账号 {_WXID} 完成"
        self._job_log(msg)
        self.client.get("/api/job")
        self.assertIn(msg, [it[1] for it in self.server._job["logs"]])


class TestBackupJobLogDesensitize(IsolatedRootCase):
    """/api/backup/status 与 /api/backup/restore/status 的 logs 同口径脱敏。"""

    def setUp(self):
        super().setUp()
        from siwx import api_backup, server
        self.api_backup = api_backup
        self.client = server.app.test_client()

    def test_backup_status_masks_logs(self):
        saved = list(self.api_backup._job["logs"])
        self.api_backup._job["logs"].append(
            f"备份 {_WXID}：源 D:\\siwx_test_out\\{_WXID}\\db_storage")
        try:
            body = self.client.get("/api/backup/status").get_json()
            joined = "\n".join(str(m) for m in body["logs"])
            self.assertNotIn(_WXID, joined)
            self.assertNotIn("siwx_test_out", joined)
            self.assertIn("wxid_***", joined)
        finally:
            self.api_backup._job["logs"][:] = saved

    def test_restore_status_masks_logs(self):
        saved = list(self.api_backup._restore_job["logs"])
        self.api_backup._restore_job["logs"].append(
            f"恢复目标 account={_ALIAS} 目录 D:\\siwx_test_out\\{_ALIAS}\\db_storage")
        try:
            body = self.client.get("/api/backup/restore/status").get_json()
            joined = "\n".join(str(m) for m in body["logs"])
            self.assertNotIn(_ALIAS, joined)
            self.assertNotIn("siwx_test_out", joined)
        finally:
            self.api_backup._restore_job["logs"][:] = saved


if __name__ == "__main__":
    unittest.main()
