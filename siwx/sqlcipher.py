"""SQLCipher 4 页级编解码与密钥验证原语。

逻辑参考原 pc_wechat_exp（已验证实现），独立整理：
page_size=4096、AES-256-CBC、HMAC-SHA512、PBKDF2-SHA512×2、
reserve=80、每页 IV 存于 reserve 区前 16 字节、page1 前 16 字节为 salt。
验证一个候选密钥只需文件前 4KB —— 全系统的验证咽喉。
"""
import hashlib
import hmac
import os
import shutil
import struct
import tempfile
from pathlib import Path

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
IV_SZ = 16
HMAC_SZ = 64
SQLITE_HDR = b"SQLite format 3\x00"


def parse_key(hex_key: str) -> bytes:
    h = hex_key.strip().lower()
    if len(h) != KEY_SZ * 2:
        raise ValueError("密钥长度不是 64 位 hex")
    return bytes.fromhex(h)


def verify_enc_key(enc_key: bytes, page1: bytes) -> bool:
    """SQLCipher 4 page-1 HMAC 校验（恒时比较）。"""
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored = page1[PAGE_SZ - HMAC_SZ:]
    hm = hmac.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hmac.compare_digest(hm.digest(), stored)


class DbEntry:
    __slots__ = ("rel", "path", "size", "salt_hex", "page1")

    def __init__(self, rel, path, size, salt_hex, page1):
        self.rel = rel
        self.path = path
        self.size = size
        self.salt_hex = salt_hex
        self.page1 = page1


def _read_page1(path: Path):
    try:
        with open(path, "rb") as f:
            page1 = f.read(PAGE_SZ)
    except OSError:
        # 微信占用中：复制到临时文件再读
        try:
            tmp = Path(tempfile.gettempdir()) / f"siwx_p1_{os.getpid()}.tmp"
            shutil.copy2(path, tmp)
            with open(tmp, "rb") as f:
                page1 = f.read(PAGE_SZ)
            tmp.unlink(missing_ok=True)
        except OSError:
            return None
    if len(page1) < PAGE_SZ or page1.count(0) == PAGE_SZ:
        return None
    return page1


def collect_db_files(db_dir: str):
    """递归收集 db_storage 下全部 .db（排除 -wal/-shm）。"""
    root = Path(db_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在: {db_dir}")
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < PAGE_SZ:
                continue
            page1 = _read_page1(p)
            if page1 is None:
                continue
            rel = str(p.relative_to(root))
            out.append(DbEntry(rel, p, size, page1[:SALT_SZ].hex(), page1))
    out.sort(key=lambda e: e.rel)
    return out


def decrypt_database(src: Path, dst: Path, enc_key: bytes, progress=None) -> int:
    """流式整库解密：页读 → AES-256-CBC → 明文 SQLite 写出。返回总页数。

    优化（v0.3.0）:
    - 内联 CBC XOR：消除函数调用开销
    - 进度回调降频：每 100 页回调一次，减少 Python 开销
    - 预计算常量：CT_LEN / body_len 提到循环外
    """
    from Crypto.Cipher import AES

    tmp_copy = None
    try:
        fin = open(src, "rb", buffering=4 * 1024 * 1024)
    except OSError:
        tmp_copy = Path(tempfile.gettempdir()) / f"siwx_db_{os.getpid()}.tmp"
        shutil.copy2(src, tmp_copy)
        fin = open(tmp_copy, "rb", buffering=4 * 1024 * 1024)

    try:
        size = os.fstat(fin.fileno()).st_size
        if size < PAGE_SZ:
            raise ValueError("文件不足一页")
        page1 = fin.read(PAGE_SZ)
        if not verify_enc_key(enc_key, page1):
            raise ValueError("page1 HMAC 验证失败（密钥不匹配）")
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        total_pages = (size + PAGE_SZ - 1) // PAGE_SZ

        with open(dst, "wb", buffering=4 * 1024 * 1024) as fout:
            aes = AES.new(enc_key, AES.MODE_ECB)
            body_len = PAGE_SZ - RESERVE_SZ  # 4016
            CT_LEN = body_len - SALT_SZ      # 4000

            # 页 1
            iv = page1[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
            ct = page1[SALT_SZ: body_len]
            raw = aes.decrypt(ct)
            prev = iv + ct[:CT_LEN - 16]
            n = int.from_bytes(raw, "little") ^ int.from_bytes(prev, "little")
            pt = n.to_bytes(CT_LEN, "little")
            fout.write(SQLITE_HDR)
            fout.write(pt)
            fout.write(b"\x00" * RESERVE_SZ)
            if progress:
                progress(1, total_pages)

            pgno = 1
            zeros = b"\x00" * RESERVE_SZ
            for chunk in iter(lambda: fin.read(PAGE_SZ), b""):
                pgno += 1
                if len(chunk) < PAGE_SZ:
                    chunk = chunk + b"\x00" * (PAGE_SZ - len(chunk))
                iv = chunk[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
                ct = chunk[SALT_SZ: body_len]
                raw = aes.decrypt(ct)
                prev = iv + ct[:CT_LEN - 16]
                n = int.from_bytes(raw, "little") ^ int.from_bytes(prev, "little")
                pt = n.to_bytes(CT_LEN, "little")
                fout.write(pt)
                fout.write(zeros)
                if progress and pgno % 100 == 0:
                    progress(pgno, total_pages)
            if progress:
                progress(total_pages, total_pages)
            return total_pages
    finally:
        fin.close()
        if tmp_copy:
            tmp_copy.unlink(missing_ok=True)
