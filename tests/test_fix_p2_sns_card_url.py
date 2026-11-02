"""P2-1：朋友圈卡片链接 scheme 白名单（存储型 XSS，前后端双侧）。

card.url 源自朋友圈 XML 里**联系人可控**的 content_url，此前被原样透传到：
  - 前端 sns.js 的 ``:href="card.url"``（music 卡 + link 卡）；
  - 导出 HTML ``_html_card._a()`` 的 ``<a href>``；
  - 导出 Markdown 的 ``[标题](url)``。
本站暴露 /api/run、/api/settings/clear、/api/backup/* 等破坏性接口，
``javascript:`` 伪协议直达 DOM 即可打这些接口。

修复口径与既有白名单完全一致（api_chat._safe_link_url / ui common.js safeUrl /
chat.js 内联白名单）：放行 ``https?:``、相对 ``/``（含协议相对 ``//``）、``./``、
``../``、``#``；其余 scheme（javascript: / data: / vbscript:，含大小写混淆）
一律拒绝。数据层（sns.public_card 的 content_url 透传）不动，在渲染/导出口拦截。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import fixtures as fx


SNS_JS = ROOT / "siwx" / "ui" / "pages" / "sns.js"
COMMON_JS = ROOT / "siwx" / "ui" / "common.js"


def _to_signed64(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v


def _make_sns_db_raw(path: Path, posts):
    """建一个最小的 sns.db，posts = ``[(tid, user_name, xml)]``（原样存 XML）。"""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, "
                "user_name TEXT, content TEXT, pack_info_buf TEXT)")
    for tid, user, xml in posts:
        con.execute("INSERT INTO SnsTimeLine VALUES (?,?,?,?)", (tid, user, xml, ""))
    con.commit()
    con.close()


def _link_xml(title, content_url, ts):
    """一条 link 卡动态：title/description/contentUrl 由联系人可控。"""
    return ("<SnsDataItem><TimelineObject><id>1</id>"
            f"<username>wxid_example_01</username><createTime>{ts}</createTime>"
            f"<contentDesc>正文 {title}</contentDesc>"
            f"<ContentObject><type>3</type><title>{title}</title>"
            f"<description>摘要</description><contentUrl>{content_url}</contentUrl>"
            "<mediaList/></ContentObject></TimelineObject></SnsDataItem>")


class TestSafeLinkUrlWhitelist(unittest.TestCase):
    """后端白名单本身：与 api_chat._safe_link_url 同口径。"""

    def setUp(self):
        from siwx import sns_export as E
        self.f = E._safe_link_url

    def test_dangerous_schemes_rejected(self):
        for bad in ["javascript:alert(1)",
                    "data:text/html;base64,PHNjcmlwdD4=",
                    "vbscript:msgbox(1)",
                    "JaVaScRiPt:alert(1)",          # 大小写混淆
                    "JAVASCRIPT:alert(1)",
                    " javascript:alert(1)",          # 前导空白（strip 后仍判 scheme）
                    "java\nscript:alert(1)"]:
            self.assertIsNone(self.f(bad), bad)

    def test_allowed_schemes_pass_through(self):
        for ok in ["https://mp.weixin.qq.com/s?__biz=abc",
                   "http://example.com/a",
                   "//example.com/page",             # 协议相对（/ 前缀放行）
                   "/path/to/page",
                   "./relative",
                   "../up",
                   "#anchor"]:
            self.assertEqual(self.f(ok), ok, ok)

    def test_empty_rejected(self):
        for empty in ["", None, "   "]:
            self.assertIsNone(self.f(empty))


class TestSnsCardUrlExport(unittest.TestCase):
    """HTML / Markdown 导出：危险 scheme 降级，正常链接保留。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_p2_sns_",
                                                ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "sns" / "sns.db"

    def tearDown(self):
        self._tmp.cleanup()

    # title 前缀即“作者”标识，导出后按 title 断言各动态的行为
    CASES = [
        # (title, contentUrl, markdown 是否应有链接)
        ("占位标题甲", "javascript:alert(1)", False),
        ("占位标题乙", "data:text/html;base64,PHNjcmlwdD4=", False),
        ("占位标题丙", "vbscript:msgbox(1)", False),
        ("占位标题丁", "JaVaScRiPt:alert(1)", False),
        ("占位正常文", "https://mp.weixin.qq.com/s/x", True),
        ("占位相对协议", "//example.com/page", True),
        ("占位路径", "/page/path", True),
        ("占位锚点", "#anchor", True),
    ]

    def _db(self):
        base = (1700000000 * 1000) << 23
        posts = []
        for i, (title, url, _) in enumerate(self.CASES):
            posts.append((_to_signed64(base + i + 1), "wxid_example_01",
                          _link_xml(title, url, 1700000000 + i)))
        _make_sns_db_raw(self.db, posts)

    def _export(self, fmt):
        from siwx import sns_export as E
        self._db()
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt=fmt,
                             export_root=self.tmp / "out")
        self.assertTrue(r["ok"], r.get("error"))
        return Path(r["file"]).read_text(encoding="utf-8")

    def test_html_export_strips_dangerous_schemes(self):
        html = self._export("html")
        low = html.lower()
        for marker in ["javascript:", "vbscript:", "data:text/html", "alert("]:
            self.assertNotIn(marker, low, f"导出 HTML 不得出现 {marker}")
        self.assertNotIn('href="javascript', low)
        # 白名单内：正常 https 链接原样可点
        self.assertIn('href="https://mp.weixin.qq.com/s/x"', html)
        # 降级为纯文本 span，标题文本保留（卡片本身还在）
        for title, _, _ in self.CASES:
            self.assertIn(title, html, f"{title} 的降级文本应保留")
        self.assertIn('class="card card--link"', html)
        self.assertIn('<span class="card-title-text">占位标题甲</span>', html)
        # 协议相对 / 路径 / 锚点仍生成 <a>
        self.assertIn('href="//example.com/page"', html)
        self.assertIn('href="/page/path"', html)
        self.assertIn('href="#anchor"', html)

    def test_markdown_export_strips_dangerous_schemes(self):
        md = self._export("markdown")
        low = md.lower()
        for marker in ["javascript:", "vbscript:", "data:text/html", "alert("]:
            self.assertNotIn(marker, low, f"导出 Markdown 不得出现 {marker}")
        # 白名单内：写成可点链接
        self.assertIn("[占位正常文](https://mp.weixin.qq.com/s/x)", md)
        self.assertIn("[占位相对协议](//example.com/page)", md)
        self.assertIn("[占位路径](/page/path)", md)
        self.assertIn("[占位锚点](#anchor)", md)
        # 白名单外：去掉链接只留标题文本（引用块卡片行还在）
        for title in ["占位标题甲", "占位标题乙", "占位标题丙", "占位标题丁"]:
            self.assertIn(f"🔗 {title}", md, f"{title} 降级后应保留标题文本")

    def test_json_export_keeps_raw_url_data_layer_unchanged(self):
        """数据层（public_card 透传）不动：JSON 导出仍是原始 content_url，
        拦截只发生在渲染/导出口。"""
        import json as _json
        data = _json.loads(self._export("json"))
        posts = {p["card"]["title"]: p for p in data["posts"]}
        self.assertEqual(posts["占位标题甲"]["card"]["url"], "javascript:alert(1)")
        self.assertEqual(posts["占位正常文"]["card"]["url"],
                         "https://mp.weixin.qq.com/s/x")


