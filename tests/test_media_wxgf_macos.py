"""media_wxgf_macos 回归测试。

用两类手段覆盖，呼应 test_macos_lldb.py 的测试哲学（本模块同样是
"生成一段 LLDB 内嵌脚本、spawn lldb 子进程执行"的模式，没有真实微信
进程时没法做端到端测试，但关键逻辑可以拆出来直接测）：

  1. 纯函数单测：ARM64 指令解码（ADRP/LDR/BL）是本模块能找到正确跳转
     目标的核心，用真实微信版本里观测到的指令编码做回归用例；
  2. 静态检查：渲染出的内嵌脚本可编译（语法回归网），且
     inspect.getsource() 注入的解码函数与脚本里实际跑的是同一份源码
     （防止手改脚本模板时和独立函数定义出现漂移）；frozen（打包）下
     回退常量与实时源码归一化一致（P1-2），脚本各退出路径都 free 掉
     目标进程里的四个 malloc 缓冲区（P1-3）；
  3. 优雅降级：找不到微信进程时直接返回 None / 空结果，不抛异常、不
     真的去 spawn lldb 子进程；转码模块导入/调用抛异常时，单图
     _finalize 降级为 None、备份计入"原始 wxgf 已保留"，均不向外抛。
"""
import inspect
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import media, media_backup
from siwx.media_wxgf_macos import (
    _adrp_target, _ldr_imm_offset, _bl_target, _DECODER_FALLBACK,
    _DECODER_SOURCE, _SCRIPT_TMPL, _decoder_source, _run_lldb_batch,
    convert_wxgf_macos, convert_wxgf_for_web,
)

_DECODER_NAMES = ("_adrp_target", "_ldr_imm_offset", "_bl_target")


def _normalize_source(src: str) -> list:
    """源码归一化：统一缩进、丢空行与行尾空白，供 fallback 与实时源码比对。"""
    return [ln.rstrip() for ln in textwrap.dedent(src).splitlines() if ln.strip()]


class TestArm64InstructionDecoders(unittest.TestCase):
    """三个解码函数的回归用例取自本项目实测的真实微信版本（Apple Silicon，
    WeChat 4.1.13）——数值不是编出来的，是本机真实观测到的
    adrp/ldr/bl 指令对应的真实地址关系，用于防止未来重构时手滑改错
    位域提取逻辑。"""

    def test_adrp_decodes_real_observed_table_slot(self):
        # 真实观测：wxam_dec_wxam2pic_5 桩函数的 adrp 指令，从桩地址所在页
        # 算到跳转表所在页（表地址 = 模块基址+0x4390，页对齐后是 +0x4000 页）
        stub_pc = 0x10302fbb8
        expected_page = 0x103030000
        imm = (expected_page - (stub_pc & ~0xfff)) >> 12
        immlo = imm & 0b11
        immhi = (imm >> 2) & 0x7ffff
        insn = (1 << 31) | (immlo << 29) | (0b10000 << 24) | (immhi << 5) | 9
        self.assertEqual(_adrp_target(insn, stub_pc), expected_page)

    def test_adrp_rejects_non_adrp_opcode(self):
        # op 位（bit31）清零 -> 不是 ADRP（是 ADR），必须返回 None 而不是
        # 误算出一个假地址
        insn = 0b0_00_10000_0000000000000000000_01001  # op=0
        self.assertIsNone(_adrp_target(insn, 0x1000))

    def test_ldr_decodes_real_observed_offset(self):
        # 真实观测：ldr x9, [x9, #0x390]（导出符号 wxam_dec_wxam2pic_5 的
        # 跳转表项偏移）
        imm12 = 0x390 // 8
        insn = (0b11 << 30) | (0b111 << 27) | (0b01 << 24) | (0b01 << 22) | (imm12 << 10) | (9 << 5) | 9
        self.assertEqual(_ldr_imm_offset(insn), 0x390)

    def test_ldr_rejects_32bit_variant(self):
        # size 位不是 11（64 位）时不应该误当成我们要的变体处理
        imm12 = 0x390 // 8
        insn = (0b10 << 30) | (0b111 << 27) | (0b01 << 24) | (0b01 << 22) | (imm12 << 10) | (9 << 5) | 9
        self.assertIsNone(_ldr_imm_offset(insn))

    def test_bl_decodes_real_observed_call(self):
        # 真实观测：wxam2pic 外层 wrapper(0x12b5545b4) 里 bl 到内层真正实现
        # (0x12b5540ec) 的那条指令，pc 在 wrapper 入口 +0x28 处
        bl_pc = 0x12b5545dc
        bl_target = 0x12b5540ec
        offset_words = (bl_target - bl_pc) >> 2
        insn = (0b100101 << 26) | (offset_words & 0x3ffffff)
        self.assertEqual(_bl_target(insn, bl_pc), bl_target)

    def test_bl_handles_negative_offset(self):
        # 跳转目标在 pc 之前（负偏移）时符号位扩展必须正确，否则会算出一个
        # 离谱的大地址而不是报错或算对
        bl_pc = 0x12b554000
        bl_target = 0x12b553000  # 在 pc 之前 0x1000
        offset_words = (bl_target - bl_pc) >> 2  # 负数
        insn = (0b100101 << 26) | (offset_words & 0x3ffffff)
        self.assertEqual(_bl_target(insn, bl_pc), bl_target)

    def test_bl_rejects_non_bl_opcode(self):
        insn = 0b010101 << 26  # 顶 6 位不是 100101
        self.assertIsNone(_bl_target(insn, 0x1000))


