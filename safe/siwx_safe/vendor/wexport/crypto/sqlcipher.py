"""SQLCipher 数据库解密：页格式、密钥派生、WAL 重放。

微信三种数据源的页格式差异只在页大小与密钥派生方式：

* PC 微信 4.x: 页 4096，文件头 16 字节盐，原始 32 字节密钥。
* PC 微信 3.x: 页 1024，盐为全 0（文件头前 16 字节是密文），原始 32 字节密钥。
* 安卓 EnMicroMsg.db: 页 1024，文件头 16 字节盐，密码 = md5(IMEI+uin)[:7]，
  经 PBKDF2-HMAC-SHA1(64000 轮) 派生密钥。

页布局（页大小 P，保留区 R）::

    SQLCipher 3（R=48）: [第 1 页: 16 字节盐(明文)] [密文 P-16-48] [HMAC 32] [IV 16]
                         [其余页:              密文 P-48]     [HMAC 32] [IV 16]
    SQLCipher 4（R=80）: [第 1 页: 16 字节盐(明文)] [密文 P-16-80] [IV 16] [HMAC 64]
                         [其余页:              密文 P-80]     [IV 16] [HMAC 64]

解密后把页尾 R 字节补零，得到标准 SQLite 页。HMAC 不校验。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..errors import DecryptError
from .aes import AesCbc

SQLITE_MAGIC = b"SQLite format 3\x00"
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 32  # SQLCipher 3
HMAC_SIZE4 = 64  # SQLCipher 4（HMAC-SHA512）
RESERVE = 48  # SQLCipher 3: 16 IV + 32 HMAC
RESERVE4 = 80  # SQLCipher 4: 16 IV + 64 HMAC

WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683

ProgressFn = Callable[[int, int], None]


@dataclass(frozen=True)
class CipherProfile:
    """一种数据源的页参数。"""

    name: str
    page_size: int
    reserve: int = RESERVE
    raw_key: bool = True  # True: 密钥就是 32 字节；False: 由密码 PBKDF2 派生
    kdf_iter: int = 64000

    def derive(self, secret: bytes, salt: bytes) -> bytes:
        if self.raw_key:
            if len(secret) != 32:
                raise DecryptError(
                    f"{self.name}: 需要 32 字节原始密钥，实际 {len(secret)} 字节"
                )
            return secret
        if len(secret) != 32 and len(secret) != 7:
            # 安卓端密码固定 7 位，但允许调用方传任意长度密码
            if not secret:
                raise DecryptError("密码为空，无法派生密钥")
        return hashlib.pbkdf2_hmac(
            "sha1", secret, salt or b"\x00" * SALT_SIZE, self.kdf_iter, 32
        )


WIN4 = CipherProfile("win4", 4096, reserve=RESERVE4)
WIN3 = CipherProfile("win3", 1024, reserve=RESERVE)
ANDROID = CipherProfile("android", 1024, reserve=RESERVE, raw_key=False)


def read_shared(path: Path | str) -> bytes:
    """读取文件。微信运行中文件被锁定，但 Python 的共享读通常可用。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise
    except PermissionError as exc:
        raise DecryptError(f"无法读取 {path}：文件被占用，请先退出微信") from exc


def read_shared_range(path: Path | str, offset: int, size: int) -> bytes:
    """只读文件的一段（用于大库探测页头）。"""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)
    except FileNotFoundError:
        raise
    except PermissionError as exc:
        raise DecryptError(f"无法读取 {path}：文件被占用，请先退出微信") from exc


def file_salt(path: Path | str) -> bytes:
    """文件头前 16 字节。Windows 3.x 为全 0；4.x 与安卓为随机盐。"""
    head = read_shared_range(path, 0, SALT_SIZE)
    if len(head) < SALT_SIZE:
        raise DecryptError(f"{path}: 文件过小，不是有效的数据库")
    return head


def verify_key(path: Path | str, secret: bytes, profile: CipherProfile) -> bool:
    """用第一页页头校验密钥是否正确。"""
    try:
        salt = file_salt(path)
    except (DecryptError, OSError):
        return False
    reserves = [profile.reserve]
    for r in (RESERVE4, RESERVE):
        if r != profile.reserve:
            reserves.append(r)
    for page_size in _candidate_sizes(profile.page_size):
        for reserve in reserves:
            prof = CipherProfile(
                profile.name,
                page_size,
                reserve,
                profile.raw_key,
                profile.kdf_iter,
            )
            try:
                key = prof.derive(secret, salt)
                page = read_shared_range(path, 0, page_size)
                if len(page) < page_size:
                    continue
                aes = AesCbc(key)
                try:
                    body = _page1_body(aes, page, prof)
                finally:
                    aes.close()
            except (DecryptError, ValueError):
                continue
            if _looks_like_sqlite_body(body, page_size):
                return True
    return False


