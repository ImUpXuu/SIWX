"""备份/恢复「杀微信」两段式确认回归（P1-1 + P2-14）。

P1-1：后端契约是「POST 不带 allowKill 且微信在跑 → 返回 needKill，用户确认后
必须带 allowKill:true 重发」。前端 backup.js 的 hideKillDialog() 原先在弹窗
关闭时就清掉 pendingPayload / rPendingApply，而确认回调（b-kill-yes）先调
hideKillDialog() 再调 startBackup(true) / startRestoreApply(true)——重发退化成
无 allowKill 的首次请求：备份端再回 needKill 弹窗重现（死循环），恢复端静默
return 界面无反应。修复：hideKillDialog 只收弹窗不清 pending，由
startBackup / startRestoreApply 取走请求体后自行清理。

P2-14：备份/恢复页轮询的 catch 原先为空且无阈值，后端挂掉时无限吞错、进度条
永久冻结。修复：与 export.js 同口径的 failStreak（连续失败 ≥5 次终局报错并
停止轮询，成功重置计数）。

后端 needKill→allowKill 契约的备份侧已由 test_regressions.py 的
TestBackupApi.test_need_kill_two_phase_then_run 钉住；本套件补恢复侧端到端
（微信在跑 → needKill → allowKill:true → 任务跑完），并静态断言前端回调
顺序与轮询失败阈值。检测点 siwx.discover.find_wechat_pids 全程 mock，
不依赖本机微信进程；全部使用合成数据，不含真实账号或本机路径。
"""
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._base import IsolatedRootCase

ROOT = Path(__file__).resolve().parent.parent
BACKUP_JS = ROOT / "siwx" / "ui" / "pages" / "backup.js"

NODE = shutil.which("node")


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path.name} 内容为空"
    return text


def _top_level_block(src: str, marker: str) -> str:
    """截取模块级函数：从 marker 到下一个顶格 `}`（页面脚本均为 0 缩进收尾）。"""
    start = src.index(marker)
    end = src.index("\n}\n", start)
    assert end > start, f"找不到 {marker} 的函数结尾"
    return src[start:end + 2]


def _reset_job_slots():
    """复位两个模块级任务槽，避免污染同进程的后续用例。"""
    import siwx.api_backup as api
    reset = {"running": False, "done": False, "ok": False, "progress": 0,
             "msg": "", "logs": [], "result": None, "error": None}
    api._job.update(reset)
    api._restore_job.update(reset)


class TestRestoreApplyKillContract(IsolatedRootCase):
    """恢复 apply 的 needKill→allowKill 契约（P1-1 后端侧，端到端）。"""

    def _fake_pids(self):
        """第 1 次（apply 端点的两段式检查）报微信在跑，其后（runner 内）无进程，
        这样带 allowKill 重发后 runner 跳过真实 taskkill，不会碰本机进程。"""
        state = {"calls": 0}

        def fake():
            state["calls"] += 1
            return [4321] if state["calls"] == 1 else []
        return fake

    def test_need_kill_then_allow_kill_runs_to_done(self):
        """微信在跑：不带 allowKill 只报 needKill 不启动；带 allowKill 才执行。"""
        from siwx import server
        client = server.app.test_client()
        stage_dir = tempfile.mkdtemp(prefix="siwx_stage_")
        self.addCleanup(shutil.rmtree, stage_dir, True)
        fake_stage = {"dir": stage_dir, "pack": "x.siwxpack"}
        fake_result = {"mode": "merge", "inserted": 3, "dup": 1,
                       "rewrote": [], "session_fixed": 0}
        body = {"stage_id": "stage_example", "target": "wechat", "chats": None}
        try:
            with mock.patch("siwx.discover.find_wechat_pids",
                            side_effect=self._fake_pids()) as fp, \
                    mock.patch("siwx.restore.load_stage",
                               return_value=fake_stage) as ls, \
                    mock.patch("siwx.restore.apply_wechat",
                               return_value=fake_result) as aw:
                # 第一段：微信在跑 + 未确认 → needKill，任务不启动
                r = client.post("/api/backup/restore/apply", json=body)
                j = r.get_json()
                self.assertTrue(j.get("needKill"), j)
                self.assertEqual(j["pids"], [4321])
                st = client.get("/api/backup/restore/status").get_json()
                self.assertFalse(st["running"])
                ls.assert_not_called()
                aw.assert_not_called()

                # 第二段：用户确认 → 带 allowKill 重发 → 任务跑完
                r2 = client.post("/api/backup/restore/apply",
                                 json={**body, "allowKill": True})
                self.assertTrue(r2.get_json().get("started"), r2.get_json())
                st = None
                for _ in range(100):
                    st = client.get("/api/backup/restore/status").get_json()
                    if st["done"]:
                        break
                    time.sleep(0.05)
                self.assertTrue(st["done"], "恢复任务未在限时内结束")
                self.assertTrue(st["ok"], st.get("error"))
                self.assertEqual(st["result"]["inserted"], 3)
                # 契约钉点：两次 POST 各查一次进程状态 + runner 内一次（无进程）
                self.assertEqual(fp.call_count, 3)
                ls.assert_called_once_with("stage_example")
                aw.assert_called_once()
                kwargs = aw.call_args.kwargs
                self.assertIsNone(kwargs.get("chats"))
        finally:
            _reset_job_slots()

    def test_cache_target_never_need_kill(self):
        """target=cache 不碰微信数据目录，微信在跑也不该要 allowKill。"""
        from siwx import server
        client = server.app.test_client()
        fake_result = {"mode": "install", "files": 1, "inserted": 0, "dup": 0}
        stage_dir = tempfile.mkdtemp(prefix="siwx_stage_")
        self.addCleanup(shutil.rmtree, stage_dir, True)
        try:
            with mock.patch("siwx.discover.find_wechat_pids",
                            return_value=[4321]), \
                    mock.patch("siwx.restore.load_stage",
                               return_value={"dir": stage_dir}), \
                    mock.patch("siwx.restore.apply",
                               return_value=fake_result) as aw:
                r = client.post("/api/backup/restore/apply", json={
                    "stage_id": "stage_example", "target": "cache"})
                self.assertTrue(r.get_json().get("started"), r.get_json())
                st = None
                for _ in range(100):
                    st = client.get("/api/backup/restore/status").get_json()
                    if st["done"]:
                        break
                    time.sleep(0.05)
                self.assertTrue(st["ok"], st.get("error"))
                aw.assert_called_once()
        finally:
            _reset_job_slots()