class TestScriptTemplate(unittest.TestCase):
    """渲染出的 LLDB 内嵌脚本必须是合法 Python（语法回归网，呼应
    test_macos_lldb.py 对 _build_lldb_script 的同类检查）；解码函数通过
    inspect.getsource() 原样注入，这里顺带校验注入确实发生（不是空字符串
    或被截断）。"""

    def test_rendered_script_compiles(self):
        rendered = _SCRIPT_TMPL.format(
            pid=1234, manifest_path="/tmp/fake_manifest.json",
            decoder_funcs=_DECODER_SOURCE)
        compile(rendered, "<media_wxgf_macos_script>", "exec")

    def test_decoder_source_injected_into_script(self):
        rendered = _SCRIPT_TMPL.format(
            pid=1234, manifest_path="/tmp/fake_manifest.json",
            decoder_funcs=_DECODER_SOURCE)
        for name in ("_adrp_target", "_ldr_imm_offset", "_bl_target"):
            self.assertIn(f"def {name}", rendered)

    def test_script_references_real_symbol_name(self):
        """没有硬编码任何具体版本观测到的绝对地址，只能靠导出符号名动态找——
        这是本模块与"写死一个地址"的方案的核心区别，用字符串存在性当回归
        网防止未来改动悄悄退化成硬编码。"""
        rendered = _SCRIPT_TMPL.format(
            pid=1234, manifest_path="/tmp/fake_manifest.json",
            decoder_funcs=_DECODER_SOURCE)
        self.assertIn("wxam_dec_wxam2pic_5", rendered)
        self.assertNotIn("0x12b5", rendered)  # 不应出现任何硬编码的实测绝对地址


class TestDecoderSourceFallback(unittest.TestCase):
    """P1-2：frozen（PyInstaller 打包）后没有 .py 源文件，inspect.getsource
    抛 OSError；旧实现在模块导入时就执行它，导致 macOS .app 里整个模块
    导入失败。修复后必须回退到模块内等价源码常量，且两份不能漂移。"""

    def test_falls_back_when_getsource_unavailable(self):
        with mock.patch("inspect.getsource", side_effect=OSError("no .py source")):
            src = _decoder_source()
        for name in _DECODER_NAMES:
            self.assertIn(f"def {name}", src)
        # 回退源码必须完整可执行，而不是被截断/手滑写坏的半段字符串
        ns = {}
        exec(compile(src, "<decoder_fallback>", "exec"), ns)
        for name in _DECODER_NAMES:
            self.assertTrue(callable(ns[name]), name)
        # 注入脚本模板后整体仍须可编译（frozen 下走的就是这条渲染路径）
        rendered = _SCRIPT_TMPL.format(
            pid=1234, manifest_path="/tmp/fake_manifest.json",
            decoder_funcs=src)
        compile(rendered, "<media_wxgf_macos_script_fallback>", "exec")

    def test_fallback_matches_live_source(self):
        """源码可用时，回退常量必须与 getsource 结果等价（归一化比对），
        防止将来改了解码函数却忘了同步 fallback。"""
        try:
            live = "\n".join(inspect.getsource(fn) for fn in (
                _adrp_target, _ldr_imm_offset, _bl_target))
        except (OSError, TypeError):
            self.skipTest("当前环境取不到源码（frozen），无实时源码可比对")
        self.assertEqual(_normalize_source(_DECODER_FALLBACK),
                         _normalize_source(live))