def _candidate_sizes(preferred: int) -> list[int]:
    sizes = [preferred, 4096, 1024, 2048, 8192, 512]
    seen: list[int] = []
    for s in sizes:
        if s not in seen:
            seen.append(s)
    return seen


def detect_profile(path: Path | str, secret: bytes) -> CipherProfile | None:
    """按页头校验自动识别页大小与密钥模式，返回匹配的 profile。"""
    try:
        salt = file_salt(path)
    except (DecryptError, OSError):
        return None
    for raw in (True, False):
        base = WIN4 if raw else ANDROID
        reserves = (RESERVE4, RESERVE) if raw else (RESERVE,)
        for reserve in reserves:
            for page_size in _candidate_sizes(base.page_size):
                prof = CipherProfile(
                    "win4" if raw else "android",
                    page_size,
                    reserve,
                    raw,
                    base.kdf_iter,
                )
                try:
                    key = prof.derive(secret, salt)
                    page = read_shared_range(path, 0, page_size)
                    if len(page) < page_size:
                        continue
                    aes = AesCbc(key)
                    try:
                        body = _page1_body(aes, page, prof)
                    finally:
                        aes.close()
                except (DecryptError, ValueError):
                    continue
                if _looks_like_sqlite_body(body, page_size):
                    return prof
    return None


def _page1_body(aes: AesCbc, page: bytes, profile: CipherProfile) -> bytes:
    """解密第 1 页的密文主体（对应明文页 offset 16 之后的内容）。"""
    p, r = profile.page_size, profile.reserve
    iv = page[p - r : p - r + IV_SIZE]
    return aes.decrypt(iv, page[SALT_SIZE : p - r])


def _looks_like_sqlite_body(body: bytes, page_size: int) -> bool:
    """页 1 明文 offset 16 起是文件头字段：页大小、读写版本、payload 参数。"""
    if len(body) < 8:
        return False
    size_field = 1 if page_size == 65536 else page_size
    if body[0:2] != struct.pack(">H", size_field):
        return False
    if body[2] not in (1, 2) or body[3] not in (1, 2):
        return False
    return body[5] == 64 and body[6] == 32 and body[7] == 32


def decrypt_page(aes: AesCbc, page: bytes, page_no: int, profile: CipherProfile) -> bytes:
    """解密一页，返回标准 SQLite 明文页（页尾保留区补零）。"""
    p = profile.page_size
    r = profile.reserve
    if len(page) != p:
        raise DecryptError(f"页长度 {len(page)} != {p}")
    iv = page[p - r : p - r + IV_SIZE]
    if page_no == 1:
        body = page[SALT_SIZE : p - r]
        plain = aes.decrypt(iv, body)
        return SQLITE_MAGIC + plain + b"\x00" * r
    body = page[: p - r]
    plain = aes.decrypt(iv, body)
    return plain + b"\x00" * r


def decrypt_database(
    path: Path | str,
    secret: bytes,
    profile: CipherProfile,
    *,
    use_wal: bool = True,
    progress: ProgressFn | None = None,
) -> bytes:
    """解密整个数据库并（可选）重放 WAL，返回内存中的 SQLite 字节。"""
    path = Path(path)
    salt = file_salt(path)
    key = profile.derive(secret, salt) if not profile.raw_key else secret
    data = read_shared(path)
    p = profile.page_size
    npages = len(data) // p
    if npages == 0:
        raise DecryptError(f"{path}: 文件为空或页大小不匹配")
    aes = AesCbc(key)
    try:
        out = bytearray(npages * p)
        for i in range(npages):
            start = i * p
            out[start : start + p] = decrypt_page(aes, data[start : start + p], i + 1, profile)
            if progress is not None:
                progress(i + 1, npages)
    finally:
        aes.close()
    if use_wal:
        wal_path = path.with_name(path.name + "-wal")
        if wal_path.exists():
            frames = parse_wal(wal_path, key, profile)
            if frames:
                out = bytearray(apply_wal(bytes(out), frames, p))
    return bytes(out)


