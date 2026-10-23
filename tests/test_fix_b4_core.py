"""P2-20 密钥主链路真实/可注入测试（修订清单 2026-10-23，批次 4）。

此前 keystore.load/save、pool._worker、策略链顺序、config_cipher 纯函数、
MMKV 文件格式与 winproc 原语都只有 mock 或零用例——主链路坏了测试仍全绿。
本文件用真实 DPAPI 落盘（Windows）、真实 AES-GCM 文件、真实进程内存读取
（仅读本测试进程）覆盖，其余位点用可注入替身钉住顺序/回退语义。

全部使用 IsolatedRootCase 的临时 SIWX_ROOT/SIWX_DATA_DIR，合成占位符
（wxid_demo_a / ab*32），不读取任何真实账号数据，不扫描微信进程。
"""
import ctypes
import hashlib
import json
import os
import struct
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._base import IsolatedRootCase

SALT_A = "ab" * 32
SALT_B = "cd" * 32
KEY_HEX = bytes(range(32)).hex()        # 32 种不同字节 → 满足唯一性检查


def _noop(_msg):
    """不落任何地方的任务轨回调（策略链测试用）。"""


def _fake_module(name, fn):
    """策略模块替身：__name__ 决定标签，extract 即被测钩子。

    用 ModuleType 而非 SimpleNamespace：后者不可哈希，放不进
    _PROCESS_DEPENDENT 集合（真实注册表里是模块对象）。
    """
    mod = types.ModuleType(f"fake.{name}")
    mod.extract = fn
    return mod


# ── 1. keystore 真实读写 ────────────────────────────────────────

class TestKeystoreReal(IsolatedRootCase):
    """不走 mock：Windows 上真实过 DPAPI，其余平台走明文兜底。"""

    def test_insert_save_load_roundtrip(self):
        from siwx import keystore
        store = {}
        keystore.insert(store, SALT_A, KEY_HEX.upper(), "mmkv")
        keystore.save(store)
        self.assertTrue(keystore.store_path().is_file())
        loaded = keystore.load()
        self.assertEqual(set(loaded), {SALT_A})
        rec = loaded[SALT_A]
        self.assertEqual(rec["key"], KEY_HEX)          # insert 统一小写
        self.assertEqual(rec["strategy"], "mmkv")
        self.assertIsInstance(rec["updated"], int)
        # 再插第二个 salt 并保存：旧条目保留（往返不丢数据）
        keystore.insert(loaded, SALT_B, "ef" * 32, "cipher")
        keystore.save(loaded)
        again = keystore.load()
        self.assertEqual(set(again), {SALT_A, SALT_B})
        self.assertEqual(again[SALT_B]["strategy"], "cipher")

    def test_corrupt_store_returns_empty_without_raise(self):
        """损坏的密钥库必须返回 {} 且不抛（否则整条解密链崩溃）。"""
        from siwx import keystore
        p = keystore.store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        for payload in (b"not-json-at-all", b"\x00\x01\x02\xff" * 8, b"{}"):
            p.write_bytes(payload)
            self.assertEqual(keystore.load(), {}, payload[:8])

    def test_legacy_location_migrated(self):
        """新位置缺失时读旧位置并尽力迁移（新文件生成后再次读到同样数据）。"""
        from siwx import keystore
        legacy = self.tmp / "legacy" / "keystore.bin"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        old_data = {SALT_A: {"key": KEY_HEX, "strategy": "keystore", "updated": 1}}
        legacy.write_bytes(keystore._protect(
            json.dumps(old_data, ensure_ascii=False).encode("utf-8")))
        self.assertFalse(keystore.store_path().is_file())

        with mock.patch.object(keystore, "_legacy_store_path", lambda: legacy):
            self.assertEqual(keystore.load(), old_data)
            self.assertTrue(keystore.store_path().is_file(),
                            "旧位置数据未迁移到新位置")
            # 迁移后新位置自洽，旧文件即使消失也不影响
            self.assertEqual(keystore.load(), old_data)
        legacy.unlink()
        self.assertEqual(keystore.load(), old_data)


# ── 2. pool：worker 语义 + 进程池创建失败回退串行 ───────────────

