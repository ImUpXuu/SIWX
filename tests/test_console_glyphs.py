"""控制台装饰字符的可编码性兜底（结果符号与框线不再在差编码控制台上变 `?`）。

背景：`run.py` 启动时把 stdout/stderr 重配成 `errors="replace"`（否则 cp1252
控制台打 `--help` 会直接 UnicodeEncodeError 崩掉）。副作用是编不了的字符在
真实 Windows 控制台上**全部静默显示成 `?`**——`? 密钥 29/30 已验证` 这种
成功行看着像报错，而同行的中文与框线却正常，用户无从判断。关键机制：
`errors="replace"` 的流不会抛 UnicodeEncodeError，所以必须按目标编码主动
降级，不能靠 try/except（`test_plain_rich_print_is_the_bug_this_fixes`
把这个前提钉住）。

两层设计（2026-10-30 换字方案）：
1. 源头换字：siwx 业务代码只输出 GB2312 自带符号（√ × ● ※ ▲），GBK 控制
   台（中文 Windows 主战场）整条降级链不再触发；`TestGlyphSourceHygiene`
   用源码扫描钉住这条纪律，防止脆弱字符（✓ ✔ ✗ ✘ ▸ ✦ ⚠）再被写回源码。
2. 保险丝：`siwx/console_glyphs.py` 仍探测目标流编码，插件残留的旧符号与
   cp1252 的框线照旧降级（`✓` → `[OK]`、`√` → `[OK]`、`※` → `*`、
   框线 → `-`/`|`/`+`），UTF-8 场景一个字节都不动。

`siwx/tui.py` 的 `_print`（str 与 Text 两条路径）/`glyph`/`box_or_ascii` 与
`siwx/logger.py` 的 `_cprint` 都走这道闸；`TestTuiLog` 另外钉住 [tag] 前缀
必须原样显示（Text 构造、不经 rich markup 解析，2026-10-30 缺陷修复）。

本套件全部用合成字符串与临时流，不含真实账号或本机路径。
"""
import contextlib
import io
import re
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich import box  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from siwx import console_glyphs, logger, tui  # noqa: E402

#: 新源头符号 + 旧残留符号 + 框线，覆盖 CLI/横幅/表格实际用到的字符
DECORATIVE = "√ × ● ※ ▲ ✓ ✔ ✗ ✘ ▸ ✦ ⚠ ╭╮╰╯ ─ │"


def _stream(encoding, errors="replace"):
    """模拟真实控制台：run.py 用 errors="replace" 重配 stdout，这里同构。"""
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding=encoding, errors=errors)
    return buf, stream


def _decode(buf, encoding):
    """读回输出：统一换行（TextIOWrapper 在 Windows 上会把 \\n 写成 \\r\\n）。"""
    buf.seek(0)
    return buf.getvalue().decode(encoding, "replace").replace("\r\n", "\n")


class TuiStreamCase(unittest.TestCase):
    """把 tui.console 换接到指定编码的临时流上（非 tty → 无 ANSI，便于断言）。"""

    def setUp(self):
        self._real_console = tui.console

    def tearDown(self):
        tui.console = self._real_console

    def use_encoding(self, encoding, errors="replace", width=100):
        buf, stream = _stream(encoding, errors)
        tui.console = Console(file=stream, width=width, theme=tui.THEME)
        self.buf, self.stream = buf, stream
        return buf, stream

    def read(self, encoding):
        self.stream.flush()
        return _decode(self.buf, encoding)


class TestProbe(unittest.TestCase):
    """纯探测层：按编码决定翻不翻。"""

    def test_utf8_needs_nothing(self):
        self.assertFalse(console_glyphs.needs_translation("utf-8"))
        self.assertTrue(console_glyphs.box_is_safe("utf-8"))
        self.assertEqual(console_glyphs.translate(DECORATIVE, "utf-8"), DECORATIVE)

    def test_missing_encoding_is_conservative(self):
        """探测不到编码（StringIO 等）时不许乱改内容。"""
        self.assertFalse(console_glyphs.needs_translation(None))
        self.assertEqual(console_glyphs.translate("✓ ok", None), "✓ ok")

    def test_unknown_codec_name_degrades(self):
        """LookupError 视同编不了：不许把异常抛到打印路径上。"""
        self.assertTrue(console_glyphs.needs_translation("no-such-codec"))
        self.assertIn("[OK]", console_glyphs.translate("✓", "no-such-codec"))

    def test_probe_is_cached(self):
        console_glyphs._probe_cache.pop("cp936", None)
        self.assertTrue(console_glyphs.needs_translation("cp936"))
        self.assertIn("cp936", console_glyphs._probe_cache)
        # 第二次必须走缓存：故意把探测函数改坏也不该再被调用
        with mock.patch.object(console_glyphs, "_encodable",
                               side_effect=AssertionError("重复探测")):
            console_glyphs.needs_translation("cp936")


