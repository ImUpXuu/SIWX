"""策略：WCDB Config.Cipher 只读扫描（WeChat 4.1.10+，无需管理员、无需重启）。

核心扫描逻辑移植自原 pc_wechat_exp.config_cipher_extract（已验证 4.1.10~4.1.12）：
进程内存中存在字符串 "com.Tencent.WCDB.Config.Cipher"（len=30 > SSO，堆分配），
定位其 std::string 节点：node+0x10=数据指针、node+0x18=长度、node+0x28=config_ptr；
config_ptr+0x88 处对象携带 blob 指针/长度；blob 为固定 32 字节 XOR 掩码混淆的
配置串，解码后含 x'<64~192 hex>' 字面量（key64 + 可选 salt32）。

自研增强（掩码失效兜底，应对微信升级）：
  ① crib-drag 约束求解 —— 已知 blob 含 x'<L hex>' 结构，穷举起点把
     "该位置必须是 x / ' / hex / '" 变成每残差类 (i%32) 掩码候选集合交集，
     每个候选掩码立即经 HMAC 验证裁决，命中即停（验证引导搜索）。
  ② ASCII 打分法 —— 无 x' 结构时按可打印占比恢复。
"""
import re
import struct

from siwx import winproc
from siwx.sqlcipher import verify_enc_key

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
CONFIG_BLOB_MAX = 1024
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
HEX_ALPHABET = b"0123456789abcdefABCDEF"


