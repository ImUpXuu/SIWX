"""前端回归（P2 批次：P2-10 MCP 页销毁与首次拉取的竞态）。

P2-10：mcp.js 的 `pollLogs()` 是 `await fetchLogs(); pollTimer = setInterval(...)`
两段式。若用户在首次 fetchLogs 的 await 窗口内切页，destroy() 执行时 pollTimer
仍为 null，随后 setInterval 照常建立且无人再清理——导航离开后每 2 秒仍打
/api/mcp/logs，并把结果写进已脱离 DOM 的节点。修复：引入模块级 `destroyed`
存活哨兵——destroy() 置位并清掉现有定时器；pollLogs 在 setInterval 前检查，
已销毁则不再建立定时器；fetchLogs 回写 DOM 前也检查，销毁后丢弃响应。

本套件只做静态断言与 `node --check` 语法检查（无浏览器环境，真实交互由
人工冒烟覆盖）；全部使用合成标识，不含真实账号或本机路径。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_JS = ROOT / "siwx" / "ui" / "pages" / "mcp.js"

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


class TestMcpDestroyRace(unittest.TestCase):
    """P2-10：切页后不得残留 2 秒轮询定时器或向脱离 DOM 的节点回写。"""

    def setUp(self):
        self.src = _read(MCP_JS)
        self.poll = _top_level_block(self.src, "async function pollLogs()")

    def test_destroy_sets_sentinel_and_clears_timer(self):
        destroy = _top_level_block(self.src, "export function destroy()")
        self.assertIn("destroyed = true", destroy,
                      "destroy() 必须先置存活哨兵")
        self.assertIn("clearInterval(pollTimer)", destroy)
        self.assertIn("pollTimer = null", destroy,
                      "定时器句柄要复位，防止悬挂引用")

    def test_init_resets_sentinel(self):
        self.assertRegex(self.src, r"export async function init\(\)\s*\{[^}]*destroyed = false",
                         "重新进入页面必须复位哨兵，否则轮询永不启动")

    def test_polllogs_skips_interval_after_destroy(self):
        # setInterval 前必须有 destroyed 守卫，且先于 setInterval 执行
        self.assertIn("if (destroyed) return;", self.poll,
                      "首次拉取后、建立定时器前必须检查哨兵")
        self.assertLess(self.poll.rindex("if (destroyed) return;"),
                        self.poll.index("setInterval(fetchLogs"),
                        "哨兵检查必须早于 setInterval，否则竞态窗口内照样建定时器")

    def test_fetchlogs_drops_response_after_destroy(self):
        fetch = self.poll[self.poll.index("const fetchLogs = async () => {"):]
        self.assertIn("if (destroyed || document.hidden) return;", fetch,
                      "已销毁页面不应再发起 /api/mcp/logs 请求")
        # 成功与失败两条路径都要在回写 DOM 前检查哨兵
        self.assertLess(fetch.index("if (destroyed) return;"),
                        fetch.index("render(d.logs || [])"),
                        "成功路径回写 DOM 前必须检查哨兵")
        catch = fetch[fetch.index("} catch (e) {"):]
        self.assertIn("if (destroyed) return;", catch,
                      "失败路径写错误提示前也必须检查哨兵")


@unittest.skipIf(NODE is None, "未找到 node，跳过语法检查")
class TestMcpJsSyntax(unittest.TestCase):
    """改动过的脚本必须是可解析的 ES 模块。

    仓库根没有 package.json(type=module)，直接 `node --check` 会把 import/export
    当 CommonJS 语法错；复制为 .mjs 再检查（同 test_fix_b5_ui.TestJsSyntax 口径）。
    """

    def _check(self, src_path: Path):
        with tempfile.TemporaryDirectory(prefix="siwx_jscheck_") as td:
            target = Path(td) / (src_path.stem + ".mjs")
            shutil.copyfile(src_path, target)
            proc = subprocess.run([NODE, "--check", str(target)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             f"{src_path.name} 语法错误：{proc.stderr.strip()}")

    def test_mcp_js(self):
        self._check(MCP_JS)


if __name__ == "__main__":
    unittest.main()