class TestTranslate(unittest.TestCase):
    """降级表：新源头符号在 GBK 上原样通过；旧符号与 cp1252 框线照旧降级。"""

    def test_cp936_keeps_native_glyphs(self):
        """√ × ● ※ ▲ · 都是 GB2312 自带：GBK 上原样通过，不触发替换。"""
        native = "√ × ● ※ ▲ ·"
        self.assertEqual(console_glyphs.translate(native, "cp936"), native)
        # 探测集仍含旧符号 → needs_translation 保持 True；降级按字符逐个判
        self.assertTrue(console_glyphs.needs_translation("cp936"))

    def test_cp1252_degrades_native_glyphs(self):
        """cp1252 放不下 √ ● ※ ▲（× 除外）：按表换成 ASCII 记号。"""
        out = console_glyphs.translate("√ ● ※ ▲ ×", "cp1252")
        self.assertIn("[OK]", out)
        self.assertEqual(out.count("*"), 2)      # ● ※ → *
        self.assertIn("!", out)                  # ▲ → !
        self.assertIn("×", out)                  # cp1252 自带，保留

    def test_cp936_keeps_box_lines(self):
        """旧符号（插件残留）在 GBK 上仍走降级表；框线保持原样。"""
        out = console_glyphs.translate("▸ 密钥 29/30 已验证 ✓ ─ │", "cp936")
        self.assertIn("[OK]", out)
        self.assertTrue(out.startswith(">"))
        self.assertIn("─", out)                  # GBK 码表里有框线，保持原样
        self.assertIn("│", out)
        self.assertNotIn("✓", out)
        self.assertTrue(console_glyphs.box_is_safe("cp936"))

    def test_cp1252_translates_box_lines(self):
        out = console_glyphs.translate("╭──╮ │ ✗ ▸ ✦", "cp1252")
        self.assertIn("[X]", out)
        self.assertEqual(out.count("+"), 2)      # ╭ ╮ → +
        self.assertIn("-", out)
        self.assertIn("|", out)
        self.assertEqual(out.count("*"), 1)      # ✦ → *
        self.assertFalse(console_glyphs.box_is_safe("cp1252"))
        for ch in "╭─│✗▸✦":
            self.assertNotIn(ch, out)

    def test_gbk_alias_equals_cp936(self):
        self.assertEqual(console_glyphs.translate("✓ ✗", "gbk"),
                         console_glyphs.translate("✓ ✗", "cp936"))

    def test_box_replacements_keep_column_width(self):
        """框线必须换成等宽 ASCII，否则横幅/表格错位。"""
        for src, dst in console_glyphs._REPLACEMENTS:
            if src in console_glyphs._BOX_GLYPHS:
                self.assertEqual(len(dst), 1, "%s→%s 不等宽" % (src, dst))

    def test_safe_text_leaves_nothing_unencodable(self):
        """降级表之外的字符（中文在 cp1252）最后交给 replace：只保证不抛。"""
        out = console_glyphs.safe_text("√ 微信密钥提取", "cp1252")
        out.encode("cp1252")                     # 不抛即达标
        self.assertIn("[OK]", out)


class TestPrintLine(unittest.TestCase):
    """logger._cprint 用的落流打印。"""

    def test_writes_translated_line(self):
        buf, stream = _stream("cp936")
        console_glyphs.print_line("✓ 备份完成 ─", stream)
        text = _decode(buf, "cp936")
        self.assertEqual(text, "[OK] 备份完成 ─\n")

    def test_writes_native_glyph_untouched(self):
        """新源头符号在 GBK 流上原样落流，不做无谓替换。"""
        buf, stream = _stream("cp936")
        console_glyphs.print_line("√ 备份完成 ─", stream)
        text = _decode(buf, "cp936")
        self.assertEqual(text, "√ 备份完成 ─\n")

    def test_utf8_stream_keeps_glyphs(self):
        buf, stream = _stream("utf-8")
        console_glyphs.print_line("√ 备份完成", stream)
        self.assertEqual(_decode(buf, "utf-8"), "√ 备份完成\n")

    def test_dead_stream_is_silent(self):
        """流已关闭：控制台只是附加通道，失败必须静默（沿用 _cprint 语义）。"""
        _, stream = _stream("cp936")
        stream.close()
        console_glyphs.print_line("√ 行", stream)   # 不抛即达标


