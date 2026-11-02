"""AES-256-CBC 后端。

优先使用 pycryptodome（若已安装，速度快）；否则在 Windows 上用 CNG
(bcrypt.dll) 的 ctypes 实现，保持零第三方依赖。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from ..errors import DecryptError

try:  # pycryptodome 是可选加速项，缺失时回退 CNG
    from Crypto.Cipher import AES as _PyAES  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - 取决于本机环境
    _PyAES = None

BLOCK_SIZE = 16

_bcrypt: ctypes.WinDLL | None = None


def _load_bcrypt() -> ctypes.WinDLL:
    """加载并声明 bcrypt.dll 的函数签名（只做一次）。"""
    global _bcrypt
    if _bcrypt is not None:
        return _bcrypt
    if sys.platform != "win32":
        raise DecryptError(
            "缺少 AES 后端：请安装 pycryptodome（pip install pycryptodome）"
        )
    B = ctypes.WinDLL("bcrypt", use_last_error=True)
    B.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.ULONG,
    ]
    B.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
    B.BCryptCloseAlgorithmProvider.argtypes = [wintypes.HANDLE, wintypes.ULONG]
    B.BCryptCloseAlgorithmProvider.restype = ctypes.c_long
    B.BCryptSetProperty.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    B.BCryptSetProperty.restype = ctypes.c_long
    B.BCryptGenerateSymmetricKey.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    B.BCryptGenerateSymmetricKey.restype = ctypes.c_long
    B.BCryptDestroyKey.argtypes = [wintypes.HANDLE]
    B.BCryptDestroyKey.restype = ctypes.c_long
    for name in ("BCryptEncrypt", "BCryptDecrypt"):
        fn = getattr(B, name)
        fn.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.ULONG,
        ]
        fn.restype = ctypes.c_long
    _bcrypt = B
    return B


class AesCbc:
    """固定密钥的 AES-CBC 加解密器，可重复使用（每页传自己的 IV）。"""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16/24/32 bytes")
        self.key = bytes(key)
        self._hAlg = None
        self._hKey = None
        if _PyAES is not None:
            self._backend = "pycryptodome"
        else:
            self._backend = "cng"
            self._init_cng()

    # ------------------------------------------------------------------ CNG
    def _init_cng(self) -> None:
        B = _load_bcrypt()
        h_alg = wintypes.HANDLE()
        status = B.BCryptOpenAlgorithmProvider(
            ctypes.byref(h_alg), "AES", None, 0
        )
        if status != 0:
            raise DecryptError(f"BCryptOpenAlgorithmProvider 失败: 0x{status & 0xFFFFFFFF:08x}")
        self._hAlg = h_alg
        mode = "ChainingModeCBC".encode("utf-16-le")
        status = B.BCryptSetProperty(
            h_alg, "ChainingMode", ctypes.c_char_p(mode), len(mode), 0
        )
        if status != 0:
            self.close()
            raise DecryptError(f"BCryptSetProperty(CBC) 失败: 0x{status & 0xFFFFFFFF:08x}")
        h_key = wintypes.HANDLE()
        status = B.BCryptGenerateSymmetricKey(
            h_alg,
            ctypes.byref(h_key),
            None,
            0,
            ctypes.c_char_p(self.key),
            len(self.key),
            0,
        )
        if status != 0:
            self.close()
            raise DecryptError(f"BCryptGenerateSymmetricKey 失败: 0x{status & 0xFFFFFFFF:08x}")
        self._hKey = h_key

    def _cng_crypt(self, fn, iv: bytes, data: bytes) -> bytes:
        if len(iv) != BLOCK_SIZE:
            raise DecryptError("IV 长度必须为 16 字节")
        if len(data) % BLOCK_SIZE:
            raise DecryptError(f"CBC 数据长度必须为 16 的倍数，实际 {len(data)}")
        if not data:
            return b""
        out = ctypes.create_string_buffer(len(data))
        result = wintypes.ULONG()
        ivbuf = ctypes.create_string_buffer(bytes(iv), BLOCK_SIZE)
        status = fn(
            self._hKey,
            ctypes.c_char_p(data),
            len(data),
            None,
            ctypes.cast(ivbuf, ctypes.c_void_p),
            BLOCK_SIZE,
            ctypes.cast(out, ctypes.c_void_p),
            len(data),
            ctypes.byref(result),
            0,
        )
        if status != 0:
            raise DecryptError(f"BCrypt 加解密失败: 0x{status & 0xFFFFFFFF:08x}")
        return out.raw[: result.value]

    # ------------------------------------------------------------ 公共接口
    def decrypt(self, iv: bytes, data: bytes) -> bytes:
        if self._backend == "pycryptodome":
            try:
                return _PyAES.new(self.key, _PyAES.MODE_CBC, iv).decrypt(data)
            except ValueError as exc:
                raise DecryptError(f"AES 解密失败: {exc}") from exc
        return self._cng_crypt(self._bcrypt().BCryptDecrypt, iv, data)

    def encrypt(self, iv: bytes, data: bytes) -> bytes:
        if self._backend == "pycryptodome":
            return _PyAES.new(self.key, _PyAES.MODE_CBC, iv).encrypt(data)
        return self._cng_crypt(self._bcrypt().BCryptEncrypt, iv, data)

    def _bcrypt(self):
        return _load_bcrypt()

    def close(self) -> None:
        if self._backend != "cng":
            return
        B = _load_bcrypt()
        if self._hKey:
            B.BCryptDestroyKey(self._hKey)
            self._hKey = None
        if self._hAlg:
            B.BCryptCloseAlgorithmProvider(self._hAlg, 0)
            self._hAlg = None

    def __del__(self):  # pragma: no cover - 解释器退出时可能已卸载
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class AesEcb:
    """AES-ECB（图片 .dat 使用），无 IV、无 padding。"""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16/24/32 bytes")
        self.key = bytes(key)
        self._hAlg = None
        self._hKey = None
        if _PyAES is not None:
            self._backend = "pycryptodome"
        else:
            self._backend = "cng"
            self._init_cng()

    def _init_cng(self) -> None:
        B = _load_bcrypt()
        h_alg = wintypes.HANDLE()
        status = B.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
        if status != 0:
            raise DecryptError(f"BCryptOpenAlgorithmProvider 失败: 0x{status & 0xFFFFFFFF:08x}")
        self._hAlg = h_alg
        mode = "ChainingModeECB".encode("utf-16-le")
        status = B.BCryptSetProperty(h_alg, "ChainingMode", ctypes.c_char_p(mode), len(mode), 0)
        if status != 0:
            self.close()
            raise DecryptError(f"BCryptSetProperty(ECB) 失败: 0x{status & 0xFFFFFFFF:08x}")
        h_key = wintypes.HANDLE()
        status = B.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, ctypes.c_char_p(self.key), len(self.key), 0
        )
        if status != 0:
            self.close()
            raise DecryptError(f"BCryptGenerateSymmetricKey 失败: 0x{status & 0xFFFFFFFF:08x}")
        self._hKey = h_key

    def _cng_crypt(self, fn, data: bytes) -> bytes:
        if len(data) % BLOCK_SIZE:
            raise DecryptError(f"ECB 数据长度必须为 16 的倍数，实际 {len(data)}")
        if not data:
            return b""
        out = ctypes.create_string_buffer(len(data))
        result = wintypes.ULONG()
        status = fn(
            self._hKey,
            ctypes.c_char_p(data),
            len(data),
            None,
            None,
            0,
            ctypes.cast(out, ctypes.c_void_p),
            len(data),
            ctypes.byref(result),
            0,
        )
        if status != 0:
            raise DecryptError(f"BCrypt 加解密失败: 0x{status & 0xFFFFFFFF:08x}")
        return out.raw[: result.value]

    def decrypt(self, data: bytes) -> bytes:
        if self._backend == "pycryptodome":
            return _PyAES.new(self.key, _PyAES.MODE_ECB).decrypt(data)
        return self._cng_crypt(_load_bcrypt().BCryptDecrypt, data)

    def encrypt(self, data: bytes) -> bytes:
        if self._backend == "pycryptodome":
            return _PyAES.new(self.key, _PyAES.MODE_ECB).encrypt(data)
        return self._cng_crypt(_load_bcrypt().BCryptEncrypt, data)

    def close(self) -> None:
        if self._backend != "cng":
            return
        B = _load_bcrypt()
        if self._hKey:
            B.BCryptDestroyKey(self._hKey)
            self._hKey = None
        if self._hAlg:
            B.BCryptCloseAlgorithmProvider(self._hAlg, 0)
            self._hAlg = None

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def decrypt_ecb(key: bytes, data: bytes) -> bytes:
    with AesEcb(key) as aes:
        return aes.decrypt(data)


def encrypt_ecb(key: bytes, data: bytes) -> bytes:
    with AesEcb(key) as aes:
        return aes.encrypt(data)


def backend_name() -> str:
    """返回当前生效的后端名，便于 CLI 展示。"""
    return "pycryptodome" if _PyAES is not None else "cng"


def decrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    with AesCbc(key) as aes:
        return aes.decrypt(iv, data)


def encrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    with AesCbc(key) as aes:
        return aes.encrypt(iv, data)
