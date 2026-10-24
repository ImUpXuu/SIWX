"""linux_hook 策略回归测试（Linux 进程内 INT3 钩子）。

历史 PR #35 只有文档和手工实测，没有自动化闸门。这里补上三类覆盖，
全部可在任意平台跑（需要 Linux 的用例显式 skip）：

  1. 注册与闸门：Linux 上 `linux_hook` 必须进 STRATEGY_REGISTRY 且计入
     `_PROCESS_DEPENDENT`（否则 use_memory=False 时会白等进程）；非 Linux 上
     不得注册（import 都不能炸）。
  2. 日志候选解析：shim 只写 `tag a=.. b=.. data=<64 hex>` 行，解析器必须
     抓住三种布局的 32 字节候选，并忽略短数据/杂行。
  3. 派生与校验：用真实 SQLCipher page-1 HMAC 构造夹具，正确的 32 字节
     passphrase 必须派生出全部密钥并标 attrib=linux_hook；错误 passphrase
     必须 0 命中且不污染 key_map（"校验不通过就不入库"）。
  4. ELF 定位：非 ELF / 无锚点二进制必须抛 HookNotFoundError 而不是崩；
     本机装了微信时，真实二进制必须能算出偏移（构建相关，不写死数值）。
  5. shim 构建：有系统 cc/gcc 时现场编译 wxhook.c 必须成功——这是运行期
     真正依赖的一步（不新增 Python 依赖，靠系统编译器）。

运行：python -m unittest discover -s tests -v
"""
import hashlib
import hmac
import platform
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx.sqlcipher import (HMAC_SZ, IV_SZ, PAGE_SZ, RESERVE_SZ, SALT_SZ,
                            _mac_key, verify_enc_key)

_PASSPHRASE = bytes(range(32))
_SALT_A = bytes.fromhex("00112233445566778899aabbccddeeff")
_SALT_B = bytes.fromhex("ffeeddccbbaa99887766554433221100")


def _derive(passphrase: bytes, salt: bytes, iterations: int | None = None) -> bytes:
    from siwx.strategies.linux_hook import PBKDF2_DIGEST, PBKDF2_DKLEN, PBKDF2_ITERATIONS
    return hashlib.pbkdf2_hmac(PBKDF2_DIGEST, passphrase, salt,
                               PBKDF2_ITERATIONS if iterations is None else iterations,
                               dklen=PBKDF2_DKLEN)