class TestLoggerConsole(TuiStreamCase):
    """结构化日志的控制台通道（模拟插件转发来的旧符号文案）。"""

    def test_warn_line_degrades_to_ascii(self):
        buf, stream = _stream("cp936")
        with mock.patch.object(logger, "_CONSOLE", stream):
            logger.warn("demo", "✓ 已验证 29/30")
        text = _decode(buf, "cp936")
        self.assertIn("[OK] 已验证", text)
        self.assertNotIn("✓", text)
        self.assertNotIn("?", text)

    def test_warn_line_keeps_glyphs_on_utf8_stdout(self):
        buf, stream = _stream("utf-8")
        # _CONSOLE 为 None 时走内置 print，跟随 sys.stdout
        with mock.patch.object(logger, "_CONSOLE", None), \
                contextlib.redirect_stdout(stream):
            logger.info("demo", "√ 已验证")
        stream.flush()
        self.assertIn("√ 已验证", _decode(buf, "utf-8"))


class TestTuiLog(TuiStreamCase):
    """tui.log：结论符号是这一行的全部信息量，不能丢；[tag] 前缀必须原样显示。"""

    def test_plain_rich_print_is_the_bug_this_fixes(self):
        """对照实验：同一条串不经 _print 直接写 rich，确实会变成 `?`。"""
        self.use_encoding("cp936")
        tui.console.print("[ok]✓ 已验证[/]")
        self.assertIn("? 已验证", self.read("cp936"))

    def test_success_line_passes_through_on_gbk(self):
        """√ 在 GBK 码表里：中文 Windows 主战场不再触发降级。"""
        self.use_encoding("cp936")
        tui.log("√ 密钥 29/30 已验证")
        text = self.read("cp936")
        self.assertIn("√ 密钥 29/30 已验证", text)
        self.assertNotIn("[OK]", text)
        self.assertNotIn("?", text)

    def test_failure_line_passes_through_on_gbk(self):
        self.use_encoding("cp936")
        tui.log("× 未能提取到任何密钥")
        text = self.read("cp936")
        self.assertIn("× 未能提取到任何密钥", text)
        self.assertNotIn("?", text)

    def test_legacy_glyphs_still_degrade(self):
        """保险丝职责：插件/历史文案里的旧符号（✓ ✗）仍按表降级。"""
        self.use_encoding("cp936")
        tui.log("✓ 提取完成，✗ 有 1 个库跳过")
        text = self.read("cp936")
        self.assertIn("[OK] 提取完成，[X] 有 1 个库跳过", text)
        self.assertNotIn("?", text)

    def test_success_line_degrades_on_cp1252(self):
        """cp1252 放不下 √：换成 [OK]（中文本体在 cp1252 仍是 ?，编码层无解）。"""
        self.use_encoding("cp1252")
        tui.log("√ 密钥 29/30 已验证")
        text = self.read("cp1252")
        self.assertIn("[OK]", text)
        self.assertNotIn("√", text)

    def test_tagged_line_keeps_tag_and_body(self):
        """[tag] 前缀必须原样显示（2026-10-30 缺陷修复）：log() 用 Text 构造，
        `[extract]` 不再被 rich 当样式标签解析后吞掉，也不再多出双空格。"""
        self.use_encoding("cp936")
        tui.log("[extract] √ 提取完成 19/19")
        text = self.read("cp936")
        self.assertIn("[extract]", text)         # 修复点：标签保留
        self.assertIn("· [extract] √ 提取完成 19/19", text)
        self.assertNotIn("[/]", text)            # 样式闭合标记不外泄
        self.assertNotIn("?", text)
        self.assertNotIn("  ", text)             # 不再有双空格

    def test_brackets_in_body_stay_literal(self):
        """正文里的方括号（进度、任务 id 等动态内容）按字面显示，不被 rich 解析。"""
        self.use_encoding("utf-8")
        tui.log("[memscan] [ 42%] 扫描中 [t1] 完成")
        self.assertIn("[ 42%] 扫描中 [t1] 完成", self.read("utf-8"))

    def test_summary_line_on_gbk(self):
        self.use_encoding("cp936")
        tui.summary_line(29, 30, 42)
        text = self.read("cp936")
        self.assertIn("● 密钥 29/30 已验证", text)
        self.assertNotIn("?", text)

    def test_decrypt_summary_on_gbk(self):
        self.use_encoding("cp936")
        tui.decrypt_summary(ok=14, failed=0, skipped=1, cached=10, ms=219,
                             out=r"D:\out\user_a")
        text = self.read("cp936")
        self.assertIn("● 解密完成", text)
        self.assertNotIn("?", text)

    def test_utf8_console_is_untouched(self):
        self.use_encoding("utf-8")
        tui.log("√ 密钥 29/30 已验证")
        text = self.read("utf-8")
        self.assertIn("√ 密钥 29/30 已验证", text)
        self.assertNotIn("[OK]", text)


