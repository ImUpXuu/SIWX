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

from siwx import logger as _slog

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
    """密钥库落盘位置：系统数据目录（与程序安装位置解耦）。

    v5.0.1 及更早版本写在 LOCALAPPDATA/USERPROFILE，缺失时回退到
    安装目录（app_root）——在 macOS 双击 .app 的场景下安装目录位于
    bundle 内部且只读，会导致读取/保存异常（issue #11）。现在统一走
    paths.data_dir()；旧位置仅作向后兼容读取（见 load）。
    """
    try:
        from siwx.paths import data_dir
        return data_dir() / "keystore.bin"
    except Exception as e:
        # 审计 §3.5：密钥库实际落在哪里不可见，是排"密钥库为什么总为空"第一问题
        _slog.detailed("keystore", f"store_path 回退 legacy: {type(e).__name__}: {e}")
        return _legacy_store_path()


def _legacy_store_path() -> Path:
    """v5.0.1 及更早版本的密钥库位置，仅用于兼容读取/迁移。"""
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
        # 审计 §3.5：落 TEMP 重启即丢，必须可见
        _slog.warn("keystore", "数据目录不可用，密钥库回退到 TEMP（重启后丢失）")
        return Path(tempfile.gettempdir())


def load() -> dict:
    """读取密钥库 → {salt_hex: {"key": hex, "strategy": str, "updated": ts}}。

    向后兼容：若新位置没有密钥库而旧位置（安装目录/stories-in-wx）存在，
    读取旧数据并尽力迁移到新位置；迁移失败（如旧目录只读）不影响读取。
    """
    p = store_path()
    legacy = _legacy_store_path()
    if not p.is_file():
        if not legacy.is_file() or legacy == p:
            return {}
        try:
            store = json.loads(_unprotect(legacy.read_bytes()).decode("utf-8"))
        except (OSError, ValueError) as e:
            _slog.warn("keystore", f"旧位置密钥库读取失败 path={legacy} err={e}")
            return {}
        try:
            save(store)  # 尽力迁移到新位置，失败不阻塞
        except OSError as e:
            _slog.detailed("keystore", f"迁移到 {p} 失败: {e}")
        return store
    try:
        return json.loads(_unprotect(p.read_bytes()).decode("utf-8"))
    except (OSError, ValueError) as e:
        # 审计 §3.5:136-139（本链路最值得修的一处）：主路径读取失败 = 整个
        # 密钥库静默清零，下次必然全量重收割
        _slog.warn("keystore", f"密钥库读取失败 path={p} err={e}（可尝试备份该文件后重跑）")
        return {}


def save(store: dict) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_bytes(_protect(json.dumps(store, ensure_ascii=False).encode("utf-8")))
        os.replace(tmp, p)
    except OSError as e:
        # 审计 §3.5:142-147：只补日志，不改异常传播语义（记完仍抛出），
        # 不要吞掉写入失败而掩盖数据丢失
        _slog.error("keystore", f"保存失败 path={p}: {e}")
        raise
    if not _USE_DPAPI:
        # 审计 S3：非 Windows 兜底路径是明文 JSON，权限至少收紧到仅属主可读写
        try:
            p.chmod(0o600)
        except OSError as e:
            _slog.detailed("keystore", f"chmod 0600 失败 {p}: {e}")


def insert(store: dict, salt: str, key: str, strategy: str) -> None:
    store[salt] = {"key": key.lower(), "strategy": strategy, "updated": int(time.time())}


def unique_keys(store: dict):
    seen = set()
    for rec in store.values():
        k = rec.get("key", "")
        if len(k) == 64 and k not in seen:
            seen.add(k)
            yield k