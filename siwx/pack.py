"""SIWXPAK1 — 自研加密备份容器（单文件、跨设备流转、密码加密）。

设计目标：
  - 与微信密钥体系/DPAPI **完全解耦**：包只认用户密码，换设备即可解开；
  - 流式读写：内存 O(单帧)（4 MiB），几 GB 的包也不会撑爆内存；
  - 防篡改 + 防错删：逐帧 AES-256-GCM 认证，manifest（加密区内）记录
    逐文件 SHA-256 与明文总长，读回时逐一对账。

格式（v1）：
  [8B  magic "SIWXPAK1"]
  [u16 格式版本 LE][u32 头部长度 LE][头部 JSON]     ← 明文：仅 KDF/加密参数，
                                                       不含任何账号/文件信息
  [12B manifest IV][u32 manifest 密文长 LE][密文+16B tag]
      manifest = AES-256-GCM(key, AAD="SIWXPAK1-manifest")
  --- 载荷：全部文件内容按 manifest 顺序拼接，按 4 MiB 分帧 ---
  [帧 i: 12B 随机 IV | 密文 | 16B tag]
      AAD = magic + u32 帧号 LE（帧序与容器绑定，防重排/抽帧）

密钥派生：scrypt(N=2^15, r=8, p=1, dklen=32)（内存难解，抗 GPU 撞库）。
参数存于明文头部，未来可升级且旧包可读。错密码在 manifest 认证处即失败。
"""
import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path

from Crypto.Cipher import AES

MAGIC = b"SIWXPAK1"
MANIFEST_AAD = b"SIWXPAK1-manifest"
FORMAT_VERSION = 1
FRAME_SIZE = 4 * 1024 * 1024
IV_SZ = 12
TAG_SZ = 16
DEFAULT_KDF = {"alg": "scrypt", "n": 1 << 15, "r": 8, "p": 1}


class PackError(Exception):
    """容器结构/校验错误（损坏、篡改、版本不符、路径非法）。"""


class PackPasswordError(PackError):
    """密码错误（manifest GCM 认证失败）。"""


def check_rel(rel: str) -> str:
    """载荷相对路径白名单化：统一分隔符、拒绝绝对路径/上跳/盘符。

    读包侧必须过这一关（manifest 虽加密，但防的是被篡改包的路径注入）。
    """
    r = str(rel or "").replace("\\", "/").strip("/")
    if not r or r.startswith(".") or ".." in r.split("/"):
        raise PackError(f"非法载荷路径: {rel!r}")
    if len(r) > 1 and r[1] == ":":
        raise PackError(f"非法载荷路径（盘符）: {rel!r}")
    return r


def derive_key(password: str, salt: bytes, params: dict) -> bytes:
    """密码 → 32 字节包密钥。参数范围双重校验（写读两侧同源）。"""
    if params.get("alg") != "scrypt":
        raise PackError(f"不支持的 KDF: {params.get('alg')!r}")
    try:
        n, r, p = int(params["n"]), int(params["r"]), int(params["p"])
    except (KeyError, TypeError, ValueError):
        raise PackError("KDF 参数缺失/非法") from None
    # 下限防弱化，上限防恶意参数触发超大内存分配（DoS）
    if n < 1 << 14 or r < 8 or p < 1 or n * r > (1 << 21):
        raise PackError(f"KDF 参数超出允许范围 n={n} r={r} p={p}")
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=32,
                          maxmem=256 * 1024 * 1024)


def _emit_frame(fout, key: bytes, idx: int, data: bytes):
    iv = os.urandom(IV_SZ)
    c = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=TAG_SZ)
    c.update(MAGIC + struct.pack("<I", idx))
    ct, tag = c.encrypt_and_digest(data)
    fout.write(iv)
    fout.write(ct)
    fout.write(tag)


