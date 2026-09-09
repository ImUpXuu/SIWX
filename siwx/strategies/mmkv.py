"""策略：MMKV 离线提取（无需微信运行，纯本地文件）。

逻辑移植自原 pc_wechat_exp.extract_keys_from_mmkv（已验证）：
db_storage/MMKV/f<hex>tinfo.mmkv 为 AES-GCM 加密，密钥派生
MD5(str(code)+清洗后wxid).hexdigest()[:16]（16 个 ASCII 字节 = AES-128 key）；
解出的明文内含数据库相对路径与相邻的 64 位 hex 密钥。
"""
import hashlib
import os
import re
import struct
from pathlib import Path

from Crypto.Cipher import AES

from siwx.sqlcipher import verify_enc_key

CODE_RE = re.compile(r"^f([0-9a-fA-F]+)tinfo\.mmkv$")
HEX64_RE = re.compile(rb"([0-9a-fA-F]{64})")


def clean_wxid(wxid: str) -> str:
    if not wxid or not wxid.startswith("wxid_"):
        return wxid
    parts = wxid.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:2])
    return wxid


def _derive_candidates(code: int, wxid: str):
    code_str = str(code)
    out = [("code+wxid", hashlib.md5((code_str + wxid).encode()).hexdigest()[:16].encode())]
    out.append(("wxid+code", hashlib.md5((wxid + code_str).encode()).hexdigest()[:16].encode()))
    out.append(("code+wxid_full", hashlib.md5((code_str + wxid).encode()).hexdigest().encode()))
    for trunc in (16, 32):
        out.append((f"sha256:{trunc}",
                    hashlib.sha256((code_str + wxid).encode()).hexdigest()[:trunc].encode()))
    return out


def _gcm_decrypt(key: bytes, iv: bytes, ct: bytes, tag: bytes):
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        return cipher.decrypt_and_verify(ct, tag)
    except (ValueError, KeyError):
        return None


def extract(ctx) -> int:
    """统一策略入口。ctx: {db_dir, entries, page1_by_salt, key_map, attrib, log}"""
    db_dir = ctx["db_dir"]
    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]

    mmkv_dir = Path(db_dir) / "MMKV"
    if not mmkv_dir.is_dir():
        return 0
    wxid_full = os.path.basename(os.path.dirname(os.path.abspath(str(db_dir))))
    wxid = clean_wxid(wxid_full)
    if not wxid.startswith("wxid_"):
        log("[mmkv] 无法从路径确定 wxid，跳过 MMKV 提取")
        return 0
    log(f"[mmkv] wxid: {wxid_full} → {wxid}")

    rel_by_salt = {}
    for e in ctx["entries"]:
        rel_by_salt.setdefault(e.salt_hex, e.rel)

    found = 0
    try:
        paths = list(mmkv_dir.iterdir())
    except OSError:
        return 0
    if not paths:
        log("[mmkv] MMKV 目录为空")
        return 0
    for path in paths:
        name = path.name
        if name.endswith(".crc"):
            continue
        m = CODE_RE.match(name)
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if len(raw) < 40:
            continue
        total_size = struct.unpack("<I", raw[:4])[0]
        if total_size < 33 or 4 + total_size > len(raw):
            continue
        iv = raw[4:20]
        ct = raw[20: 4 + total_size - 16]
        tag = raw[4 + total_size - 16: 4 + total_size]
        candidates = (_derive_candidates(int(m.group(1), 16), wxid) if m else
                      [("wxid_only", hashlib.md5(wxid.encode()).hexdigest()[:16].encode())])

        plaintext = None
        label = ""
        for label, aes_key in candidates:
            plaintext = _gcm_decrypt(aes_key, iv, ct, tag)
            if plaintext is not None:
                break
        if plaintext is None:
            continue
        log(f"[mmkv] 解密 {name} 成功 ({label}, {len(plaintext)} bytes)")

        for salt_hex, rel in rel_by_salt.items():
            if salt_hex in key_map:
                continue
            page1 = page1_by_salt[salt_hex]
            for sep in ("\\", "/"):
                pos = plaintext.find(rel.replace("\\", sep).encode())
                if pos < 0:
                    continue
                nearby = plaintext[pos: pos + 512 + len(rel)]
                for hm in HEX64_RE.finditer(nearby):
                    try:
                        kb = bytes.fromhex(hm.group(1).decode())
                    except ValueError:
                        continue
                    if verify_enc_key(kb, page1):
                        key_map[salt_hex] = hm.group(1).decode().lower()
                        attrib[salt_hex] = "mmkv"
                        found += 1
                        log(f"  [mmkv] salt={salt_hex[:16]}… 已验证")
                        break
                if salt_hex in key_map:
                    break
    if found:
        log(f"[mmkv] MMKV 离线提取完成: +{found}")
    else:
        log("[mmkv] MMKV 文件存在但密钥未命中（可能未登录微信或密钥已过期）")
    return found
