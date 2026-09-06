"""诊断：收集微信进程中的 WCDB 配置 blob，dump 内容并尝试掩码恢复。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import struct

from siwx import winproc
from siwx.strategies.config_cipher import (
    CONFIG_BLOB_MAX,
    CONFIG_CIPHER_NAME,
    CONFIG_LITERAL_RE,
    CONFIG_XOR_MASK,
    _u64_from,
    _xor_repeat,
)

OUT = Path(__file__).parent / "blobs.txt"


def collect_blobs(pid):
    h = winproc.open_process(pid)
    if not h:
        return []
    try:
        regions = winproc.enum_regions(h)
        needle_addrs = set()
        for base, data in winproc.iter_chunks(h, regions, overlap=len(CONFIG_CIPHER_NAME) - 1):
            pos = data.find(CONFIG_CIPHER_NAME)
            while pos >= 0:
                needle_addrs.add(base + pos)
                pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
        print(f"PID={pid}: needles={len(needle_addrs)}")
        if not needle_addrs:
            return []

        blobs, seen = [], set()
        pair_patterns = [struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                         for a in needle_addrs]
        for base, data in winproc.iter_chunks(h, regions, overlap=0x80):
            for pat in pair_patterns:
                pos = data.find(pat)
                while pos >= 0:
                    qaddr = base + pos
                    node = winproc.read_mem(h, qaddr - 0x10, 0x50)
                    if node and len(node) >= 0x40:
                        if (_u64_from(node, 0x10) in needle_addrs
                                and _u64_from(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                            config_ptr = _u64_from(node, 0x28)
                            if 0x10000 <= config_ptr < winproc.MAX_USER_ADDRESS:
                                obj = winproc.read_mem(h, config_ptr + 0x88, 0x28)
                                if obj and len(obj) >= 0x18:
                                    dptr = _u64_from(obj, 0x8)
                                    dlen = _u64_from(obj, 0x10)
                                    if 0 < dlen <= CONFIG_BLOB_MAX and 0x10000 <= dptr:
                                        blob = winproc.read_mem(h, dptr, int(dlen))
                                        if blob and blob not in seen:
                                            seen.add(blob)
                                            blobs.append(blob)
                    pos = data.find(pat, pos + 1)
        return blobs
    finally:
        winproc.close_handle(h)


def main():
    pids = winproc.psutil_pid_list()
    print("pids:", pids)
    all_blobs = []
    for pid in pids:
        all_blobs.extend(collect_blobs(pid))
    print(f"total blobs: {len(all_blobs)}")

    lines = []
    for i, blob in enumerate(all_blobs):
        dec = _xor_repeat(blob, CONFIG_XOR_MASK)
        m = CONFIG_LITERAL_RE.search(dec)
        lines.append(f"== blob#{i} len={len(blob)} builtin_mask_hits_literal={bool(m)}")
        lines.append("  raw[:96] : " + blob[:96].hex())
        lines.append("  dec[:96] : " + dec[:96].hex())
        if m:
            lines.append("  literal  : " + m.group(1).decode()[:96])
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT}")
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main()