class TestPoolReal(IsolatedRootCase):
    REL = os.path.join("message", "message_0.db")

    def test_worker_ok(self):
        """_worker 内部 import decrypt_database → patch 模块属性即可注入。"""
        from siwx import pool
        seen = {}

        def fake_decrypt(src, dst, key):
            seen["src"], seen["dst"], seen["key"] = src, dst, key
            return 42

        task = (self.REL, "src.db", "dst.db", KEY_HEX)
        with mock.patch("siwx.sqlcipher.decrypt_database", fake_decrypt):
            rel, pages, status, err = pool._worker(task)
        self.assertEqual((rel, pages, status, err), (self.REL, 42, "ok", ""))
        self.assertEqual(seen["key"], bytes(range(32)))
        self.assertEqual(seen["src"], Path("src.db"))

    def test_worker_failure_becomes_failed_status(self):
        from siwx import pool

        def boom(src, dst, key):
            raise RuntimeError("bad key")

        task = (self.REL, "src.db", "dst.db", KEY_HEX)
        with mock.patch("siwx.sqlcipher.decrypt_database", boom):
            rel, pages, status, err = pool._worker(task)
        self.assertEqual(rel, self.REL)
        self.assertEqual(pages, 0)
        self.assertEqual(status, "failed")
        self.assertIn("RuntimeError", err)      # 异常类型名不能丢（审计 §3.2）
        self.assertIn("bad key", err)
        self.assertNotIn("Traceback", err)      # 不带 traceback 进任务面板

    def test_pool_creation_failure_falls_back_to_serial(self):
        """Windows spawn 环境失败：回退串行且结果条数恒等于任务数。"""
        from siwx import pool
        tasks = [(f"message/m{i}.db", f"s{i}.db", f"d{i}.db", KEY_HEX)
                 for i in range(3)]
        with mock.patch("multiprocessing.Pool", side_effect=OSError("no spawn")), \
                mock.patch("siwx.sqlcipher.decrypt_database", lambda *a: 7):
            results = pool.decrypt_parallel(tasks, workers=4)
        self.assertEqual(len(results), len(tasks))
        self.assertEqual([r[0] for r in results], [t[0] for t in tasks])
        self.assertTrue(all(r[2] == "ok" and r[1] == 7 for r in results))

    def test_callback_called_once_per_task_in_serial_fallback(self):
        from siwx import pool
        done = []
        tasks = [(f"m{i}.db", f"s{i}", f"d{i}", KEY_HEX) for i in range(2)]
        with mock.patch("multiprocessing.Pool", side_effect=OSError("no spawn")), \
                mock.patch("siwx.sqlcipher.decrypt_database", lambda *a: 1):
            pool.decrypt_parallel(tasks, workers=2, on_done=done.append)
        self.assertEqual(len(done), 2)


# ── 3. 策略链顺序（假策略）──────────────────────────────────────

