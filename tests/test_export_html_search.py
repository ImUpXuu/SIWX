"""P1-5 导出 HTML 搜索入口冒烟测试。

项目没有 jsdom/浏览器测试设施，这里对模板做静态断言，锁住四件事：
1. 页头放大镜（searchToggle）与搜索栏（searchBar）的挂载点存在；
2. 搜索栏默认隐藏、`.active` 时显示；
3. renderer.js 里有 searchToggle 的 click 监听，且 toggle `.active` 后
   focus/select 输入框（否则搜索栏永远不可达）；
4. searchClose 仍保留"收起 + 清空条件 + 重新过滤"的既有行为。
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "siwx" / "templates" / "default"
HEAD = (TEMPLATE_DIR / "head.html").read_text(encoding="utf-8")
JS = (TEMPLATE_DIR / "renderer.js").read_text(encoding="utf-8")


def _listener(elem_id):
    """截取某个 getElementById(id).addEventListener(...) 的监听体。

    监听器统一以两空格缩进的 `});` 收尾，够用即可，无需引入 JS 解析器。
    """
    marker = f"getElementById('{elem_id}').addEventListener"
    start = JS.index(marker)
    end = JS.index("\n  });", start)
    return JS[start:end]


class TestExportHtmlSearchEntry(unittest.TestCase):

    def test_markup_has_search_toggle_and_bar(self):
        self.assertIn('id="searchToggle"', HEAD)
        self.assertIn('id="searchBar"', HEAD)
        self.assertIn('id="searchInput"', HEAD)
        self.assertIn('id="searchClose"', HEAD)

    def test_css_hides_bar_until_active(self):
        self.assertIn(".search-bar{display:none", HEAD)
        self.assertIn(".search-bar.active{display:flex}", HEAD)

    def test_toggle_listener_opens_and_focuses_input(self):
        body = _listener("searchToggle")
        self.assertIn("classList.toggle('active'", body)
        self.assertIn("focus()", body)
        self.assertIn("select()", body)

    def test_close_listener_still_clears_and_refilters(self):
        body = _listener("searchClose")
        self.assertIn("classList.remove('active')", body)
        self.assertIn("applyFilter()", body)


if __name__ == "__main__":
    unittest.main()