class TestTuiBanner(TuiStreamCase):
    """横幅：框线与 ※ 在 GBK 上保留，在 cp1252 上换成等宽 ASCII。"""

    def test_gbk_keeps_box_and_star(self):
        self.use_encoding("cp936")
        tui.banner()
        text = self.read("cp936")
        self.assertIn("╭", text)
        self.assertIn("stories-in-wx", text)
        self.assertIn("※", text)                 # ✦ → ※（GB2312 自带，GBK 可编码）
        self.assertNotIn("✦", text)
        self.assertNotIn("?", text)

    def test_cp1252_falls_back_to_ascii_box(self):
        self.use_encoding("cp1252")
        tui.banner()
        text = self.read("cp1252")
        self.assertIn("stories-in-wx", text)
        self.assertIn("+", text)
        self.assertNotIn("╭", text)
        self.assertNotIn("─", text)
        self.assertNotIn("※", text)              # ※ → *

    def test_banner_row_lengths_survive_translation(self):
        """字符数在降级后必须不变（中文的显示宽度另说，这里只钉字符数）。"""
        self.use_encoding("cp1252")
        tui.banner()
        box_lines = [ln for ln in self.read("cp1252").splitlines()
                     if ln.startswith("+")]
        self.assertEqual(len(box_lines), 2)
        self.assertEqual(len({len(ln) for ln in box_lines}), 1)


class TestTuiTables(TuiStreamCase):
    """表格：单元格里的 √ × 与边框选型。"""

    def _report(self):
        return {"salts": [
            {"verified": True, "salt": "a1" * 8, "dbs": ["x.db"],
             "strategy": "memscan", "key_masked": "ab**"},
            {"verified": False, "salt": "b2" * 8, "dbs": ["y.db"],
             "strategy": None, "key_masked": None}]}

    def test_salt_table_marks_on_gbk(self):
        self.use_encoding("cp936")
        tui.salt_table(self._report())
        text = self.read("cp936")
        self.assertIn("√", text)
        self.assertIn("×", text)
        self.assertNotIn("✓", text)
        self.assertNotIn("?", text)

    def test_salt_table_keeps_glyphs_on_utf8(self):
        self.use_encoding("utf-8")
        tui.salt_table(self._report())
        text = self.read("utf-8")
        self.assertIn("√", text)
        self.assertNotIn("[OK]", text)

    def test_salt_table_marks_on_cp1252(self):
        """cp1252：√ → [OK]；× 本来就在码表里，直接保留。"""
        self.use_encoding("cp1252")
        tui.salt_table(self._report())
        text = self.read("cp1252")
        self.assertIn("[OK]", text)
        self.assertIn("×", text)
        self.assertNotIn("[X]", text)

    def test_account_table_and_step_survive_cp1252(self):
        """cp1252 上中文本身也编不了：不许抛异常，允许 replace 残留。"""
        self.use_encoding("cp1252")
        tui.account_table([("user_a", r"D:\data\user_a", 30)])
        tui.step("账号 user_a")
        self.assertIn("user_a", self.read("cp1252"))


class TestBoxSelection(TuiStreamCase):
    """边框选型：只有编不了框线的控制台才换 ASCII。"""

    def test_gbk_keeps_unicode_box(self):
        self.use_encoding("cp936")
        self.assertIs(tui.box_or_ascii(box.SIMPLE_HEAVY), box.SIMPLE_HEAVY)

    def test_utf8_keeps_unicode_box(self):
        self.use_encoding("utf-8")
        self.assertIs(tui.box_or_ascii(box.SQUARE), box.SQUARE)

    def test_cp1252_switches_to_ascii_box(self):
        self.use_encoding("cp1252")
        self.assertIs(tui.box_or_ascii(box.SIMPLE), box.ASCII)

    def test_glyph_is_identity_on_utf8(self):
        self.use_encoding("utf-8")
        self.assertEqual(tui.glyph("[ok]√[/] ─"), "[ok]√[/] ─")