class TestStrategyChain(IsolatedRootCase):

    def _ctx(self):
        salts = ("s1", "s2")
        return {"page1_by_salt": {s: b"" for s in salts}, "key_map": {},
                "attrib": {}, "log": _noop, "use_memory": True}

    def _patch(self, mods, dependent=()):
        from siwx import strategies
        return (
            mock.patch.object(strategies, "STRATEGY_REGISTRY", list(mods)),
            mock.patch.object(strategies, "_PROCESS_DEPENDENT", set(dependent)),
        )

    def test_runs_in_registry_order_and_stops_when_all_salts_filled(self):
        from siwx import strategies
        calls = []

        def one(ctx):
            calls.append("one")

        def two(ctx):                         # 补齐全部 salt
            calls.append("two")
            for s in ctx["page1_by_salt"]:
                ctx["key_map"][s] = KEY_HEX
                ctx["attrib"][s] = "fake"

        def three(ctx):                       # 已被早停跳过
            calls.append("three")

        mods = [_fake_module("one", one), _fake_module("two", two),
                _fake_module("three", three)]
        p1, p2 = self._patch(mods)
        ctx = self._ctx()
        with p1, p2:
            strategies.run_strategies(ctx)
        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(set(ctx["key_map"]), {"s1", "s2"})
        self.assertEqual(set(ctx["attrib"].values()), {"fake"})

    def test_strategy_exception_does_not_break_chain(self):
        from siwx import strategies
        calls, logged = [], []

        def boom(ctx):
            calls.append("boom")
            raise RuntimeError("策略炸了")

        def after(ctx):
            calls.append("after")
            ctx["key_map"]["s1"] = KEY_HEX

        mods = [_fake_module("boom", boom), _fake_module("after", after)]
        p1, p2 = self._patch(mods)
        ctx = self._ctx()
        ctx["log"] = logged.append
        with p1, p2:
            strategies.run_strategies(ctx)
        self.assertEqual(calls, ["boom", "after"])
        self.assertEqual(ctx["key_map"], {"s1": KEY_HEX})
        self.assertTrue(any("异常" in m and "boom" in m for m in logged),
                        logged)

    def test_use_memory_false_skips_process_dependent(self):
        from siwx import strategies
        calls = []

        def plain(ctx):
            calls.append("plain")

        def mem(ctx):
            calls.append("mem")

        mods = [_fake_module("plain", plain), _fake_module("mem", mem)]
        p1, p2 = self._patch(mods, dependent={mods[1]})
        ctx = self._ctx()
        ctx["use_memory"] = False
        with p1, p2:
            strategies.run_strategies(ctx)
        self.assertEqual(calls, ["plain"])

    def test_use_memory_false_skips_only_dependent_modules(self):
        """反向确认：use_memory=True 时 _PROCESS_DEPENDENT 也照常执行。"""
        from siwx import strategies
        calls = []

        def mem(ctx):
            calls.append("mem")

        mods = [_fake_module("mem", mem)]
        p1, p2 = self._patch(mods, dependent={mods[0]})
        ctx = self._ctx()
        with p1, p2:
            strategies.run_strategies(ctx)
        self.assertEqual(calls, ["mem"])


# ── 4. config_cipher 纯函数（非 mock）───────────────────────────
# config_cipher 模块顶层 import winproc（ctypes.WinDLL），非 Windows 直接
# 导入即失败；macOS/Linux 的 CI 必须跳过，不能让平台差异伪装成代码回归。

@unittest.skipUnless(sys.platform == "win32", "config_cipher 仅 Windows 策略链加载")
class TestConfigCipherPure(unittest.TestCase):

    def test_blob_candidates_recover_literal(self):
        from siwx.strategies import config_cipher as cc
        plain = b"head-" + b"x'" + KEY_HEX.encode() + b"'-tail"
        blob = cc._xor_repeat(plain, cc.CONFIG_XOR_MASK)   # XOR 自逆
        self.assertNotEqual(blob, plain)
        cands = list(cc._blob_key_candidates(blob))
        self.assertEqual(cands, [(KEY_HEX, None)])

    def test_probable_key_rejects_degenerate(self):
        from siwx.strategies import config_cipher as cc
        self.assertFalse(cc._probable_32_byte_key(b"\x00" * 32))
        self.assertFalse(cc._probable_32_byte_key(b"\xff" * 32))
        self.assertFalse(cc._probable_32_byte_key(b"\x11" * 32))   # 全同
        self.assertFalse(cc._probable_32_byte_key(bytes([0] * 31 + [1])))  # 唯一性
        self.assertFalse(cc._probable_32_byte_key(bytes(range(31))))       # 长度
        self.assertTrue(cc._probable_32_byte_key(bytes(range(32))))

    def test_xor_is_self_inverse(self):
        from siwx.strategies import config_cipher as cc
        data = bytes(range(64))
        self.assertEqual(
            cc._xor_repeat(cc._xor_repeat(data, cc.CONFIG_XOR_MASK),
                           cc.CONFIG_XOR_MASK), data)

    def test_scoring_mask_recovers_mostly_printable_mask(self):
        """ASCII 打分法：可打印明文应恢复出高可打印率掩码。

        32 个残差类各自独立取最优，个别类存在等价映射（同为可打印）时
        掩码字节可能不同——断言可打印率而非逐字节相等。
        """
        from siwx.strategies import config_cipher as cc
        plain = bytes(0x21 + ((i * 37) % 90) for i in range(160))
        mask = bytes((i * 13 + 7) % 256 for i in range(32))
        recovered = cc._scoring_mask(cc._xor_repeat(plain, mask))
        self.assertIsNotNone(recovered)
        decoded = cc._xor_repeat(cc._xor_repeat(plain, mask), recovered)
        printable = sum(1 for c in decoded if 0x20 <= c <= 0x7E)
        self.assertGreaterEqual(printable / len(decoded), 0.9)