_TEMP_DIR: Path | None = None


def set_temp_dir(path: Path | str | None) -> None:
    """设置临时明文目录；设置后 open_sqlite 会落盘临时文件并在关闭时删除。"""
    global _TEMP_DIR
    _TEMP_DIR = Path(path) if path else None


class _TempConnection(sqlite3.Connection):
    _path: str = ""

    def close(self) -> None:  # pragma: no cover - 依赖临时文件
        super().close()
        try:
            os.unlink(self._path)
        except OSError:
            pass


def open_sqlite(db_bytes: bytes) -> sqlite3.Connection:
    """把解密后的字节装进 SQLite 连接。

    默认使用内存库（不落盘明文）；调用 set_temp_dir() 后改为临时文件。
    """
    if not db_bytes:
        raise DecryptError("数据库为空")
    if _TEMP_DIR is not None:
        _TEMP_DIR.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=".db", dir=str(_TEMP_DIR))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(db_bytes)
            conn = sqlite3.connect(name, factory=_TempConnection, check_same_thread=False)
        except Exception as exc:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise DecryptError(f"无法打开临时数据库: {exc}") from exc
        conn._path = name
        return conn
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    # 解密出的页头仍是 WAL 模式（读写版本字段=2），内存库无法建 -wal 文件，
    # deserialize 会在首次查询时报 "unable to open database file"。
    # WAL 已经由 decrypt_database 重放，这里把日志模式改标为 legacy 即可。
    if len(db_bytes) >= 20 and db_bytes[18] == 2 and db_bytes[19] == 2:
        db_bytes = db_bytes[:18] + b"\x01\x01" + db_bytes[20:]
    try:
        conn.deserialize(db_bytes)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise DecryptError(f"SQLite 无法打开解密结果: {exc}") from exc
    return conn


# --------------------------------------------------------------------- WAL
def parse_wal(
    path: Path | str, key: bytes, profile: CipherProfile
) -> list[tuple[int, int, bytes]]:
    """解析 WAL 文件，返回 [(page_no, db_size, 明文页)]。

    salt 与 WAL 头不一致的帧属于旧周期（已 checkpoint），直接停止。
    不完整的尾帧（微信正在写）也停止。checksum 不校验。
    """
    data = read_shared(path)
    if len(data) < WAL_HEADER_SIZE:
        return []
    magic, _version, page_size, _seq, salt1, salt2 = struct.unpack(">6I", data[:24])
    if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
        return []
    if page_size <= 0:
        return []
    p = page_size
    frames: list[tuple[int, int, bytes]] = []
    aes = AesCbc(key)
    try:
        offset = WAL_HEADER_SIZE
        while offset + WAL_FRAME_HEADER_SIZE + p <= len(data):
            page_no, db_size, f_salt1, f_salt2 = struct.unpack(
                ">4I", data[offset : offset + 16]
            )
            if (f_salt1, f_salt2) != (salt1, salt2):
                break
            if page_no == 0:
                break
            page = data[
                offset + WAL_FRAME_HEADER_SIZE : offset + WAL_FRAME_HEADER_SIZE + p
            ]
            try:
                plain = decrypt_page(aes, page, page_no, profile)
            except DecryptError:
                break
            frames.append((page_no, db_size, plain))
            offset += WAL_FRAME_HEADER_SIZE + p
    finally:
        aes.close()
    return frames


def apply_wal(
    db_bytes: bytes, frames: Iterable[tuple[int, int, bytes]], page_size: int
) -> bytes:
    """把 WAL 帧覆盖到解密后的主库上，并按最后 commit 的 db_size 截断。"""
    frames = list(frames)
    if not frames:
        return db_bytes
    pages: dict[int, bytes] = {}
    committed: int | None = None
    for page_no, db_size, plain in frames:
        pages[page_no] = plain
        if db_size:
            committed = db_size
    current = len(db_bytes) // page_size
    target = committed if committed else max([current, *pages.keys()])
    out = bytearray(db_bytes)
    want = target * page_size
    if want > len(out):
        out.extend(b"\x00" * (want - len(out)))
    elif want < len(out):
        del out[want:]
    for page_no, plain in pages.items():
        if 1 <= page_no <= target:
            start = (page_no - 1) * page_size
            out[start : start + page_size] = plain
    return bytes(out)
