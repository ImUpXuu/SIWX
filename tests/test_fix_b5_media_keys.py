"""P3 批次：媒体/密钥/日志/诊断脚本加固回归（修订清单 2026-10-23，批次 5）。

覆盖：
- P3-1  get_image 单候选异常隔离（坏图/读取竞态不再 500）
- P3-2  keystore 并发保存（唯一临时名）
- P3-3  MMKV 自定义微信号目录（非 wxid_）多候选派生
- P3-4  kvcomm 扫描走 %APPDATA%
- P3-5  config_cipher 只在 salt 全覆盖时提前结束
- P3-22 media_key.json 非 Windows 权限收紧
- P3-23 日志脱敏补 32 位 hex / 数字 ID / 含空格路径
- P3-28 诊断脚本强制账号参数 + key/token 遮蔽

全部使用 IsolatedRootCase 的临时 SIWX_ROOT/SIWX_DATA_DIR 与合成占位符
（wxid_test_* / alias_abc12345 / ab*32），不读取真实账号数据、不扫描真实
用户目录、不发真实网络请求。
"""
import ast
import hashlib
import os
import stat
import struct
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._base import ROOT, IsolatedRootCase

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 40 + b"\xff\xd9"


class _BadPath:
    """is_file() 为真但 read_bytes() 抛 OSError 的候选（模拟被删/被占用）。"""

    def __init__(self, name: str = "busy_candidate.dat"):
        self.name = name

    def is_file(self) -> bool:
        return True

    def read_bytes(self) -> bytes:
        raise OSError("文件被占用 / 已删除")

    def __str__(self) -> str:  # 日志里 str(path) 的兜底
        return self.name


# ── P3-1 get_image 全链路异常隔离 ───────────────────────────────

class TestGetImageCandidateIsolation(IsolatedRootCase):
    """坏图/读取竞态只丢该候选：get_image 返回 (None, reason) 而不是抛异常。"""

    def setUp(self):
        super().setUp()
        from siwx import media
        self.media = media
        self.out = self.tmp / "out"
        self.account = "wxid_test_a"

    def test_unreadable_hardlink_candidate_returns_reason(self):
        bad = _BadPath()
        with mock.patch.object(self.media, "resolve_image_path",
                               lambda *a, **kw: [bad]):
            body, reason = self.media.get_image(
                self.account, "a" * 32, self.out)
        self.assertIsNone(body)
        # 读取失败归入"本地拿不到"（api_chat._image_fail_reason → missing_local）
        self.assertEqual(reason, "未找到文件")

    def test_unreadable_candidate_falls_through_to_good_one(self):
        good = self.tmp / "good_hardlink.dat"
        good.write_bytes(JPEG_BYTES)
        with mock.patch.object(self.media, "resolve_image_path",
                               lambda *a, **kw: [_BadPath(), good]):
            body, ctype = self.media.get_image(
                self.account, "b" * 32, self.out)
        self.assertEqual(body, JPEG_BYTES)
        self.assertEqual(ctype, "image/jpeg")

    def test_unreadable_attach_candidate_falls_through_to_good_one(self):
        good = self.tmp / "good_attach.dat"
        good.write_bytes(JPEG_BYTES)
        with mock.patch.object(self.media, "attach_paths",
                               lambda *a, **kw: [_BadPath("attach_busy.dat"), good]):
            body, ctype = self.media.get_image(
                self.account, "c" * 32, self.out, chat="wxid_test_chat")
        self.assertEqual(body, JPEG_BYTES)
        self.assertEqual(ctype, "image/jpeg")

    def test_finalize_exception_does_not_raise(self):
        """转码（DLL/sips）异常按"本候选不可用"处理，绝不冒泡成 500。"""
        good = self.tmp / "good_for_finalize.dat"
        good.write_bytes(JPEG_BYTES)

        def boom(*a, **kw):
            raise RuntimeError("模拟解码器崩溃")

        with mock.patch.object(self.media, "resolve_image_path",
                               lambda *a, **kw: [good]), \
             mock.patch.object(self.media, "_finalize", boom):
            body, _reason = self.media.get_image(
                self.account, "d" * 32, self.out)
        self.assertIsNone(body)


# ── P3-2 keystore 并发保存 ──────────────────────────────────────