def write_pack(path, password: str, files, meta: dict, progress=None) -> dict:
    """写包。files: [(rel, src_path)]；meta: 任意 JSON 安全字段（进加密 manifest）。

    两遍式：先逐文件算 SHA-256/大小（对账信息），再写头 + 加密 manifest +
    分帧密文。落盘走「临时文件 + 原子替换」，中途失败不留半个包。
    """
    if not isinstance(password, str) or len(password) < 8:
        raise PackError("密码至少 8 位（严格模式）")

    entries = []
    total = 0
    for rel, src in files:
        rel = check_rel(rel)
        src = Path(src)
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        size = src.stat().st_size
        entries.append({"rel": rel, "size": size, "sha256": h.hexdigest()})
        total += size

    salt = os.urandom(16)
    kdf = dict(DEFAULT_KDF, salt=salt.hex())
    key = derive_key(password, salt, kdf)
    manifest = {**meta, "payload_len": total, "files": entries}
    manifest_b = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".part",
                                    dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    n_frames = (total + FRAME_SIZE - 1) // FRAME_SIZE
    try:
        with open(tmp, "wb", buffering=1 << 20) as fout:
            header = {"format": FORMAT_VERSION, "kdf": kdf,
                      "cipher": {"alg": "aes-256-gcm", "frame_size": FRAME_SIZE,
                                 "iv_bytes": IV_SZ, "tag_bytes": TAG_SZ}}
            hb = json.dumps(header, ensure_ascii=False).encode("utf-8")
            fout.write(MAGIC)
            fout.write(struct.pack("<HI", FORMAT_VERSION, len(hb)))
            fout.write(hb)

            iv = os.urandom(IV_SZ)
            c = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=TAG_SZ)
            c.update(MANIFEST_AAD)
            ct, tag = c.encrypt_and_digest(manifest_b)
            fout.write(iv)
            fout.write(struct.pack("<I", len(ct) + TAG_SZ))
            fout.write(ct + tag)

            fidx = 0
            buf = bytearray()
            for src in (Path(s) for _rel, s in files):
                with open(src, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        buf += chunk
                        while len(buf) >= FRAME_SIZE:
                            _emit_frame(fout, key, fidx, bytes(buf[:FRAME_SIZE]))
                            del buf[:FRAME_SIZE]
                            fidx += 1
                        if progress and total and fidx % 8 == 0:
                            progress(min(99, fidx * FRAME_SIZE * 100 // total),
                                     f"已加密 {fidx} 帧")
            if buf:
                _emit_frame(fout, key, fidx, bytes(buf))
                fidx += 1
            if fidx != n_frames:
                raise PackError(f"帧数不一致 {fidx} != {n_frames}（内部错误）")
        os.replace(tmp, dst)
        tmp = None
        if progress:
            progress(100, f"包完成: {dst.name}")
        return {"path": str(dst), "size": dst.stat().st_size,
                "files": len(entries), "payload_len": total}
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def peek_pack(path) -> dict:
    """免密码读取明文头部（格式版本 + KDF 参数）。用于列出/识别包。"""
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != MAGIC:
            raise PackError("不是 SIWXPAK1 包（magic 不符）")
        ver, hlen = struct.unpack("<HI", f.read(6))
        if ver != FORMAT_VERSION:
            raise PackError(f"不支持的包格式版本: {ver}")
        header = json.loads(f.read(hlen).decode("utf-8"))
    return {"format": ver, "kdf": header.get("kdf"),
            "cipher": header.get("cipher")}


def read_pack(path, password: str, dest_dir, progress=None) -> dict:
    """读包：验密码 → 流式解帧 → 按_manifest 逐文件落盘 + SHA-256 对账。

    返回 manifest（含 mode/account/selection/files 等全部元数据）。
    每个文件走「临时文件 + 原子替换」，对账失败即中止。
    """
    dest = Path(dest_dir)
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != MAGIC:
            raise PackError("不是 SIWXPAK1 包（magic 不符）")
        ver, hlen = struct.unpack("<HI", f.read(6))
        if ver != FORMAT_VERSION:
            raise PackError(f"不支持的包格式版本: {ver}")
        header = json.loads(f.read(hlen).decode("utf-8"))
        frame_size = int(header["cipher"]["frame_size"])

        iv = f.read(IV_SZ)
        (mlen,) = struct.unpack("<I", f.read(4))
        if not (0 < mlen <= (1 << 26)):
            raise PackError(f"manifest 长度非法: {mlen}")
        blob = f.read(mlen)
        if len(blob) != mlen:
            raise PackError("manifest 被截断")
        key = derive_key(password, bytes.fromhex(header["kdf"]["salt"]),
                         header["kdf"])
        c = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=TAG_SZ)
        c.update(MANIFEST_AAD)
        try:
            manifest_b = c.decrypt_and_verify(blob[:-TAG_SZ], blob[-TAG_SZ:])
        except ValueError:
            raise PackPasswordError("密码错误（或 manifest 已损坏）") from None
        manifest = json.loads(manifest_b.decode("utf-8"))

        total = int(manifest["payload_len"])
        n_frames = (total + frame_size - 1) // frame_size
        state = {"buf": bytearray()}
        fidx = 0

        def fill() -> bool:
            nonlocal fidx
            if fidx >= n_frames:
                return False
            fiv = f.read(IV_SZ)
            ct_len = (frame_size if fidx < n_frames - 1
                      else total - frame_size * (n_frames - 1))
            ct = f.read(ct_len)
            tag = f.read(TAG_SZ)
            if len(fiv) != IV_SZ or len(ct) != ct_len or len(tag) != TAG_SZ:
                raise PackError(f"帧 {fidx} 被截断")
            fc = AES.new(key, AES.MODE_GCM, nonce=fiv, mac_len=TAG_SZ)
            fc.update(MAGIC + struct.pack("<I", fidx))
            try:
                pt = fc.decrypt_and_verify(ct, tag)
            except ValueError:
                raise PackError(f"帧 {fidx} 认证失败（包被篡改或损坏）") from None
            state["buf"] += pt
            fidx += 1
            return True

        def take(n: int) -> bytes:
            while len(state["buf"]) < n:
                if not fill():
                    raise PackError("载荷意外结束（帧数不足）")
            out = bytes(state["buf"][:n])
            del state["buf"][:n]
            return out

        done = 0
        for ent in manifest["files"]:
            rel = check_rel(ent["rel"])
            out_file = dest / Path(*rel.split("/"))
            out_file.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            left = int(ent["size"])
            fd, tmp_name = tempfile.mkstemp(prefix=out_file.name + ".",
                                            suffix=".part", dir=out_file.parent)
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                with open(tmp, "wb") as fo:
                    while left:
                        chunk = take(min(left, 1 << 20))
                        h.update(chunk)
                        fo.write(chunk)
                        left -= len(chunk)
                if h.hexdigest() != ent["sha256"]:
                    raise PackError(f"文件对账失败: {rel}")
                os.replace(tmp, out_file)
                tmp = None
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
            done += 1
            if progress:
                progress(done * 100 // len(manifest["files"]),
                         f"已还原 {done}/{len(manifest['files'])}")
        if f.read(1):
            raise PackError("包尾部有多余数据")
        if fidx != n_frames or state["buf"]:
            raise PackError("帧数/剩余字节不一致（载荷不完整）")
    return manifest
