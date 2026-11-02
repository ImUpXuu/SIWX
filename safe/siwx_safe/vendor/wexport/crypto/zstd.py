"""zstd 解压（微信 4.x 的消息正文常用 zstd 压缩）。

Python 3.13 还没有内置 ``compression.zstd``，也不希望强制安装第三方包，
因此这里按下面的顺序找解压实现：

1. ``pyzstd`` / ``zstandard``（用户机器上装了就用，最快）；
2. 系统里已有的 ``libzstd.dll`` / ``zstd.dll``，用 ctypes 调用流式 API；
3. 都没有时 ``decompress()`` 返回 ``None``，调用方降级为保留原始内容。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import sys
from ctypes import POINTER, byref, c_size_t, c_void_p, cast, create_string_buffer
from pathlib import Path

__all__ = ["available", "backend_name", "decompress", "is_zstd"]

MAGIC = b"\x28\xb5\x2f\xfd"

_KNOWN_DIRS = (
    r"C:\Program Files\Git\ucrt64\bin",
    r"C:\Program Files\Git\usr\bin",
    r"C:\Program Files\Git\mingw64\bin",
    r"C:\Program Files\Calibre2\app\bin",
    r"C:\Program Files (x86)\Calibre2\app\bin",
    r"E:\Program Files\BaiduNetdisk\module\ImageViewer",
    r"E:\Program Files\BaiduNetdisk\module\ImageViewer\lib",
    r"D:\Program Files\Tencent\Weixin",
    r"C:\Program Files\Tencent\Weixin",
    r"C:\Program Files\7-Zip",
)

_MAX_INPUT = 256 * 1024 * 1024


class _InBuffer(ctypes.Structure):
    _fields_ = [("src", c_void_p), ("size", c_size_t), ("pos", c_size_t)]


class _OutBuffer(ctypes.Structure):
    _fields_ = [("dst", c_void_p), ("size", c_size_t), ("pos", c_size_t)]


_lib = None
_lib_error: str | None = None
_loaded = False
_py_impl = None


def _candidate_paths() -> list[str]:
    paths: list[str] = []
    env = os.environ.get("WEXPORT_LIBZSTD")
    if env:
        paths.append(env)
    roots = []
    try:
        roots.append(Path(sys.executable).resolve().parent)
    except OSError:
        pass
    roots.append(Path(__file__).resolve().parents[2])
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    for root in roots:
        for name in ("libzstd.dll", "zstd.dll", "libzstd-1.dll"):
            paths.append(str(root / name))
    for folder in _KNOWN_DIRS:
        if os.path.isdir(folder):
            for name in ("libzstd.dll", "zstd.dll", "libzstd-1.dll"):
                paths.append(os.path.join(folder, name))
    for name in ("libzstd.dll", "zstd.dll"):
        found = shutil.which(name)
        if found:
            paths.append(found)
    try:
        found = ctypes.util.find_library("zstd")
        if found:
            paths.append(found)
    except (OSError, AttributeError):
        pass
    return paths


def _load_lib():
    global _lib, _lib_error, _loaded
    if _loaded:
        return _lib
    _loaded = True
    seen: set[str] = set()
    for path in _candidate_paths():
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        try:
            lib = ctypes.WinDLL(path)
        except OSError as exc:  # 不是合法 DLL 或位数不对
            _lib_error = f"{path}: {exc}"
            continue
        try:
            lib.ZSTD_createDCtx.restype = c_void_p
            lib.ZSTD_freeDCtx.argtypes = [c_void_p]
            lib.ZSTD_decompressStream.argtypes = [
                c_void_p,
                POINTER(_OutBuffer),
                POINTER(_InBuffer),
            ]
            lib.ZSTD_decompressStream.restype = c_size_t
            lib.ZSTD_isError.argtypes = [c_size_t]
            lib.ZSTD_isError.restype = ctypes.c_uint
        except AttributeError as exc:
            _lib_error = f"{path}: {exc}"
            continue
        _lib = lib
        _lib.path = path  # type: ignore[attr-defined]
        _lib_error = None
        return _lib
    try:  # 最后交给系统搜索路径
        lib = ctypes.CDLL("libzstd.dll")
        lib.ZSTD_decompressStream.restype = c_size_t
        _lib = lib
        _lib.path = "libzstd.dll"  # type: ignore[attr-defined]
    except OSError as exc:
        _lib_error = _lib_error or str(exc)
    return _lib


def _py_backend():
    global _py_impl
    if _py_impl is not None:
        return _py_impl or None
    try:
        import zstandard  # type: ignore

        _py_impl = lambda data: zstandard.ZstdDecompressor().decompress(  # noqa: E731
            data, max_output_size=_MAX_INPUT
        )
        return _py_impl
    except ImportError:
        pass
    try:
        import pyzstd  # type: ignore

        _py_impl = pyzstd.decompress
        return _py_impl
    except ImportError:
        pass
    _py_impl = False
    return None


def is_zstd(data: bytes) -> bool:
    """是否 zstd 帧（含 skippable 帧）。"""
    if len(data) < 4:
        return False
    if data[:4] == MAGIC:
        return True
    magic = int.from_bytes(data[:4], "little")
    return 0x184D2A50 <= magic <= 0x184D2A5F


def backend_name() -> str:
    impl = _py_backend()
    if impl is not None:
        try:
            import zstandard  # noqa: F401

            return "zstandard"
        except ImportError:
            return "pyzstd"
    lib = _load_lib()
    if lib is not None:
        return "libzstd"
    return "none"


def available() -> bool:
    return _py_backend() is not None or _load_lib() is not None


def _decompress_ctypes(data: bytes) -> bytes | None:
    lib = _load_lib()
    if lib is None:
        return None
    dctx = lib.ZSTD_createDCtx()
    if not dctx:
        return None
    src = create_string_buffer(data, len(data))
    in_buf = _InBuffer(cast(src, c_void_p), len(data), 0)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = 256 * 1024
            out = create_string_buffer(chunk)
            out_buf = _OutBuffer(cast(out, c_void_p), chunk, 0)
            ret = lib.ZSTD_decompressStream(dctx, byref(out_buf), byref(in_buf))
            if lib.ZSTD_isError(ret):
                return None
            if out_buf.pos:
                total += out_buf.pos
                if total > _MAX_INPUT:
                    return None
                chunks.append(out.raw[: out_buf.pos])
            if ret == 0 and in_buf.pos >= len(data):
                break
            if in_buf.pos >= len(data) and out_buf.pos == 0:
                break
    finally:
        lib.ZSTD_freeDCtx(dctx)
    return b"".join(chunks)


def decompress(data: bytes) -> bytes | None:
    """解压一段 zstd 数据；没有可用后端或数据损坏时返回 None。"""
    if not data:
        return None
    if not is_zstd(data):
        return None
    impl = _py_backend()
    if impl is not None:
        try:
            out = impl(data)
        except Exception:  # noqa: BLE001 - 第三方库异常类型不统一
            out = None
        if out:
            return bytes(out)
    return _decompress_ctypes(data)