def _u64_from(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _probable_32_byte_key(data: bytes) -> bool:
    return (len(data) == 32 and len(set(data)) >= 15
            and data not in (b"\x00" * 32, b"\xff" * 32))


def _xor_repeat(data: bytes, mask: bytes) -> bytes:
    mlen = len(mask)
    return bytes(v ^ mask[i % mlen] for i, v in enumerate(data))


def _blob_key_candidates(blob: bytes):
    """XOR 解码并产出 (key_hex, embedded_salt_or_None)。"""
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return
    decoded = _xor_repeat(blob, CONFIG_XOR_MASK)
    seen = set()
    for m in CONFIG_LITERAL_RE.finditer(decoded):
        run = m.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            key_hex = run[start: start + 64]
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if not _probable_32_byte_key(key):
                continue
            embedded = run[start + 64: start + 96] if start + 96 <= len(run) else None
            item = (key_hex, embedded)
            if item not in seen:
                seen.add(item)
                yield item


def _candidates_from_decoded_generic(decoded: bytes, mask: bytes):
    """任意掩码解码后的候选提取（ crib 恢复用）。"""
    out = []
    seen = set()
    for m in CONFIG_LITERAL_RE.finditer(decoded):
        run = m.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start + 64 > len(run):
                continue
            key_hex = run[start: start + 64]
            embedded = run[start + 64: start + 96] if start + 96 <= len(run) else None
            if (key_hex, embedded) in seen:
                continue
            seen.add((key_hex, embedded))
            try:
                kb = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if _probable_32_byte_key(kb):
                out.append((key_hex, embedded))
    return out


def _try_candidates(cands, page1_by_salt, key_map, attrib, strategy, log) -> int:
    found = 0
    for key_hex, emb_salt in cands:
        try:
            kb = bytes.fromhex(key_hex)
        except ValueError:
            continue
        targets = []
        if emb_salt and emb_salt in page1_by_salt and emb_salt not in key_map:
            targets.append(emb_salt)
        targets.extend(s for s in page1_by_salt if s not in key_map and s != emb_salt)
        for salt in targets:
            if verify_enc_key(kb, page1_by_salt[salt]):
                log(f"  [{strategy}] salt={salt[:16]}… 已验证 (key={key_hex[:8]}…)")
                key_map[salt] = key_hex
                attrib[salt] = strategy
                found += 1
            if len(key_map) >= len(page1_by_salt):
                break
        if len(key_map) >= len(page1_by_salt):
            break
    return found


# ---------------- 自研掩码恢复（crib-drag 约束求解） ----------------

def _crib_masks(blob: bytes, check):
    """穷举 x' 字面量起点 → 残差类掩码集合交集 → 逐候选掩码交给 check 裁决。"""
    n = len(blob)
    if n < 130:
        return None
    for p in range(0, n - 130):
        for lit_len in (96, 64, 128):
            if p + lit_len + 3 > n:
                continue
            sets = [set(range(256)) for _ in range(32)]
            ok = True

            def constrain(pos, plain_set):
                nonlocal ok
                if not ok:
                    return
                r = pos % 32
                b = blob[pos]
                s = sets[r]
                s.intersection_update(b ^ c for c in plain_set)
                if not s:
                    ok = False

            constrain(p, b"x")
            if not ok:
                continue
            constrain(p + 1, b"'")
            for i in range(p + 2, p + 2 + lit_len):
                if not ok:
                    break
                constrain(i, HEX_ALPHABET)
            if ok:
                constrain(p + 2 + lit_len, b"'")
            if not ok:
                continue

            # 组合展开（全部单解为常态；歧义时按序尝试，check 兜底）
            combo = [0] * 32
            if _enumerate(sets, combo, 0, check, blob):
                return combo
    return None


def _enumerate(sets, combo, r, check, blob) -> bool:
    if r == 32:
        return check(bytes(combo))
    count = 0
    for m in sorted(sets[r]):
        combo[r] = m
        count += 1
        if count > 64:
            break
        if _enumerate(sets, combo, r + 1, check, blob):
            return True
    return False


def _scoring_mask(blob: bytes):
    """ASCII 打分法：每残差类选使解码 mostly 可打印的掩码字节。"""
    if len(blob) < 128:
        return None
    mask = bytearray(32)
    for r in range(32):
        best_m, best_score = 0, 0.0
        for m in range(256):
            good = total = 0
            for p in range(r, len(blob), 32):
                c = blob[p] ^ m
                total += 1
                if c == 0 or c in (9, 10, 13) or 0x20 <= c <= 0x7E:
                    good += 1
            if total and good / total > best_score:
                best_score = good / total
                best_m = m
        if best_score < 0.8:
            return None
        mask[r] = best_m
    return bytes(mask)


def extract(ctx) -> int:
    """统一策略入口（两遍只读扫描 + 掩码恢复兜底）。"""
    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]

    pids = winproc.psutil_pid_list()
    if not pids:
        log("[cipher] 未检测到微信进程，跳过 Config.Cipher 扫描")
        return 0
    log(f"[cipher] 微信进程 {pids} — 只读扫描 (无需管理员、无需重启)")

    entry = len(key_map)
    found_any = False
    for pid in pids:
        if len(key_map) >= len(page1_by_salt):
            break
        h = winproc.open_process(pid)
        if not h:
            log(f"[cipher] PID={pid} 无法打开 (权限不足?)")
            continue
        try:
            regions = winproc.enum_regions(h)
            total_mb = sum(s for _b, s in regions) // 1048576
            log(f"[cipher] PID={pid}: {len(regions)} 个区域 / {total_mb} MB")

            # 第一遍：定位 needle 字符串出现地址
            needle_addrs = set()
            for base, data in winproc.iter_chunks(
                    h, regions, overlap=len(CONFIG_CIPHER_NAME) - 1):
                pos = data.find(CONFIG_CIPHER_NAME)
                while pos >= 0:
                    needle_addrs.add(base + pos)
                    pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
            if not needle_addrs:
                log(f"[cipher] PID={pid}: 未找到 WCDB 配置对象")
                continue
            log(f"[cipher] PID={pid}: 定位 {len(needle_addrs)} 个 needle")

            # 第二遍：找指向 needle 的 (ptr, len) 节点 → config 对象 → blob
            blobs = []
            seen = set()
            pair_patterns = [
                struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                for a in needle_addrs
            ]
            for base, data in winproc.iter_chunks(h, regions, overlap=0x80):
                if len(key_map) >= len(page1_by_salt):
                    break
                for pat in pair_patterns:
                    pos = data.find(pat)
                    while pos >= 0:
                        qaddr = base + pos
                        node_base = qaddr - 0x10
                        node = winproc.read_mem(h, node_base, 0x50)
                        if node and len(node) >= 0x40:
                            if (_u64_from(node, 0x10) in needle_addrs
                                    and _u64_from(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                                config_ptr = _u64_from(node, 0x28)
                                if 0x10000 <= config_ptr < winproc.MAX_USER_ADDRESS:
                                    obj = winproc.read_mem(h, config_ptr + 0x88, 0x28)
                                    if obj and len(obj) >= 0x18:
                                        data_ptr = _u64_from(obj, 0x8)
                                        data_len = _u64_from(obj, 0x10)
                                        if (0 < data_len <= CONFIG_BLOB_MAX
                                                and 0x10000 <= data_ptr < winproc.MAX_USER_ADDRESS):
                                            blob = winproc.read_mem(h, data_ptr, int(data_len))
                                            if blob and len(blob) == data_len and blob not in seen:
                                                seen.add(blob)
                                                blobs.append(blob)
                        pos = data.find(pat, pos + 1)
            if not blobs:
                log(f"[cipher] PID={pid}: 未取得配置 blob")
                continue
            log(f"[cipher] PID={pid}: 取得 {len(blobs)} 个配置 blob，尝试内置掩码…")

            for blob in blobs:
                cands = list(_blob_key_candidates(blob))
                if _try_candidates(cands, page1_by_salt, key_map, attrib, "cipher", log):
                    found_any = True

            # 掩码恢复兜底（微信升级导致内置掩码失效时）
            if not found_any and len(key_map) < len(page1_by_salt):
                log("[cipher] 内置掩码未命中，启用验证引导的掩码求解…")

                def check(mask_bytes, _blobs=blobs):
                    for blob in _blobs:
                        decoded = _xor_repeat(blob, mask_bytes)
                        cands = _candidates_from_decoded_generic(decoded, mask_bytes)
                        if cands and _try_candidates(
                                cands, page1_by_salt, key_map, attrib,
                                "cipher/掩码恢复", log):
                            return True
                    return False

                mask = _crib_masks(blobs[0], check)
                if mask is None:
                    sm = _scoring_mask(blobs[0])
                    if sm is not None and check(sm):
                        mask = sm
                if mask is not None:
                    log(f"[cipher] 求解掩码: {mask.hex()}")
                    found_any = True
        finally:
            winproc.close_handle(h)
        if found_any:
            break

    found = len(key_map) - entry
    log(f"[cipher] Config.Cipher 扫描完成: 验证 {found} 个密钥")
    return found