# ── 5. MMKV 真实文件格式（AES-GCM，非 mock）─────────────────────

class TestMmkvRealFile(IsolatedRootCase):
    WXID = "wxid_demo_a"
    CODE = 0x2A

    def _write_mmkv(self, db_dir: Path, rel: str, key_hex: str) -> None:
        from Crypto.Cipher import AES
        from siwx.strategies import mmkv
        d = db_dir / "MMKV"
        d.mkdir(parents=True, exist_ok=True)
        plaintext = (b"\x08\x01junk" + rel.encode() + b"|"
                     + key_hex.encode() + b"|tail")
        aes_key = hashlib.md5(
            (str(self.CODE) + mmkv.clean_wxid(self.WXID)).encode()
        ).hexdigest()[:16].encode()
        iv = bytes(range(16))
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
        ct, tag = cipher.encrypt_and_digest(plaintext)
        total = 16 + len(ct) + 16
        (d / f"f{self.CODE:x}tinfo.mmkv").write_bytes(
            struct.pack("<I", total) + iv + ct + tag)

    def test_extract_derives_key_from_tinfo_file(self):
        from siwx.strategies import mmkv
        rel = os.path.join("message", "message_0.db")
        db_dir = self.tmp / self.WXID / "db_storage"
        self._write_mmkv(db_dir, rel, KEY_HEX)

        ctx = {"db_dir": db_dir,
               "entries": [SimpleNamespace(salt_hex=SALT_A, rel=rel)],
               "page1_by_salt": {SALT_A: b"x"},
               "key_map": {}, "attrib": {}, "log": _noop}
        with mock.patch.object(mmkv, "verify_enc_key", lambda kb, p: True):
            found = mmkv.extract(ctx)
        self.assertEqual(found, 1)
        self.assertEqual(ctx["key_map"][SALT_A], KEY_HEX)
        self.assertEqual(ctx["attrib"][SALT_A], "mmkv")

    def test_wrong_derivation_leaves_key_map_empty(self):
        """文件可解但 HMAC 验证失败：不得写入 key_map（propose-verify 分离）。"""
        from siwx.strategies import mmkv
        rel = os.path.join("message", "message_0.db")
        db_dir = self.tmp / self.WXID / "db_storage"
        self._write_mmkv(db_dir, rel, KEY_HEX)
        ctx = {"db_dir": db_dir,
               "entries": [SimpleNamespace(salt_hex=SALT_A, rel=rel)],
               "page1_by_salt": {SALT_A: b"x"},
               "key_map": {}, "attrib": {}, "log": _noop}
        with mock.patch.object(mmkv, "verify_enc_key", lambda kb, p: False):
            found = mmkv.extract(ctx)
        self.assertEqual(found, 0)
        self.assertEqual(ctx["key_map"], {})


# ── 6. winproc 原语（仅 Windows，读本测试进程）──────────────────

@unittest.skipUnless(sys.platform == "win32", "winproc 仅 Windows 可用")
class TestWinprocSelf(IsolatedRootCase):
    """只读本测试进程：进程内 buffer 的真实地址经 ReadProcessMemory 读回。"""

    def setUp(self):
        super().setUp()
        from siwx import winproc
        self.w = winproc
        self.h = winproc.open_process(os.getpid())
        self.assertIsNotNone(self.h, "无法以只读权限打开本进程")

    def tearDown(self):
        self.w.close_handle(self.h)
        super().tearDown()

    def test_read_mem_reads_own_buffer(self):
        buf = ctypes.create_string_buffer(b"WXPROC")
        got = self.w.read_mem(self.h, ctypes.addressof(buf), 6)
        self.assertEqual(got, b"WXPROC")

    def test_read_u64(self):
        val = ctypes.c_uint64(0x1122_3344_5566_7788)
        self.assertEqual(self.w.read_u64(self.h, ctypes.addressof(val)),
                         0x1122_3344_5566_7788)

    def test_enum_regions_non_empty(self):
        regions = self.w.enum_regions(self.h)
        self.assertTrue(regions)
        self.assertTrue(all(0 < size for _base, size in regions))

    def test_bad_address_returns_none(self):
        self.assertIsNone(self.w.read_mem(self.h, 1, 16))


if __name__ == "__main__":
    unittest.main()
