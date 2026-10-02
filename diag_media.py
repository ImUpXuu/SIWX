"""媒体解密研究脚本 —— 阶段性验证，不是正式模块。

研究目标：
  1. 朋友圈图片缓存位于 cache/<YYYY-MM>/Sns/Img/，全部为 V2 格式；
  2. 密钥来源两条路：
     a) 离线：MMKV kvcomm statistic 派生账号级 key（py_wx_key 算法）；
     b) 动态：微信进程内存（浏览图片时密钥在内存）——V2 魔数邻近扫描 + 32hex 正则；
  3. 验证方式：AES-ECB 解密首块出现合法图像头（JPEG/PNG/GIF/WebP/wxgf）。
"""
import hashlib
import os
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from Crypto.Cipher import AES

from siwx import winproc

WXID_DIR = Path(r"D:/xwechat_files/<wxid>_1234")
SNS_IMG = WXID_DIR / "cache/2026-09/Sns/Img"
OUT = Path(__file__).parent / "media_research"
OUT.mkdir(exist_ok=True)

V2_MAGIC = b"\x07\x08V2\x08\x07"
HEX32_RE = re.compile(rb"(?<![a-zA-Z0-9])[a-fA-F0-9]{32}(?![a-zA-Z0-9])")

IMAGE_SIGS = [
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG", "PNG"),
    (b"GIF8", "GIF"),
    (b"RIFF", "WEBP"),
    (b"wxgf", "WXGF"),
    (b"\x00\x00\x00", "HEIC?"),
]


def image_format(block16: bytes):
    for sig, name in IMAGE_SIGS:
        if block16.startswith(sig):
            return name
    return None