class TestSnsFrontendSafeUrl(unittest.TestCase):
    """前端静态断言：sns.js 两处 :href 必须过 safeUrl，common.js 导出 safeUrl。"""

    def setUp(self):
        self.sns_src = SNS_JS.read_text(encoding="utf-8")
        self.common_src = COMMON_JS.read_text(encoding="utf-8")

    def test_card_href_goes_through_safe_url(self):
        self.assertEqual(self.sns_src.count(':href="safeUrl(card.url)"'), 2,
                         "music 卡与 link 卡两处 :href 都必须过 safeUrl")
        self.assertNotIn(':href="card.url"', self.sns_src,
                         "card.url 不得绕过白名单直通 :href")

    def test_unsafe_url_degrades_to_no_href(self):
        # safeUrl 返回空串时归一为 null，Vue 会把 href 属性整个去掉
        self.assertIn("safeUrl: u => safeUrl(u) || null", self.sns_src)

    def test_common_exports_safe_url(self):
        self.assertIn("function safeUrl(", self.common_src)
        # 白名单口径：https?: / 相对 / ./ ../ / #，忽略大小写（与后端同一条规则）
        self.assertIn(r"/^(https?:|\/|\.\/|\.\.\/|#)/i", self.common_src)
        self.assertIn("safeUrl };", self.common_src,
                      "window.SX 必须导出 safeUrl 供页面复用")


if __name__ == "__main__":
    unittest.main()