class TestStrictStreamFallback(TuiStreamCase):
    """严格流（未经 run.py 重配）下靠 except 兜底：不许抛、不许整行丢失。"""

    def test_uncovered_char_does_not_crash(self):
        self.use_encoding("cp936", errors="strict")
        table = Table(box=box.SIMPLE)
        table.add_column("状态")
        table.add_row("★ 探测集之外的字符")        # ★ 不在降级表里
        tui.print_line(table)                      # 不抛即达标
        self.stream.flush()
        self.assertTrue(self.buf.getvalue())       # 确有内容落盘，没整行丢掉


class FragileGlyphWarning(UserWarning):
    """siwx 源码出现脆弱装饰字符字面量（提示级，不阻塞测试/CI）。"""


class TestGlyphSourceHygiene(unittest.TestCase):
    """源头卫生（2026-10-30 换字方案）：

    siwx 业务代码输出 GB2312 自带符号（√ × ● ※ ▲ ·）。脆弱字符（不在
    GBK/cp1252 码表、会被 `errors="replace"` 流静默变 `?`）不鼓励以字面量
    写回源码——但降级保险丝兜得住它们，所以这是**卫生提示而非硬性拦截**：
    违例以 warning 形式出现在 pytest 汇总里，测试保持绿色。
    """

    FRAGILE = "✓✔✗✘▸✦⚠"
    NATIVE = "√×●※▲·"

    def test_sources_emit_only_gbk_safe_glyphs(self):
        offenders = []
        for p in sorted((ROOT / "siwx").glob("*.py")):
            if p.name == "console_glyphs.py":   # 降级表本身要引用旧符号
                continue
            text = p.read_text(encoding="utf-8")
            for ch in self.FRAGILE:
                if ch in text:
                    offenders.append("%s: %r" % (p.name, ch))
        if offenders:
            warnings.warn(
                "siwx 源码发现脆弱装饰字符字面量（提示级：保险丝可兜底，建议"
                "换成 GB2312 自带符号 √ × ● ※ ▲）: " + "; ".join(offenders),
                FragileGlyphWarning, stacklevel=2)

    def test_native_glyphs_are_gbk_encodable(self):
        """设计前提（硬性）：源头符号集必须在 GBK 码表里——这是"主战场免降级"
        的地基，破坏它等于换字方案整体失效，不属于提示级问题。"""
        for ch in self.NATIVE:
            ch.encode("gbk")                    # 编不了会抛 UnicodeEncodeError


class TestRunPreflight(unittest.TestCase):
    """`run.py` 的缺依赖预检：把 ModuleNotFoundError 换成可执行指引。"""

    def _load_run(self):
        """以模块级命名空间执行 run.py（`__name__` 非 main，不触发 CLI）。"""
        ns = {"__name__": "run_under_test", "__file__": str(ROOT / "run.py")}
        exec(compile((ROOT / "run.py").read_text(encoding="utf-8"),
                     "run.py", "exec"), ns)
        return ns

    def test_missing_deps_point_at_the_interpreter(self):
        ns = self._load_run()
        err = io.StringIO()
        saved = dict(ns["_REQUIRED_DEPS"])
        ns["_REQUIRED_DEPS"] = {"psutil": "psutil", "Crypto": "pycryptodome"}
        try:
            with mock.patch("importlib.util.find_spec", return_value=None):
                self.assertEqual(ns["_missing_deps"](), ["psutil", "Crypto"])
                with contextlib.redirect_stderr(err):
                    code = ns["_deps_failure"](["psutil", "Crypto"])
        finally:
            ns["_REQUIRED_DEPS"] = saved
        self.assertEqual(code, 2)
        text = err.getvalue()
        self.assertIn(sys.executable, text)        # 多解释器机器必须点名当前用的哪个
        self.assertIn("requirements.txt", text)
        self.assertIn("pycryptodome", text)

    def test_complete_env_reports_nothing_missing(self):
        ns = self._load_run()
        self.assertEqual(ns["_missing_deps"](), [])

    def test_required_deps_match_requirements_txt(self):
        """预检表必须跟 requirements.txt 一致：新增依赖忘了登记，报错又会退回
        看不懂的 ModuleNotFoundError（与 test_version_sync.py 同一类钉定）。"""
        ns = self._load_run()
        declared = {
            re.match(r"([A-Za-z0-9_-]+)", ln).group(1).lower()
            for ln in (ROOT / "requirements.txt").read_text(
                encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        }
        self.assertEqual({p.lower() for p in ns["_REQUIRED_DEPS"].values()}, declared)


if __name__ == "__main__":
    unittest.main()
