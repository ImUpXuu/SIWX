"""策略：全内存 x'<hex>' 字面量兜底扫描（对 WeChat < 4.1.10 有效）。

逻辑移植自原 pc_wechat_exp.extract_keys_via_memory_scan。
"""
import re

from siwx import winproc
from siwx.sqlcipher import verify_enc_key

HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")


def extract(ctx) -> int:
    page1_by_salt = ctx["page1_by_salt"]
    key_map = ctx["key_map"]
    attrib = ctx["attrib"]
    log = ctx["log"]

    pids = winproc.psutil_pid_list()
    if not pids:
        log("[memscan] 未检测到微信进程，跳过内存字面量扫描")
        return 0

    entry = len(key_map)
    for pid in pids:
        if len(key_map) >= len(page1_by_salt):
            break
        h = winproc.open_process(pid)
        if not h:
            continue
        try:
            regions = winproc.enum_regions(h)
            for base, data in winproc.iter_chunks(h, regions, chunk_size=4 * 1024 * 1024):
                for m in HEX_RE.finditer(data):
                    hex_str = m.group(1).decode()
                    if len(hex_str) == 96:
                        key_hex, salt_hex = hex_str[:64], hex_str[64:]
                        if salt_hex in page1_by_salt and salt_hex not in key_map:
                            kb = bytes.fromhex(key_hex)
                            if verify_enc_key(kb, page1_by_salt[salt_hex]):
                                log(f"  [memscan] {salt_hex[:16]}… 已验证")
                                key_map[salt_hex] = key_hex.lower()
                                attrib[salt_hex] = "memscan"
                    else:
                        kb = bytes.fromhex(hex_str[:64])
                        for salt, page1 in page1_by_salt.items():
                            if salt not in key_map and verify_enc_key(kb, page1):
                                log(f"  [memscan] salt={salt[:16]}… 已验证")
                                key_map[salt] = hex_str[:64].lower()
                                attrib[salt] = "memscan"
                                break
                    if len(key_map) >= len(page1_by_salt):
                        break
        finally:
            winproc.close_handle(h)

    found = len(key_map) - entry
    if found:
        log(f"[memscan] 内存字面量扫描完成: +{found}")
    return found
