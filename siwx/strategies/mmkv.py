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
# 目录名是否可能是一个账号名（wxid_xxx / 自定义 alias_xxx）。空、"." 、
# 纯符号都不该拿去派生密钥（P3-3：只有"目录名空/非法"才跳过）。
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{2,}$")


def clean_wxid(wxid: str) -> str:
    if not wxid or not wxid.startswith("wxid_"):
        return wxid
    parts = wxid.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:2])
    return wxid


def _wxid_variants(wxid_full: str) -> list:
    """目录名 → 派生用账号名候选（与 media.wxid_key_variants 同源）。

    WeChat 4.x 目录名是 `<username>_<4位hex>`；自定义微信号（alias_xxx）此前
    因 `not startswith("wxid_")` 直接整目录跳过 → 该账号图片/数据库密钥永远
    提取不出来。复用 media 的多候选（clean / 剥尾部4位hex / 原样）即可。
    延迟导入：media 依赖 Crypto/logger，策略模块保持轻量，且避免任何
    导入环（media → 无策略依赖）。
    """
    try:
        from siwx.media import wxid_key_variants
        return [v for v in wxid_key_variants(wxid_full) if v]
    except Exception:
        return [v for v in (clean_wxid(wxid_full), wxid_full) if v]


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
    dbg = ctx.get("dbg", log)

    mmkv_dir = Path(db_dir) / "MMKV"
    if not mmkv_dir.is_dir():
        # 审计 §3.4:58-60：目录不存在静默 return 0 → Debug 可见
        dbg(f"[mmkv] MMKV 目录不存在: {mmkv_dir}")
        return 0
    wxid_full = os.path.basename(os.path.dirname(os.path.abspath(str(db_dir))))
    variants = _wxid_variants(wxid_full)
    # P3-3：不再要求目录名以 wxid_ 开头（自定义微信号目录此前被整段跳过）。
    # 只有目录名空/明显非法（"." 、纯符号）时仍跳过——派生无意义。
    primary = variants[0] if variants else ""
    if not _ACCOUNT_NAME_RE.match(primary):
        log("[mmkv] 无法从路径确定 wxid，跳过 MMKV 提取")
        dbg(f"[mmkv] 目录名不可用: {wxid_full!r}")
        return 0
    log(f"[mmkv] 账号目录: {wxid_full} → 派生候选 {len(variants)} 个")

    rel_by_salt = {}
    for e in ctx["entries"]:
        rel_by_salt.setdefault(e.salt_hex, e.rel)

    found = 0
    try:
        paths = list(mmkv_dir.iterdir())
    except OSError as e:
        # 审计 §3.4:73-76：枚举失败 warn（结果残缺，用户可感）
        log(f"[mmkv] ⚠ MMKV 目录枚举失败: {e}")
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
        except OSError as e:
            # 审计 §3.4:85-88：单文件读失败 detailed
            dbg(f"[mmkv] {name} 读取失败: {e}")
            continue
        if len(raw) < 40:
            dbg(f"[mmkv] {name} 过短 ({len(raw)} bytes)，跳过")
            continue
        total_size = struct.unpack("<I", raw[:4])[0]
        if total_size < 33 or 4 + total_size > len(raw):
            # 审计 §3.4:89-93：头部校验失败是格式变化的唯一线索
            dbg(f"[mmkv] {name} 头部异常 len={len(raw)} total_size={total_size}")
            continue
        iv = raw[4:20]
        ct = raw[20: 4 + total_size - 16]
        tag = raw[4 + total_size - 16: 4 + total_size]
        # 每个账号名候选都生成一套派生密钥（label 保留派生方案名，后缀 #i
        # 标注来自第 i 个候选，日志里能区分"哪个变体解开的"）；密钥字节
        # 去重保序——同一账号的 clean/原样候选可能重复。
        candidates = []
        seen_keys = set()

        def _add(label, aes_key):
            if aes_key in seen_keys:
                return
            seen_keys.add(aes_key)
            candidates.append((label, aes_key))

        if m:
            code = int(m.group(1), 16)
            for vi, variant in enumerate(variants):
                for label, aes_key in _derive_candidates(code, variant):
                    _add(f"{label}#{vi}", aes_key)
        else:
            for vi, variant in enumerate(variants):
                _add(f"wxid_only#{vi}",
                     hashlib.md5(variant.encode()).hexdigest()[:16].encode())

        plaintext = None
        label = ""
        for label, aes_key in candidates:
            plaintext = _gcm_decrypt(aes_key, iv, ct, tag)
            if plaintext is not None:
                break
        if plaintext is None:
            # 审计 §3.4:100-107：全部候选派生密钥 GCM 失败 = "文件存在但密钥
            # 未命中"的最重要细分（派生密钥不对 vs 文件不是 tinfo 格式）。
            # 只记候选 label 列表，绝不记 aes_key 本身。
            dbg(f"[mmkv] {name}: {len(candidates)} 个候选派生密钥全部 GCM 校验失败 "
                f"labels={[l for l, _ in candidates]}")
            continue
        log(f"[mmkv] 解密 {name} 成功 ({label}, {len(plaintext)} bytes)")

        missing_rels = []
        for salt_hex, rel in rel_by_salt.items():
            if salt_hex in key_map:
                continue
            page1 = page1_by_salt[salt_hex]
            hit_rel = False
            for sep in ("\\", "/"):
                pos = plaintext.find(rel.replace("\\", sep).encode())
                if pos < 0:
                    continue
                hit_rel = True
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
            if not hit_rel:
                missing_rels.append(rel)
        if missing_rels:
            # 审计 §3.4:115-117：明文中找不到 rel 路径（按文件聚合，防刷屏）
            dbg(f"[mmkv] {name} 明文中未找到 {len(missing_rels)} 个库路径: "
                f"{missing_rels[:5]}")
    if found:
        log(f"[mmkv] MMKV 离线提取完成: +{found}")
    else:
        log("[mmkv] MMKV 文件存在但密钥未命中（可能未登录微信或密钥已过期）")
    return found
