"""密钥库：以 salt 为索引（数据库的真实身份），静止时加密落盘。

Windows 使用 DPAPI（CryptProtectData + 项目熵）；非 Windows（macOS/Linux）
作为开关兜底降级为明文 JSON——见 MACOS_SUPPORT.md 的差异说明。接口一致：
load / save / insert / unique_keys / store_path。
"""
import ctypes
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ENTROPY = b"stories-in-wx::keystore::v1"

_USE_DPAPI = sys.platform == "win32"

if _USE_DPAPI:
    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = ctypes.c_int
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = ctypes.c_int

    def _protect(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
        blob_ent = _DATA_BLOB(len(_ENTROPY), ctypes.cast(ent, ctypes.c_void_p))
        blob_out = _DATA_BLOB()
        if not _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0,
            ctypes.byref(blob_out),
        ):
            raise OSError("CryptProtectData 失败")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            if blob_out.pbData:
                _kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))

    def _unprotect(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
        blob_ent = _DATA_BLOB(len(_ENTROPY), ctypes.cast(ent, ctypes.c_void_p))
        blob_out = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0,
            ctypes.byref(blob_out),
        ):
            raise OSError("CryptUnprotectData 失败")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            if blob_out.pbData:
                _kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))

else:
    _crypt32 = _kernel32 = None
    _DATA_BLOB = None

    def _protect(data: bytes) -> bytes:
        # 非 Windows（macOS/Linux）：明文 JSON 兜底（见 MACOS_SUPPORT.md）
        return data

    def _unprotect(data: bytes) -> bytes:
        return data


def store_path() -> Path:
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("USERPROFILE")
            or str(_safe_root()))
    return Path(base) / "stories-in-wx" / "keystore.bin"


def _safe_root() -> Path:
    """LOCALAPPDATA/USERPROFILE 都缺失时的最后兜底（导入放函数内避免循环）。"""
    try:
        from siwx.paths import app_root
        return app_root()
    except Exception:
        return Path(tempfile.gettempdir())


def load() -> dict:
    """读取密钥库 → {salt_hex: {"key": hex, "strategy": str, "updated": ts}}。"""
    p = store_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(_unprotect(p.read_bytes()).decode("utf-8"))
    except (OSError, ValueError):
        return {}


def save(store: dict) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(_protect(json.dumps(store, ensure_ascii=False).encode("utf-8")))
    os.replace(tmp, p)


def insert(store: dict, salt: str, key: str, strategy: str) -> None:
    store[salt] = {"key": key.lower(), "strategy": strategy, "updated": int(time.time())}


def unique_keys(store: dict):
    seen = set()
    for rec in store.values():
        k = rec.get("key", "")
        if len(k) == 64 and k not in seen:
            seen.add(k)
            yield k