"""前端回归（修订清单 2026-10-23 批次 5：P3-14 / P3-15 / P3-16 / P3-17 / P3-18 / P3-19）。

P3-14：取消的任务 `job.ok` 仍为 true、`report.cancelled=true`，终局 toast 却报
“导出完成”。修复：先判 `report.cancelled`，按任务类型给“已取消”文案。

P3-15：补生成任务级失败后的“重试”无条件调 `run()`，退化成一次全量导出。
修复：模块级 `lastTask` 记录类型与参数，错误分支按钮带 `data-retry-mode`，
`bindResultActions` 按 mode 分派到 `runConvert()` / `run()`。

P3-16：引导页 add 模式运行中关闭向导，`teardown()` 把 `body` 置空，onLog 与
完成回调仍访问 `body.contains()` → TypeError。修复：teardown 复位
`state.running`，两处回调加 `body &&` / `logBox &&` 守卫，关闭后静默丢弃。

P3-17：导出 HTML 灯箱直接采信 `mediaPath` / `cdnurl` 作 src/href。
修复：图库 src 与 `#lbDownload` 的 href 统一走 scheme 白名单（`safeUrl`，
媒体路径经 `safeMedia` 包装以放行裸相对路径），判空即跳过/隐藏下载。

P3-18：聊天页“增量同步”后重渲染一律不跟底，浏览位置丢失。
修复：刷新前记 atBottom 与视口锚点消息 key，`loadMessages(true, {stick: atBottom})`，
不在底部时按锚点 `scrollIntoView({block:'start'})` 恢复。

P3-19：浅色主题筛选文字用 `--accent` 落在浅底上约 2.2:1。
修复：三处文字色改 `--accent-fg`，并用 WCAG 公式计算对比度钉住 ≥ 4.5。

本套件只做静态断言、对比度计算与 `node --check` 语法检查（无浏览器环境，
点击/滚动等真实交互由人工冒烟覆盖）；全部使用合成数据，不含真实账号或本机路径。
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORT_JS = ROOT / "siwx" / "ui" / "pages" / "export.js"
ONBOARDING_JS = ROOT / "siwx" / "ui" / "onboarding.js"
CHAT_JS = ROOT / "siwx" / "ui" / "pages" / "chat.js"
CHAT_CSS = ROOT / "siwx" / "ui" / "pages" / "chat.css"
RENDERER_JS = ROOT / "siwx" / "templates" / "default" / "renderer.js"
APP_CSS = ROOT / "siwx" / "ui" / "app.css"

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


def _indented_block(src: str, marker: str) -> str:
    """截取 IIFE 内两空格缩进的函数（renderer.js 模板脚本）。"""
    start = src.index(marker)
    end = src.index("\n  }\n", start)
    assert end > start, f"找不到 {marker} 的函数结尾"
    return src[start:end + 3]


def _srgb_channel_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.x 相对亮度：输入 #rrggbb。"""
    h = hex_color.lstrip("#")
    assert len(h) == 6, f"只接受 #rrggbb：{hex_color}"
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * _srgb_channel_to_linear(r) +
            0.7152 * _srgb_channel_to_linear(g) +
            0.0722 * _srgb_channel_to_linear(b))


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 对比度：(L_light + 0.05) / (L_dark + 0.05)。"""
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _light_token(hex_name: str) -> str:
    """取 app.css 浅色 `:root` 里的颜色 token（首次出现的定义）。"""
    m = re.search(r"--" + re.escape(hex_name) + r":\s*(#[0-9a-fA-F]{6})", _read(APP_CSS))
    assert m, f"app.css 缺少 --{hex_name} 的十六进制定义"
    return m.group(1)


class TestExportCancelToast(unittest.TestCase):
    """P3-14：取消的终局不再提示“导出完成”。"""

    def setUp(self):
        self.src = _read(EXPORT_JS)
        self.poll = _top_level_block(self.src, "function poll()")

    def test_cancelled_checked_before_done_toast(self):
        self.assertIn("job.report.cancelled", self.poll,
                      "终局必须先判 report.cancelled")
        self.assertLess(self.poll.index("job.report.cancelled"),
                        self.poll.index("'导出完成'"),
                        "cancelled 判定要早于“完成”文案")
        # 旧写法：if (job.ok) toast(...) 一行收尾，取消任务会被报成完成
        self.assertNotIn("if (job.ok) toast(", self.poll)

    def test_cancelled_toasts_are_per_task_kind(self):
        self.assertIn("导出已取消（已完成部分保留）", self.poll)
        self.assertIn("格式补生成已取消（已完成部分保留）", self.poll)
        cancelled = self.poll[self.poll.index("if (cancelled)"):]
        self.assertIn("kind === 'convert'", cancelled,
                      "取消文案仍需按 kind 区分导出与补生成")
        self.assertLess(cancelled.index("格式补生成已取消"),
                        cancelled.index("导出已取消"),
                        "convert 分支应排在前面（三元条件）")

    def test_success_and_failure_paths_kept(self):
        self.assertIn("'格式补生成完成'", self.poll)
        self.assertIn("toast(job.error || '导出失败', true)", self.poll)


class TestExportRetryDispatch(unittest.TestCase):
    """P3-15：补生成失败后的“重试”不得跑成全量导出。"""

    def setUp(self):
        self.src = _read(EXPORT_JS)

    def test_last_task_records_kind_and_convert_params(self):
        self.assertRegex(self.src, r"(?m)^let lastTask = \{")
        conv = _top_level_block(self.src, "async function runConvert(")
        self.assertIn("lastTask = { mode: 'convert', formats, overwrite };", conv,
                      "补生成必须记住 formats/overwrite，重试才能原样重跑")
        run = _top_level_block(self.src, "async function run()")
        self.assertIn("lastTask = { mode: 'export' };", run)

    def test_error_branch_retry_carries_mode(self):
        block = _top_level_block(self.src, "function renderResult(job)")
        start = block.index("if (job.error) {")
        end = block.index("const r = job.report || {};", start)
        err = block[start:end]
        self.assertIn("data-retry-mode=", err, "错误分支的重试按钮要带 mode")
        self.assertIn("retryMode()", err)
        # 无 report 的兜底分支同样分派
        tail = block[block.index("if (!r.sessions) {"):]
        self.assertIn("data-retry-mode=", tail)

    def test_retry_mode_helper_picks_convert_only_for_convert(self):
        helper = _top_level_block(self.src, "function retryMode()")
        self.assertIn("lastTask.mode === 'convert'", helper)

    def test_bind_dispatches_convert_to_run_convert(self):
        bind = _top_level_block(self.src, "function bindResultActions(box)")
        self.assertIn("a.dataset.retryMode", bind)
        self.assertIn("runConvert(lastTask.formats", bind)
        self.assertIn("run();", bind)
        # 分派顺序：先判 convert，再落回全量导出
        self.assertLess(bind.index("mode === 'convert'"), bind.index("run();"))
        # 目录未知时不得静默退化成全量导出
        self.assertIn("导出目录未知，无法重试补生成", bind)


class TestOnboardingCloseWhileRunning(unittest.TestCase):
    """P3-16：add 模式运行中关闭向导，回调不得抛 TypeError。"""

    def setUp(self):
        self.src = _read(ONBOARDING_JS)

    def test_teardown_resets_running(self):
        teardown = _top_level_block(self.src, "function teardown()")
        self.assertIn("if (state) state.running = false;", teardown,
                      "关闭后必须复位 running，否则再次打开会被 tryRun 拦住")
        self.assertIn("body = null", teardown)

    def test_onlog_guards_body_and_logbox(self):
        extract = _top_level_block(self.src, "function runExtract()")
        self.assertIn("if (body && logBox && body.contains(logBox))", extract,
                      "onLog 必须同时守卫 body 与 logBox")
        self.assertNotIn("if (body.contains(logBox))", extract)

    def test_completion_callback_guards_body_and_resbox(self):
        extract = _top_level_block(self.src, "function runExtract()")
        self.assertIn("if (!body || !resBox) return;", extract,
                      "完成回调在向导已关闭时应静默丢弃")
        # 守卫必须出现在第一次访问 DOM 之前
        self.assertLess(extract.index("if (!body || !resBox) return;"),
                        extract.index("progress.classList.add('hidden')"))

    def test_close_still_allowed(self):
        close = _top_level_block(self.src, "function close()")
        self.assertIn("if (!closable || !ov) return;", close,
                      "关闭入口不应被本次修复收紧")


class TestRendererLightboxScheme(unittest.TestCase):
    """P3-17：灯箱 src / 下载 href 过 scheme 白名单。

    媒体路径是导出目录内的裸相对路径（media/x.jpg），`safeUrl` 的白名单要求
    显式前缀，故新增 `safeMedia` 包装：先走 `safeUrl`，未命中时只放行“无
    scheme、不以 // 开头”的相对路径。图库与灯箱都经它过滤。
    """

    def setUp(self):
        self.src = _read(RENDERER_JS)

    def test_safe_media_wraps_safe_url(self):
        helper = _indented_block(self.src, "  function safeMedia(u) {")
        self.assertIn("var s = safeUrl(u);", helper)
        self.assertIn("if (s) return s;", helper)
        self.assertIn(r"/^[a-z][a-z0-9+.-]*:/i", helper, "必须显式拒绝 scheme")
        self.assertIn("raw.slice(0, 2) === '//'", helper, "协议相对地址也要拒绝")

    def test_build_gallery_filters_every_src(self):
        gallery = _indented_block(self.src, "  function buildGallery() {")
        self.assertIn("safeMedia(m.mediaPath)", gallery)
        self.assertIn("safeMedia(xmlAttr(", gallery)
        self.assertNotIn("src = m.mediaPath", gallery, "raw mediaPath 不得直通 src")
        # 判空即跳过：空 src 不进图库
        self.assertIn("if (!src) continue;", gallery)

    def test_show_lb_guards_src_and_download(self):
        lb = _indented_block(self.src, "  function showLb(pos) {")
        self.assertIn("var src = safeMedia(it.src);", lb)
        self.assertIn("img.src = src;", lb)
        self.assertIn("dl.href = src;", lb)
        self.assertNotIn("dl.href = it.src;", lb, "raw src 不得直通下载链接")
        # 源不可用时不得留可点击 href
        self.assertIn("dl.removeAttribute('href');", lb)
        self.assertIn("dl.style.display = 'none';", lb)


class TestChatRefreshKeepsScroll(unittest.TestCase):
    """P3-18：增量同步后按 atBottom / 锚点恢复浏览位置。"""

    def setUp(self):
        self.src = _read(CHAT_JS)
        start = self.src.index("el('c-refresh').addEventListener")
        self.refresh = self.src[start:self.src.index("el('c-select').addEventListener", start)]

    def test_refresh_records_bottom_and_anchor(self):
        self.assertIn("const atBottom =", self.refresh)
        self.assertIn("viewportAnchorKey(box)", self.refresh)
        self.assertRegex(self.refresh, r"loadMessages\(true,\s*\{\s*stick:\s*atBottom\s*\}\)",
                         "刷新应按是否贴底决定是否跟底")
        self.assertIn("restoreScrollAnchor(anchor)", self.refresh)
        self.assertLess(self.refresh.index("viewportAnchorKey(box)"),
                        self.refresh.index("loadMessages(true"),
                        "锚点必须在重载前采集")

    def test_anchor_helpers_use_css_escape_and_start_block(self):
        pick = _top_level_block(self.src, "function viewportAnchorKey(box)")
        self.assertIn(".m-row[data-key]", pick)
        restore = _top_level_block(self.src, "function restoreScrollAnchor(key)")
        self.assertIn("CSS.escape(key)", restore)
        self.assertIn("scrollIntoView({ block: 'start' })", restore)
        # 瞬时归位：临时关掉 #c-msgs 的平滑滚动，别让恢复变成一段长动画
        self.assertIn("box.style.scrollBehavior = 'auto';", restore)
        self.assertIn("box.style.scrollBehavior = prev;", restore)
        self.assertIn("if (!target) return;", restore, "锚点不在窗口内则保持现状")

    def test_other_load_messages_calls_unchanged(self):
        open_chat = _top_level_block(self.src, "async function openChat(")
        self.assertIn("loadMessages(true, { stick: true })", open_chat)
        jump = _top_level_block(self.src, "async function jumpToTs(")
        self.assertIn("loadMessages(true, { before: ts + 1, limit: 80 })", jump)


class TestChatCssContrast(unittest.TestCase):
    """P3-19：筛选文字在浅底上达到 AA（≥ 4.5:1）。"""

    def setUp(self):
        self.css = _read(CHAT_CSS)

    def test_three_places_use_accent_fg(self):
        self.assertIn(".chip:hover { border-color: var(--accent); color: var(--accent-fg); }", self.css)
        self.assertIn(".chip.on {", self.css)
        chip_on = self.css[self.css.index(".chip.on {"):]
        chip_on = chip_on[:chip_on.index("}")]
        self.assertIn("color: var(--accent-fg);", chip_on)
        self.assertIn("background: var(--accent-soft);", chip_on, "背景保持不变")
        self.assertIn(".c-filter-info b { color: var(--accent-fg); }", self.css)

    def test_no_accent_text_color_left(self):
        # 只剩 border-color / background 可以用 --accent，`color:` 文字色不允许
        # （注意 border-color 以 "color:" 结尾，必须用左边界排除）
        accent_text = re.compile(r"(?<![-a-z])color:\s*var\(--accent\)\s*;")
        self.assertIsNone(accent_text.search(self.css),
                          "仍有用 --accent 作文字色的规则")
        for rule in (".chip:hover {", ".chip.on {", ".c-filter-info b {"):
            start = self.css.index(rule)
            body = self.css[start:self.css.index("}", start) + 1]
            self.assertIsNone(accent_text.search(body), f"{rule} 仍用 --accent 作文字色")

    def test_contrast_meets_aa(self):
        fg = _light_token("accent-fg")
        bg = _light_token("accent-soft")
        self.assertGreaterEqual(contrast_ratio(fg, bg), 4.5,
                                f"{fg} on {bg} 未达 AA 4.5:1")
        # 旧值确实不达标（修复动机），避免有人把 token 改回去还“碰巧”过关
        old = _light_token("accent")
        self.assertLess(contrast_ratio(old, bg), 3.0,
                        f"{old} on {bg} 本应不达标；对比度公式或 token 需复核")

    def test_contrast_formula_sanity(self):
        # 黑白对比度固定为 21:1，钉住公式本身
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#ffffff", "#ffffff"), 1.0, places=4)


@unittest.skipUnless(NODE, "未安装 node，跳过 JS 语法检查")
class TestJsSyntax(unittest.TestCase):
    """改动过的四个脚本必须是可解析的 ES 模块。

    仓库根没有 package.json(type=module)，直接 `node --check` 会把 import/export
    当 CommonJS 语法错；复制为 .mjs 再检查（renderer.js 是 IIFE，同样能过）。
    """

    def _check(self, src_path: Path):
        with tempfile.TemporaryDirectory(prefix="siwx_jscheck_") as td:
            target = Path(td) / (src_path.stem + ".mjs")
            shutil.copyfile(src_path, target)
            proc = subprocess.run([NODE, "--check", str(target)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             f"{src_path.name} 语法错误：{proc.stderr.strip()}")

    def test_export_js(self):
        self._check(EXPORT_JS)

    def test_onboarding_js(self):
        self._check(ONBOARDING_JS)

    def test_chat_js(self):
        self._check(CHAT_JS)

    def test_renderer_js(self):
        self._check(RENDERER_JS)


if __name__ == "__main__":
    unittest.main()