class TestCallerIsolation(unittest.TestCase):
    """P1-2：转码模块异常不得把单图请求（_finalize）或备份转码打断。"""

    def test_finalize_returns_none_when_macos_module_raises(self):
        logs = []

        class _BoomModule:
            @staticmethod
            def convert_wxgf_for_web(_body, log=None):
                raise OSError("no .py source")

        with mock.patch("siwx.media.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media.convert_wxgf", return_value=None), \
             mock.patch("siwx.media.event", logs.append), \
             mock.patch("siwx.media_wxgf_macos", _BoomModule(), create=True):
            result = media._finalize(b"wxgf-plain-bytes", "wxgf", "image/wxgf")
        self.assertIsNone(result)
        self.assertTrue(any("异常" in m for m in logs), logs)

    def test_finalize_returns_none_when_macos_import_fails(self):
        """精确复现 frozen 事故：转码模块导入即抛异常，也必须降级为 None。"""
        import siwx
        real = siwx.media_wxgf_macos
        del siwx.media_wxgf_macos  # 让 from siwx import ... 真的走一次导入
        try:
            with mock.patch.dict(sys.modules, {"siwx.media_wxgf_macos": None}), \
                 mock.patch("siwx.media.platform.system", return_value="Darwin"), \
                 mock.patch("siwx.media.convert_wxgf", return_value=None):
                result = media._finalize(b"wxgf-plain-bytes", "wxgf", "image/wxgf")
        finally:
            siwx.media_wxgf_macos = real
        self.assertIsNone(result)

    def test_backup_darwin_branch_counts_preserved_on_exception(self):
        pending = [(Path("wxid_test_img.wxgf"), Path("wxid_test_img.heic"))]
        stats = {"ok_viewable": 0, "ok_wxgf_preserved": 0, "bytes_written": 0}
        logs = []

        class _BoomModule:
            @staticmethod
            def convert_wxgf_batch(_pending, log=None):
                raise OSError("no .py source")

        with mock.patch("siwx.media_backup.platform.system", return_value="Darwin"), \
             mock.patch("siwx.media_wxgf_macos", _BoomModule(), create=True):
            media_backup._convert_pending_wxgf(pending, stats, logs.append)
        self.assertEqual(stats["ok_wxgf_preserved"], 1)
        self.assertEqual(stats["ok_viewable"], 0)
        self.assertTrue(any("异常" in m for m in logs), logs)


class TestLldbScriptFreesBuffers(unittest.TestCase):
    """P1-3：脚本在目标微信进程里 malloc 的四个缓冲区必须 free。

    本机没有 macOS + 运行中的微信 + 关闭的 SIP，跑不了真实 LLDB 会话，
    按修订清单用静态断言钉住三条退出路径约束：
      - 四个缓冲区各有一条 free 调用；
      - 正常结束（含中途异常）的 free 在 finally 里、且位于 Detach 之前
        （Detach 之后表达式求值不可用，free 就落空了）；
      - 分配失败分支也要 free 已成功分配的缓冲区。
    """

    BUFFERS = ("in_ptr", "out_ptr", "outlen_ptr", "cfg_ptr")

    def _rendered(self):
        return _SCRIPT_TMPL.format(
            pid=1234, manifest_path="/tmp/fake_manifest.json",
            decoder_funcs=_DECODER_SOURCE)

    def test_all_four_buffers_freed_in_finally_before_detach(self):
        src = self._rendered()
        tail = src[src.rindex("finally:"):]
        self.assertEqual(tail.count("process.Detach()"), 1,
                         "正常路径只应有一个 Detach（已移进 finally）")
        detach_at = tail.index("process.Detach()")
        for name in self.BUFFERS:
            call = f"free_ptr({name})"
            self.assertIn(call, tail, f"{name} 必须在 finally 中 free")
            self.assertLess(tail.index(call), detach_at,
                            f"{name} 的 free 必须在 Detach 之前")

    def test_alloc_failure_branch_frees_partial_allocations(self):
        src = self._rendered()
        start = src.index("if not (in_ptr and out_ptr and outlen_ptr and cfg_ptr):")
        end = src.index('print("FAIL:AllocFailed")', start)
        branch = src[start:end]
        for name in self.BUFFERS:
            self.assertIn(f"free_ptr({name})", branch,
                          f"AllocFailed 分支必须 free {name}")

    def test_free_helper_skips_null_pointer(self):
        """空指针跳过 = 分配失败分支不会 double free。"""
        src = self._rendered()
        start = src.index("def free_ptr(ptr):")
        end = src.index("def call9(", start)
        self.assertIn("if not ptr:", src[start:end])


class TestGracefulDegradation(unittest.TestCase):
    """微信未运行 / 找不到进程时必须优雅返回，不抛异常、不真的 spawn lldb。"""

    def test_empty_batch_returns_empty_without_subprocess(self):
        with mock.patch("subprocess.run") as m:
            result = _run_lldb_batch(1234, [], log=lambda *_a, **_k: None)
        self.assertEqual(result, {})
        m.assert_not_called()

    def test_convert_single_returns_none_when_no_wechat_process(self):
        with mock.patch("siwx.media_wxgf_macos.find_wechat_pid", return_value=None), \
             mock.patch("subprocess.run") as m:
            result = convert_wxgf_macos(b"fake-wxgf-bytes", log=lambda *_a, **_k: None)
        self.assertIsNone(result)
        m.assert_not_called()

    def test_convert_for_web_returns_none_when_no_wechat_process(self):
        with mock.patch("siwx.media_wxgf_macos.find_wechat_pid", return_value=None), \
             mock.patch("subprocess.run") as m:
            result = convert_wxgf_for_web(b"fake-wxgf-bytes", log=lambda *_a, **_k: None)
        self.assertIsNone(result)
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
