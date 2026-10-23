"""前端回归（修订清单 2026-10-23 批次 3：P2-16 / P2-17 / P2-18）。

P2-16：导出/补生成启动失败（409/网络错误）后，进度卡与取消按钮此前不回收；
`requestCancel()` 无条件 POST `/api/job/cancel`，可能取消其他页面发起的全局任务。
修复：模块级 `currentRunMode` 记录本页任务 mode，启动失败统一清理 UI，
取消前先拉 `/api/job` 核对 mode，POST body 带上本页 mode。

P2-17：聊天内搜索的“找到 N 条匹配”滞后一次。`loadMessages(fresh)` 写回
`filterTotal = data.total` 之后没有再刷新信息条；修复为渲染/高亮后补刷。

P2-18：搜索/跳转失败停在“搜索中…”且无重试入口。修复：`filterError` 状态 +
消息区空态/重试按钮（错误文本经 `esc()` 转义），跳转调用补 catch。

本套件只做静态断言与 `node --check` 语法检查（无浏览器环境，交互行为由人工
冒烟覆盖）；使用合成的错误文案，不包含任何真实账号或本机路径。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORT_JS = ROOT / "siwx" / "ui" / "pages" / "export.js"
CHAT_JS = ROOT / "siwx" / "ui" / "pages" / "chat.js"

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


class TestExportCancelScope(unittest.TestCase):
    """P2-16：启动失败回收 UI；取消只作用于本页发起的任务。"""

    def setUp(self):
        self.src = _read(EXPORT_JS)

    def test_current_run_mode_state_exists(self):
        self.assertRegex(self.src, r"(?m)^let currentRunMode = null;")

    def test_run_and_convert_record_their_mode(self):
        self.assertIn("currentRunMode = 'export';",
                      _top_level_block(self.src, "async function run()"))
        self.assertIn("currentRunMode = 'convert';",
                      _top_level_block(self.src, "async function runConvert("))

    def test_start_failure_clears_mode_and_hides_progress_and_cancel(self):
        for marker in ("async function run()", "async function runConvert("):
            block = _top_level_block(self.src, marker)
            catch = block[block.index("} catch (e) {"):]
            self.assertIn("running = false;", catch)
            self.assertIn("currentRunMode = null;", catch)
            self.assertIn("clearRunUiOnStartFailure(e.message)", catch)
        cleanup = _top_level_block(self.src, "function clearRunUiOnStartFailure(")
        self.assertIn("e-progress-card", cleanup)
        self.assertIn("resetCancelButton()", cleanup)
        self.assertIn("el('e-cancel').classList.add('hidden')", cleanup)
        self.assertIn("esc(message)", cleanup, "错误日志必须转义后再进 innerHTML")

    def test_cancel_checks_current_job_mode_before_posting(self):
        block = _top_level_block(self.src, "async function requestCancel(")
        self.assertIn("await (await fetch('/api/job')).json()", block)
        self.assertIn("if (!job.running)", block)
        self.assertIn("job.mode !== currentRunMode", block)
        self.assertIn("当前运行的任务不是本页面发起的", block)
        # 先读 /api/job 再决定是否 POST 取消
        self.assertLess(block.index("fetch('/api/job')"),
                        block.index("/api/job/cancel"))
        # 取消请求必须带 JSON body 与 mode，后端据此拒绝误取消
        self.assertIn("headers: { 'content-type': 'application/json' }", block)
        self.assertIn("JSON.stringify({ mode: currentRunMode })", block)

    def test_poll_terminal_clears_mode(self):
        block = _top_level_block(self.src, "function poll()")
        start = block.index("if (!job.running) {")
        end = block.index("renderResult(job)", start)
        terminal = block[start:end]
        self.assertIn("currentRunMode = null;", terminal)
        self.assertIn("el('e-cancel').classList.add('hidden')", terminal)


class TestChatFilterFeedback(unittest.TestCase):
    """P2-17：计数及时刷新；P2-18：搜索/跳转失败有错误态与重试。"""

    def setUp(self):
        self.src = _read(CHAT_JS)

    def test_filter_info_refreshed_after_total_writeback(self):
        block = _top_level_block(self.src, "async function loadMessages(")
        start = block.index("filterTotal = data.total")
        end = block.index("if (opts.stick)", start)
        between = block[start:end]
        self.assertIn("updateFilterInfo();", between,
                      "写入 filterTotal 后必须刷新信息条")
        self.assertLess(between.index("highlightMatches(box)"),
                        between.index("updateFilterInfo();"),
                        "计数应在渲染/高亮之后刷新")

    def test_filter_failure_renders_retry_and_error_info(self):
        block = _top_level_block(self.src, "function runFilteredLoad()")
        self.assertIn("filterError = '';", block, "每次搜索开始应清空上次错误")
        catch = block[block.index("pending.catch((e) => {"):]
        self.assertIn("filterError =", catch)
        self.assertIn("esc(filterError)", catch, "错误文本必须转义（防 XSS）")
        self.assertIn('id="c-filter-retry"', catch)
        self.assertIn("runFilteredLoad()", catch, "重试按钮应重新发起搜索")
        self.assertIn("updateFilterInfo();", catch)
        self.assertNotIn("filterActive = false", catch,
                         "失败不得静默切回未筛选列表")

    def test_update_filter_info_has_failure_state(self):
        block = _top_level_block(self.src, "function updateFilterInfo()")
        self.assertIn("if (filterError)", block)
        self.assertIn("搜索失败：${esc(filterError)}", block)
        self.assertIn("找到 <b>${filterTotal}</b> 条匹配", block)

    def test_exit_filter_clears_error(self):
        block = _top_level_block(self.src, "function exitFilter(")
        self.assertIn("filterError = '';", block)

    def test_jump_to_ts_call_has_catch(self):
        marker = "jumpToTs(Number(hit.dataset.ts) || 0, hit.dataset.key || '')"
        start = self.src.index(marker)
        tail = self.src[start:start + 240]
        self.assertIn(".catch(", tail, "跳转失败必须有兜底，不能留未处理 rejection")
        self.assertIn("跳转失败", tail)


@unittest.skipUnless(NODE, "未安装 node，跳过 JS 语法检查")
class TestJsSyntax(unittest.TestCase):
    """导出页/聊天页脚本必须是可解析的 ES 模块。

    不能直接对 .js 跑 `node --check`：仓库根没有 package.json(type=module)，
    node 会按 CommonJS 解析并在 import/export 上报错；复制为 .mjs 再检查。
    """

    def _check(self, src_path: Path):
        with tempfile.TemporaryDirectory(prefix="siwx_jscheck_") as td:
            target = Path(td) / (src_path.stem + ".mjs")
            shutil.copyfile(src_path, target)
            proc = subprocess.run([NODE, "--check", str(target)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             f"{src_path.name} 语法错误：{proc.stderr.strip()}")

    def test_export_js_syntax(self):
        self._check(EXPORT_JS)

    def test_chat_js_syntax(self):
        self._check(CHAT_JS)


if __name__ == "__main__":
    unittest.main()
