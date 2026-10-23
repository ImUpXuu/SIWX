"""版本号"多处同步"的单一断言源（P2-22 / P2-23）。

历史上版本号散落在多处：`siwx/__init__.py`、README 徽章、`docs/README.md` 头部、
`version.json`、`siwx/ui/app.js` 的 UI_VERSION 与 `siwx/ui/index.html` 的静态资源
`?v=` 查询串。缺任何一处都会让用户读到旧版本号、或让浏览器继续用缓存的旧 JS
（前端改了却"没生效"）。本文件把这几处的关系钉死，避免发版清单靠人肉核对。

`siwx.__version__` 对 `version.json`、README 徽章的断言已在
`tests/test_regressions.py::TestVersionSource` 中；这里补齐 docs/README.md
头部与前端 UI_VERSION（后者此前完全无人校验）。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

VER_RE = r"([0-9]+\.[0-9]+\.[0-9]+)"


class TestDocVersionSync(unittest.TestCase):
    """siwx.__version__ == README 徽章 == docs/README.md 头部。"""

    def test_docs_readme_head_matches_package_version(self):
        from siwx import __version__

        text = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        m = re.search(r"^>\s*\*\*版本\*\*:\s*v" + VER_RE, text, re.MULTILINE)
        self.assertIsNotNone(m, "docs/README.md 头部未找到 `> **版本**: vX.Y.Z`")
        self.assertEqual(
            m.group(1), __version__,
            "docs/README.md 头部版本与 siwx.__version__ 不一致（发版时四处同步）")

    def test_readme_badge_matches_package_version(self):
        from siwx import __version__

        text = (ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"badge/version-" + VER_RE + r"-success", text)
        self.assertIsNotNone(m, "README 未找到 version-<ver>-success 徽章")
        self.assertEqual(m.group(1), __version__,
                         "README 徽章版本与 siwx.__version__ 不一致")

    def test_all_three_agree(self):
        """三处一次性对齐，失败信息能直接看出是哪一处漂了。"""
        from siwx import __version__

        docs = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        badge = (ROOT / "README.md").read_text(encoding="utf-8")
        v_docs = re.search(r"^>\s*\*\*版本\*\*:\s*v" + VER_RE, docs, re.MULTILINE)
        v_badge = re.search(r"badge/version-" + VER_RE + r"-success", badge)
        self.assertEqual(
            {__version__,
             v_docs.group(1) if v_docs else None,
             v_badge.group(1) if v_badge else None},
            {__version__},
            "版本号不一致：siwx.__version__=%s, docs/README=%s, README 徽章=%s"
            % (__version__,
               v_docs.group(1) if v_docs else "<未找到>",
               v_badge.group(1) if v_badge else "<未找到>"))


class TestUiVersionSync(unittest.TestCase):
    """app.js 的 UI_VERSION 必须等于 index.html 里所有 js 的 ?v=。"""

    @staticmethod
    def _app_ui_version():
        text = (ROOT / "siwx" / "ui" / "app.js").read_text(encoding="utf-8")
        m = re.search(r"UI_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
        return m.group(1) if m else None

    def test_ui_version_declared(self):
        self.assertIsNotNone(self._app_ui_version(),
                             "siwx/ui/app.js 未找到 UI_VERSION 字面量")

    def test_index_html_script_versions_match_ui_version(self):
        ui_version = self._app_ui_version()
        self.assertIsNotNone(ui_version)

        html = (ROOT / "siwx" / "ui" / "index.html").read_text(encoding="utf-8")
        pairs = re.findall(r"/(?:app|common)\.js\?v=([0-9A-Za-z_.-]+)", html)
        self.assertGreaterEqual(
            len(pairs), 2,
            "index.html 应至少包含 /common.js?v= 与 /app.js?v= 两处版本串，实际: %r" % pairs)
        self.assertEqual(
            set(pairs), {ui_version},
            "index.html 的 ?v= (%r) 与 app.js 的 UI_VERSION=%s 不一致" % (pairs, ui_version))

    def test_app_js_served_without_query_in_html(self):
        """防止有人把带 ?v= 的写法改回裸引用（裸引用会绕过缓存戳）。"""
        html = (ROOT / "siwx" / "ui" / "index.html").read_text(encoding="utf-8")
        bare = [s for s in re.findall(r'src="(/[^"]*\.js)"', html)
                if s.endswith("/app.js") or s.endswith("/common.js")]
        self.assertEqual(bare, [], "index.html 存在未带 ?v= 的 app.js/common.js 引用: %r" % bare)


if __name__ == "__main__":
    unittest.main()