def _make_page1(key: bytes, salt: bytes, filler: bytes) -> bytes:
    """按 verify_enc_key 的口径造一页合法 page-1（salt + 数据 + HMAC）。"""
    page = bytearray(PAGE_SZ)
    page[0:SALT_SZ] = salt
    body_lo, body_hi = SALT_SZ, PAGE_SZ - RESERVE_SZ + IV_SZ
    page[body_lo:body_hi] = filler[:body_hi - body_lo].ljust(body_hi - body_lo, b"\x00")
    hm = hmac.new(_mac_key(key, salt), bytes(page[body_lo:body_hi]), hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    page[PAGE_SZ - HMAC_SZ:] = hm.digest()
    return bytes(page)


def _fixture_page1s(iterations: int | None = None) -> dict:
    """两张库的 page1，密钥都从同一个 passphrase 派生（与微信实际一致）。"""
    filler = hashlib.sha512(b"page-body").digest() * 80
    return {
        _SALT_A.hex(): _make_page1(_derive(_PASSPHRASE, _SALT_A, iterations), _SALT_A, filler),
        _SALT_B.hex(): _make_page1(_derive(_PASSPHRASE, _SALT_B, iterations), _SALT_B, filler),
    }


class RegistrationTest(unittest.TestCase):
    """策略注册与平台闸门。"""

    def test_non_linux_does_not_register(self):
        from siwx.strategies import STRATEGY_REGISTRY, _PROCESS_DEPENDENT
        if platform.system() == "Linux":
            self.skipTest("本机就是 Linux")
        mods = {m.__name__.rsplit(".", 1)[-1] for m in STRATEGY_REGISTRY}
        self.assertNotIn("linux_hook", mods)
        self.assertFalse(any(getattr(m, "__name__", "").endswith("linux_hook")
                             for m in _PROCESS_DEPENDENT))

    @unittest.skipUnless(platform.system() == "Linux", "仅 Linux 注册 linux_hook")
    def test_linux_registers_and_is_process_dependent(self):
        from siwx.strategies import STRATEGY_REGISTRY, _PROCESS_DEPENDENT
        mods = {m.__name__.rsplit(".", 1)[-1]: m for m in STRATEGY_REGISTRY}
        self.assertIn("linux_hook", mods)
        # 依赖微信进程内存/进程内钩子的策略必须登记，否则 use_memory=False 时不会跳过
        self.assertIn(mods["linux_hook"], _PROCESS_DEPENDENT)

    def test_linux_hook_importable_everywhere(self):
        """模块级 import 不许依赖 Linux 专有东西（否则 `import siwx` 在 macOS 崩）。"""
        import importlib
        mod = importlib.import_module("siwx.strategies.linux_hook")
        self.assertTrue(callable(mod.extract))

    def test_extract_noop_on_non_linux(self):
        from siwx.strategies import linux_hook
        logs = []
        ctx = {"page1_by_salt": {_SALT_A.hex(): b"x" * PAGE_SZ},
               "key_map": {}, "attrib": {}, "log": logs.append, "dbg": logs.append}
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(linux_hook.extract(ctx), 0)
        self.assertEqual(ctx["key_map"], {})

    def test_extract_skips_non_x86_64(self):
        from siwx.strategies import linux_hook
        logs = []
        ctx = {"page1_by_salt": {_SALT_A.hex(): b"x" * PAGE_SZ},
               "key_map": {}, "attrib": {}, "log": logs.append, "dbg": logs.append}
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(platform, "machine", return_value="aarch64"):
            self.assertEqual(linux_hook.extract(ctx), 0)
        self.assertTrue(any("仅支持 x86-64" in m for m in logs))


class CandidateParseTest(unittest.TestCase):
    """shim 日志 → 候选 passphrase。"""

    def test_parses_three_layouts(self):
        from siwx.strategies.linux_hook import _CANDIDATE_RE
        a = "11" * 32
        b = "22" * 32
        c = "33" * 32
        text = (
            f"hit-raw a=7ffd1234 b=20 data={a}\n"
            f"hit-flat0 a=7ffd5678 b=20 data={b}\n"
            f"hit-flat8 a=7ffd9abc b=20 data={c}\n"
            "hit-unreadable\n"
            "disarmed-one-shot\n"
        )
        got = [m.group(3) for m in _CANDIDATE_RE.finditer(text)]
        self.assertEqual(got, [a, b, c])

    def test_ignores_wrong_length_and_junk(self):
        from siwx.strategies.linux_hook import _CANDIDATE_RE
        text = (
            "hit-raw a=1 b=16 data=" + "ab" * 16 + "\n"       # 16 字节，不是 passphrase
            "garbage a=zz b=20 data=" + "cd" * 32 + "\n"      # a 不是 hex
            "random log line\n"
        )
        self.assertEqual([m.group(3) for m in _CANDIDATE_RE.finditer(text)], [])


class DeriveKeysTest(unittest.TestCase):
    """PBKDF2 派生 + page-1 HMAC 校验（校验不通过就不入库）。

    真实微信是 256000 轮，单次派生在慢机器上要秒级；这里把迭代数降到 800
    跑逻辑，参数本身由 KdfParamsTest 单独钉死——否则整套测试被 PBKDF2 拖成分钟级。
    """

    ITER = 800

    def setUp(self):
        from siwx.strategies import linux_hook
        self.mod = linux_hook
        self._saved = linux_hook.PBKDF2_ITERATIONS
        linux_hook.PBKDF2_ITERATIONS = self.ITER
        self.pages = _fixture_page1s(self.ITER)
        self.logs = []

    def tearDown(self):
        self.mod.PBKDF2_ITERATIONS = self._saved

    def _ctx(self):
        return {}, {}

    def test_fixture_verifies(self):
        """夹具自检：造出来的 page1 必须能被 verify_enc_key 认（否则测试无意义）。"""
        for salt_hex, page1 in self.pages.items():
            key = _derive(_PASSPHRASE, bytes.fromhex(salt_hex), self.ITER)
            self.assertTrue(verify_enc_key(key, page1))

    def test_correct_passphrase_derives_all(self):
        key_map, attrib = self._ctx()
        n = self.mod._derive_keys(_PASSPHRASE.hex(), self.pages, key_map, attrib,
                                  self.logs.append, self.logs.append)
        self.assertEqual(n, 2)
        self.assertEqual(set(key_map), set(self.pages))
        self.assertEqual(set(attrib.values()), {"linux_hook"})
        for salt_hex, hex_key in key_map.items():
            self.assertEqual(bytes.fromhex(hex_key),
                             _derive(_PASSPHRASE, bytes.fromhex(salt_hex), self.ITER))

    def test_wrong_passphrase_stores_nothing(self):
        key_map, attrib = self._ctx()
        n = self.mod._derive_keys(("00" * 32), self.pages, key_map, attrib,
                                  self.logs.append, self.logs.append)
        self.assertEqual(n, 0)
        self.assertEqual(key_map, {})
        self.assertEqual(attrib, {})

    def test_existing_salts_are_skipped(self):
        key_map, attrib = self._ctx()
        done = _SALT_A.hex()
        key_map[done] = "11" * 32
        n = self.mod._derive_keys(_PASSPHRASE.hex(), self.pages, key_map, attrib,
                                  self.logs.append, self.logs.append)
        self.assertEqual(n, 1)
        self.assertEqual(key_map[done], "11" * 32, "已入库的 salt 不许被覆盖")
        self.assertIn(_SALT_B.hex(), key_map)

    def test_no_missing_salt_is_noop(self):
        key_map, attrib = self._ctx()
        for s in self.pages:
            key_map[s] = "11" * 32
        n = self.mod._derive_keys(_PASSPHRASE.hex(), self.pages, key_map, attrib,
                                  self.logs.append, self.logs.append)
        self.assertEqual(n, 0)


class MultiAccountGateTest(unittest.TestCase):
    """多账号：未登录账号不许再重启微信。

    真实事故（Windows/macOS 没有这条路径，Linux 独有）：extract 按账号目录循环，
    第一个账号抓到 25/25 之后，对未登录的第二个账号又走一遍
    "停微信 → 注入 → 拉起 → 等 300 秒"，把用户正在用的微信杀掉且必然空等到超时，
    期间 systemd 用户环境的 LD_PRELOAD 也一直挂着不还原。
    """

    def setUp(self):
        from siwx.strategies import linux_hook
        self.mod = linux_hook

    @staticmethod
    def _ctx(db_dir) -> dict:
        return {"db_dir": str(db_dir), "page1_by_salt": {"aa" * 16: b"x" * PAGE_SZ},
                "key_map": {}, "attrib": {}, "log": lambda *_a: None,
                "dbg": lambda *_a: None}

    def test_unlogged_account_is_skipped_without_touching_wechat(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(self.mod, "_ATTEMPTED", True), \
             mock.patch.object(self.mod, "_account_is_live", return_value=False), \
             mock.patch.object(self.mod, "_resolve_binary") as rb, \
             mock.patch.object(self.mod, "_stop_wechat") as stop, \
             mock.patch.object(self.mod, "_launch_wechat") as launch:
            self.assertEqual(self.mod.extract(self._ctx(td)), 0)
            rb.assert_not_called()
            stop.assert_not_called()      # 关键：不许杀用户的微信
            launch.assert_not_called()

    def test_first_account_still_bootstraps(self):
        """第一次注入必须照旧：用户没登录时也要把微信拉起来让他扫码。"""
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(self.mod, "_ATTEMPTED", False), \
             mock.patch.object(self.mod, "_account_is_live", return_value=False), \
             mock.patch.object(self.mod, "_resolve_binary", return_value=Path("/bin/true")), \
             mock.patch.object(self.mod, "_DEFAULT_LOG", str(Path(td) / "hook.log")), \
             mock.patch.object(self.mod, "_has_user_systemd", return_value=False), \
             mock.patch("siwx.linux_hook.elfhook.find_hook_offset", return_value=0x10), \
             mock.patch.object(self.mod, "_build_shim", return_value=Path("/fake/wxhook.so")), \
             mock.patch.object(self.mod, "_wait_for_candidates", return_value=[]), \
             mock.patch.object(self.mod, "_stop_wechat") as stop, \
             mock.patch.object(self.mod, "_launch_wechat") as launch:
            self.mod.extract(self._ctx(td))
            stop.assert_called_once()
            launch.assert_called_once()

    def test_wait_aborts_when_wechat_vanishes(self):
        """微信进程没了就立刻收工，别拖满超时（否则环境还原一起被拖住）。"""
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "hook.log"
            logs: list[str] = []
            t0 = time.monotonic()
            with mock.patch.object(self.mod, "_LAUNCH_GRACE", 0.1):
                out = self.mod._wait_for_candidates(missing, 30.0, logs.append,
                                                   alive=lambda: False)
            self.assertEqual(out, [])
            self.assertLess(time.monotonic() - t0, 10.0, "微信没了还等满 30 秒就是 bug")
            self.assertTrue(any("退出" in line for line in logs), logs)


class KdfParamsTest(unittest.TestCase):
    """KDF 参数直接决定能不能解开真库，钉死不许漂。"""

    def test_params_match_wechat(self):
        from siwx.strategies import linux_hook
        self.assertEqual(linux_hook.PBKDF2_ITERATIONS, 256000)
        self.assertEqual(linux_hook.PBKDF2_DIGEST, "sha512")
        self.assertEqual(linux_hook.PBKDF2_DKLEN, 32)
        # 与 macOS LLDB 策略同一套参数，两条链路不许各写一份不同的值
        from siwx.strategies import macos_lldb
        for attr in ("PBKDF2_ITERATIONS", "PBKDF2_DIGEST", "PBKDF2_DKLEN"):
            self.assertEqual(getattr(linux_hook, attr), getattr(macos_lldb, attr), attr)


class ElfHookTest(unittest.TestCase):
    """ELF 定位：失败要报错，不能崩。"""

    def test_rejects_non_elf(self):
        from siwx.linux_hook.elfhook import HookNotFoundError, find_hook_offset
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"#!/bin/sh\necho not an elf\n")
            path = Path(fh.name)
        try:
            with self.assertRaises(HookNotFoundError):
                find_hook_offset(path)
        finally:
            path.unlink(missing_ok=True)

    def test_elf_without_anchor_raises(self):
        """/bin/true 是合法 ELF64，但没有 WCDB 锚点 → 必须明确报错。"""
        from siwx.linux_hook.elfhook import HookNotFoundError, find_hook_offset
        if platform.system() != "Linux":
            self.skipTest("需要 Linux ELF 二进制")
        with self.assertRaises(HookNotFoundError):
            find_hook_offset(Path("/bin/true"))

    def test_real_wechat_binary_offset(self):
        """本机装了微信时，真实二进制必须能算出偏移（偏移随构建变化，不写死）。"""
        from siwx.linux_hook.elfhook import find_hook_offset
        from siwx.strategies.linux_hook import _resolve_binary
        if platform.system() != "Linux":
            self.skipTest("需要 Linux")
        binary = _resolve_binary()
        if binary is None:
            self.skipTest("本机无微信二进制")
        off = find_hook_offset(Path(binary))
        self.assertGreater(off, 0)
        self.assertLess(off, 0x2000_0000)


class ShimBuildTest(unittest.TestCase):
    """现场编译 shim：运行期真正依赖的一步。"""

    def test_builds_shared_object(self):
        import shutil
        from siwx.strategies.linux_hook import _build_shim
        if not (shutil.which("cc") or shutil.which("gcc")):
            self.skipTest("本机无 cc/gcc")
        with tempfile.TemporaryDirectory() as tmp:
            so = _build_shim(0x123456, "/nonexistent/wechat", Path(tmp))
            self.assertIsNotNone(so, "shim 构建失败（wxhook.c 或编译参数坏了）")
            data = Path(so).read_bytes()
            self.assertEqual(data[:4], b"\x7fELF")
            # sigaction 必须真导出，否则 crashpad 一装处理器就把断点撇成致命信号
            nm = shutil.which("nm")
            if nm:
                out = subprocess.run([nm, "-D", "--defined-only", str(so)],
                                     capture_output=True, text=True).stdout
                for sym in ("sigaction", "__libc_sigaction", "__sigaction"):
                    self.assertIn(sym, out, f"shim 未导出 {sym}")


if __name__ == "__main__":
    unittest.main()