def collect_ciphertexts(limit_recent=40):
    """取最近 N 张 V2 图的 (路径, AES首块16B, XOR密钥字节?, xor段前16B)。"""
    files = sorted(SNS_IMG.rglob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        if not f.is_file():
            continue
        data = f.read_bytes()[: 15 + 64]
        if data[:6] != V2_MAGIC or len(data) < 31:
            continue
        aes_block = data[15:31]
        out.append((f, aes_block))
        if len(out) >= limit_recent:
            break
    return out


def try_key_aes(key16: bytes, ciphertexts):
    """key 对样本解密首块，返回命中的 fmt 或 None。"""
    try:
        c = AES.new(key16, AES.MODE_ECB)
    except ValueError:
        return None
    block = c.decrypt(ciphertexts[0][1])
    return image_format(block)


def decrypt_v2(path: Path, key16: bytes, xor_key: int) -> bytes:
    """按 V2 布局整文件解密。"""
    data = path.read_bytes()
    if data[:6] != V2_MAGIC:
        raise ValueError("非 V2")
    aes_size = struct.unpack("<I", data[6:10])[0]
    xor_size = struct.unpack("<I", data[10:14])[0]
    head = data[15: 15 + aes_size]
    tail = data[15 + aes_size + 16: 15 + aes_size + 16 + xor_size]
    aes = AES.new(key16, AES.MODE_ECB)
    pt_head = aes.decrypt(head)
    pt_tail = bytes(b ^ xor_key for b in tail)
    return pt_head + pt_tail


def harvest_memory(ciphertexts, log):
    """动态提取：扫微信进程内存找 key。策略 1 魔数邻近窗口；策略 2 32hex 正则。"""
    found = {}
    sample = ciphertexts[:8]
    pids = winproc.psutil_pid_list()
    log(f"微信进程: {pids}")
    for pid in pids:
        h = winproc.open_process(pid)
        if not h:
            continue
        try:
            regions = winproc.enum_regions(h)
            total_mb = sum(s for _b, s in regions) // 1048576
            log(f"PID={pid}: {len(regions)} 区域 / {total_mb} MB")

            # 策略 1：V2 魔数邻近 ±256B，16B 滑窗 step=1
            magic_addrs = []
            for base, data in winproc.iter_chunks(h, regions, chunk_size=2 * 1024 * 1024,
                                                  overlap=len(V2_MAGIC) - 1):
                pos = data.find(V2_MAGIC)
                while pos >= 0 and len(magic_addrs) < 4000:
                    magic_addrs.append(base + pos)
                    pos = data.find(V2_MAGIC, pos + 1)
            log(f"  V2 魔数出现 {len(magic_addrs)} 处（内存中正在解密的图）")
            tested = 0
            for addr in magic_addrs:
                buf = winproc.read_mem(h, addr - 256, 512)
                if not buf or len(buf) < 16:
                    continue
                for i in range(len(buf) - 16):
                    cand = buf[i: i + 16]
                    tested += 1
                    fmt = try_key_aes(cand, sample)
                    if fmt:
                        found[cand] = fmt
                        log(f"  [命中·魔数窗] key={cand.hex()} → {fmt} "
                            f"(addr=0x{addr:x}+{i})")
                        if len(found) >= 6:
                            return found, tested
            log(f"  策略1完成: 测试 {tested} 个候选")

            # 策略 2：全内存 32hex 正则（hex 解码后作 key）
            tested2 = 0
            for base, data in winproc.iter_chunks(h, regions, chunk_size=4 * 1024 * 1024):
                for m in HEX32_RE.finditer(data):
                    try:
                        cand = bytes.fromhex(m.group().decode())
                    except ValueError:
                        continue
                    tested2 += 1
                    fmt = try_key_aes(cand, sample)
                    if fmt:
                        found[cand] = fmt
                        log(f"  [命中·32hex] key={cand.hex()} → {fmt}")
                        if len(found) >= 6:
                            return found, tested2
            log(f"  策略2完成: 测试 {tested2} 个候选")
        finally:
            winproc.close_handle(h)
    return found, 0


def try_mmkv_offline(log):
    """离线路线：kvcomm key_*.statistic 的 code 派生账号级 key。
    实测位置：xwechat/net/kvcomm、xwechat/ilink/kvcomm、Tencent/WeChat/*/kvcomm。
    """
    import glob
    wxid = "wxid_demo"   # 清洗后 wxid
    codes = set()
    pats = [
        r"C:/Users/*/AppData/Roaming/Tencent/xwechat/net/kvcomm/key_*_*.statistic",
        r"C:/Users/*/AppData/Roaming/Tencent/xwechat/ilink/kvcomm/key_*_*.statistic",
        r"C:/Users/*/AppData/Roaming/Tencent/WeChat/*/kvcomm/key_*_*.statistic",
    ]
    for pat in pats:
        for f in glob.glob(pat):
            m = re.match(r".*[\\/]key_(\d+)_", f.replace("\\", "/"))
            if m:
                codes.add(int(m.group(1)))
    log(f"MMKV codes: {sorted(codes)}")
    cts = collect_ciphertexts(5)
    hits = {}
    for code in sorted(codes):
        for wx in (wxid, wxid + "_6409"):
            key16 = hashlib.md5(f"{code}{wx}".encode()).hexdigest()[:16].encode()
            fmt = try_key_aes(key16, cts)
            if fmt:
                hits[key16] = (code, fmt)
                log(f"  [命中·MMKV离线] code={code} wxid={wx} key={key16.decode()} → {fmt}")
                return hits
    return hits


def main():
    def log(m):
        print(m, flush=True)

    log("== 1. 收集最近的朋友圈图片密文 ==")
    cts = collect_ciphertexts(40)
    log(f"V2 样本 {len(cts)} 张（最近修改优先，含当前浏览的图）")

    log("== 2. 离线路线：MMKV 派生 ==")
    hits = try_mmkv_offline(log)

    if not hits:
        log("== 3. 动态路线：内存扫描 ==")
        found, tested = harvest_memory(cts, log)
        hits.update({k: (None, v) for k, v in found.items()})

    if not hits:
        log("!! 两条路线都没命中 —— 请确认微信里那张朋友圈图片正打开着，再跑一次")
        return

    log("== 4. 用命中的 key 整文件解密验证 ==")
    for key16, (code, fmt) in list(hits.items())[:3]:
        ok = 0
        for path, _blk in cts:
            for xk in (0xC9, code & 0xFF if code else 0xC9):
                try:
                    raw = decrypt_v2(path, key16, xk)
                    if image_format(raw[:16]):
                        ok += 1
                        if ok <= 3:
                            out = OUT / f"{path.name}_{fmt.lower()}"
                            out.write_bytes(raw)
                            log(f"  ✔ {path.name} → {out.name} ({len(raw)}B, {fmt})")
                        break
                except Exception:
                    continue
        log(f"  key {key16.hex()[:16]}… 成功解密 {ok}/{len(cts)} 张")


if __name__ == "__main__":
    main()