class TestBackupJsKillDialogOrder(unittest.TestCase):
    """P1-1 前端侧：hideKillDialog 不得清 pending，重发必须带原请求体。"""

    def setUp(self):
        self.src = _read(BACKUP_JS)
        self.hide = _top_level_block(self.src, "function hideKillDialog()")
        self.start_backup = _top_level_block(self.src, "async function startBackup(")
        self.start_restore = _top_level_block(self.src, "async function startRestoreApply(")
        yes_at = self.src.index("el('b-kill-yes').addEventListener")
        self.kill_yes = self.src[yes_at:self.src.index("el('b-kill-no').addEventListener", yes_at)]

    def test_hide_kill_dialog_does_not_clear_pending(self):
        self.assertIn("classList.add('hidden')", self.hide)
        self.assertNotIn("pendingPayload = null", self.hide,
                         "hideKillDialog 清 pending 会让确认重发丢掉请求体（P1-1）")
        self.assertNotIn("rPendingApply = null", self.hide)

    def test_kill_yes_confirms_then_resends_with_allow_kill(self):
        self.assertIn("startBackup(true)", self.kill_yes)
        self.assertIn("startRestoreApply(true)", self.kill_yes)

    def test_start_backup_reuses_pending_payload_and_clears_it(self):
        self.assertIn("allowKill && pendingPayload", self.start_backup)
        self.assertIn("{ ...pendingPayload, allowKill: true }", self.start_backup)
        # 取走即清，且清空发生在发请求之前
        clear_at = self.start_backup.index("pendingPayload = null;")
        fetch_at = self.start_backup.index("fetchJSON('/api/backup/start'")
        self.assertLess(clear_at, fetch_at)
        # 降级路径也必须带上 allowKill，否则带 true 重发仍会再收 needKill
        self.assertIn("buildPayload(allowKill)", self.start_backup)
        self.assertNotIn("buildPayload(false)", self.start_backup)

    def test_start_restore_apply_reuses_pending_body_and_clears_it(self):
        self.assertIn("{ ...rPendingApply, allowKill: true }", self.start_restore)
        clear_at = self.start_restore.index("rPendingApply = null;")
        fetch_at = self.start_restore.index("fetchJSON('/api/backup/restore/apply'")
        self.assertLess(clear_at, fetch_at)
        # needKill 分支要把请求体放回去，供下次确认后重发
        self.assertIn("r.needKill) { rPendingApply = body; return; }",
                      self.start_restore)


class TestBackupJsPollFailStreak(unittest.TestCase):
    """P2-14：轮询连续失败 ≥5 次终局报错并停止，成功重置计数（对齐 export.js）。"""

    def setUp(self):
        self.src = _read(BACKUP_JS)
        self.poll = _top_level_block(self.src, "async function poll()")
        self.rpoll = _top_level_block(self.src, "async function rPoll()")

    def test_backup_poll_has_threshold(self):
        self.assertIn("pollFailStreak = 0", self.poll)
        self.assertIn("++pollFailStreak >= 5", self.poll)
        catch_at = self.poll.index("} catch (e) {")
        catch = self.poll[catch_at:]
        self.assertIn("clearInterval(pollTimer)", catch, "达阈值后必须停止轮询")
        self.assertIn("pollTimer = null", catch)
        self.assertIn("与后端失去联系", catch)
        self.assertIn("esc(e.message)", catch, "错误文案要过 esc()")

    def test_restore_poll_has_threshold(self):
        self.assertIn("rPollFailStreak = 0", self.rpoll)
        self.assertIn("++rPollFailStreak >= 5", self.rpoll)
        catch_at = self.rpoll.index("} catch (e) {")
        catch = self.rpoll[catch_at:]
        self.assertIn("clearInterval(rPollTimer)", catch)
        self.assertIn("rPollTimer = null", catch)
        self.assertIn("与后端失去联系", catch)

    def test_counters_reset_on_new_polling(self):
        self.assertIn("pollFailStreak = 0;", _top_level_block(self.src, "function startPolling()"))
        self.assertIn("rPollFailStreak = 0;",
                      _top_level_block(self.src, "function startRestorePolling()"))


@unittest.skipUnless(NODE, "未安装 node，跳过 JS 语法检查")
class TestBackupJsSyntax(unittest.TestCase):
    """backup.js 必须是可解析的 ES 模块（与 CI 的 node --check 门禁同口径）。"""

    def test_backup_js_parses(self):
        with tempfile.TemporaryDirectory(prefix="siwx_jscheck_") as td:
            target = Path(td) / "backup.mjs"
            shutil.copyfile(BACKUP_JS, target)
            proc = subprocess.run([NODE, "--check", str(target)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             f"backup.js 语法错误：{proc.stderr.strip()}")


if __name__ == "__main__":
    unittest.main()