class TestKeystoreConcurrentSave(IsolatedRootCase):
    def test_parallel_saves_leave_single_parsable_file(self):
        from siwx import keystore

        stores = [
            {("%064x" % (i + 1)): {"key": "%064x" % (i + 101),
                                   "strategy": "test", "updated": i}}
            for i in range(8)
        ]
        errors = []
        barrier = threading.Barrier(len(stores))

        def worker(store):
            try:
                barrier.wait(timeout=20)
                keystore.save(store)
            except Exception as e:            # noqa: BLE001 — 测试要看到任何失败
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads))

        # 最终文件必须是某个完整 store（os.replace 原子），且可被 load 解析
        self.assertIn(keystore.load(), stores)
        leftovers = list(keystore.store_path().parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertNotIn(".tmp", keystore.store_path().name)


# ── P3-3 MMKV 非 wxid_ 自定义目录 ──────────────────────────────

class TestMmkvCustomAliasDir(IsolatedRootCase):
    CODE = 0x2A
    KEY_HEX = bytes(range(32)).hex()

    def _write_mmkv(self, db_dir: Path, rel: str, derive_name: str) -> None:
        from Crypto.Cipher import AES
        d = db_dir / "MMKV"
        d.mkdir(parents=True, exist_ok=True)
        plaintext = (b"\x08\x01junk" + rel.encode() + b"|"
                     + self.KEY_HEX.encode() + b"|tail")
        aes_key = hashlib.md5(
            (str(self.CODE) + derive_name).encode()).hexdigest()[:16].encode()
        iv = bytes(range(16))
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
        ct, tag = cipher.encrypt_and_digest(plaintext)
        total = 16 + len(ct) + 16
        (d / (f"f{self.CODE:x}tinfo.mmkv")).write_bytes(
            struct.pack("<I", total) + iv + ct + tag)

    def _ctx(self, db_dir: Path, rel: str):
        from types import SimpleNamespace
        salt = "ab" * 32
        return {"db_dir": db_dir,
                "entries": [SimpleNamespace(salt_hex=salt, rel=rel)],
                "page1_by_salt": {salt: b"x"},
                "key_map": {}, "attrib": {}, "log": lambda _m: None,
                "dbg": lambda _m: None}

    def test_custom_alias_dir_is_not_skipped(self):
        from siwx.strategies import mmkv
        rel = os.path.join("message", "message_0.db")
        db_dir = self.tmp / "alias_abc12345" / "db_storage"
        self._write_mmkv(db_dir, rel, "alias_abc12345")
        ctx = self._ctx(db_dir, rel)
        with mock.patch.object(mmkv, "verify_enc_key", lambda kb, p: True):
            found = mmkv.extract(ctx)
        self.assertEqual(found, 1)
        self.assertEqual(list(ctx["key_map"].values()), [self.KEY_HEX])

    def test_wxid_dir_still_works(self):
        from siwx.strategies import mmkv
        rel = os.path.join("message", "message_0.db")
        db_dir = self.tmp / "wxid_test_a" / "db_storage"
        self._write_mmkv(db_dir, rel, "wxid_test")   # clean_wxid("wxid_test_a")
        ctx = self._ctx(db_dir, rel)
        with mock.patch.object(mmkv, "verify_enc_key", lambda kb, p: True):
            found = mmkv.extract(ctx)
        self.assertEqual(found, 1)

    def test_invalid_dir_name_still_skipped(self):
        from siwx.strategies import mmkv
        rel = os.path.join("message", "message_0.db")
        db_dir = self.tmp / "---" / "db_storage"
        self._write_mmkv(db_dir, rel, "---")
        logs = []
        ctx = self._ctx(db_dir, rel)
        ctx["log"] = logs.append
        with mock.patch.object(mmkv, "verify_enc_key", lambda kb, p: True):
            found = mmkv.extract(ctx)
        self.assertEqual(found, 0)
        self.assertEqual(ctx["key_map"], {})
        self.assertTrue(any("跳过" in m for m in logs))


# ── P3-4 kvcomm 走 %APPDATA% ───────────────────────────────────

class TestKvcommUsesAppData(IsolatedRootCase):
    def test_appdata_env_is_scanned(self):
        import glob as _glob
        from siwx import media
        appdata = self.tmp / "appdata"
        kv = appdata / "Tencent" / "xwechat" / "net" / "kvcomm"
        kv.mkdir(parents=True)
        code = 1234567890
        (kv / f"key_{code}_1_1_1_3600_input.statistic").write_bytes(b"")
        real_glob = _glob.glob
        tmp_norm = str(self.tmp).replace("\\", "/")

        def fake_glob(pat):
            # 真实用户目录/兜底通配符一律不读，只放行临时目录下的模式
            if tmp_norm not in str(pat).replace("\\", "/"):
                return []
            return real_glob(pat)

        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
             mock.patch.object(media.platform, "system", return_value="Windows"), \
             mock.patch("glob.glob", side_effect=fake_glob):
            codes = media.find_kvcomm_codes()
        self.assertIn(code, codes)

    def test_missing_appdata_falls_back_without_crash(self):
        from siwx import media
        with mock.patch.dict(os.environ, {"APPDATA": ""}), \
             mock.patch.object(media.platform, "system", return_value="Windows"), \
             mock.patch("glob.glob", return_value=[]):
            self.assertEqual(media.find_kvcomm_codes(), [])

    def test_source_prefers_appdata(self):
        src = (ROOT / "siwx" / "media.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("APPDATA")', src)
        # 旧兜底通配符保留（APPDATA 缺失时仍可用）
        self.assertIn("C:/Users/*/AppData/Roaming/Tencent", src)


# ── P3-5 config_cipher 多开不漏扫 ──────────────────────────────

CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
_MEM_BASE = 0x100000
_CONFIG_PTR = 0x200000
_DATA_PTR = 0x300000


def _fake_process_memory(blob: bytes) -> dict:
    """构造一条可被 config_cipher 指针链走通的假内存布局（单 chunk）。"""
    chunk = bytearray(0x80)
    chunk[:len(CIPHER_NAME)] = CIPHER_NAME
    pair_off = 0x40
    chunk[pair_off:pair_off + 8] = struct.pack("<Q", _MEM_BASE)
    chunk[pair_off + 8:pair_off + 16] = struct.pack("<Q", len(CIPHER_NAME))
    node_base = _MEM_BASE + pair_off - 0x10
    node = bytearray(0x50)
    node[0x10:0x18] = struct.pack("<Q", _MEM_BASE)
    node[0x18:0x20] = struct.pack("<Q", len(CIPHER_NAME))
    node[0x28:0x30] = struct.pack("<Q", _CONFIG_PTR)
    obj = bytearray(0x28)
    obj[0x8:0x10] = struct.pack("<Q", _DATA_PTR)
    obj[0x10:0x18] = struct.pack("<Q", len(blob))
    return {
        "chunk": (_MEM_BASE, bytes(chunk)),
        "mem": {
            (node_base, 0x50): bytes(node),
            (_CONFIG_PTR + 0x88, 0x28): bytes(obj),
            (_DATA_PTR, len(blob)): blob,
        },
    }


class TestConfigCipherMultiPid(IsolatedRootCase):
    """两个微信实例各持一个 salt 的密钥：第一个命中后必须继续扫第二个。"""

    def test_second_pid_scanned_until_all_salts_covered(self):
        from siwx.strategies import config_cipher as cc
        salt_a, salt_b = "ab" * 32, "cd" * 32
        key_a, key_b = "aa" * 32, "bb" * 32
        procs = {1111: _fake_process_memory(b"blob_a"),
                 2222: _fake_process_memory(b"blob_b")}
        by_blob = {b"blob_a": [(key_a, None)], b"blob_b": [(key_b, None)]}
        logs, dbg_msgs = [], []

        def fake_iter(h, regions, chunk_size=0, overlap=0):
            yield procs[h]["chunk"]

        def fake_read(h, addr, size):
            return procs[h]["mem"].get((addr, size))

        ctx = {"page1_by_salt": {salt_a: bytes.fromhex(key_a),
                                 salt_b: bytes.fromhex(key_b)},
               "key_map": {}, "attrib": {},
               "log": logs.append, "dbg": dbg_msgs.append}
        with mock.patch.object(cc.winproc, "psutil_pid_list",
                               lambda: [1111, 2222]), \
             mock.patch.object(cc.winproc, "open_process", lambda pid: pid), \
             mock.patch.object(cc.winproc, "close_handle", lambda h: None), \
             mock.patch.object(cc.winproc, "enum_regions",
                               lambda h: [(_MEM_BASE, 0x1000)]), \
             mock.patch.object(cc.winproc, "iter_chunks", fake_iter), \
             mock.patch.object(cc.winproc, "read_mem", fake_read), \
             mock.patch.object(cc, "verify_enc_key", lambda kb, p1: kb == p1), \
             mock.patch.object(cc, "_blob_key_candidates",
                               lambda blob: by_blob.get(blob, [])):
            found = cc.extract(ctx)

        self.assertEqual(found, 2)
        self.assertEqual(ctx["key_map"], {salt_a: key_a, salt_b: key_b})
        # 第一个 PID 命中但未全覆盖时必须显式说明"继续下一个进程"
        self.assertTrue(any("继续下一个进程" in m for m in dbg_msgs), dbg_msgs)
        src = (ROOT / "siwx" / "strategies" / "config_cipher.py").read_text(
            encoding="utf-8")
        self.assertNotIn("if found_any:", src)
        self.assertIn("len(key_map) >= total_salts", src)


# ── P3-22 media_key.json 权限 ──────────────────────────────────

@unittest.skipIf(os.name == "nt", "Windows 无 POSIX 权限位（P3-22 只收紧非 Windows）")
class TestMediaKeyPermissions(IsolatedRootCase):
    def test_saved_key_file_is_0600(self):
        from siwx import media
        media._save_key_cache({"ab" * 16: {"aes": "cd" * 16, "xor": "09"}})
        p = media._key_file()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_existing_loose_file_tightened_on_lookup(self):
        from siwx import media
        p = media._key_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        p.chmod(0o644)
        media._key_file()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)


# ── P3-23 日志脱敏补充规则 ────────────────────────────────────

class TestDesensitizeExtraRules(unittest.TestCase):
    def setUp(self):
        from siwx.logger import desensitize_msg
        self.d = desensitize_msg

    def test_32_hex_masked(self):
        secret = "ab" * 16
        out = self.d(f"md5={secret}")
        self.assertNotIn(secret, out)
        self.assertIn("****", out)

    def test_64_hex_masked_without_32bit_leftover(self):
        secret = "ab" * 32
        out = self.d(f"key={secret}")
        self.assertNotIn(secret, out)
        self.assertNotIn(secret[:32], out)
        self.assertEqual(out, "key=****")

    def test_numeric_ids_in_kv_form_masked(self):
        out = self.d("local_id=123456789 serverId=987654321")
        self.assertNotIn("123456789", out)
        self.assertNotIn("987654321", out)
        self.assertIn("local_id=***", out)
        self.assertIn("serverId=***", out)

    def test_plain_numbers_are_preserved(self):
        text = "耗时 1234567890ms 比例 3/5 分数 img=3/5"
        self.assertEqual(self.d(text), text)

    def test_path_with_spaces_keeps_only_last_segment(self):
        cases = [
            (r"path=C:\Program Files\Tencent\Weixin\4.1.8\config.ini err=x",
             "config.ini", "Program Files"),
            (r"path=C:\Program Files (x86)\Tencent\WeChat\cfg.ini err=x",
             "cfg.ini", "Program Files"),
            (r"path=C:\Users\John Doe\AppData\Roaming\x.db err=x",
             "x.db", "John Doe"),
            (r"path=/Users/user_a/Library/Application Support/app/x.bin err=x",
             "x.bin", "Application Support"),
            (r"path=~/Library/Application Support/app/y.bin err=x",
             "y.bin", "Application Support"),
        ]
        for text, keep, drop in cases:
            out = self.d(text)
            self.assertIn(keep, out, out)
            self.assertNotIn(drop, out, out)

    def test_idempotent_on_combined_message(self):
        text = (r"local_id=123456789 path=C:\Users\John Doe\a\b.dat "
                "md5=" + "ab" * 16 + " account=wxalias_abc12345")
        once = self.d(text)
        self.assertEqual(once, self.d(once))


# ── P3-28 诊断脚本（强制账号 + 遮蔽 key/token） ────────────────

def _load_script_function(path: Path, name: str):
    """从脚本源码里单独取出纯函数执行（脚本顶层会连真实服务/库，不能整体导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), ns)
    return ns[name]


class TestDiagScripts(IsolatedRootCase):
    SCRIPTS = ("diag_sns_api.py", "diag_sns_cdn.py")

    def _run(self, name: str):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / name)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, env=env)

    def test_scripts_require_account_argument(self):
        for name in self.SCRIPTS:
            r = self._run(name)
            self.assertEqual(r.returncode, 2, f"{name}: {r.stdout}{r.stderr}")
            self.assertIn("用法", (r.stdout or "") + (r.stderr or ""))

    def test_scripts_have_no_default_account(self):
        for name in self.SCRIPTS:
            src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotRegex(src, r'else\s+["\']wxid_')
            self.assertNotIn("sys.argv[1] if len(sys.argv) > 1 else", src)
            self.assertIn("_mask_secret", src)
            self.assertIn("SystemExit(2)", src)

    def test_mask_secret_does_not_keep_prefix(self):
        for name in self.SCRIPTS:
            mask = _load_script_function(ROOT / "scripts" / name, "_mask_secret")
            out = mask("abcdef1234567890")
            self.assertNotIn("abcdef", out)
            self.assertIn("len=16", out)
            self.assertEqual(mask(""), "(空)")
            self.assertEqual(mask(None), "(空)")

    def test_cdn_probe_result_masks_key_and_token(self):
        src = (ROOT / "scripts" / "diag_sns_cdn.py").read_text(encoding="utf-8")
        self.assertIn('"key": _mask_secret(m.get("key"))', src)
        self.assertIn('"token": _mask_secret(m.get("token"))', src)
        # 失败样本 URL 去掉查询串，token 不再随 URL 打印
        self.assertIn("_url_without_query", src)


if __name__ == "__main__":
    unittest.main()
